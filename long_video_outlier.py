#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["python-dotenv>=1.0"]
# ///
"""
shorts_outlier.py
YouTubeチャンネルURLを指定して、チャンネル内のLong動画を走査し、
自前のoutlier判定(チャンネル通常パフォーマンス比の倍率)を行い、
直近N日(デフォルト7日)にアップロードされた動画をCSVで出力する。

必要なもの:
    - YouTube Data API v3 のAPIキー(.env の YOUTUBE_API_KEY に記載)
    - uv (推奨。依存は上のPEP 723メタデータから自動で解決される)
      https://docs.astral.sh/uv/  →  winget install astral-sh.uv

使い方:
    # 1. .env.example をコピーして .env を作り、APIキーを書く
    #    YOUTUBE_API_KEY=YOUR_API_KEY
    # 2. main() の中の設定を書き換えて実行する
    uv run shorts_outlier.py

    # プロジェクトの .venv を使いたい場合(IDE補完などのため)
    #   uv sync
    #   uv run python shorts_outlier.py

    # 他のスクリプトから使う場合
    #   from shorts_outlier import ShortsOutlierExtractor
    #   ShortsOutlierExtractor(days=7, video_type="long").run("https://www.youtube.com/@チャンネル名")
"""

import csv
import json
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

API_BASE = "https://www.googleapis.com/youtube/v3"
JST = timezone(timedelta(hours=9))
ENV_KEY = "YOUTUBE_API_KEY"
CSV_FIELDS = ["動画名", "動画URL", "アップロード日", "視聴数", "倍率(vs中央値)", "outlier"]
# 複数チャンネルをまとめて出力する場合の列(先頭にチャンネル列を追加)
CSV_FIELDS_MULTI = ["チャンネル"] + CSV_FIELDS


class ShortsOutlierError(Exception):
    """このスクリプト由来のエラー(APIキー未設定、チャンネル解決失敗など)"""


class ChannelNotFoundError(ShortsOutlierError):
    """チャンネルが存在しない/対象動画が無いなど、その1チャンネルだけの問題。

    複数チャンネルを一括処理するときは、この例外だけをスキップ対象にする
    (クォータ超過などの全体的な障害は握りつぶさず中断させるため)。
    """


