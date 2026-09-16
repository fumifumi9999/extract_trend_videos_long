"""メール通知の重複除外を確認するテスト。

DBに残すのは「メールで通知した動画」だけ。判定そのものは毎回APIの最新値で
やり直すので、未通知の動画を貯めておく必要はない。
"""
import sqlite3

import pytest

from long_video_outlier import ShortsOutlierExtractor
from video_store import VideoStore


# 「よい動画」の閾値は long_video_outlier 側の既定値に追従させる
THRESHOLD = ShortsOutlierExtractor(api_key="dummy", verbose=False).threshold


@pytest.fixture
def store(tmp_path):
    with VideoStore(tmp_path / "test.db", verbose=False) as s:
        yield s


def row(video_id, views, subscribers=1000, title="video"):
    """extractor.evaluate() が返す形の行を作る"""
    multiplier = views / max(subscribers, 100)
    return {
        "id": video_id,
        "channel_id": "UCdummy",
        "channel_url": "https://www.youtube.com/@example",
        "チャンネル": "https://www.youtube.com/@example",
        "動画名": title,
        "動画URL": f"https://www.youtube.com/watch?v={video_id}",
        "アップロード日": "2026-09-01 12:00",
        "視聴数": views,
        "登録者数": subscribers,
        "倍率(vs登録者数)": round(multiplier, 2),
        "outlier": "○" if multiplier >= THRESHOLD else "",
    }


# --- 記録するのは通知した動画だけ -------------------------------------------

def test_only_notified_videos_are_stored(store):
    rows = [row("good", 30_000), row("bad", 500)]
    store.mark_notified(store.unnotified(rows))

    assert store.counts() == (1, 1)
    stored = [r[0] for r in store.conn.execute("SELECT video_id FROM notified_videos")]
    assert stored == ["good"]


def test_nothing_is_stored_until_mark_notified(store):
    """dry_run のように mark_notified() を呼ばなければDBは空のまま"""
    rows = [row("A", 30_000)]
    assert len(store.unnotified(rows)) == 1
    assert store.counts() == (0, 0)

    # 次回もそのまま通知対象
    assert len(store.unnotified(rows)) == 1


def test_stored_row_keeps_the_numbers_at_notification_time(store):
    store.mark_notified([row("A", 30_000, subscribers=1000, title="よい動画")])

    stored = store.conn.execute(
        "SELECT title, views, subscribers, multiplier FROM notified_videos").fetchone()
    assert stored == ("よい動画", 30_000, 1000, 30.0)


# --- 重複除外 ---------------------------------------------------------------

def test_unnotified_returns_only_good_videos(store):
    assert [r["id"] for r in store.unnotified([row("good", 30_000), row("bad", 500)])] == ["good"]


def test_notified_video_is_not_reported_again(store):
    assert store.mark_notified(store.unnotified([row("A", 30_000)])) == 1

    # 視聴数が伸びても、一度通知した動画は再通知しない
    assert store.unnotified([row("A", 900_000)]) == []


def test_video_that_grows_past_the_threshold_is_reported_later(store):
    """投稿直後は基準未満 → DBには何も残らない → 伸びて基準に届いたら通知する"""
    assert store.unnotified([row("A", 500, subscribers=1000)]) == []      # 0.5倍
    assert store.counts() == (0, 0)

    new_rows = store.unnotified([row("A", 35_000, subscribers=1000)])     # 35倍
    assert [r["id"] for r in new_rows] == ["A"]
    assert store.mark_notified(new_rows) == 1


def test_mark_notified_is_idempotent(store):
    rows = [row("A", 30_000)]
    assert store.mark_notified(rows) == 1
    assert store.mark_notified(rows) == 0   # すでに記録済みなので増えない
    assert store.counts() == (1, 1)


def test_state_survives_reopening_the_database(tmp_path):
    db = tmp_path / "test.db"
    with VideoStore(db, verbose=False) as first:
        first.mark_notified(first.unnotified([row("A", 30_000)]))

    with VideoStore(db, verbose=False) as second:
        assert second.unnotified([row("A", 30_000)]) == []
        assert second.counts() == (1, 1)


# --- 旧スキーマからの移行 ---------------------------------------------------

def test_migration_keeps_notified_rows_and_drops_the_rest(tmp_path):
    """未通知も貯めていた頃のDBを開いたら、通知済みだけ引き継いで残りは捨てる"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE channels (channel_id TEXT PRIMARY KEY, subscribers INTEGER);
        CREATE TABLE videos (
            video_id TEXT PRIMARY KEY, channel_id TEXT, channel_url TEXT, title TEXT,
            published_at TEXT, views INTEGER, subscribers INTEGER, multiplier REAL,
            first_seen_at TEXT, updated_at TEXT, notified_at TEXT);
        INSERT INTO channels VALUES ('UCdummy', 1000);
        INSERT INTO videos VALUES
            ('done', 'UCdummy', 'u', '通知済み', '2026-09-01 12:00', 30000, 1000, 30.0,
             'x', 'x', '2026-09-01 13:00'),
            ('pending', 'UCdummy', 'u', '未通知', '2026-09-01 12:00', 500, 1000, 0.5,
             'x', 'x', NULL);
    """)
    conn.commit()
    conn.close()

    with VideoStore(db, verbose=False) as store:
        assert store.counts() == (1, 1)
        assert store.notified_ids(["done", "pending"]) == {"done"}
        tables = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "videos" not in tables and "channels" not in tables


# --- extractor が返す行をそのまま渡せること ---------------------------------

def test_accepts_rows_produced_by_the_extractor(monkeypatch, store):
    from datetime import datetime, timedelta, timezone

    extractor = ShortsOutlierExtractor(api_key="dummy", verbose=False)
    now = datetime.now(timezone.utc)
    videos = [
        {"id": "A", "title": "よい動画", "published_utc": now - timedelta(days=1), "views": 30_000},
        {"id": "B", "title": "基準未満", "published_utc": now - timedelta(days=2), "views": 3_000},
    ]
    monkeypatch.setattr(extractor, "resolve_channel_id", lambda url: "UCdummy")
    monkeypatch.setattr(extractor, "fetch_subscriber_count", lambda channel_id: 1000)
    monkeypatch.setattr(extractor, "fetch_videos",
                        lambda channel_id, max_items, cutoff=None: ["A", "B"])
    monkeypatch.setattr(extractor, "fetch_video_details", lambda video_ids: videos)

    rows = extractor.evaluate("https://www.youtube.com/@example")
    new_rows = store.unnotified(rows)
    assert [r["動画名"] for r in new_rows] == ["よい動画"]
    assert store.mark_notified(new_rows) == 1


def test_csv_does_not_contain_internal_keys(store, tmp_path):
    extractor = ShortsOutlierExtractor(api_key="dummy", verbose=False)
    path = tmp_path / "out.csv"
    extractor.to_csv([row("A", 30_000)], output=str(path))

    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("チャンネル,動画名")
    assert "channel_id" not in header and "id" not in header.split(",")
