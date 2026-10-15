"""飞书多维表客户端（唯一一份）。

设计要点（来自旧仓库的实战教训）：
- 只用标准库 urllib，`http` 可注入 → 测试零网络。
- tenant_access_token 缓存并在过期前提前刷新。
- 批量建行每批 ≤500，返回的 record_id 顺序与入参一致。
- 附件超过 19MB 直接拒绝（飞书 20MB 上限，留安全边界）；仅对 99991400 / 99991403 / 1061045
  三个瞬时错误码做退避重试（这三个是免费的限流/并发码，重试不产生额外费用）。
- 写完必须回读校验：值逐项比对，附件比对名字与数量。
- 任何异常信息只带 HTTP 状态、错误码、错误文案，绝不带凭证或请求体。
"""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Attachment, DeliveryRecord, FeishuTableRef

DEFAULT_BASE_URL = "https://open.feishu.cn"
BITABLE = "/open-apis/bitable/v1/apps"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_PATH = "/open-apis/drive/v1/medias/upload_all"
TMP_URL_PATH = "/open-apis/drive/v1/medias/batch_get_tmp_download_url"
#: 临时下载地址有效期（飞书文档：24 小时）；导轨托管要在这个窗口内完成外部验证与提交
TMP_URL_TTL_S = 24 * 3600

#: 飞书附件安全上限（官方 20MB，留 1MB 余量；超过一律不传）
MAX_ATTACHMENT_BYTES = 19 * 1024 * 1024
#: 单批记录上限
BATCH_LIMIT = 500
#: 只有这三个错误码是瞬时的（频控/并发/上传通道忙），可退避重试；其余一律立刻失败
RETRYABLE_UPLOAD_CODES: frozenset[int] = frozenset({99991400, 99991403, 1061045})
#: token 提前多少秒刷新
TOKEN_REFRESH_MARGIN_S = 120.0

HttpFn = Callable[..., tuple[int, Any]]
SleepFn = Callable[[float], None]


class FeishuError(RuntimeError):
    """飞书接口错误。code 为飞书业务错误码（无则 0），msg 为飞书返回文案。"""

    def __init__(self, message: str, *, code: int = 0, msg: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.msg = msg or message


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _maybe_json(raw: Any) -> Any:
    """响应体能解析成 JSON 就返回对象，否则原样返回 bytes。"""
    if isinstance(raw, (dict, list)) or raw is None:
        return raw
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        return raw
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw


def _build_multipart(form: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----tvp" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in form.items():
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
             f"{value}\r\n").encode()
        )
    for key, (filename, content, mime) in files.items():
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode()
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return f"{text!r}" if isinstance(value, str) else text
    return f"{text[:limit]}…(len={len(text)})"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _same(actual: Any, want: Any) -> bool:
    if _empty(actual) and _empty(want):
        return True
    if _is_number(actual) and _is_number(want):
        return math.isclose(float(actual), float(want), rel_tol=1e-9, abs_tol=1e-9)
    if _is_number(actual) != _is_number(want) and not isinstance(actual, list) and not isinstance(want, list):
        return str(actual) == str(want)
    return actual == want


