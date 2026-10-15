"""飞书凭证读取与飞书模块用到的公共类型。凭证只从环境变量或仓库外配置读，永不入库。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CONFIG_DIR = Path(os.environ.get("SCOUT_CONFIG_DIR", "~/.config/worldview-scout")).expanduser()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class FeishuTableRef(StrictModel):
    """飞书多维表引用。field_map: 逻辑字段名 -> 表内实际列名。"""
    app_token: str = ""
    wiki_node_token: str | None = None
    table_id: str
    view_id: str | None = None
    field_map: dict[str, str] = Field(default_factory=dict)


class Attachment(StrictModel):
    name: str
    file_token: str | None = None
    md5: str = ""
    size: int = 0


class DeliveryRecord(StrictModel):
    table: FeishuTableRef
    record_id: str
    fields_written: list[str]
    attachments: list[Attachment] = Field(default_factory=list)
    readback_ok: bool = False
    readback_diffs: list[str] = Field(default_factory=list)
    card_id: str | None = None
    at: str


@dataclass
class Credentials:
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    sources: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.sources is None:
            self.sources = []


def load_credentials() -> Credentials:
    """读取顺序：环境变量 → $SCOUT_CONFIG_DIR/credentials.json → 历史 kos-pipeline-config.json。"""
    creds = Credentials()
    creds.feishu_app_id = os.environ.get("FEISHU_APP_ID") or None
    creds.feishu_app_secret = os.environ.get("FEISHU_APP_SECRET") or None
    if creds.feishu_app_id:
        creds.sources.append("env")

    for name in ("credentials.json", "kos-pipeline-config.json"):
        path = CONFIG_DIR / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        creds.feishu_app_id = creds.feishu_app_id or data.get("feishu_app_id")
        creds.feishu_app_secret = creds.feishu_app_secret or data.get("feishu_app_secret")
        if data.get("feishu_app_id"):
            creds.sources.append(str(path))
    return creds
