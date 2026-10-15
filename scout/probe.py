"""探针：在接管的浏览器里试搜一次，回答「这个号现在能不能直接爬」。

只读。不解验证码——遇到就如实报告并停下，由人决定怎么办。
"""
from __future__ import annotations

import json

from . import browser
from .search import fetch_query


def run(query: str, *, port: int = browser.DEFAULT_PORT, scrolls: int = 2) -> dict:
    from playwright.sync_api import sync_playwright

    if not browser.cdp_alive(port):
        return {"结论": "专用浏览器没在跑。先跑 scout login。"}

    ok, count = browser.login_state(port)
    report = {"关键词": query, "端口": port, "TikTok cookie": count,
              "已登录": ok}
    if not ok:
        report["结论"] = "浏览器没登录 TikTok。跑 scout login，在窗口里登录你自己的号。"
        return report

    with sync_playwright() as play:
        chrome = play.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=20000)
        context = chrome.contexts[0] if chrome.contexts else chrome.new_context()
        try:
            items, seen = fetch_query(context, query, scrolls=scrolls, wait_human=0)
        finally:
            chrome.close()

    report["页面链接数"] = seen.get("页面链接数", 0)
    report["抓到视频"] = len(items)
    if seen.get("拦截"):
        report["结论"] = {
            "验证码": "撞到验证码。在浏览器窗口里拖一下滑块，然后重跑。",
            "验证码未在超时内通过": "撞到验证码且没人处理。在窗口里完成验证后重跑。",
            "登录墙": "撞到登录墙，登录态没生效。重跑 scout login。",
        }.get(seen["拦截"], seen["拦截"])
    elif items:
        report["结论"] = f"能直接爬，本次拿到 {len(items)} 条"
        report["样例"] = [
            {"作者": i.get("author", {}).get("uniqueId"),
             "播放": i.get("stats", {}).get("playCount"),
             "文案": (i.get("desc") or "")[:36]}
            for i in items[:3]
        ]
    else:
        report["结论"] = "没被拦，但也没抓到。看页面文本判断"
        report["页面文本"] = seen.get("页面文本", "")[:300]
    return report


def main(query: str, *, port: int = browser.DEFAULT_PORT) -> int:
    print(json.dumps(run(query, port=port), ensure_ascii=False, indent=2))
    return 0
