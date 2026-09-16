"""「よい動画」の判定ルールを確認するテスト。

    ルール: 直近180日以内 かつ 視聴数 >= max(登録者数, 100) × 10
"""
from datetime import datetime, timedelta, timezone

import pytest

from long_video_outlier import ShortsOutlierExtractor, SubscriberCountUnavailableError


def make_extractor(**overrides):
    """既定値どおりの抽出器(テストで値を変えたいものだけ上書きする)"""
    return ShortsOutlierExtractor(api_key="dummy", verbose=False, **overrides)


def stub_channel(monkeypatch, extractor, subscribers, videos):
    monkeypatch.setattr(extractor, "resolve_channel_id", lambda url: "UCdummy")
    monkeypatch.setattr(extractor, "fetch_subscriber_count", lambda channel_id: subscribers)
    monkeypatch.setattr(extractor, "fetch_videos",
                        lambda channel_id, max_items, cutoff=None: [v["id"] for v in videos])
    monkeypatch.setattr(extractor, "fetch_video_details", lambda video_ids: videos)


def video(video_id, views, days_ago, title="video"):
    return {
        "id": video_id,
        "title": title,
        "published_utc": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "views": views,
    }


def analyze(monkeypatch, subscribers, videos, **overrides):
    extractor = make_extractor(**overrides)
    stub_channel(monkeypatch, extractor, subscribers, videos)
    return extractor.analyze("https://www.youtube.com/@example")


# --- 既定値 -----------------------------------------------------------------

def test_defaults():
    extractor = make_extractor()
    assert extractor.video_type == "long"
    assert (extractor.days, extractor.threshold, extractor.subscriber_floor) == (180, 10.0, 100)


def test_playlist_id_for_long_video_uses_uploads_playlist():
    extractor = make_extractor(video_type="long")
    assert extractor.playlist_id_for("UC12345678901234567890") == "UU12345678901234567890"


def test_video_url_for_long_video_uses_watch_url():
    extractor = make_extractor(video_type="long")
    assert extractor.video_url("abc123xyz") == "https://www.youtube.com/watch?v=abc123xyz"


# --- 登録者数が下限以上のチャンネル: そのまま「登録者数の10倍」 -------------

def test_views_at_ten_times_subscribers_is_a_good_video(monkeypatch):
    rows = analyze(monkeypatch, 1000, [video("A", 10_000, 10, "ちょうど10倍")])
    assert len(rows) == 1
    assert rows[0]["登録者数"] == 1000
    assert rows[0]["倍率(vs登録者数)"] == 10.0
    assert rows[0]["outlier"] == "○"


def test_views_below_ten_times_subscribers_is_excluded(monkeypatch):
    """3倍では足りない: 閾値を上げたことがここに出る"""
    assert analyze(monkeypatch, 1000, [video("A", 9_999, 10)]) == []
    assert analyze(monkeypatch, 1000, [video("B", 3_000, 10)]) == []


# --- 登録者数が下限未満のチャンネル: 分母が100で下支えされ「1,000回以上」 ---

def test_small_channel_with_huge_view_count_is_kept(monkeypatch):
    """登録者99人でも50万回再生なら拾う(チャンネルごと除外しない)"""
    rows = analyze(monkeypatch, 99, [video("A", 500_000, 10, "小規模の大バズ")])
    assert len(rows) == 1
    assert rows[0]["登録者数"] == 99
    # 分母は登録者数99ではなく下限の100を使う
    assert rows[0]["倍率(vs登録者数)"] == 5000.0
    assert rows[0]["outlier"] == "○"


def test_small_channel_needs_one_thousand_views(monkeypatch):
    rows = analyze(monkeypatch, 5, [
        video("ok", 1000, 10, "ちょうど1000回"),
        video("ng", 999, 10, "1回足りない"),
    ])
    assert [r["動画名"] for r in rows] == ["ちょうど1000回"]


def test_zero_subscriber_channel_uses_the_floor(monkeypatch):
    """登録者0人でもゼロ除算にならず、1,000回以上なら拾う"""
    rows = analyze(monkeypatch, 0, [video("A", 9_000, 10)])
    assert [r["倍率(vs登録者数)"] for r in rows] == [90.0]


# --- 期間 -------------------------------------------------------------------

def test_videos_older_than_half_a_year_are_excluded(monkeypatch):
    rows = analyze(monkeypatch, 1000, [
        video("recent", 50_000, 179, "半年ぎりぎり"),
        video("old", 900_000, 181, "半年より前"),
    ])
    assert [r["動画名"] for r in rows] == ["半年ぎりぎり"]


# --- その他 -----------------------------------------------------------------

def test_hidden_subscriber_count_is_skipped(monkeypatch):
    """登録者数が非公開だと分母が作れないので、そのチャンネルはスキップする"""
    extractor = make_extractor()
    monkeypatch.setattr(extractor, "resolve_channel_id", lambda url: "UCdummy")
    monkeypatch.setattr(
        extractor, "_api_get",
        lambda endpoint, params: {"items": [{"statistics": {"hiddenSubscriberCount": True}}]})

    with pytest.raises(SubscriberCountUnavailableError):
        extractor.analyze("https://www.youtube.com/@example")


def test_include_all_keeps_videos_below_threshold(monkeypatch):
    rows = analyze(monkeypatch, 1000, [video("A", 5_000, 10)], include_all=True)
    assert len(rows) == 1
    assert rows[0]["倍率(vs登録者数)"] == 5.0
    assert rows[0]["outlier"] == ""


def test_rule_text_states_the_floor_in_view_counts():
    assert "1,000回以上" in make_extractor().rule


def test_threshold_is_configurable(monkeypatch):
    """恒久的な既定は10倍。必要なら呼び出し側で変えられる"""
    rows = analyze(monkeypatch, 1000, [video("A", 3_000, 10)], threshold=3.0)
    assert [r["outlier"] for r in rows] == ["○"]


# --- 名前解決の順序 ---------------------------------------------------------

def test_prefer_ipv4_puts_ipv4_first_without_dropping_ipv6(monkeypatch):
    import socket
    from long_video_outlier import prefer_ipv4

    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
    fake = lambda *a, **k: [v6, v6, v4]   # OSはIPv6を先に返す
    monkeypatch.setattr(socket, "getaddrinfo", fake)

    prefer_ipv4()
    results = socket.getaddrinfo("example.com", 443)
    assert [r[0] for r in results] == [socket.AF_INET, socket.AF_INET6, socket.AF_INET6]

    # 2回呼んでも二重に巻かれない
    wrapped = socket.getaddrinfo
    prefer_ipv4()
    assert socket.getaddrinfo is wrapped
