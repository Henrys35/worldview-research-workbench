"""TikTok public HTTP collector. No browser, cookies, login, or paid API fallback.

Creator embeds expose a bounded sample, not a complete account history. Details
are read from TikTok's public hydration JSON. Missing data remains missing.
Only allowlisted public fields are persisted; app/session hydration is discarded.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .schemas import Competitor, VideoEvidence

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
VIDEO_PATH = re.compile(r"^/@([A-Za-z0-9_.]+)/video/(\d{5,30})/?$")
SCRIPT_IDS = {"__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE",
              "__FRONTITY_CONNECT_STATE__"}


def canonical_video(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    match = VIDEO_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
            or parsed.username or parsed.password or parsed.port or not match):
        raise ValueError("expected a full https://www.tiktok.com/@handle/video/id URL")
    handle, vid = match.groups()
    return f"https://www.tiktok.com/@{handle}/video/{vid}", handle, vid


def http_get(url: str) -> tuple[int, str]:
    """One bounded public GET; failures never launch an alternative service."""
    if urlsplit(url).hostname != "www.tiktok.com":
        raise ValueError("this collector only requests www.tiktok.com")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en"})
    try:
        with urlopen(request, timeout=25) as response:
            if urlsplit(response.url).hostname not in {"www.tiktok.com", "tiktok.com"}:
                raise ValueError("unexpected redirect")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            return response.status, raw.decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, ""
    except (URLError, TimeoutError, OSError) as exc:
        # Do not expose arbitrary response text, URLs or proxy configuration.
        raise RuntimeError(type(exc).__name__) from None


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: dict[str, str] = {}
        self.links: list[str] = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") in SCRIPT_IDS:
            self.active = attrs["id"]
            self.scripts[self.active] = ""
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_data(self, text):
        if self.active:
            self.scripts[self.active] += text

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = None


def parse_page(html: str) -> tuple[dict, list[str]]:
    page = _Page()
    page.feed(html)
    data = {}
    for key, raw in page.scripts.items():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data[key] = parsed
        except (ValueError, RecursionError):
            continue
    return data, page.links


def video_items(data):
    """Extract actual detail items; do not confuse user IDs with video IDs."""
    found = {}
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
    walk(data)
    return list(found.values())


def _number(value):
    try:
        number = int(value)
        return number if number >= 0 and not isinstance(value, bool) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _published(value):
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat() if int(value) > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _get(url, get, evidence):
    checked = datetime.now(UTC).isoformat()
    try:
        status, body = get(url)
        evidence.append({"url": url, "captured_at": checked, "http_status": status,
                         "body_sha256": hashlib.sha256(body.encode()).hexdigest()})
        if status != 200:
            return {}, [], f"http_{status}"
        data, links = parse_page(body)
        return data, links, "" if data else "no_public_json"
    except (RuntimeError, ValueError, OSError) as exc:
        error = type(exc).__name__
        evidence.append({"url": url, "captured_at": checked, "error": error})
        return {}, [], error


def discover(competitor: Competitor, *, get=http_get) -> dict:
    """Fetch profile identity and a public creator-embed sample (no pagination)."""
    handle = competitor.handle
    base = {"brand": competitor.brand, "handle": handle, "identity": competitor.identity,
            "official_source": competitor.official_source,
            "identity_checked_at": competitor.identity_checked_at,
            "identity_note": "manifest evidence; not reverified by this TikTok crawl",
            "requests": [], "candidates": [], "profile": {}, "errors": [],
            "coverage": "bounded_public_embed_sample; not_full_history",
            "status": "unconfigured"}
    if not handle:
        base["errors"].append("missing_verified_account")
        return base
    url = f"https://www.tiktok.com/@{handle}"
    data, links, error = _get(url, get, base["requests"])
    if error:
        base["errors"].append(f"profile:{error}")
    scope = _mapping(_mapping(data.get("__UNIVERSAL_DATA_FOR_REHYDRATION__"))
                     .get("__DEFAULT_SCOPE__"))
    info = _mapping(_mapping(scope.get("webapp.user-detail")).get("userInfo"))
    user, stats = _mapping(info.get("user")), _mapping(info.get("stats"))
    embedded, embed_links, error = _get(f"https://www.tiktok.com/embed/@{handle}",
                                       get, base["requests"])
    if error:
        base["errors"].append(f"embed:{error}")
    source = _mapping(_mapping(_mapping(embedded.get("__FRONTITY_CONNECT_STATE__"))
                               .get("source")).get("data"))
    entry = _mapping(source.get(f"/embed/@{handle}", source.get(f"/embed/@{handle}/")))
    embed_user = _mapping(entry.get("userInfo"))
    if not user:
        user, stats = embed_user, embed_user
    actual_handle = user.get("uniqueId", "")
    if actual_handle and actual_handle.lower() != handle.lower():
        base["errors"].append("profile_handle_mismatch")
        base["status"] = "blocked"
        return base
    base["profile"] = {key: user.get(key) for key in
                       ("uniqueId", "nickname", "signature", "verified", "privateAccount")}
    base["profile"].update({key: _number(stats.get(key)) for key in
                            ("followerCount", "heartCount", "videoCount")})
    candidates = {}
    listing = entry.get("videoList")
    for item in listing if isinstance(listing, list) else []:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("id", ""))
        author = str(item.get("authorUniqueId") or handle)
        if re.fullmatch(r"\d{5,30}", vid) and author.lower() == handle.lower():
            candidates[vid] = {"id": vid, "handle": handle,
                               "url": f"{url}/video/{vid}",
                               "caption": str(item.get("desc") or ""),
                               "views": _number(item.get("playCount")),
                               "captured_at": base["requests"][-1]["captured_at"]}
    for link in [*competitor.seed_videos, *links, *embed_links]:
        try:
            canonical, author, vid = canonical_video(
                "https://www.tiktok.com" + link if link.startswith("/@") else link)
        except ValueError:
            continue
        if author.lower() == handle.lower():
            candidates.setdefault(vid, {"id": vid, "handle": handle, "url": canonical,
                                       "caption": "", "views": None})
    base["candidates"] = list(candidates.values())
    base["status"] = "ok" if candidates else "no_public_videos"
    if not candidates:
        base["errors"].append("no_public_sample; does_not_prove_account_has_no_posts")
    return base


def detail(candidate: dict, brand: str, *, get=http_get) -> dict:
    url, handle, vid = canonical_video(candidate["url"])
    requests = []
    data, _, error = _get(url, get, requests)
    item = next((v for v in video_items(data) if str(v["id"]) == vid), None)
    if item and item.get("author", {}).get("uniqueId", "").lower() != handle.lower():
        item, error = None, "video_author_mismatch"
    stats = _mapping((item or {}).get("stats")) or _mapping((item or {}).get("statsV2"))
    video = (item or {}).get("video", {})
    duration = _number(video.get("duration"))
    record = VideoEvidence(
        id=vid, brand=brand, handle=handle, url=url,
        caption=str(item.get("desc") or "") if item else candidate.get("caption", ""),
        captured_at=(requests[-1]["captured_at"] if item else
                     candidate.get("captured_at") or requests[-1]["captured_at"]),
        published_at=_published(item.get("createTime")) if item else None,
        views=_number(stats.get("playCount")) if item else candidate.get("views"),
        likes=_number(stats.get("diggCount")), comments=_number(stats.get("commentCount")),
        shares=_number(stats.get("shareCount")), duration_s=duration,
        region=str((item or {}).get("locationCreated") or ""),
        source="tiktok_http_detail" if item else "tiktok_http_embed",
        status="ok" if item else "partial",
        detail_error="" if item else error or "video_json_unavailable",
    )
    return {"record": record.model_dump(), "requests": requests}