def attachment_names(value: Any) -> list[str]:
    """把附件列（或期望值）归一成文件名列表：接受 Attachment / dict / str / Path。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    names: list[str] = []
    for item in items:
        if isinstance(item, (Attachment, Path)):
            names.append(item.name)
        elif isinstance(item, dict):
            names.append(str(item.get("name") or item.get("file_name") or ""))
        else:
            names.append(Path(str(item)).name)
    return names


class FeishuClient:
    """飞书多维表最小可用客户端。`http` 可注入以便离线测试。"""

    #: 附件上传总尝试次数（仅瞬时码才会用到）
    max_upload_attempts = 4

    def __init__(self, app_id: str, app_secret: str, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 60, http: HttpFn | None = None, sleep: SleepFn | None = None) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http: HttpFn = http or self._default_http
        self._sleep: SleepFn = sleep or time.sleep
        self._clock: Callable[[], float] = time.time      # 测试里可替换
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self.last_readback_diffs: list[str] = []

    # ---------------------------------------------------------------- 传输层

    def _default_http(self, method: str, url: str, headers: dict[str, str] | None = None,
                      body: Any = None, files: dict[str, tuple[str, bytes, str]] | None = None) -> tuple[int, Any]:
        hdrs = dict(headers or {})
        data: bytes | None = None
        if files:
            form = {k: str(v) for k, v in (body or {}).items()}
            data, content_type = _build_multipart(form, files)
            hdrs["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _maybe_json(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _maybe_json(exc.read())
        except urllib.error.URLError as exc:      # 不回显 url，避免带出查询串
            raise FeishuError(f"飞书网络请求失败：{exc.reason}") from exc

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.tenant_token()
        return headers

    @staticmethod
    def _check(status: int, payload: Any, what: str) -> dict:
        if not isinstance(payload, dict):
            raise FeishuError(f"{what}：飞书返回非 JSON 响应（HTTP {status}）")
        code = payload.get("code")
        code = int(code) if isinstance(code, int) else 0
        if status < 200 or status >= 300 or code != 0:
            msg = str(payload.get("msg") or payload.get("message") or "未知错误")
            raise FeishuError(f"{what}失败（HTTP {status} / code {code}）：{msg}", code=code, msg=msg)
        return payload.get("data") or {}

    def _call(self, method: str, path: str, *, body: Any = None, auth: bool = True, what: str = "飞书接口") -> dict:
        url = path if path.startswith("http") else self.base_url + path
        status, payload = self._http(method, url, self._headers(auth=auth), body, None)
        return self._check(status, payload, what)

    def _table_base(self, table: FeishuTableRef) -> str:
        return f"{BITABLE}/{table.app_token}/tables/{table.table_id}"

    # ---------------------------------------------------------------- 鉴权

    def tenant_token(self) -> str:
        """返回 tenant_access_token；缓存并在过期前 120 秒刷新。"""
        now = self._clock()
        if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._token
        url = self.base_url + TOKEN_PATH
        status, payload = self._http(
            "POST", url, {"Content-Type": "application/json; charset=utf-8"},
            {"app_id": self.app_id, "app_secret": self.app_secret}, None,
        )
        if not isinstance(payload, dict):
            raise FeishuError(f"取 tenant_access_token：飞书返回非 JSON 响应（HTTP {status}）")
        code = int(payload.get("code") or 0)
        if status < 200 or status >= 300 or code != 0:
            msg = str(payload.get("msg") or "未知错误")
            raise FeishuError(f"取 tenant_access_token 失败（HTTP {status} / code {code}）：{msg}", code=code, msg=msg)
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuError("飞书鉴权成功但响应里没有 tenant_access_token")
        self._token = str(token)
        expire = payload.get("expire")
        self._token_expires_at = now + float(expire if isinstance(expire, (int, float)) else 7200)
        return self._token

    # ---------------------------------------------------------------- 表结构

    def list_tables(self, app_token: str) -> list[dict]:
        """列出一个 bitable 下的全部数据表（自动翻页）。对账、体检时用来找表，不改任何字段。"""
        items: list[dict] = []
        page_token = ""
        while True:
            query = {"page_size": "100"}
            if page_token:
                query["page_token"] = page_token
            data = self._call(
                "GET",
                f"/open-apis/bitable/v1/apps/{app_token}/tables?" + urllib.parse.urlencode(query),
                what="列数据表",
            )
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return items

    def list_fields(self, table: FeishuTableRef) -> list[dict]:
        """列出表的全部字段定义（自动翻页）。"""
        items: list[dict] = []
        page_token = ""
        while True:
            query = {"page_size": "100"}
            if page_token:
                query["page_token"] = page_token
            data = self._call("GET", f"{self._table_base(table)}/fields?" + urllib.parse.urlencode(query),
                              what="列字段")
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                return items

    # ---------------------------------------------------------------- 记录读

    def list_records(self, table: FeishuTableRef, *, view_id: str | None = None,
                     filter_expr: str | None = None, page_size: int = 500) -> list[dict]:
        """全量翻页读取记录。view_id 缺省用 table.view_id；filter_expr 是飞书公式串。"""
        records: list[dict] = []
        page_token = ""
        size = max(1, min(int(page_size), BATCH_LIMIT))
        view = view_id if view_id is not None else table.view_id
        while True:
            query: dict[str, str] = {"page_size": str(size)}
            if view:
                query["view_id"] = view
            if filter_expr:
                query["filter"] = filter_expr
            if page_token:
                query["page_token"] = page_token
            data = self._call("GET", f"{self._table_base(table)}/records?" + urllib.parse.urlencode(query),
                              what="列记录")
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuError("飞书分页响应缺少 page_token")

    def get_record(self, table: FeishuTableRef, record_id: str) -> dict:
        data = self._call("GET", f"{self._table_base(table)}/records/{record_id}", what="读记录")
        return data.get("record") or {}

    # ---------------------------------------------------------------- 记录写

    def create_records(self, table: FeishuTableRef, rows: Sequence[dict]) -> list[str]:
        """批量建行；每批 ≤500；返回的 record_id 顺序与 rows 一致。"""
        ids: list[str] = []
        rows = list(rows)
        for offset in range(0, len(rows), BATCH_LIMIT):
            chunk = rows[offset:offset + BATCH_LIMIT]
            body = {"records": [{"fields": dict(r.get("fields") if isinstance(r.get("fields"), dict) else r)}
                                for r in chunk]}
            data = self._call("POST", f"{self._table_base(table)}/records/batch_create", body=body, what="批量建行")
            created = data.get("records") or []
            if len(created) != len(chunk):
                raise FeishuError(f"批量建行返回条数异常：预期 {len(chunk)} 实际 {len(created)}")
            for record in created:
                rid = record.get("record_id") or record.get("id")
                if not rid:
                    raise FeishuError("批量建行成功但缺少 record_id")
                ids.append(str(rid))
        return ids

    def update_records(self, table: FeishuTableRef, updates: Sequence[dict]) -> int:
        """批量改行，updates=[{"record_id":..,"fields":{..}}]；返回成功条数。"""
        updates = list(updates)
        done = 0
        for offset in range(0, len(updates), BATCH_LIMIT):
            chunk = updates[offset:offset + BATCH_LIMIT]
            for item in chunk:
                if not item.get("record_id"):
                    raise FeishuError("批量改行缺少 record_id")
            data = self._call("POST", f"{self._table_base(table)}/records/batch_update",
                              body={"records": [{"record_id": str(i["record_id"]), "fields": dict(i.get("fields") or {})}
                                                for i in chunk]},
                              what="批量改行")
            updated = data.get("records") or []
            if len(updated) != len(chunk):
                raise FeishuError(f"批量改行返回条数异常：预期 {len(chunk)} 实际 {len(updated)}")
            done += len(updated)
        return done

    # ---------------------------------------------------------------- 附件

    @staticmethod
    def _reject_oversize(path: Path) -> int:
        if not path.is_file():
            raise FeishuError(f"附件不存在或不是文件：{path.name}")
        size = path.stat().st_size
        if size <= 0:
            raise FeishuError(f"附件为空：{path.name}")
        if size > MAX_ATTACHMENT_BYTES:
            raise FeishuError(
                f"附件超过飞书安全上限 {MAX_ATTACHMENT_BYTES} 字节：{path.name}（{size} 字节）"
            )
        return size

    def upload_attachment(self, path: Path, *, parent_node: str) -> Attachment:
        """上传附件到 medias/upload_all。>19MB 直接拒绝；只对瞬时码退避重试。"""
        path = Path(path)
        size = self._reject_oversize(path)
        content = path.read_bytes()
        md5 = hashlib.md5(content).hexdigest()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = {"file_name": path.name, "parent_type": "bitable_file",
                "parent_node": parent_node, "size": str(size)}
        files = {"file": (path.name, content, mime)}
        url = self.base_url + UPLOAD_PATH
        last = ""
        for attempt in range(self.max_upload_attempts):
            status, payload = self._http("POST", url, self._headers(), form, files)
            code = 0
            token = ""
            if isinstance(payload, dict):
                raw_code = payload.get("code")
                code = int(raw_code) if isinstance(raw_code, int) else 0
                token = str((payload.get("data") or {}).get("file_token") or "")
                msg = str(payload.get("msg") or "")
            else:
                msg = "非 JSON 响应"
            if 200 <= status < 300 and code == 0 and token:
                return Attachment(name=path.name, file_token=token, md5=md5, size=size)
            last = f"HTTP {status} / code {code}：{msg or '未知错误'}"
            if code in RETRYABLE_UPLOAD_CODES:
                if attempt < self.max_upload_attempts - 1:
                    self._sleep(min(8.0, 1.5 ** attempt))
                    continue
                raise FeishuError(f"附件上传重试耗尽：{path.name}；{last}", code=code, msg=msg)
            raise FeishuError(f"附件上传失败：{path.name}；{last}", code=code, msg=msg)
        raise FeishuError(f"附件上传重试耗尽：{path.name}；{last}")

    def download_attachment(self, file_token: str, dest: Path) -> Path:
        dest = Path(dest)
        url = f"{self.base_url}/open-apis/drive/v1/medias/{urllib.parse.quote(file_token)}/download"
        status, payload = self._http("GET", url, self._headers(), None, None)
        if isinstance(payload, dict):
            code = int(payload.get("code") or 0)
            msg = str(payload.get("msg") or "未知错误")
            raise FeishuError(f"附件下载失败（HTTP {status} / code {code}）：{msg}", code=code, msg=msg)
        if not isinstance(payload, bytes) or status < 200 or status >= 300:
            raise FeishuError(f"附件下载失败（HTTP {status}）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(payload)
        os.replace(str(part), str(dest))
        return dest

    def temp_download_urls(self, file_tokens: list[str]) -> dict[str, str]:
        """批量换取附件的临时下载地址：GET medias/batch_get_tmp_download_url?file_tokens=…

        返回 {file_token: https 地址}。地址是带签名的公网直链，**有效期 24 小时**
        （`TMP_URL_TTL_S`），过期要重新换取。这是导轨对外托管的唯一已验证通道
        （教训 gen-guide-url-must-be-verified：file:// 与 ARK Files API 被拒、cloudflared
        隧道被掐 UDP）——拿到地址后仍必须做一次真实外部 GET 再写进请求体。

        请求里缺哪个 token 的地址就抛 FeishuError，绝不静默漏掉一部分。
        （多维表附件在应用缺少云空间权限时可能需要 `extra` bitablePerm 参数；旧产线实测
        同一 bitable 内用 tenant token 直接换取即可，这里不带。）
        """
        tokens: list[str] = []
        for token in file_tokens:
            token = str(token or "").strip()
            if token and token not in tokens:
                tokens.append(token)
        if not tokens:
            return {}
        query = urllib.parse.urlencode({"file_tokens": tokens}, doseq=True)
        data = self._call("GET", f"{TMP_URL_PATH}?{query}", what="换取附件临时下载地址")
        mapping: dict[str, str] = {}
        for item in data.get("tmp_download_urls") or []:
            if not isinstance(item, dict):
                continue
            token = str(item.get("file_token") or "")
            url = str(item.get("tmp_download_url") or "")
            if token and url:
                mapping[token] = url
        missing = [t for t in tokens if t not in mapping]
        if missing:
            raise FeishuError(
                f"飞书未返回 {len(missing)}/{len(tokens)} 个附件的临时下载地址（首个：{missing[0]}）"
            )
        return mapping

    # ---------------------------------------------------------------- 回读

    def readback_verify(self, table: FeishuTableRef, record_id: str, expected: dict,
                        *, attachment_fields: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
        """写后回读：普通列比值，附件列比名字与数量。返回 (是否一致, 差异清单)。"""
        record = self.get_record(table, record_id)
        fields = record.get("fields") or {}
        diffs: list[str] = []
        for name, want in expected.items():
            if name in attachment_fields:
                want_names = attachment_names(want)
                got_names = attachment_names(fields.get(name))
                if len(want_names) != len(got_names):
                    diffs.append(f"{name}: 附件数量 期望 {len(want_names)} 实际 {len(got_names)}")
                elif want_names != got_names:
                    diffs.append(f"{name}: 附件名 期望 {want_names} 实际 {got_names}")
                continue
            actual = self.normalize_field_value(fields.get(name))
            wanted = self.normalize_field_value(want)
            if not _same(actual, wanted):
                diffs.append(f"{name}: 期望 {_short(wanted)} 实际 {_short(actual)}")
        self.last_readback_diffs = diffs
        return (not diffs, diffs)

    @staticmethod
    def normalize_field_value(value: Any) -> Any:
        """把飞书回读值归一成普通值：富文本分段列表 → 字符串；附件/选项 → 名字（列表）。"""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return value.name
        if isinstance(value, Attachment):
            return value.name
        if isinstance(value, dict):
            for key in ("text", "name", "link", "en_name"):
                if key in value:
                    return str(value.get(key) or "")
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (list, tuple)):
            items = list(value)
            if items and all(isinstance(i, dict) and "text" in i and "file_token" not in i for i in items):
                return "".join(str(i.get("text") or "") for i in items)
            return [FeishuClient.normalize_field_value(i) for i in items]
        return str(value)


class MemoryFeishuClient(FeishuClient):
    """内存假实现：接口同名，记录调用，附件返回伪 token。dry-run 与测试用，永不联网。"""

    def __init__(self, app_id: str = "memory-app-id", app_secret: str = "memory-app-secret", *,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 60,
                 http: HttpFn | None = None, sleep: SleepFn | None = None) -> None:
        super().__init__(app_id, app_secret, base_url=base_url, timeout=timeout,
                         http=http or self._forbidden_http, sleep=sleep or (lambda _s: None))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tables: dict[str, dict[str, dict]] = {}
        self.media: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._token_seq = 0

    @staticmethod
    def _forbidden_http(*_args: Any, **_kwargs: Any) -> tuple[int, Any]:
        raise FeishuError("MemoryFeishuClient 不联网")

    # ------------------------------------------------------------ 内部工具

    def _record(self, _op: str, **kwargs: Any) -> None:
        self.calls.append((_op, kwargs))

    @staticmethod
    def _key(table: FeishuTableRef) -> str:
        return f"{table.app_token}/{table.table_id}"

    def _store(self, table: FeishuTableRef) -> dict[str, dict]:
        return self.tables.setdefault(self._key(table), {})

    def _expand(self, fields: dict) -> dict:
        """把 [{"file_token": ..}] 展开成带 name/size 的附件值，模拟飞书回读形态。"""
        out: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, list) and value and all(isinstance(i, dict) and "file_token" in i for i in value):
                expanded = []
                for item in value:
                    meta = self.media.get(str(item.get("file_token")), {})
                    expanded.append({
                        "file_token": str(item.get("file_token")),
                        "name": str(item.get("name") or meta.get("name") or "unknown.bin"),
                        "size": int(item.get("size") or meta.get("size") or 0),
                        "type": str(meta.get("mime") or "application/octet-stream"),
                    })
                out[key] = expanded
            else:
                out[key] = value
        return out

    # ------------------------------------------------------------ 接口

    def tenant_token(self) -> str:
        self._token_seq += 1
        self._record("tenant_token")
        self._token = f"memory-tenant-token-{self._token_seq}"
        self._token_expires_at = self._clock() + 7200
        return self._token

    def list_tables(self, app_token: str) -> list[dict]:
        """列出该 bitable 下已知的表。内部 key 形如 `<app_token>/<table_id>`，按前缀过滤。"""
        self._record("list_tables", app_token=app_token)
        prefix = f"{app_token}/"
        return [{"table_id": key[len(prefix):], "name": key[len(prefix):]}
                for key in sorted(self.tables) if key.startswith(prefix)]

    def list_fields(self, table: FeishuTableRef) -> list[dict]:
        self._record("list_fields", table=self._key(table))
        names: list[str] = []
        for record in self._store(table).values():
            for key in record.get("fields", {}):
                if key not in names:
                    names.append(key)
        return [{"field_name": n, "type": 1} for n in names]

    def list_records(self, table: FeishuTableRef, *, view_id: str | None = None,
                     filter_expr: str | None = None, page_size: int = 500) -> list[dict]:
        self._record("list_records", table=self._key(table), view_id=view_id,
                     filter_expr=filter_expr, page_size=page_size)
        return [json.loads(json.dumps(r, ensure_ascii=False)) for r in self._store(table).values()]

    def get_record(self, table: FeishuTableRef, record_id: str) -> dict:
        self._record("get_record", table=self._key(table), record_id=record_id)
        record = self._store(table).get(record_id)
        if record is None:
            raise FeishuError(f"记录不存在：{record_id}", code=1254043, msg="RecordIdNotFound")
        return json.loads(json.dumps(record, ensure_ascii=False))

    def create_records(self, table: FeishuTableRef, rows: Sequence[dict]) -> list[str]:
        rows = list(rows)
        store = self._store(table)
        ids: list[str] = []
        for offset in range(0, len(rows), BATCH_LIMIT):
            chunk = rows[offset:offset + BATCH_LIMIT]
            self._record("create_records", table=self._key(table), count=len(chunk))
            for row in chunk:
                fields = row.get("fields") if isinstance(row.get("fields"), dict) else row
                self._seq += 1
                rid = f"recmem{self._seq:06d}"
                store[rid] = {"record_id": rid, "fields": self._expand(dict(fields))}
                ids.append(rid)
        return ids

    def update_records(self, table: FeishuTableRef, updates: Sequence[dict]) -> int:
        updates = list(updates)
        store = self._store(table)
        done = 0
        for offset in range(0, len(updates), BATCH_LIMIT):
            chunk = updates[offset:offset + BATCH_LIMIT]
            self._record("update_records", table=self._key(table), count=len(chunk))
            for item in chunk:
                rid = str(item.get("record_id") or "")
                record = store.get(rid)
                if record is None:
                    raise FeishuError(f"记录不存在：{rid}", code=1254043, msg="RecordIdNotFound")
                record["fields"].update(self._expand(dict(item.get("fields") or {})))
                done += 1
        return done

    def upload_attachment(self, path: Path, *, parent_node: str) -> Attachment:
        path = Path(path)
        size = self._reject_oversize(path)
        content = path.read_bytes()
        md5 = hashlib.md5(content).hexdigest()
        token = "fkmem" + md5[:20]
        self.media[token] = {
            "name": path.name, "size": size, "content": content,
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        self._record("upload_attachment", name=path.name, size=size, parent_node=parent_node)
        return Attachment(name=path.name, file_token=token, md5=md5, size=size)

    def download_attachment(self, file_token: str, dest: Path) -> Path:
        dest = Path(dest)
        self._record("download_attachment", file_token=file_token, dest=dest.name)
        meta = self.media.get(file_token)
        if meta is None:
            raise FeishuError(f"内存里没有该附件：{file_token}", code=1061045, msg="NotFound")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(meta["content"])
        return dest

    #: 内存假实现的临时地址前缀（.invalid 顶级域永不解析，误提交也打不出去）
    MEMORY_TMP_URL_BASE = "https://memory.feishu.invalid/tmp"

    def temp_download_urls(self, file_tokens: list[str]) -> dict[str, str]:
        """确定性假地址：同一 token 永远同一 URL；未知 token 与真实客户端一样报错。"""
        tokens = [str(t or "").strip() for t in file_tokens]
        tokens = [t for i, t in enumerate(tokens) if t and t not in tokens[:i]]
        self._record("temp_download_urls", file_tokens=list(tokens))
        missing = [t for t in tokens if t not in self.media]
        if missing:
            raise FeishuError(
                f"内存里没有 {len(missing)}/{len(tokens)} 个附件，无法给临时地址（首个：{missing[0]}）",
                code=1061045, msg="NotFound",
            )
        return {t: f"{self.MEMORY_TMP_URL_BASE}/{urllib.parse.quote(t)}.mp4" for t in tokens}


def write_row_verified(client: FeishuClient, table: FeishuTableRef, fields: dict,
                       attachments: dict[str, list[Path]] | None = None) -> DeliveryRecord:
    """建行 → 传附件 → 更新附件列 → 回读校验。回读不一致时 readback_ok=False，
    差异见 client.last_readback_diffs（不抛异常，交由调用方决定是否停）。"""
    record_id = client.create_records(table, [dict(fields)])[0]
    written = list(fields.keys())
    uploaded: list[Attachment] = []
    expected = dict(fields)
    attachment_cols: list[str] = []
    if attachments:
        updates: dict[str, list[dict]] = {}
        for column, paths in attachments.items():
            items = [Path(p) for p in (paths or [])]
            if not items:
                continue
            metas = [client.upload_attachment(p, parent_node=table.app_token) for p in items]
            uploaded.extend(metas)
            updates[column] = [{"file_token": m.file_token, "name": m.name, "size": m.size} for m in metas]
            expected[column] = [m.name for m in metas]
            attachment_cols.append(column)
        if updates:
            client.update_records(table, [{"record_id": record_id, "fields": updates}])
            written.extend(updates.keys())
    ok, _diffs = client.readback_verify(table, record_id, expected,
                                        attachment_fields=tuple(attachment_cols))
    return DeliveryRecord(table=table, record_id=record_id, fields_written=written,
                          attachments=uploaded, readback_ok=ok, at=_now_iso())
