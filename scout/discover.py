"""按条件找候选账号：关键词搜索 → 本地三重过滤 → 按账号聚合。

世界观是账号级的东西，所以这一步的产出是「值得细看的账号」，不是散落的视频。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from . import viral

# viral 的 dateRange 只有 6 档，先用最接近的一档去拉，再用 --since 在本地精确切。
COARSE_WINDOW = ((7, "this-week"), (31, "this-month"), (92, "last-3-months"),
                 (184, "last-6-months"))


def coarse_range(since_days: int) -> str:
    for limit, name in COARSE_WINDOW:
        if since_days <= limit:
            return name
    return "all-time"


def _keep(video: dict, *, market: str | None, cutoff: str, min_views: int) -> bool:
    if market and (video.get("region") or "").upper() != market.upper():
        return False
    if (video.get("published_at") or "")[:10] < cutoff:
        return False
    return (video.get("view_count") or 0) >= min_views


def search(queries: list[str], *, market: str | None = None, since_days: int = 30,
           min_views: int = 0, pages: int = 3, platform: str = "tiktok") -> dict:
    """返回 {漏斗, 账号, 视频}。每个关键词每页一次调用，消耗 credits。"""
    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    window = coarse_range(since_days)

    raw: list[dict] = []
    per_query: dict[str, int] = {}
    for query in queries:
        found = viral.search_videos(query, date_range=window, sort_by="date",
                                    pages=pages, platform=platform)
        per_query[query] = len(found)
        for video in found:
            video["_query"] = query
        raw.extend(found)

    return aggregate(raw, per_query, market=market, cutoff=cutoff, min_views=min_views,
                     query_info={"关键词": queries, "市场": market,
                                 "时间窗": f"近 {since_days} 天（>= {cutoff}）",
                                 "播放门槛": min_views, "每词翻页": pages, "粗窗口": window},
                     since_days=since_days)


def aggregate(raw: list[dict], per_query: dict[str, int], *, market: str | None, cutoff: str,
              min_views: int, query_info: dict, since_days: int) -> dict:
    """两条发现路径共用：去重 → 三重过滤 → 按账号聚合。输入必须是规范化后的视频记录。"""
    unique = list({v["platform_video_id"]: v for v in raw}.values())
    by_market = [v for v in unique if not market or (v.get("region") or "").upper() == market.upper()]
    by_date = [v for v in by_market if (v.get("published_at") or "")[:10] >= cutoff]
    kept = [v for v in by_date if (v.get("view_count") or 0) >= min_views]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for video in kept:
        grouped[video["account_username"]].append(video)

    accounts = []
    for handle, videos in grouped.items():
        views = sorted((v.get("view_count") or 0) for v in videos)
        tags: dict[str, int] = defaultdict(int)
        for video in videos:
            for tag in (video.get("hashtags") or []):
                tags[tag] += 1
        accounts.append({
            "handle": handle,
            "display_name": videos[0].get("account_display_name"),
            "region": videos[0].get("region"),
            "命中条数": len(videos),
            "播放中位": int(median(views)),
            "播放最高": max(views),
            "常用话题": [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:6]],
            "命中关键词": sorted({v.get("_query", "") for v in videos} - {""}),
            "样例": [{"url": f"https://www.tiktok.com/@{handle}/video/{v['platform_video_id']}",
                      "发布": (v.get("published_at") or "")[:10],
                      "播放": v.get("view_count"),
                      "文案": (v.get("caption") or "")[:120]}
                     for v in sorted(videos, key=lambda v: -(v.get("view_count") or 0))[:3]],
        })
    accounts.sort(key=lambda a: -a["播放中位"])

    return {
        "查询": query_info,
        "漏斗": {"各词捞回": per_query, "去重后": len(unique),
                 f"地区={market or '不限'}": len(by_market),
                 f"近{since_days}天": len(by_date),
                 f"播放>={min_views}": len(kept), "聚出账号": len(accounts)},
        "账号": accounts,
        "视频": kept,
    }


def save(result: dict, out: Path) -> Path:
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    path = out / "candidates.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summarize(result: dict) -> str:
    lines = ["漏斗："]
    lines += [f"  {k}: {v}" for k, v in result["漏斗"].items()]
    lines.append(f"\n候选账号 {len(result['账号'])} 个（按播放中位排序）：")
    for account in result["账号"][:20]:
        lines.append(
            f"  @{account['handle']:<24} {account['region']:<3} "
            f"命中{account['命中条数']:>2}条 中位{account['播放中位']:>9,} "
            f"最高{account['播放最高']:>9,}  {'/'.join(account['常用话题'][:3])}"
        )
    return "\n".join(lines)


def draft_spec(result: dict, *, name: str, product_context: str = "",
               min_hits: int = 1, min_median_views: int = 0, limit: int = 30) -> dict:
    """把候选账号导成取证清单草稿。

    归属一律标 unverified：搜索命中说明账号发过相关内容，不等于它是某品牌的官方号。
    要标 official_link，必须人工核实官网社媒链接后自己改。
    """
    picked = [a for a in result["账号"]
              if a["命中条数"] >= min_hits and a["播放中位"] >= min_median_views][:limit]
    return {
        "name": name,
        "product_context": product_context or "（填：我们的用户、产品能力、本轮想回答的问题）",
        "max_videos": 20,
        "recent_days": 90,
        "competitors": [
            {"brand": account["display_name"] or account["handle"],
             "handle": account["handle"],
             "identity": "unverified",
             "seed_videos": [account["样例"][0]["url"]] if account["样例"] else []}
            for account in picked
        ],
    }
