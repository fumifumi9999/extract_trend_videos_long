#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["python-dotenv>=1.0"]
# ///
"""
shorts_outlier.py
YouTubeチャンネルURLを指定して、チャンネル内のLong動画を走査し、
「よい動画」判定(視聴数がチャンネル登録者数の何倍か)を行い、
直近N日(デフォルト180日=半年)にアップロードされた動画をCSVで出力する。

よい動画の定義:
    直近180日(半年)以内に公開され、かつ

        視聴数 >= max(登録者数, 100) x 10

    を満たす動画。登録者数が100人以上ならふつうに「登録者数の10倍」、
    100人未満のチャンネルは分母が100で下支えされるので「1,000回以上」となる。
    (180=days, 100=subscriber_floor, 10=threshold)

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
    #   ShortsOutlierExtractor(days=180, video_type="long").run("https://www.youtube.com/@チャンネル名")
"""

import csv
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

API_BASE = "https://www.googleapis.com/youtube/v3"
JST = timezone(timedelta(hours=9))
ENV_KEY = "YOUTUBE_API_KEY"
# CSVに出しうる列を出力順に並べたもの。
# 「チャンネル」は複数チャンネルをまとめたときにだけ付くので、
# 実際にrowsに存在する列だけが出力される。
CSV_FIELDS = ["チャンネル", "動画名", "動画URL", "アップロード日", "視聴数", "登録者数",
              "倍率(vs登録者数)", "outlier"]
OPTIONAL_CSV_FIELDS = {"チャンネル"}


def prefer_ipv4():
    """名前解決の結果をIPv4優先に並べ替える(このプロセス全体に効く)。

    IPv6アドレスが割り当てられているのにIPv6で外に出られない回線だと、Pythonは
    「IPv6アドレス(8個ほど)を順に試して全部失敗 → ようやくIPv4」と動くため、
    APIリクエスト1回ごとに数十秒〜数分待たされ、数千回のリクエストが終わらなくなる
    (2026-09-14 から実際にタスクが1時間の制限で毎日強制終了されていた)。

    IPv6を捨てるのではなく順番を後ろに回すだけなので、IPv4が使えない環境でも壊れない。
    urllib / smtplib / requests(gspread) はどれも socket.getaddrinfo を使うので、
    これ1つで YouTube API・Gmail・スプレッドシートの全通信に効く。
    """
    if getattr(socket.getaddrinfo, "_prefer_ipv4", False):
        return
    original = socket.getaddrinfo

    def ipv4_first(*args, **kwargs):
        results = original(*args, **kwargs)
        # sorted は安定ソートなので、同じファミリ内の順序はOSが返したまま
        return sorted(results, key=lambda r: r[0] != socket.AF_INET)

    ipv4_first._prefer_ipv4 = True
    ipv4_first._original = original
    socket.getaddrinfo = ipv4_first


class ShortsOutlierError(Exception):
    """このスクリプト由来のエラー(APIキー未設定、チャンネル解決失敗など)"""


class ChannelSkipError(ShortsOutlierError):
    """そのチャンネル1件だけをスキップすれば済む問題。

    複数チャンネルを一括処理するときは、この例外だけをスキップ対象にする
    (クォータ超過などの全体的な障害は握りつぶさず中断させるため)。
    """


class ChannelNotFoundError(ChannelSkipError):
    """チャンネルが存在しない/対象動画が無い。"""


class SubscriberCountUnavailableError(ChannelSkipError):
    """登録者数が非公開などで取得できず、倍率を計算できない。"""


