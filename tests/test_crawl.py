"""Offline public-response fixtures; never start a browser or call live APIs."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from scout import cli
from scout.crawl import (
    cohort_stats,
    collect,
    digest,
    load_spec,
    read,
    report,
    save,
    validate_analysis,
)
from scout.feishu import BITABLE, MemoryFeishuClient
from scout.feishu_tables import deliver
from scout.schemas import Competitor, ResearchSpec
from scout.tiktok_public import canonical_video, detail, discover, video_items

HANDLE = "tutor_example"
VID = "7000000000000000001"
URL = f"https://www.tiktok.com/@{HANDLE}/video/{VID}"


def page(script_id, data):
    return f'<script id="{script_id}" type="application/json">{json.dumps(data)}</script>'


def fake_get(url):
    if "/embed/" in url:
        return 200, page("__FRONTITY_CONNECT_STATE__", {
            "user": {"ttwid": "private-session-marker"},
            "source": {"data": {f"/embed/@{HANDLE}": {
                "userInfo": {"uniqueId": HANDLE, "followerCount": 12},
                "videoList": [{"id": VID, "desc": "Teach one step", "playCount": 22,
                               "authorUniqueId": HANDLE},
                              {"id": VID, "desc": "Teach one step", "playCount": 22,
                               "authorUniqueId": HANDLE}],
            }}}})
    if "/video/" in url:
        return 200, page("__UNIVERSAL_DATA_FOR_REHYDRATION__", {"__DEFAULT_SCOPE__": {
            "webapp.app-context": {"nonce": "private-session-marker"},
            "webapp.video-detail": {"itemInfo": {"itemStruct": {
                "id": VID, "desc": "=Teach one step", "createTime":
                    int((datetime.now(UTC) - timedelta(days=2)).timestamp()),
                "author": {"uniqueId": HANDLE}, "video": {"duration": 15},
                "stats": {"playCount": "100", "diggCount": 0, "commentCount": 2},
            }}}}})
    return 200, page("__UNIVERSAL_DATA_FOR_REHYDRATION__", {"__DEFAULT_SCOPE__": {
        "webapp.user-detail": {"userInfo": {
            "user": {"uniqueId": HANDLE, "nickname": "Tutor", "signature": "Interrupt anytime"},
            "stats": {"followerCount": 12, "videoCount": 200},
        }}}})


@pytest.fixture
def spec():
    return ResearchSpec(name="Tutor research", competitors=[
        Competitor(brand="Tutor", handle=HANDLE, seed_videos=[URL])])


@pytest.fixture
def evidence(tmp_path, spec):
    snapshot = collect(spec, tmp_path, get=fake_get)
    return tmp_path, snapshot


def analysis_for(snapshot):
    return {"analyst": "offline test", "snapshot_sha256": digest(snapshot), "findings": [{
        "handle": HANDLE, "worldview": "A patient teacher is available",
        "audience_tension": "Stuck on a step", "product_role": "Patient tutor",
        "recurring_formats": ["Step-by-step caption"], "evidence_ids": [VID],
        "review_scope": "caption_and_metadata_only", "review_notes": "Read caption only",
        "limitations": "One caption; no video review", "adaptation": "Demonstrate one question",
    }], "ideas": [{"title": "Wait, why?", "hook": "Wait, why?", "story": "A learner asks why",
                   "product_proof": "Real whiteboard demonstration", "ai_ugc_execution":
                   "Dramatized student; real product capture", "evidence_ids": [VID],
                   "hypothesis": "Could make the teaching interaction clearer"}]}


def test_only_real_items_are_extracted():
    item = {"id": VID, "createTime": 12, "author": {}, "video": {}}
    assert video_items({"user": {"id": "user1"}, "posts": [item, item]}) == [item]


@pytest.mark.parametrize("url", ["http://www.tiktok.com/@x/video/12345",
                                 "https://evil.example/@x/video/12345",
                                 "https://www.tiktok.com.evil.example/@x/video/12345",
                                 "https://password@www.tiktok.com/@x/video/12345",
                                 "https://www.tiktok.com:8443/@x/video/12345"])
def test_unexpected_video_origins_rejected(url):
    with pytest.raises(ValueError):
        canonical_video(url)


def test_discovery_deduplicates_and_discards_session_payload(spec):
    account = discover(spec.competitors[0], get=fake_get)
    assert len(account["candidates"]) == 1
    assert account["profile"]["videoCount"] == 200
    assert account["coverage"].endswith("not_full_history")
    assert "private-session-marker" not in json.dumps(account)


def test_missing_json_is_a_gap_not_zero_metrics(spec):
    account = discover(spec.competitors[0].model_copy(update={"seed_videos": []}),
                       get=lambda _: (200, "<html>verify or login</html>"))
    assert account["status"] == "no_public_videos"
    assert "no_public_sample" in account["errors"][-1]
    result = detail({"url": URL, "caption": "sample", "views": 22}, "Tutor",
                    get=lambda _: (403, ""))["record"]
    assert result["status"] == "partial" and result["views"] == 22
    assert result["likes"] is None and result["published_at"] is None
    assert result["source"] == "tiktok_http_embed"


def test_null_site_data_leaves_a_gap_instead_of_crashing(spec):
    def changed_schema(url):
        if "/embed/" in url:
            return 200, page("__FRONTITY_CONNECT_STATE__", {"source": {"data": None}})
        return 200, page("__UNIVERSAL_DATA_FOR_REHYDRATION__", {
            "__DEFAULT_SCOPE__": {"webapp.user-detail": {"userInfo": None}}})
    competitor = spec.competitors[0].model_copy(update={"seed_videos": []})
    assert discover(competitor, get=changed_schema)["status"] == "no_public_videos"


def test_profile_identity_mismatch_stops_discovery(spec):
    def wrong_user(url):
        status, text = fake_get(url)
        return status, text.replace('"uniqueId": "tutor_example"', '"uniqueId": "someone_else"')
    account = discover(spec.competitors[0], get=wrong_user)
    assert account["status"] == "blocked" and not account["candidates"]


def test_wrong_author_detail_does_not_become_evidence():
    result = detail({"url": URL, "views": None}, "Tutor", get=lambda url: (
        200, fake_get(url)[1].replace('"uniqueId": "tutor_example"', '"uniqueId": "other"')))
    assert result["record"]["status"] == "partial"
    assert result["record"]["detail_error"] == "video_author_mismatch"


def test_run_resume_does_not_recrawl_and_manifest_cannot_change(evidence, spec):
    out, snapshot = evidence
    def forbidden(_):
        raise AssertionError("completed runs must resume without network")
    assert collect(spec, out, get=forbidden) == snapshot
    assert "private-session-marker" not in (out / "snapshot.json").read_text()
    with pytest.raises(ValueError, match="spec changed"):
        collect(spec.model_copy(update={"recent_days": 30}), out, get=forbidden)


def test_recent_and_old_posts_and_missing_views_are_not_mixed(evidence):
    _, snapshot = evidence
    recent = snapshot["videos"][0]
    old = {**recent, "id": "2", "views": 1000000,
           "published_at": (datetime.now(UTC) - timedelta(days=200)).isoformat()}
    missing = {**recent, "id": "3", "views": None}
    unknown = {**recent, "id": "4", "published_at": None}
    snapshot["videos"] += [old, missing, unknown]
    stats = cohort_stats(snapshot)[0]
    assert stats["recent"] == {"count": 2, "known_views": 1, "median_views": 100}
    assert stats["older"]["median_views"] == 1000000
    assert stats["unknown_date"]["count"] == 1


def test_report_no_invented_analysis_and_csv_formula_guard(evidence):
    out, _ = evidence
    result = report(out)
    assert result["analysis_ready"] is False
    assert "尚未生成世界观结论" in (out / "report.md").read_text()
    assert "'=Teach one step" in (out / "videos.csv").read_text()
    assert read(out / "analysis-input.json")["videos"][0]["likes"] == 0


def test_analysis_must_cite_this_snapshot_and_correct_account(evidence):
    out, snapshot = evidence
    analysis = analysis_for(snapshot)
    validate_analysis(snapshot, analysis)
    analysis["findings"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(ValueError, match="missing evidence"):
        validate_analysis(snapshot, analysis)
    analysis = analysis_for(snapshot)
    analysis["findings"][0]["handle"] = "other"
    with pytest.raises(ValueError, match="another account"):
        validate_analysis(snapshot, analysis)
    analysis = analysis_for(snapshot)
    analysis["snapshot_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="another snapshot"):
        validate_analysis(snapshot, analysis)
    save(out / "analysis.json", analysis_for(snapshot))
    assert report(out)["analysis_ready"]
    assert URL in (out / "report.md").read_text()


def test_unknown_official_status_not_promoted(tmp_path):
    spec = ResearchSpec(name="Unknown", competitors=[Competitor(brand="New product")])
    def forbidden(_):
        raise AssertionError("unknown handles cannot trigger a network call")
    snapshot = collect(spec, tmp_path, get=forbidden)
    assert snapshot["videos"] == []
    assert snapshot["accounts"][0]["status"] == "unconfigured"


def test_bad_seed_account_is_rejected_before_network(tmp_path):
    path = tmp_path / "spec.yaml"
    save(path, {"name": "bad", "competitors": [{"brand": "x", "handle": "other",
                                                "seed_videos": [URL]}]})
    with pytest.raises(ValueError, match="does not belong"):
        load_spec(path)


def test_cli_dry_run_has_no_network_credentials_or_output(tmp_path, spec, monkeypatch):
    path = tmp_path / "spec.json"
    save(path, spec.model_dump())
    out = tmp_path / "out"
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run must be offline")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("scout.config.load_credentials", forbidden)
    assert cli.main(["crawl", "--spec", str(path), "--out", str(out),
                     "--dry-run"]) == 0
    assert not out.exists()


class ResearchFeishu(MemoryFeishuClient):
    def __init__(self):
        super().__init__()
        self.created = []
        self.schemas = {}

    def _call(self, method, path, *, body=None, **kwargs):
        assert method == "POST"
        self.created.append(path)
        if path == BITABLE:
            return {"app": {"app_token": "testbase", "url": "https://example.feishu.cn/base/test"}}
        assert path == f"{BITABLE}/testbase/tables"
        tid = f"table{len(self.schemas) + 1}"
        self.schemas[tid] = deepcopy(body["table"]["fields"])
        return {"table_id": tid}

    def list_fields(self, table):
        return self.schemas[table.table_id]

    def list_records(self, table, **kwargs):
        records = super().list_records(table, **kwargs)
        # Feishu returns numeric strings in real API readbacks.
        for record in records:
            for key, value in record["fields"].items():
                if isinstance(value, (int, float)):
                    record["fields"][key] = str(float(value))
        return records


def prepare_delivery(evidence):
    out, snapshot = evidence
    save(out / "analysis.json", analysis_for(snapshot))
    report(out)
    return out


def test_feishu_preview_never_loads_credentials(evidence, monkeypatch):
    out = prepare_delivery(evidence)
    def forbidden():
        raise AssertionError("preview cannot load credentials")
    monkeypatch.setattr("scout.config.load_credentials", forbidden)
    assert deliver(out)["action"] == "preview_only"
    assert not (out / "feishu-delivery.json").exists()


def test_feishu_requires_analysis_and_resumes_without_duplicate_writes(evidence):
    out, _ = evidence
    report(out)
    with pytest.raises(ValueError, match="finish evidence-based"):
        deliver(out, write=True, client=ResearchFeishu())
    out = prepare_delivery(evidence)
    client = ResearchFeishu()
    receipt = deliver(out, write=True, client=client)
    assert receipt["verified"]
    assert len(client.created) == 5  # One new base and four research tables.
    assert sum(t["verified_rows"] for t in receipt["tables"].values()) == 4
    creates = [x for x in client.calls if x[0] == "create_records"]
    deliver(out, write=True, client=client)
    assert creates == [x for x in client.calls if x[0] == "create_records"]
    assert len(client.created) == 5


def test_feishu_hyperlink_target_corruption_fails_readback(evidence):
    out = prepare_delivery(evidence)
    client = ResearchFeishu()
    deliver(out, write=True, client=client)
    for records in client.tables.values():
        for record in records.values():
            if "原帖" in record["fields"]:
                record["fields"]["原帖"]["link"] = "https://www.tiktok.com/@wrong/video/12345"
    with pytest.raises(ValueError, match="field=原帖"):
        deliver(out, write=True, client=client)
    assert read(out / "feishu-delivery.json")["verified"] is False


def test_unknown_base_creation_is_not_blindly_retried(evidence):
    out = prepare_delivery(evidence)
    client = ResearchFeishu()
    def timeout(*args, **kwargs):
        raise TimeoutError("unknown result")
    client._call = timeout
    with pytest.raises(TimeoutError):
        deliver(out, write=True, client=client)
    with pytest.raises(ValueError, match="outcome unknown"):
        deliver(out, write=True, client=ResearchFeishu())


def test_partial_row_write_recovers_without_duplicate_create(evidence):
    out = prepare_delivery(evidence)
    client = ResearchFeishu()
    create = client.create_records
    def create_then_timeout(table, rows):
        create(table, rows)
        raise TimeoutError("reply lost")
    client.create_records = create_then_timeout
    with pytest.raises(TimeoutError):
        deliver(out, write=True, client=client)
    client.create_records = create
    assert deliver(out, write=True, client=client)["verified"]
    assert sum(call[1]["count"] for call in client.calls if call[0] == "create_records") == 4


def test_changed_preview_cannot_be_written(evidence):
    out = prepare_delivery(evidence)
    payload = read(out / "feishu-preview.json")
    payload["name"] = "silently changed"
    save(out / "feishu-preview.json", payload)
    with pytest.raises(ValueError, match="preview is stale"):
        deliver(out, write=True, client=ResearchFeishu())
