"""用系统 Chrome 正常启动一个专用配置，登录一次长期可用。

不用 playwright 自己启动浏览器：那样起来的实例带自动化标记，TikTok 会要求滑块验证。
这里用系统 Chrome 正常启动，再通过调试端口接管——浏览器本身跟人手动打开的没有区别。
用的是独立配置目录，不碰使用者日常那个 Chrome。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_PROFILE = Path.home() / ".config/worldview-scout/research/chrome-profile"
DEFAULT_PORT = 9222
LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def chrome_binary() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise SystemExit("找不到 Chrome。请先安装 Google Chrome。")


def cdp_alive(port: int = DEFAULT_PORT, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - 连不上就是没起来
        return False


def free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    """避开被占用的端口，比如使用者自己开了 9222。"""
    for port in range(start, start + tries):
        if cdp_alive(port):
            return port  # 已经有我们的实例在跑，直接用
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit(f"{start}–{start + tries} 端口都不可用")


def launch(*, profile: Path = DEFAULT_PROFILE, port: int = DEFAULT_PORT, url: str = "",
           headless: bool = False, wait_s: int = 25) -> int:
    """启动（或复用）专用 Chrome，返回实际端口。

    headless=True 用于无人值守：不弹窗口，但仍是系统 Chrome 正常启动，
    没有 playwright launch 注入的自动化标记。登录必须先在有头模式下完成一次。
    """
    if cdp_alive(port):
        if url:
            open_tab(url, port=port)
        return port

    profile = Path(profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome_binary(), f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--window-size=1440,900"]
    args.append(url or "about:blank")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(wait_s * 2):
        if cdp_alive(port):
            return port
        time.sleep(0.5)
    raise SystemExit(f"Chrome 启动了但 {wait_s} 秒内调试端口没就绪")


def open_tab(url: str, *, port: int = DEFAULT_PORT) -> None:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    try:
        urllib.request.urlopen(request, timeout=5)
    except Exception:  # noqa: BLE001 - 老版本 Chrome 用 GET
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/new?{url}", timeout=5)
        except Exception:  # noqa: BLE001,S110 - 开不了新标签不致命，忽略
            pass


def tiktok_cookies(port: int = DEFAULT_PORT) -> list[dict]:
    """从接管的浏览器里读出 TikTok 的 cookie。只读，不写回。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
        try:
            contexts = browser.contexts
            if not contexts:
                return []
            return [c for c in contexts[0].cookies() if "tiktok" in (c.get("domain") or "")]
        finally:
            browser.close()  # 只断开控制，不关使用者的窗口


def login_state(port: int = DEFAULT_PORT) -> tuple[bool, int]:
    cookies = tiktok_cookies(port)
    return bool(LOGIN_COOKIES & {c["name"] for c in cookies}), len(cookies)


def wait_for_login(*, port: int = DEFAULT_PORT, timeout_s: int = 300) -> int:
    """轮询直到检测到登录。返回 TikTok cookie 数量。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            ok, count = login_state(port)
        except Exception:  # noqa: BLE001 - 浏览器还在起，稍后重试
            ok, count = False, 0
        if ok:
            return count
        time.sleep(3)
    raise SystemExit(f"{timeout_s} 秒内没有检测到登录。浏览器窗口还开着，登录后重跑这条命令即可。")


def state_path_for(port: int = DEFAULT_PORT) -> dict:
    """给需要 storage_state 的调用方用。"""
    return {"cookies": tiktok_cookies(port)}


def shutdown(port: int = DEFAULT_PORT) -> bool:
    """关掉这个端口上的专用浏览器。登录态留在配置目录里，下次启动还在。"""
    if not cdp_alive(port):
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/browser", timeout=5)
    except Exception:  # noqa: BLE001 - 老版本没有这个端点，退回进程级
        subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], check=False)
    for _ in range(20):
        if not cdp_alive(port, timeout=1):
            return True
        time.sleep(0.5)
    return False


def info(port: int = DEFAULT_PORT) -> dict:
    if not cdp_alive(port):
        return {"运行中": False}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as resp:
            version = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        version = {}
    ok, count = login_state(port)
    return {"运行中": True, "端口": port, "浏览器": version.get("Browser"),
            "已登录 TikTok": ok, "TikTok cookie 数": count}