class ShortsOutlierExtractor:
    """YouTubeチャンネルのShorts/Long動画からoutlier動画を抽出する。

    Attributes:
        days:      直近何日分を抽出対象にするか
        threshold: outlier判定の倍率閾値(視聴数 ÷ 中央値 がこれ以上でoutlier)
        baseline:  中央値算出に使う直近動画本数
        include_all: Trueならoutlier以外も出力(outlier列で判別可能)
        video_type: "long" または "short" を選択。デフォルトは long
        verbose:   進捗をprintするか
    """

    def __init__(self, api_key=None, days=7, threshold=3.0, baseline=30,
                 include_all=False, env_file=None, video_type="long",
                 min_views=100, verbose=True):
        self.api_key = api_key or self.load_api_key(env_file)
        if not self.api_key:
            raise ShortsOutlierError(
                f"APIキーが未設定です.\n"
                f"  .env.example を .env にコピーし、{ENV_KEY}=あなたのAPIキー を記入してください\n"
                f"  (ShortsOutlierExtractor(api_key=\"...\") で直接渡すこともできます)")
        self.video_type = self._normalize_video_type(video_type)
        self.days = days
        self.threshold = threshold
        self.baseline = baseline
        self.include_all = include_all
        self.min_views = min_views
        self.verbose = verbose
        self.skipped = []   # analyze_many() で除外したチャンネル [(URL, 理由), ...]

    @staticmethod
    def _normalize_video_type(video_type):
        if video_type is None:
            return "long"
        normalized = str(video_type).strip().lower()
        if normalized not in {"short", "long"}:
            raise ShortsOutlierError(
                f"video_type は 'short' または 'long' を指定してください: {video_type!r}")
        return normalized

    @property
    def video_label(self):
        return "Shorts" if self.video_type == "short" else "Long動画"

    def playlist_id_for(self, channel_id):
        """チャンネルIDから対象動画のプレイリストIDを返す。"""
        suffix = channel_id[2:]
        if self.video_type == "short":
            return "UUSH" + suffix
        return "UU" + suffix

    def video_url(self, video_id):
        if self.video_type == "short":
            return f"https://www.youtube.com/shorts/{video_id}"
        return f"https://www.youtube.com/watch?v={video_id}"

    # ------------------------------------------------------------------
    # セットアップ / 低レベルAPI
    # ------------------------------------------------------------------
    @staticmethod
    def load_api_key(env_file=None):
        """.env を読み込み、APIキーを返す(既存の環境変数は上書きしない)。

        探索順: env_file 指定 → スクリプトと同じディレクトリ → カレント以上の親階層。
        """
        if env_file:
            if not Path(env_file).is_file():
                raise ShortsOutlierError(f".envファイルが見つかりません: {env_file}")
            env_path = env_file
        else:
            script_env = Path(__file__).resolve().parent / ".env"
            env_path = str(script_env) if script_env.is_file() else find_dotenv(usecwd=True)

        if env_path:
            load_dotenv(env_path, encoding="utf-8")
        return os.environ.get(ENV_KEY)

    def _log(self, message):
        if self.verbose:
            print(message)

    def _api_get(self, endpoint, params):
        """YouTube Data API v3 にGETリクエストを送る"""
        params = dict(params)
        params["key"] = self.api_key
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(body)["error"]["message"]
            except Exception:
                msg = body[:300]
            # 404はそのチャンネル固有の問題(削除済み・対象プレイリスト無しなど)
            error = ChannelNotFoundError if e.code == 404 else ShortsOutlierError
            raise error(f"APIリクエスト失敗 ({endpoint}, HTTP {e.code}): {msg}") from e

    # ------------------------------------------------------------------
    # データ取得
    # ------------------------------------------------------------------
    def resolve_channel_id(self, channel_url):
        """チャンネルURL(@ハンドル / /channel/UC... / /user/... )からチャンネルIDを取得"""
        url = channel_url.strip()
        # /channel/UCxxxx 形式 → そのままIDを抜く
        m = re.search(r"/channel/(UC[\w-]+)", url)
        if m:
            return m.group(1)
        # UC... を直接渡された場合
        if re.fullmatch(r"UC[\w-]{20,}", url):
            return url
        # @ハンドル形式
        m = re.search(r"@([^/?\s]+)", url)
        if m:
            handle = urllib.parse.unquote(m.group(1))
            data = self._api_get("channels", {"part": "id", "forHandle": "@" + handle})
            items = data.get("items", [])
            if items:
                return items[0]["id"]
            raise ChannelNotFoundError(f"ハンドル @{handle} のチャンネルが見つかりません")
        # /user/xxx 形式(旧ユーザー名)
        m = re.search(r"/user/([^/?\s]+)", url)
        if m:
            data = self._api_get("channels", {"part": "id", "forUsername": m.group(1)})
            items = data.get("items", [])
            if items:
                return items[0]["id"]
        raise ChannelNotFoundError(
            f"チャンネルURLを解決できません: {channel_url}\n"
            "  https://www.youtube.com/@ハンドル名 または /channel/UC... 形式で指定してください")

    def fetch_shorts(self, channel_id, max_items):
        """Shorts専用プレイリスト(UC→UUSH)から直近の動画IDを取得"""
        shorts_playlist = self.playlist_id_for(channel_id)
        video_ids = []
        page_token = None
        while len(video_ids) < max_items:
            params = {
                "part": "contentDetails",
                "playlistId": shorts_playlist,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._api_get("playlistItems", params)
            for item in data.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return video_ids[:max_items]

    def fetch_uploads(self, channel_id, max_items):
        """通常のアップロード一覧(UC→UU)から直近の動画IDを取得。"""
        uploads_playlist = self.playlist_id_for(channel_id)
        video_ids = []
        page_token = None
        while len(video_ids) < max_items:
            params = {
                "part": "contentDetails",
                "playlistId": uploads_playlist,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._api_get("playlistItems", params)
            for item in data.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return video_ids[:max_items]

    def fetch_videos(self, channel_id, max_items):
        if self.video_type == "short":
            return self.fetch_shorts(channel_id, max_items)
        return self.fetch_uploads(channel_id, max_items)

    def fetch_video_details(self, video_ids):
        """videos.list で動画名・公開日時・視聴数を一括取得(50件ずつ)"""
        videos = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            data = self._api_get("videos", {
                "part": "snippet,statistics",
                "id": ",".join(batch),
            })
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                published = datetime.fromisoformat(
                    snippet["publishedAt"].replace("Z", "+00:00"))
                videos.append({
                    "id": item["id"],
                    "title": snippet.get("title", ""),
                    "published_utc": published,
                    "views": int(stats.get("viewCount", 0)),
                })
        return videos

    # ------------------------------------------------------------------
    # 分析 / 出力
    # ------------------------------------------------------------------
    def analyze(self, channel_url):
        """チャンネルを解析し、CSV列に対応するdictのリストを返す(CSVは書き出さない)"""
        self._log(f"チャンネルIDを解決中: {channel_url}")
        channel_id = self.resolve_channel_id(channel_url)
        self._log(f"  → {channel_id}")

        # 直近N日分 + 中央値算出用ベースラインを確保するため多めに取得
        fetch_count = max(self.baseline, 50)
        label = self.video_label
        self._log(f"{label}を取得中(直近 最大{fetch_count}本)...")
        video_ids = self.fetch_videos(channel_id, fetch_count)
        if not video_ids:
            raise ChannelNotFoundError(f"このチャンネルに{label}が見つかりません")
        self._log(f"  → {len(video_ids)}本の{label}を検出")

        videos = self.fetch_video_details(video_ids)
        videos.sort(key=lambda v: v["published_utc"], reverse=True)

        # ベースライン: 直近N本の視聴数の中央値(自前outlier判定の基準)
        baseline_videos = videos[:self.baseline]

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days)
        recent = [v for v in videos if v["published_utc"] >= cutoff]
        self._log(f"直近{self.days}日以内の{self.video_label}: {len(recent)}本")

        rows = []
        for v in recent:
            # 判定対象自身を除いた、正の視聴数のみを中央値の基準に使う。
            # すべて 0 / 基準が存在しない場合は倍率比較不能なので除外する。
            others = [b["views"] for b in baseline_videos if b["id"] != v["id"] and b["views"] > 0]
            if not others:
                continue
            median_views = statistics.median(others)
            if median_views <= 0:
                continue
            if v["views"] < self.min_views:
                continue
            multiplier = v["views"] / median_views
            is_outlier = multiplier >= self.threshold
            if is_outlier or self.include_all:
                rows.append({
                    "動画名": v["title"],
                    "動画URL": self.video_url(v["id"]),
                    "アップロード日": v["published_utc"].astimezone(JST).strftime("%Y-%m-%d %H:%M"),
                    "視聴数": v["views"],
                    "倍率(vs中央値)": round(multiplier, 2),
                    "outlier": "○" if is_outlier else "",
                })
        return rows

    def analyze_many(self, channel_urls, skip_missing=True):
        """複数チャンネルをまとめて解析し、「チャンネル」列付きの行リストを返す。

        skip_missing=True なら、存在しないチャンネル(404)や対象動画が無いチャンネルは
        警告を出して出力から除外する。クォータ超過などの全体的な障害は中断させる。
        """
        channel_urls = list(channel_urls)
        rows, skipped = [], []
        for i, url in enumerate(channel_urls, 1):
            self._log(f"\n[{i}/{len(channel_urls)}] {url}")
            try:
                for row in self.analyze(url):
                    rows.append({"チャンネル": url, **row})
            except ChannelNotFoundError as e:
                if not skip_missing:
                    raise
                skipped.append((url, str(e).splitlines()[0]))
                self._log(f"  → スキップ: {str(e).splitlines()[0]}")

        # verbose=False でも後から確認できるように保持しておく
        self.skipped = skipped
        if skipped:
            self._log(f"\n除外した{len(skipped)}件(出力には含まれません):")
            for url, reason in skipped:
                self._log(f"  - {url}  ({reason})")
        return rows

    def to_csv(self, rows, output=None, fields=None):
        """analyze() / analyze_many() の結果をCSVに書き出し、出力先パスを返す"""
        prefix = "long_videos" if self.video_type == "long" else "shorts_outliers"
        output = output or f"{prefix}_{datetime.now(JST).strftime('%Y%m%d')}.csv"
        if fields is None:
            fields = CSV_FIELDS_MULTI if any("チャンネル" in r for r in rows) else CSV_FIELDS
        with open(output, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return output

    def run(self, channel_url, output=None):
        """解析からCSV出力までを一括実行し、出力先パスを返す"""
        rows = self.analyze(channel_url)
        return self._finish(rows, output)

    def run_many(self, channel_urls, output=None, skip_missing=True):
        """複数チャンネルを解析して1つのCSVにまとめ、出力先パスを返す"""
        rows = self.analyze_many(channel_urls, skip_missing=skip_missing)
        return self._finish(rows, output)

    def _finish(self, rows, output=None):
        output = self.to_csv(rows, output)
        outlier_count = sum(1 for r in rows if r["outlier"])
        self._log(f"\n完了: {output} に {len(rows)}行を出力(うちoutlier {outlier_count}本)")
        self._log(f"  outlier基準: 直近{self.baseline}本の視聴数中央値の {self.threshold}倍以上")
        return output


def main():
    """使い方の例。ここを書き換えて `uv run shorts_outlier.py` で実行する。"""

    # === 設定(すべてデフォルト値を明記。ここの値を書き換えて使う) =======
    # 対象チャンネル(@ハンドル / /channel/UC... / /user/... のURLに対応)
    channel_url = "https://www.youtube.com/@%E7%B5%B5%E4%B8%80%E6%9E%9A-y5e/shorts"

    # 出力CSVパス。None なら shorts_outliers_YYYYMMDD.csv に自動命名
    output = None

    extractor = ShortsOutlierExtractor(
        api_key=None,       # APIキー。None なら .env の YOUTUBE_API_KEY を使う
        days=3,             # 直近何日分を抽出対象にするか
        threshold=3.0,      # 視聴数 ÷ 中央値 がこの倍率以上でoutlier判定
        baseline=30,        # 中央値の算出に使う直近Shorts本数
        include_all=False,  # True ならoutlier以外も出力(outlier列で判別)
        env_file=None,      # .envのパス。None ならスクリプト位置→カレントの順に自動探索
        verbose=True,       # False にすると進捗表示を止める
    )
    extractor.run(channel_url, output=output)

    # === CSVにせずPython側で結果を扱う例 ===============================
    # rows = ShortsOutlierExtractor(include_all=True).analyze(channel_url)
    # for r in sorted(rows, key=lambda r: r["視聴数"], reverse=True)[:5]:
    #     print(r["視聴数"], r["倍率(vs中央値)"], r["動画名"])

    # === 複数チャンネルを1つのCSVにまとめる例 ===========================
    # 存在しないチャンネル(404)やShortsが無いチャンネルは自動で除外される
    # extractor.run_many([
    #     "https://www.youtube.com/@channel_a",
    #     "https://www.youtube.com/@channel_b",
    # ], output="まとめ.csv")
    # print("除外:", extractor.skipped)   # [(URL, 理由), ...]

    # === スプレッドシートのチャンネル一覧をそのまま処理する例 ===========
    # from sheet_reader import SheetReader
    # urls = SheetReader(verbose=False).read_values()   # ①YouTubeベンチマーク_雑学 のH6以降
    # extractor.run_many(urls)

    # === APIキーを直接渡す例(.env を使わない場合) =====================
    # extractor = ShortsOutlierExtractor(api_key="AIza...")


if __name__ == "__main__":
    try:
        main()
    except ShortsOutlierError as e:
        raise SystemExit(f"[エラー] {e}")
