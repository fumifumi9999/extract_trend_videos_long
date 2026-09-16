#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
video_store.py
メールで通知した動画だけをSQLiteに記録し、同じ動画を二度通知しないようにする。

    store = VideoStore()
    new_rows = store.unnotified(rows)   # 1. まだ通知していないよい動画だけに絞る
    ...メール送信...
    store.mark_notified(new_rows)       # 2. 送信できたものをDBに記録する

「投稿直後は基準未満だったが、あとから視聴数が伸びて基準に届いた動画」も
ちゃんと通知される。判定は毎回 YouTube API から取り直した最新の視聴数で
やり直しているので、DBは「もう通知したかどうか」だけを覚えていればよい。
そのため未通知の動画はDBに残さない(記録は通知した本数までしか増えない)。

DBファイルはデフォルトでこのスクリプトと同じディレクトリの trend_videos.db。
中身を確認するには:  uv run video_store.py
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DEFAULT_DB = "trend_videos.db"

# SQLiteの変数上限(SQLITE_MAX_VARIABLE_NUMBER)に触れないための IN 句の分割サイズ
_CHUNK = 900

COLUMNS = ("video_id", "channel_id", "channel_url", "title", "published_at",
           "views", "subscribers", "multiplier", "notified_at")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS notified_videos (
    video_id     TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    channel_url  TEXT NOT NULL,
    title        TEXT NOT NULL,
    published_at TEXT NOT NULL,
    views        INTEGER NOT NULL,   -- 通知した時点の視聴数
    subscribers  INTEGER NOT NULL,   -- 通知した時点の登録者数
    multiplier   REAL NOT NULL,      -- 通知した時点の倍率
    notified_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notified_channel ON notified_videos(channel_id);
"""


def _now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _chunks(items, size=_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class VideoStore:
    """メール通知した動画を覚えておくSQLite。

    Attributes:
        db_path: DBファイルのパス
        verbose: 進捗をprintするか
    """

    def __init__(self, db_path=None, verbose=True):
        self.db_path = str(db_path or Path(__file__).resolve().parent / DEFAULT_DB)
        self.verbose = verbose
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self.conn.close()

    def _log(self, message):
        if self.verbose:
            print(message)

    def _migrate(self):
        """旧スキーマ(未通知も含めて全動画を貯めていた頃)から通知済みだけ引き継ぐ"""
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "videos" not in tables:
            return
        with self.conn:
            self.conn.execute(
                f"INSERT OR IGNORE INTO notified_videos ({', '.join(COLUMNS)})"
                f" SELECT {', '.join(COLUMNS)} FROM videos WHERE notified_at IS NOT NULL")
            self.conn.execute("DROP TABLE videos")
            self.conn.execute("DROP TABLE IF EXISTS channels")
        self._log("旧スキーマから通知済みの動画を引き継ぎ、未通知の記録は削除しました")

    # ------------------------------------------------------------------
    # 抽出 / 記録
    # ------------------------------------------------------------------
    def unnotified(self, rows):
        """よい動画(outlier)のうち、まだメール通知していないものだけを返す"""
        good = [r for r in rows if r["outlier"]]
        if not good:
            return []
        notified = self.notified_ids([r["id"] for r in good])
        new_rows = [r for r in good if r["id"] not in notified]
        self._log(f"よい動画 {len(good)}本 → 未通知 {len(new_rows)}本"
                  f"(通知済みのため除外 {len(notified)}本) [{self.db_path}]")
        return new_rows

    def mark_notified(self, rows):
        """メール送信できた動画をDBに記録し、実際に追加した件数を返す"""
        if not rows:
            return 0
        now = _now()
        before = self.conn.total_changes
        with self.conn:   # ブロックを抜けるときにcommit(例外時はrollback)
            self.conn.executemany(
                f"INSERT OR IGNORE INTO notified_videos ({', '.join(COLUMNS)})"
                f" VALUES ({', '.join('?' * len(COLUMNS))})",
                [(r["id"], r.get("channel_id", ""), r.get("channel_url", ""),
                  r["動画名"], r["アップロード日"], r["視聴数"], r["登録者数"],
                  r["倍率(vs登録者数)"], now) for r in rows])
        return self.conn.total_changes - before

    def notified_ids(self, video_ids):
        """渡した動画IDのうち、通知済みのものの集合を返す(IN句は分割して発行する)"""
        found = set()
        for chunk in _chunks(list(video_ids)):
            placeholders = ",".join("?" * len(chunk))
            found.update(row[0] for row in self.conn.execute(
                f"SELECT video_id FROM notified_videos"
                f" WHERE video_id IN ({placeholders})", chunk))
        return found

    def counts(self):
        """(通知済み動画数, 通知実績のあるチャンネル数)"""
        return self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT channel_id) FROM notified_videos").fetchone()


def main():
    """DBの中身をざっと確認する。 uv run video_store.py"""
    store = VideoStore(verbose=False)
    videos, channels = store.counts()
    print(f"DB: {store.db_path}")
    print(f"  通知済みの動画: {videos}本 / {channels}チャンネル")

    print("\n直近に通知した動画 10件:")
    for title, views, subs, mult, notified_at in store.conn.execute(
            "SELECT title, views, subscribers, multiplier, notified_at"
            " FROM notified_videos ORDER BY notified_at DESC LIMIT 10"):
        print(f"  {notified_at}  {mult:>8.2f}倍  {views:>9,}回 / 登録{subs:>8,}人  "
              f"{title[:40]}")
    store.close()


if __name__ == "__main__":
    main()
