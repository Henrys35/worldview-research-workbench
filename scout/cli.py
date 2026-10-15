"""scout 命令行入口。"""
from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout", description="找竞品账号，抓公开证据，归纳内容世界观")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="打开浏览器登录你自己的 TikTok，之后就能直接爬")
    login.add_argument("--port", type=int, default=9222, help="调试端口，默认 9222")
    login.add_argument("--timeout", type=int, default=300, help="等待登录的秒数")

    status = sub.add_parser("status", help="看专用浏览器有没有在跑、有没有登录")
    status.add_argument("--port", type=int, default=9222)

    stop = sub.add_parser("stop", help="关掉专用浏览器（登录态保留，下次启动还在）")
    stop.add_argument("--port", type=int, default=9222)

    probe = sub.add_parser("probe", help="试搜一次，看现在能不能直接爬")
    probe.add_argument("query", help="搜索关键词。找哪国内容就用哪国语言")
    probe.add_argument("--port", type=int, default=9222, help="专用浏览器的调试端口")
    probe.add_argument("--show-browser", action="store_true", help="显示浏览器窗口，便于人工过验证码")

    discover = sub.add_parser("discover", help="按关键词/市场/时间/播放门槛找候选账号")
    discover.add_argument("--query", action="append", required=True,
                          help="搜索关键词，可重复。找哪个市场就用哪个市场的语言")
    discover.add_argument("--market", help="只保留这个地区的内容，如 KR。留空表示不限")
    discover.add_argument("--since", type=int, default=30, help="只要最近 N 天发布的，默认 30")
    discover.add_argument("--min-views", type=int, default=0, help="播放量门槛")
    discover.add_argument("--pages", type=int, default=3, help="每个关键词翻几页，每页 12 条")
    discover.add_argument("--out", type=Path, required=True, help="输出目录，必须在仓库外")
    discover.add_argument("--dry-run", action="store_true", help="只报会发起多少次调用，不真跑")
    discover.add_argument("--min-hits", type=int, default=1, help="进清单草稿的最少命中条数")
    discover.add_argument("--min-median-views", type=int, default=0, help="进清单草稿的播放中位门槛")

    search = sub.add_parser("search", help="用自己的登录态直接搜 TikTok 找候选账号（免 credits）")
    search.add_argument("--query", action="append", required=True,
                        help="搜索关键词，可重复。找哪个市场就用哪个市场的语言")
    search.add_argument("--market", help="只保留这个地区发布的内容，如 KR")
    search.add_argument("--since", type=int, default=30, help="只要最近 N 天发布的，默认 30")
    search.add_argument("--min-views", type=int, default=0, help="播放量门槛")
    search.add_argument("--scrolls", type=int, default=6, help="每个关键词滚动几次，越多捞得越深")
    search.add_argument("--port", type=int, default=9222, help="专用浏览器的调试端口")
    search.add_argument("--headless", action="store_true",
                        help="不弹窗口跑，用于定时任务。必须已经登录过")
    search.add_argument("--out", type=Path, required=True, help="输出目录，必须在仓库外")
    search.add_argument("--min-hits", type=int, default=1, help="进清单草稿的最少命中条数")
    search.add_argument("--min-median-views", type=int, default=0, help="进清单草稿的播放中位门槛")

    from .crawl import add_parser as add_crawl_commands
    add_crawl_commands(sub)

    return parser


def _write_draft(result, args, candidates_path) -> None:
    """两条发现路径共用：把候选账号导成取证清单草稿。"""
    import yaml

    from .discover import draft_spec
    spec = draft_spec(result, name=f"{args.market or '不限市场'} {' / '.join(args.query)}",
                      min_hits=args.min_hits, min_median_views=args.min_median_views)
    spec_path = Path(args.out).expanduser() / "spec.draft.yaml"
    spec_path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\n候选清单：{candidates_path}")
    print(f"取证清单草稿：{spec_path}（{len(spec['competitors'])} 个账号，"
          f"归属都标 unverified，删掉不相关的再跑 scout crawl）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        from . import browser
        port = browser.launch(port=args.port, url="https://www.tiktok.com/login")
        ok, count = browser.login_state(port)
        if ok:
            print(f"这个浏览器已经登录过了（TikTok cookie {count} 个），直接跑 scout search 就行")
            return 0
        print(f"浏览器已打开（端口 {port}）。请在窗口里登录你自己的 TikTok 账号，"
              f"登录成功我会自动认出来。")
        count = browser.wait_for_login(port=port, timeout_s=args.timeout)
        print(f"登录成功，拿到 {count} 个 TikTok cookie。"
              f"窗口留着别关，接下来 scout search 会接管它抓取。")
        return 0

    if args.command == "stop":
        from . import browser
        print("已关闭" if browser.shutdown(args.port) else "本来就没在跑")
        return 0

    if args.command == "status":
        from . import browser
        for key, value in browser.info(args.port).items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "probe":
        from .probe import main as probe_main
        return probe_main(args.query, port=args.port)

    if args.command == "discover":
        from .discover import save, search, summarize
        calls = len(args.query) * args.pages
        print(f"将对 {len(args.query)} 个关键词各翻 {args.pages} 页，共 {calls} 次 viral 调用（消耗 credits）")
        if args.dry_run:
            return 0
        if Path(args.out).expanduser().resolve().is_relative_to(Path(__file__).resolve().parent.parent):
            raise SystemExit("--out 必须指向仓库外目录")
        result = search(args.query, market=args.market, since_days=args.since,
                        min_views=args.min_views, pages=args.pages)
        path = save(result, args.out)
        print(summarize(result))

        import yaml

        from .discover import draft_spec
        spec = draft_spec(result, name=f"{args.market or '不限市场'} {' / '.join(args.query)}",
                          min_hits=args.min_hits, min_median_views=args.min_median_views)
        spec_path = Path(args.out).expanduser() / "spec.draft.yaml"
        spec_path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        print(f"\n候选清单：{path}")
        print(f"取证清单草稿：{spec_path}（{len(spec['competitors'])} 个账号，"
              f"归属都标 unverified，删掉不相关的再跑 scout crawl）")
        return 0

    if args.command == "search":
        from .discover import save, summarize
        from .search import search as run_search
        repo = Path(__file__).resolve().parent.parent
        if Path(args.out).expanduser().resolve().is_relative_to(repo):
            raise SystemExit("--out 必须指向仓库外目录")
        print(f"搜 {len(args.query)} 个关键词，每个滚动 {args.scrolls} 次（不消耗 credits）")
        result = run_search(args.query, port=args.port, market=args.market,
                            since_days=args.since, min_views=args.min_views,
                            scrolls=args.scrolls, headless=args.headless, out=args.out)
        path = save(result, args.out)
        print(summarize(result))
        if result.get("被拦住的关键词"):
            print("\n被拦住的关键词：")
            for query, why in result["被拦住的关键词"].items():
                print(f"  {query}: {why}")
            print("在浏览器窗口里完成验证后重跑，已抓到的关键词会跳过")
        _write_draft(result, args, path)
        return 0

    if hasattr(args, "func"):
        return args.func(args)

    return 1
