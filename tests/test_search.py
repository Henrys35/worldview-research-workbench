"""浏览器搜索路径：字段映射、拦截识别、与 viral 路径的产出一致性。"""
from scout.discover import aggregate
from scout.search import _blocked, normalize, video_items

# 字段名与嵌套关系对应 TikTok 接口；账号、文案、时间、时长和统计值均为设计的合成测试值。
ITEM = {
    "id": "7000000000000000002",
    "createTime": 1704067200,
    "desc": "합성 테스트용 학습 예시 #공부 #공부앱",
    "locationCreated": "KR",
    "video": {"duration": 23},
    "author": {"uniqueId": "synthetic_study_creator", "nickname": "가상 학습자"},
    "stats": {"playCount": 12000, "diggCount": 800, "commentCount": 12,
              "shareCount": 45, "collectCount": "67"},
    "challenges": [{"title": "공부"}, {"title": "공부앱"}],
}


def test_normalize_maps_every_field_we_filter_on():
    row = normalize(ITEM, "공부법")
    assert row["platform_video_id"] == "7000000000000000002"
    assert row["account_username"] == "synthetic_study_creator"
    assert row["account_display_name"] == "가상 학습자"
    assert row["region"] == "KR"                      # locationCreated 是发布地
    assert row["published_at"].startswith("2024-01-01")
    assert row["view_count"] == 12000
    assert row["bookmark_count"] == 67              # 源数据是字符串，要转成数字
    assert row["hashtags"] == ["공부", "공부앱"]
    assert row["_query"] == "공부법"


def test_normalize_falls_back_to_text_extra_for_hashtags():
    item = {**ITEM, "challenges": [], "textExtra": [{"hashtagName": "시험기간"}, {"other": 1}]}
    assert normalize(item, "q")["hashtags"] == ["시험기간"]


def test_normalize_drops_records_without_author_or_id():
    assert normalize({**ITEM, "author": {}}, "q") is None
    assert normalize({**ITEM, "id": None}, "q") is None


def test_normalize_survives_missing_stats():
    row = normalize({**ITEM, "stats": {}}, "q")
    assert row["view_count"] is None                  # 缺失记成 None，不补成 0


def test_blocked_recognizes_wall_in_every_ui_language():
    assert _blocked("Drag the puzzle piece into place") == "验证码"
    assert _blocked("로그인하여 인기 콘텐츠 검색") == "登录墙"      # 韩语界面
    assert _blocked("登录以搜索热门内容") == "登录墙"
    assert _blocked("Log in to search for top content") == "登录墙"
    assert _blocked("正常的搜索结果页面") is None


def test_video_items_only_picks_real_videos():
    payload = {"data": [{"item": ITEM}, {"item": {"id": "x", "createTime": 1}}],
               "noise": {"id": "y", "createTime": 2, "video": {}}}
    found = video_items(payload)
    assert [v["id"] for v in found] == ["7000000000000000002"]


def test_browser_rows_aggregate_same_as_viral_rows():
    """两条发现路径的产出必须能进同一个聚合，下游才通用。"""
    rows = [normalize(ITEM, "공부법"),
            normalize({**ITEM, "id": "999", "stats": {"playCount": 100}}, "공부법")]
    result = aggregate(rows, {"공부법": 2}, market="KR", cutoff="2020-01-01",
                       min_views=1000, query_info={}, since_days=3000)
    assert result["漏斗"]["播放>=1000"] == 1          # 100 播放的那条被门槛滤掉
    assert len(result["账号"]) == 1
    account = result["账号"][0]
    assert account["handle"] == "synthetic_study_creator"
    assert account["播放中位"] == 12000
    assert account["命中关键词"] == ["공부법"]
    assert account["样例"][0]["url"].endswith("/video/7000000000000000002")


def test_market_filter_uses_publish_location():
    rows = [normalize(ITEM, "q"), normalize({**ITEM, "id": "2", "locationCreated": "TH"}, "q")]
    result = aggregate(rows, {"q": 2}, market="KR", cutoff="2020-01-01", min_views=0,
                       query_info={}, since_days=3000)
    assert result["漏斗"]["地区=KR"] == 1


def test_blocked_recognizes_bare_login_button_page():
    """未登录时页面只剩导航条和一个登录按钮，也要算被拦，不能当成"抓到 0 条"。"""
    assert _blocked("综合 | 用户 | 视频 | 直播 | 照片 | 登录") == "登录墙"
    assert _blocked("Discover | Users | Videos | Log in") == "登录墙"


def test_blocked_does_not_misfire_on_real_results():
    """正常结果页里也会出现"登录"两个字，不能因此误判。"""
    page = "综合 用户 视频 登录 " + "합성 학습 자료 예시 12K 가상계정 " * 20
    assert _blocked(page) is None


def test_evidence_carries_publish_region():
    """发布地要一路带到 CSV：混进来的外国内容全靠它筛掉。"""
    import csv
    import io

    from scout.crawl import _csv_cell
    from scout.schemas import VideoEvidence

    record = VideoEvidence(id="1", brand="b", handle="h", url="https://x/1",
                           captured_at="2024-01-01T00:00:00Z", region="KR",
                           source="tiktok_http_detail", status="ok")
    assert record.region == "KR"
    assert VideoEvidence(id="2", brand="b", handle="h", url="https://x/2",
                         captured_at="2024-01-01T00:00:00Z",
                         source="tiktok_http_detail", status="ok").region == ""

    columns = ["id", "region", "views"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerow({k: _csv_cell(record.model_dump().get(k)) for k in columns})
    assert "region" in stream.getvalue().splitlines()[0]
