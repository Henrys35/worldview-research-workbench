"""New research bases only; reuse the repository Feishu client and verify writes."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import FeishuTableRef
from .feishu import BITABLE, FeishuClient


def _table(name, rows, columns):
    fields = [{"field_name": label, "type": kind} for label, kind in columns]
    return {"name": name, "fields": fields, "rows": rows}


def build_payload(snapshot, stats, analysis):
    from .crawl import digest
    findings = {f.handle: f for f in analysis.findings} if analysis else {}
    worlds, coverage, ideas = [], [], []
    for index, account in enumerate(snapshot["accounts"]):
        finding = findings.get(account["handle"])
        row = {"品牌": account["brand"], "记录键": f"account-{index}",
               "账号": account["handle"] or "未核实", "样本数": stats[index]["count"],
               "归属证据": account["identity"],
               "世界观归纳": finding.worldview if finding else "待分析／证据不足",
               "用户处境": finding.audience_tension if finding else "",
               "产品角色": finding.product_role if finding else "",
               "内容形式": "；".join(finding.recurring_formats) if finding else "",
               "借鉴建议": finding.adaptation if finding else "",
               "原帖证据": "\n".join(v["url"] for v in snapshot["videos"]
                                      if finding and v["id"] in finding.evidence_ids),
               "审看范围": finding.review_scope if finding else "caption_and_metadata_only",
               "局限": finding.limitations if finding else "尚未形成内容世界观结论"}
        worlds.append(row)
        coverage.append({"品牌": account["brand"], "记录键": f"coverage-{index}",
                         "账号": account["handle"] or "未核实", "采集状态": account["status"],
                         "样本范围": account["coverage"],
                         "失败与缺口": "；".join(account["errors"]),
                         "官网来源": account["official_source"],
                         "归属核验日期": account["identity_checked_at"],
                         "近期样本": stats[index]["recent"]["count"],
                         "历史样本": stats[index]["older"]["count"],
                         "日期未知": stats[index]["unknown_date"]["count"],
                         "近期播放中位数": str(stats[index]["recent"]["median_views"]
                                               if stats[index]["recent"]["known_views"] else "未获取"),
                         "数据说明": "；".join(snapshot["limitations"])})
    samples = []
    for video in snapshot["videos"]:
        row = {"文案": video["caption"] or "未获取", "记录键": video["id"],
               "品牌": video["brand"], "账号": video["handle"],
               "原帖": {"link": video["url"], "text": "打开原帖"},
               "发布时间": video["published_at"] or "未获取", "抓取时间": video["captured_at"],
               "数据来源": video["source"], "状态": video["status"],
               "缺口": video["detail_error"], "审看范围": video["review_scope"],
               "是否付费推广": "未知"}
        for source, label in (("views", "播放"), ("likes", "点赞"), ("comments", "评论数"),
                              ("shares", "分享"), ("duration_s", "时长秒")):
            if video[source] is not None:
                row[label] = video[source]
        samples.append(row)
    for index, idea in enumerate(analysis.ideas if analysis else []):
        ideas.append({"创意": idea.title, "记录键": f"idea-{index}", "开场": idea.hook,
                      "情节": idea.story, "真实产品证明": idea.product_proof,
                      "AI UGC 执行": idea.ai_ugc_execution, "待验证假设": idea.hypothesis,
                      "原帖证据": "\n".join(v["url"] for v in snapshot["videos"]
                                             if v["id"] in idea.evidence_ids)})
    def columns(labels, numeric=(), links=()):
        return [(label, 2 if label in numeric else 15 if label in links else 1)
                for label in labels.split("| ")]
    tables = [
        _table("01 竞品世界观", worlds, columns(
            "品牌| 记录键| 账号| 样本数| 归属证据| 世界观归纳| 用户处境| 产品角色| 内容形式| 借鉴建议| 原帖证据| 审看范围| 局限",
            numeric=("样本数",))),
        _table("02 原始视频样本", samples, columns(
            "文案| 记录键| 品牌| 账号| 原帖| 发布时间| 抓取时间| 播放| 点赞| 评论数| 分享| 时长秒| 数据来源| 状态| 缺口| 审看范围| 是否付费推广",
            numeric=("播放", "点赞", "评论数", "分享", "时长秒"), links=("原帖",))),
        _table("03 内容建议", ideas, columns(
            "创意| 记录键| 开场| 情节| 真实产品证明| AI UGC 执行| 待验证假设| 原帖证据")),
        _table("04 覆盖与缺口", coverage, columns(
            "品牌| 记录键| 账号| 采集状态| 样本范围| 失败与缺口| 官网来源| 归属核验日期| 近期样本| 历史样本| 日期未知| 近期播放中位数| 数据说明",
            numeric=("近期样本", "历史样本", "日期未知"))),
    ]
    return {"name": f"{snapshot['spec']['name']} · {snapshot['created_at'][:10]}",
            "snapshot_sha256": digest(snapshot), "analysis_ready": bool(analysis), "tables": tables}


def _records(client, ref):
    records = client.list_records(ref)
    keys = [str(client.normalize_field_value(r.get("fields", {}).get("记录键")) or "")
            for r in records]
    if any(not key for key in keys) or any(n > 1 for n in Counter(keys).values()):
        raise ValueError("research table has missing/duplicate record keys; refusing to mutate")
    return dict(zip(keys, records, strict=True))


def _verify(client, existing, rows, fields):
    """Compare the complete readback, including hyperlink targets and numeric strings."""
    kinds = {f["field_name"]: f["type"] for f in fields}
    for row in rows:
        got = existing[row["记录键"]].get("fields", {})
        for name, want in row.items():
            actual = got.get(name)
            if kinds[name] == 15:
                same = (isinstance(actual, dict) and actual.get("link") == want["link"]
                        and actual.get("text") == want["text"])
            elif kinds[name] == 2:
                try:
                    same = Decimal(str(actual)) == Decimal(str(want))
                except InvalidOperation:
                    same = False
            else:
                same = (client.normalize_field_value(actual) or "") == (want or "")
            if not same:
                raise ValueError(f"research readback mismatch: key={row['记录键']}, field={name}")


def deliver(out: Path, *, write=False, client=None) -> dict:
    from .crawl import (
        cohort_stats,
        digest,
        read,
        run_lock,
        save,
        validate_analysis,
    )
    with run_lock(out):
        payload = read(out / "feishu-preview.json")
        snapshot = read(out / "snapshot.json")
        analysis = (validate_analysis(snapshot, read(out / "analysis.json"))
                    if (out / "analysis.json").exists() else None)
        if payload != build_payload(snapshot, cohort_stats(snapshot), analysis):
            raise ValueError("preview is stale or edited; rerun research report")
        if not write:
            return {"action": "preview_only", "analysis_ready": payload["analysis_ready"],
                    "tables": {t["name"]: len(t["rows"]) for t in payload["tables"]}}
        if not analysis:
            raise ValueError("finish evidence-based analysis and research report before Feishu write")
        if client is None:
            from .config import load_credentials
            credentials = load_credentials()
            credentials.require("feishu_app_id", "feishu_app_secret")
            client = FeishuClient(credentials.feishu_app_id, credentials.feishu_app_secret)
        path = out / "feishu-delivery.json"
        receipt = read(path) if path.exists() else {"payload_sha256": digest(payload), "tables": {}}
        if receipt["payload_sha256"] != digest(payload):
            raise ValueError("delivered payload changed; use a separate research run")
        receipt["verified"] = False
        save(path, receipt)
        if not receipt.get("app_token"):
            if receipt.get("pending"):
                raise ValueError("previous base creation outcome unknown; reconcile it before retry")
            receipt["pending"] = "create_base"
            save(path, receipt)
            app = client._call("POST", BITABLE,
                               body={"name": payload["name"], "time_zone": "Asia/Singapore"},
                               what="创建调研多维表")["app"]
            receipt.update(app_token=app["app_token"], url=app["url"], pending=None)
            save(path, receipt)
        app_token = receipt["app_token"]
        # Only tables in a newly created, receipted research base are ever written.
        for table in payload["tables"]:
            name = table["name"]
            if name not in receipt["tables"]:
                if receipt.get("pending"):
                    raise ValueError("previous table creation outcome unknown; reconcile before retry")
                receipt["pending"] = f"create_table:{name}"
                save(path, receipt)
                data = client._call("POST", f"{BITABLE}/{app_token}/tables",
                                    body={"table": {"name": name, "default_view_name": "全部",
                                                    "fields": table["fields"]}}, what="创建调研数据表")
                receipt["tables"][name] = {"table_id": data["table_id"]}
                receipt["pending"] = None
                save(path, receipt)
            info = receipt["tables"][name]
            ref = FeishuTableRef(app_token=app_token, table_id=info["table_id"])
            field_types = {f["field_name"]: f["type"] for f in client.list_fields(ref)}
            if any(field_types.get(f["field_name"]) != f["type"] for f in table["fields"]):
                raise ValueError(f"research table schema changed: {name}")
            existing = _records(client, ref)
            _verify(client, existing, [r for r in table["rows"] if r["记录键"] in existing],
                    table["fields"])
            if info.get("pending_keys"):
                if not set(info["pending_keys"]).issubset(existing):
                    raise ValueError("previous row write outcome unknown; no automatic duplicate write")
                info.pop("pending_keys")
                save(path, receipt)
            missing = [row for row in table["rows"] if row["记录键"] not in existing]
            for offset in range(0, len(missing), 100):
                batch = missing[offset:offset + 100]
                info["pending_keys"] = [row["记录键"] for row in batch]
                save(path, receipt)
                client.create_records(ref, batch)
                info.pop("pending_keys")
                save(path, receipt)
            existing = _records(client, ref)
            if len(existing) != len(table["rows"]):
                raise ValueError(f"row count mismatch: {name}")
            if not {r["记录键"] for r in table["rows"]}.issubset(existing):
                raise ValueError(f"missing row after write: {name}")
            _verify(client, existing, table["rows"], table["fields"])
            info["verified_rows"] = len(table["rows"])
            save(path, receipt)
        receipt["verified"] = True
        receipt["verified_at"] = datetime.now(UTC).isoformat()
        save(path, receipt)
        return receipt
