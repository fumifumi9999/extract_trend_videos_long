from datetime import datetime, timedelta, timezone

from long_video_outlier import ShortsOutlierExtractor


def test_video_type_default_is_long():
    extractor = ShortsOutlierExtractor(api_key="dummy", verbose=False)
    assert extractor.video_type == "long"


def test_playlist_id_for_long_video_uses_uploads_playlist():
    extractor = ShortsOutlierExtractor(api_key="dummy", video_type="long", verbose=False)
    assert extractor.playlist_id_for("UC12345678901234567890") == "UU12345678901234567890"


def test_video_url_for_long_video_uses_watch_url():
    extractor = ShortsOutlierExtractor(api_key="dummy", video_type="long", verbose=False)
    assert extractor.video_url("abc123xyz") == "https://www.youtube.com/watch?v=abc123xyz"


def test_zero_median_baseline_does_not_get_flagged(monkeypatch):
    extractor = ShortsOutlierExtractor(api_key="dummy", days=7, threshold=3.0, baseline=2, include_all=False, min_views=100, verbose=False)

    now = datetime.now(timezone.utc)
    videos = [
        {"id": "A", "title": "zero1", "published_utc": now - timedelta(days=1), "views": 0},
        {"id": "B", "title": "zero2", "published_utc": now - timedelta(days=2), "views": 0},
    ]

    monkeypatch.setattr(extractor, "resolve_channel_id", lambda url: "UCdummy")
    monkeypatch.setattr(extractor, "fetch_videos", lambda channel_id, max_items: ["A", "B"])
    monkeypatch.setattr(extractor, "fetch_video_details", lambda video_ids: videos)

    rows = extractor.analyze("https://www.youtube.com/@example")
    assert rows == []


def test_min_views_filters_small_noise(monkeypatch):
    extractor = ShortsOutlierExtractor(api_key="dummy", days=7, threshold=3.0, baseline=2, include_all=False, min_views=100, verbose=False)

    now = datetime.now(timezone.utc)
    videos = [
        {"id": "A", "title": "base1", "published_utc": now - timedelta(days=1), "views": 2},
        {"id": "B", "title": "base2", "published_utc": now - timedelta(days=2), "views": 2},
        {"id": "C", "title": "noise", "published_utc": now - timedelta(days=3), "views": 8},
    ]

    monkeypatch.setattr(extractor, "resolve_channel_id", lambda url: "UCdummy")
    monkeypatch.setattr(extractor, "fetch_videos", lambda channel_id, max_items: ["A", "B", "C"])
    monkeypatch.setattr(extractor, "fetch_video_details", lambda video_ids: videos)

    rows = extractor.analyze("https://www.youtube.com/@example")
    assert rows == []