class ShortsOutlierExtractor:
    """YouTubeチャンネルのShorts/Long動画からoutlier動画を抽出する。

    Attributes:
        days:      直近何日分を抽出対象にするか(デフォルト180日=半年)
        threshold: よい動画と判定する倍率(視聴数 ÷ 分母 がこれ以上)
        subscriber_floor: 倍率の分母の下限。登録者数がこれ未満のチャンネルは
                   この人数として扱う(小規模チャンネルを切り捨てず、かつ
                   分母が小さすぎて倍率が暴走するのを防ぐため)
        max_videos:  1チャンネルあたり最大何本まで遡って取得するか
        include_all: Trueなら基準未満の動画も出力(outlier列で判別可能)
        video_type:  "long" または "short" を選択。デフォルトは long
        verbose:     進捗をprintするか
    """

    def __init__(self, api_key=None, days=180, threshold=10.0, subscriber_floor=100,
                 include_all=False, env_file=None, video_type="long",
                 max_videos=300, verbose=True):
        self.api_key = api_key or self.load_api_key(env_file)
        if not self.api_key:
            raise ShortsOutlierError(
                f"APIキーが未設定です.\n"
                f"  .env.example を .env にコピーし、{ENV_KEY}=あなたのAPIキー を記入してください\n"
                f"  (ShortsOutlierExtractor(api_key=\"...\") で直接渡すこともできます)")
        self.video_type = self._normalize_video_type(video_type)
        self.days = days
        self.threshold = threshold
        self.subscriber_floor = subscriber_floor
        self.include_all = include_all
        self.max_videos = max_videos
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

    @property
    def rule(self):
        """判定ルールを1行で説明する文字列(ログ・メール本文で使い回す)"""
        return (f"視聴数 >= max(登録者数, {self.subscriber_floor:,}) × {self.threshold}"
                f"(登録者{self.subscriber_floor:,}人未満のチャンネルは "
                f"{int(self.subscriber_floor * self.threshold):,}回以上)")

    def divisor_for(self, subscribers):
        """倍率の分母。登録者数が少なすぎるチャンネルは subscriber_floor で下支えする"""
        return max(subscribers, self.subscriber_floor)

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

    def fetch_subscriber_count(self, channel_id):
        """チャンネルの登録者数を返す。

        YouTube Data APIの subscriberCount は上位3桁に丸められた概数
        (例: 1,234人 → 1,230)。判定基準としてはこの精度で十分だが、
        出力される数値は正確な実数ではない点に注意。
        """
        data = self._api_get("channels", {"part": "statistics", "id": channel_id})
        items = data.get("items", [])
        if not items:
            raise ChannelNotFoundError(f"チャンネルが見つかりません: {channel_id}")
        stats = items[0].get("statistics", {})
        # 非公開だと分母が作れない。0人扱いにすると全動画がよい動画になってしまうので、
        # 誤検知させるよりスキップ一覧に理由付きで載せる。
        if stats.get("hiddenSubscriberCount"):
            raise SubscriberCountUnavailableError("登録者数が非公開のため判定できません")
        try:
            return int(stats["subscriberCount"])
        except (KeyError, TypeError, ValueError):
            raise SubscriberCountUnavailableError("登録者数を取得できません") from None

    def fetch_videos(self, channel_id, max_items, cutoff=None):
        """対象プレイリスト(Long: UU / Shorts: UUSH)から直近の動画IDを取得する。

        プレイリストは新しい順に並ぶので、cutoff より古い動画に到達した時点で
        ページングを打ち切る(半年分を遡ってもAPIクォータを無駄にしないため)。
        """
        playlist_id = self.playlist_id_for(channel_id)
        video_ids = []
        page_token = None
        reached_cutoff = False
        while len(video_ids) < max_items and not reached_cutoff:
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._api_get("playlistItems", params)
            for item in data.get("items", []):
                details = item["contentDetails"]
                published = details.get("videoPublishedAt")
                # 公開日が取れない項目(非公開・削除済みなど)は打ち切り判断に使わない
                if cutoff and published and self._parse_time(published) < cutoff:
                    reached_cutoff = True
                    break
                video_ids.append(details["videoId"])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return video_ids[:max_items]

    @staticmethod
    def _parse_time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

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
                published = self._parse_time(snippet["publishedAt"])
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
    def evaluate(self, channel_url):
        """直近N日の動画を「すべて」判定結果つきで返す(CSVは書き出さない)。

        outlier列が "○" の行がよい動画。基準未満の行も返すのは、視聴数の推移を
        記録しておき、あとから伸びて基準に届いた動画を拾えるようにするため
        (video_store.VideoStore を参照)。
        """
        self._log(f"チャンネルIDを解決中: {channel_url}")
        channel_id = self.resolve_channel_id(channel_url)
        self._log(f"  → {channel_id}")

        # 判定の分母は登録者数。ただし小規模チャンネルは切り捨てず、下限で下支えする。
        subscribers = self.fetch_subscriber_count(channel_id)
        divisor = self.divisor_for(subscribers)
        self._log(f"  登録者数: {subscribers:,}人"
                  + (f" → 分母は下限の{divisor:,}を使用" if divisor != subscribers else ""))

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days)
        label = self.video_label
        self._log(f"{label}を取得中(直近{self.days}日 / 最大{self.max_videos}本)...")
        video_ids = self.fetch_videos(channel_id, self.max_videos, cutoff=cutoff)
        if not video_ids:
            raise ChannelNotFoundError(
                f"このチャンネルに直近{self.days}日の{label}が見つかりません")
        self._log(f"  → {len(video_ids)}本の{label}を検出")

        videos = self.fetch_video_details(video_ids)
        # プレイリストの並び順は信用しきらず、取得した公開日で改めて期間を絞る
        recent = [v for v in videos if v["published_utc"] >= cutoff]
        recent.sort(key=lambda v: v["published_utc"], reverse=True)
        self._log(f"直近{self.days}日以内の{label}: {len(recent)}本")

        rows = []
        for v in recent:
            multiplier = v["views"] / divisor
            rows.append({
                # 先頭3つはCSVには出さない内部用のキー(DBの主キーなどに使う)
                "id": v["id"],
                "channel_id": channel_id,
                "channel_url": channel_url,
                "動画名": v["title"],
                "動画URL": self.video_url(v["id"]),
                "アップロード日": v["published_utc"].astimezone(JST).strftime("%Y-%m-%d %H:%M"),
                "視聴数": v["views"],
                "登録者数": subscribers,
                "倍率(vs登録者数)": round(multiplier, 2),
                "outlier": "○" if multiplier >= self.threshold else "",
            })
        return rows

    def analyze(self, channel_url):
        """evaluate() のうち、よい動画だけを返す(include_all=Trueなら全件)"""
        rows = self.evaluate(channel_url)
        return rows if self.include_all else [r for r in rows if r["outlier"]]

    def analyze_many(self, channel_urls, skip_missing=True):
        """複数チャンネルのよい動画を、「チャンネル」列付きの行リストで返す"""
        return self._for_each(channel_urls, self.analyze, skip_missing)

    def evaluate_many(self, channel_urls, skip_missing=True):
        """複数チャンネルの直近N日の動画を「すべて」判定結果つきで返す"""
        return self._for_each(channel_urls, self.evaluate, skip_missing)

    def _for_each(self, channel_urls, analyze_one, skip_missing=True):
        """各チャンネルに analyze_one を適用して結果を連結する。

        skip_missing=True なら、存在しないチャンネル(404)・対象動画が無いチャンネル・
        登録者数が非公開で判定できないチャンネルは警告を出して出力から除外する。
        クォータ超過などの全体的な障害は中断させる。
        """
        channel_urls = list(channel_urls)
        rows, skipped = [], []
        for i, url in enumerate(channel_urls, 1):
            self._log(f"\n[{i}/{len(channel_urls)}] {url}")
            try:
                for row in analyze_one(url):
                    rows.append({"チャンネル": url, **row})
            except ChannelSkipError as e:
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
        """analyze() / analyze_many() の結果をCSVに書き出し、出力先パスを返す。

        列は CSV_FIELDS の順。「チャンネル」は rows に含まれるときだけ出す。
        """
        prefix = "long_videos" if self.video_type == "long" else "shorts_outliers"
        output = output or f"{prefix}_{datetime.now(JST).strftime('%Y%m%d')}.csv"
        if fields is None:
            fields = [f for f in CSV_FIELDS
                      if f not in OPTIONAL_CSV_FIELDS or any(f in r for r in rows)]
        with open(output, "w", newline="", encoding="utf-8-sig") as f:
            # 内部用のキー(id / channel_id / channel_url)はCSVには書かない
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
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
        good_count = sum(1 for r in rows if r["outlier"])
        self._log(f"\n完了: {output} に {len(rows)}行を出力(うちよい動画 {good_count}本)")
        self._log(f"  よい動画の基準: 直近{self.days}日以内 かつ {self.rule}")
        return output


