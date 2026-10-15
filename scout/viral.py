"""viral.app 实时搜索的只读客户端。

凭证从环境变量或 ~/.config/worldview-scout 读取，永不入库。
参数取值是 2026-09-22 实测确认的服务端白名单，传别的会 400。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://viral.app/api/v1"
DEFAULT_CONFIG = Path.home() / ".config/worldview-scout/viral-api.json"

# 服务端只认这几个取值。注意没有「近 N 天」这种自定义窗口，精确时间窗要本地过滤。
DATE_RANGES = ("yesterday", "this-week", "this-month", "last-3-months", "last-6-months", "all-time")
# 注意没有 views。按播放量筛选只能本地做。
SORT_BY = ("relevance", "date", "likes")
PLATFORMS = ("tiktok", "instagram", "youtube")


def load_key() -> tuple[str, str]:
    if key := os.environ.get("VIRAL_API_KEY", "").strip():
        return key, os.environ.get("VIRAL_BASE_URL", "").strip() or DEFAULT_BASE
    path = Path(os.environ.get("VIRAL_CONFIG_PATH", "") or DEFAULT_CONFIG).expanduser()
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if key := str(data.get("apiKey", "")).strip():
            return key, str(data.get("baseUrl") or DEFAULT_BASE)
    raise SystemExit(
        "找不到 viral API key。设环境变量 VIRAL_API_KEY，"
        f"或把 {{\"apiKey\": \"...\"}} 写进 {DEFAULT_CONFIG}"
    )


def _get(path: str, params: dict) -> dict:
    key, base = load_key()
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"x-api-key": key,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def search_videos(query: str, *, date_range: str = "this-month", sort_by: str = "date",
                  pages: int = 1, platform: str = "tiktok") -> list[dict]:
    """按关键词搜视频。每页 12 条，每页一次调用，消耗 credits。

    `region` 参数故意不暴露：实测它不过滤结果（传 KR 搜英文词，96 条里 0 条韩国）。
    要定位市场，请用该市场的语言写 query。
    """
    if date_range not in DATE_RANGES:
        raise ValueError(f"date_range 只能是 {DATE_RANGES}")
    if sort_by not in SORT_BY:
        raise ValueError(f"sort_by 只能是 {SORT_BY}（没有 views，按播放筛请本地做）")
    if platform not in PLATFORMS:
        raise ValueError(f"platform 只能是 {PLATFORMS}")

    collected: list[dict] = []
    token = None
    for _ in range(max(1, pages)):
        params = {"query": query, "dateRange": date_range, "sortBy": sort_by}
        if token:
            params["pageToken"] = token
        payload = _get(f"/live/{platform}/search/videos", params)
        collected.extend(payload.get("videos") or [])
        token = payload.get("nextPageToken")
        if not token:
            break
    return collected
