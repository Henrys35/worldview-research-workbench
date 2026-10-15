"""浏览器路径的关键词搜索：接管使用者登录过的 Chrome，批量翻页取回视频记录。

与 `discover`（viral 接口）产出同一种格式，下游 crawl/report/feishu 完全通用。

浏览器由 `scout.browser` 用系统 Chrome 正常启动，这里只通过调试端口接管它操作页面。
不自己 launch 浏览器——那样起来的实例带自动化标记，TikTok 会要求滑块验证。

遇到验证码时程序不作任何绕过：停下来等人在窗口里处理完再继续。
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import browser
from .discover import aggregate

CAPTCHA_MARKS = ("Drag the puzzle piece", "拖动拼图", "퍼즐", "Verify to continue", "安全验证")
# 登录墙文案随界面语言变化，这几种都见过。
LOGIN_WALL_MARKS = ("登录以搜索", "Log in to search", "로그인하여", "登录以继续")


def video_items(payload) -> list[dict]:
    """从嵌套响应里只挑出真正的视频记录。"""
    found: dict[str, dict] = {}

    def walk(value):
        if isinstance(value, dict):
            if (value.get("id") and value.get("createTime") is not None
                    and isinstance(value.get("video"), dict)
                    and isinstance(value.get("author"), dict)):
                found[str(value["id"])] = value
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return list(found.values())


def normalize(item: dict, query: str) -> dict | None:
    """TikTok 页面内部结构 → 与 viral 一致的字段名。缺关键字段的丢弃。"""
    handle = (item.get("author") or {}).get("uniqueId")
    if not handle or not item.get("id"):
        return None
    stats = item.get("stats") or {}
    tags = [c.get("title") for c in (item.get("challenges") or []) if c.get("title")]
    if not tags:
        tags = [t.get("hashtagName") for t in (item.get("textExtra") or []) if t.get("hashtagName")]

    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    published = None
    if item.get("createTime"):
        published = datetime.fromtimestamp(int(item["createTime"]), UTC).isoformat()

    return {
        "platform": "tiktok",
        "platform_video_id": str(item["id"]),
        "account_username": handle,
        "account_display_name": (item.get("author") or {}).get("nickname") or handle,
        # locationCreated 是发布地，不是受众所在地。
        "region": item.get("locationCreated") or "",
        "caption": item.get("desc") or "",
        "hashtags": tags,
        "published_at": published,
        "view_count": number(stats.get("playCount")),
        "like_count": number(stats.get("diggCount")),
        "comment_count": number(stats.get("commentCount")),
        "share_count": number(stats.get("shareCount")),
        "bookmark_count": number(stats.get("collectCount")),
        "duration_seconds": (item.get("video") or {}).get("duration"),
        "_query": query,
    }


def _blocked(text: str) -> str | None:
    if any(mark in text for mark in CAPTCHA_MARKS):
        return "验证码"
    if any(mark in text for mark in LOGIN_WALL_MARKS):
        return "登录墙"
    # 页面渲染了但只剩导航条和一个登录按钮，也是没登录。
    stripped = text.strip()
    if stripped and len(stripped) < 40 and any(
            mark in stripped for mark in ("登录", "Log in", "로그인")):
        return "登录墙"
    return None


def fetch_query(context, query: str, *, scrolls: int, wait_human: int = 180
                ) -> tuple[list[dict], dict]:
    """在接管的浏览器里搜一个关键词。返回（原始 item 列表，观察记录）。"""
    payloads: list[dict] = []
    seen: dict = {}

    def capture(response):
        if any(x in urlsplit(response.url).path
               for x in ("/api/search/", "/api/post/item_list/")):
            try:
                data = response.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应跳过
                return
            if video_items(data):
                payloads.append(data)

    def check(page) -> bool:
        """检查是否被拦。窗口是开着的，验证码停下来等人处理完，程序不做任何绕过。"""
        blocked = _blocked(page.locator("body").inner_text()[:2000])
        if not blocked:
            return True
        if blocked == "登录墙":
            seen["拦截"] = "登录墙"
            return False
        print(f"  [{query}] TikTok 要求人工验证，请在浏览器窗口里完成，"
              f"最多等 {wait_human} 秒……", flush=True)
        for _ in range(max(1, wait_human // 3)):
            page.wait_for_timeout(3000)
            if not _blocked(page.locator("body").inner_text()[:2000]):
                print(f"  [{query}] 验证已通过，继续抓取", flush=True)
                return True
        seen["拦截"] = "验证码未在超时内通过"
        return False

    def action(page):
        # 验证码可能在加载后立刻出现，也可能滚动几次才触发，所以每个阶段都查。
        if not check(page):
            return
        try:
            page.wait_for_selector('a[href*="/video/"]', timeout=15000)
        except Exception:  # noqa: BLE001 - 等不到就记录现象继续
            seen["无视频链接"] = True
        page.mouse.move(900, 550)
        for index in range(scrolls):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)
            if index % 2 == 1 and not check(page):
                return
        check(page)
        seen["页面链接数"] = page.locator('a[href*="/video/"]').count()
        if not payloads:
            seen["页面文本"] = page.locator("body").inner_text()[:400]

    url = f"https://www.tiktok.com/search/video?q={quote(query)}"
    page = context.new_page()
    page.on("response", capture)
    try:
        # 后台标签页会被 Chrome 节流，一行内容都不渲染，必须放到前台。
        page.bring_to_front()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(10):  # 等页面真的渲染出东西再判断
            page.wait_for_timeout(1500)
            if page.locator("body").inner_text().strip():
                break
        action(page)
    except Exception as exc:  # noqa: BLE001 - 单个关键词失败不该中断整轮
        seen["异常"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001,S110 - 页面可能已被使用者关掉，忽略
            pass

    items: dict[str, dict] = {}
    for payload in payloads:
        for item in video_items(payload):
            items[str(item["id"])] = item
    return list(items.values()), seen


def search(queries: list[str], *, port: int = browser.DEFAULT_PORT, market: str | None = None,
           since_days: int = 30, min_views: int = 0, scrolls: int = 6,
           headless: bool = False, out: Path | None = None) -> dict:
    """跑完所有关键词并聚合。原始响应落盘存证，同一目录重跑时已完成的关键词跳过。"""
    from playwright.sync_api import sync_playwright

    port = browser.launch(port=port, headless=headless)
    logged_in, cookie_count = browser.login_state(port)
    if not logged_in:
        raise SystemExit("接管的浏览器还没登录 TikTok。先跑 scout login，在窗口里登录你自己的号。")
    print(f"已接管浏览器（端口 {port}，TikTok cookie {cookie_count} 个）", flush=True)

    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    raw_dir = (Path(out).expanduser() / "raw") if out else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    raw: list[dict] = []
    per_query: dict[str, int] = {}
    blocked: dict[str, str] = {}

    with sync_playwright() as play:
        chrome = play.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=20000)
        context = chrome.contexts[0] if chrome.contexts else chrome.new_context()
        try:
            for query in queries:
                cached = raw_dir / f"{re.sub(r'[^\w.-]+', '_', query).strip('_') or 'query'}.json" \
                    if raw_dir else None
                if cached and cached.exists():
                    items = json.loads(cached.read_text(encoding="utf-8"))["items"]
                    print(f"  [{query}] 复用已有结果 {len(items)} 条", flush=True)
                else:
                    items, seen = fetch_query(context, query, scrolls=scrolls)
                    if seen.get("拦截"):
                        blocked[query] = seen["拦截"]
                    print(f"  [{query}] {len(items)} 条"
                          + (f"  ← 被{seen['拦截']}拦住" if seen.get("拦截") else ""), flush=True)
                    if cached:
                        cached.write_text(json.dumps({"query": query, "items": items,
                                                      "checked_at": datetime.now(UTC).isoformat(),
                                                      "observations": seen}, ensure_ascii=False),
                                          encoding="utf-8")
                per_query[query] = len(items)
                raw.extend(v for v in (normalize(i, query) for i in items) if v)
        finally:
            chrome.close()  # 只断开控制，浏览器窗口留给使用者

    result = aggregate(raw, per_query, market=market, cutoff=cutoff, min_views=min_views,
                       query_info={"关键词": queries, "市场": market,
                                   "时间窗": f"近 {since_days} 天（>= {cutoff}）",
                                   "播放门槛": min_views, "滚动次数": scrolls,
                                   "来源": "接管浏览器搜索"},
                       since_days=since_days)
    if blocked:
        result["被拦住的关键词"] = blocked
    return result