def main():
    """使い方の例。ここを書き換えて `uv run shorts_outlier.py` で実行する。"""
    prefer_ipv4()

    # === 設定(すべてデフォルト値を明記。ここの値を書き換えて使う) =======
    # 対象チャンネル(@ハンドル / /channel/UC... / /user/... のURLに対応)
    channel_url = "https://www.youtube.com/@%E7%B5%B5%E4%B8%80%E6%9E%9A-y5e/shorts"

    # 出力CSVパス。None なら shorts_outliers_YYYYMMDD.csv に自動命名
    output = None

    extractor = ShortsOutlierExtractor(
        api_key=None,        # APIキー。None なら .env の YOUTUBE_API_KEY を使う
        days=180,            # 直近何日分を抽出対象にするか(180日=半年)
        threshold=10.0,      # 視聴数 ÷ 分母 がこの倍率以上で「よい動画」
        subscriber_floor=100,# 分母の下限。登録者100人未満は1,000回以上で検知される
        max_videos=300,      # 1チャンネルあたり遡る最大本数
        include_all=False,   # True なら基準未満の動画も出力(outlier列で判別)
        env_file=None,       # .envのパス。None ならスクリプト位置→カレントの順に自動探索
        verbose=True,        # False にすると進捗表示を止める
    )
    extractor.run(channel_url, output=output)

    # === CSVにせずPython側で結果を扱う例 ===============================
    # rows = ShortsOutlierExtractor(include_all=True).analyze(channel_url)
    # for r in sorted(rows, key=lambda r: r["視聴数"], reverse=True)[:5]:
    #     print(r["視聴数"], r["倍率(vs登録者数)"], r["動画名"])

    # === 複数チャンネルを1つのCSVにまとめる例 ===========================
    # 存在しないチャンネル(404)・対象動画が無い/登録者数が非公開のチャンネルは自動で除外される
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
