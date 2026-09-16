#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["gspread>=6.0", "python-dotenv>=1.0"]
# ///
"""
main.py
3つのスクリプトを繋いだ全体の実行役。

    sheet_reader       スプレッドシートからチャンネルURL一覧を取得
        ↓
    long_video_outlier 各チャンネルのLong動画を解析し、よい動画を判定
        ↓
    video_store        通知済みの動画をSQLiteで覚えておき、二重通知を防ぐ
        ↓
    mail_sender        結果を見やすい表(HTMLメール)にして送信

必要なもの:
    .env に以下がすべて設定されていること(詳しくは .env.example を参照)
      - YOUTUBE_API_KEY                (shorts_outlier)
      - SPREADSHEET_URL / GOOGLE_SERVICE_ACCOUNT_FILE  (sheet_reader)
      - GMAIL_ADDRESS / GMAIL_APP_PASSWORD / MAIL_TO   (mail_sender)

使い方:
    # main() の中の設定を書き換えて実行する
    uv run main.py

    # いきなり送信せず、メール本文をブラウザで確認したい場合
    uv run main.py --dry-run     → report_preview.html が出力される
                                   (通知済みの記録も付かないので何度でも試せる)

    # 毎日自動で実行する場合は run_daily.cmd をタスクスケジューラに登録する
    # (README.md の「タスクスケジューラへの登録」を参照)
"""

import html as html_lib
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from mail_sender import GmailSender, MailSenderError
from sheet_reader import SheetReader, SheetReaderError
from long_video_outlier import JST, ShortsOutlierError, ShortsOutlierExtractor, prefer_ipv4
from video_store import VideoStore

PREVIEW_FILE = "report_preview.html"


class TrendReporter:
    """シート → 解析 → メール送信 を一気通貫で実行する。

    Attributes:
        days/threshold/subscriber_floor/include_all: Long動画の「よい動画」判定条件
        sheet_name/start_cell:               読み取るシートと開始セル
        max_channels: 動作確認用に処理するチャンネル数を制限する(Noneで全件)
        attach_csv:   Trueなら結果CSVをメールに添付する
        use_db/db_path: メール通知した動画をSQLiteに記録し、次回以降は
                      同じ動画を通知しない(db_path=None で trend_videos.db)
        dry_run:      Trueなら送信せず report_preview.html に本文を書き出す
                      (このとき通知済みの記録は付けないので、次回あらためて通知される)
    """

    def __init__(self, days=180, threshold=10.0, subscriber_floor=100, include_all=False,
                 sheet_name=None, start_cell=None, max_channels=None,
                 attach_csv=True, to=None, subject=None, use_db=True, db_path=None,
                 dry_run=False, verbose=True, max_videos=300):
        self.days = days
        self.threshold = threshold
        self.subscriber_floor = subscriber_floor
        self.include_all = include_all
        self.max_videos = max_videos
        self.sheet_name = sheet_name
        self.start_cell = start_cell
        self.max_channels = max_channels
        self.attach_csv = attach_csv
        self.to = to
        self.subject = subject
        self.use_db = use_db
        self.db_path = db_path
        self.dry_run = dry_run
        self.verbose = verbose

    def _log(self, message):
        if self.verbose:
            print(message)

    # ------------------------------------------------------------------
    # 1. チャンネルURLの取得
    # ------------------------------------------------------------------
    def fetch_channel_urls(self):
        """スプレッドシートからチャンネルURLを取得する(重複は除く)"""
        reader = SheetReader(verbose=self.verbose)
        kwargs = {}
        if self.sheet_name:
            kwargs["sheet_name"] = self.sheet_name
        if self.start_cell:
            kwargs["start_cell"] = self.start_cell
        urls = reader.read_values(**kwargs)

        unique = list(dict.fromkeys(urls))   # 順序を保ったまま重複を除去
        if len(unique) < len(urls):
            self._log(f"  重複を除外: {len(urls)}件 → {len(unique)}件")
        if self.max_channels:
            unique = unique[:self.max_channels]
            self._log(f"  max_channels の指定により先頭{len(unique)}件のみ処理します")
        return unique

    # ------------------------------------------------------------------
    # 2. 解析
    # ------------------------------------------------------------------
    def evaluate(self, channel_urls):
        """各チャンネルの直近N日の動画を、基準未満も含めてすべて評価する。

        毎回すべて評価し直しているので、投稿直後は基準未満だった動画が
        あとから伸びて基準に届いた場合も、その回の実行で拾える。
        存在しないチャンネルは自動で除外される。
        """
        extractor = ShortsOutlierExtractor(
            days=self.days,
            threshold=self.threshold,
            subscriber_floor=self.subscriber_floor,
            include_all=self.include_all,
            video_type="long",
            max_videos=self.max_videos,
            verbose=self.verbose,
        )
        return extractor.evaluate_many(channel_urls, skip_missing=True), extractor

    def select(self, all_rows, store):
        """メールに載せる行を決める。

        store があれば、よい動画のうち「まだ通知していないもの」だけに絞る。
        include_all=True のときは確認用に、通知済み・基準未満も含めて全部載せる
        (このとき通知済みの動画は倍率などが最新値で出る)。
        """
        if self.include_all:
            return list(all_rows)
        if store is None:
            return [r for r in all_rows if r["outlier"]]
        return store.unnotified(all_rows)

    @staticmethod
    def sort_key(row):
        """登録者数比の降順に並べる(倍率が出せない行は最上位に)"""
        value = row.get("倍率(vs登録者数)")
        return float("inf") if value in (None, "") else float(value)

    # ------------------------------------------------------------------
    # 3. 表の組み立て
    # ------------------------------------------------------------------
    @staticmethod
    def channel_label(url):
        """チャンネルURLから表示用の短い名前(@ハンドル)を作る"""
        name = urllib.parse.unquote(url).rstrip("/")
        for suffix in ("/shorts", "/videos", "/featured"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return name.rsplit("/", 1)[-1]

    def summary(self, rows, skipped, extractor, channel_count, suppressed=0):
        """メール冒頭に出す集計情報"""
        good = sum(1 for r in rows if r["outlier"])
        items = [
            ("集計日時", datetime.now(JST).strftime("%Y-%m-%d %H:%M")),
            ("対象期間", f"直近{self.days}日"),
            ("よい動画の基準", extractor.rule),
            ("対象チャンネル", f"{channel_count}件(うち除外 {len(skipped)}件)"),
            ("抽出動画", f"{len(rows)}本(うちよい動画 {good}本)"),
        ]
        if suppressed:
            items.append(("通知済みのため除外", f"{suppressed}本"))
        return items

    def build_html(self, rows, skipped, extractor, channel_count, suppressed=0):
        """メール本文(HTML)を組み立てる。メーラーでも崩れないようインラインCSSで書く"""
        esc = html_lib.escape
        border = "1px solid #dcdcdc"
        th = (f"padding:8px 10px;border:{border};background:#f2f4f7;"
              "text-align:left;font-weight:600;white-space:nowrap;")
        td = f"padding:8px 10px;border:{border};vertical-align:top;"
        num = td + "text-align:right;white-space:nowrap;"

        parts = [
            '<div style="font-family:\'Segoe UI\',Meiryo,sans-serif;font-size:14px;'
            'color:#222;line-height:1.6;">',
            '<h2 style="font-size:18px;margin:0 0 12px;">Long動画 よい動画レポート</h2>',
            '<table style="border-collapse:collapse;margin-bottom:18px;">',
        ]
        for label, value in self.summary(rows, skipped, extractor, channel_count, suppressed):
            parts.append(f'<tr><td style="{td}background:#f8f9fa;white-space:nowrap;">'
                         f'{esc(label)}</td><td style="{td}">{esc(str(value))}</td></tr>')
        parts.append("</table>")

        if not rows:
            parts.append('<p style="color:#c0392b;">条件に合う動画はありませんでした。</p>')
        else:
            parts.append('<table style="border-collapse:collapse;width:100%;">')
            parts.append(
                "<tr>"
                f'<th style="{th}">#</th>'
                f'<th style="{th}">チャンネル</th>'
                f'<th style="{th}">動画</th>'
                f'<th style="{th}">投稿日</th>'
                f'<th style="{th}">視聴数</th>'
                f'<th style="{th}">登録者数</th>'
                f'<th style="{th}">倍率</th>'
                "</tr>")
            for i, r in enumerate(rows, 1):
                multiplier = r["倍率(vs登録者数)"]
                # 基準を満たした動画は倍率を赤字・太字にして目立たせる
                style = ("color:#c0392b;font-weight:700;" if r["outlier"] else "")
                shown = f"{multiplier}倍" if multiplier not in (None, "") else "—"
                stripe = "background:#fafbfc;" if i % 2 == 0 else ""
                parts.append(
                    f'<tr style="{stripe}">'
                    f'<td style="{num}">{i}</td>'
                    f'<td style="{td}white-space:nowrap;">'
                    f'<a href="{esc(r["チャンネル"])}" style="color:#1a5fb4;'
                    f'text-decoration:none;">{esc(self.channel_label(r["チャンネル"]))}</a></td>'
                    f'<td style="{td}">'
                    f'<a href="{esc(r["動画URL"])}" style="color:#1a5fb4;">'
                    f'{esc(r["動画名"])}</a></td>'
                    f'<td style="{td}white-space:nowrap;">{esc(r["アップロード日"])}</td>'
                    f'<td style="{num}">{r["視聴数"]:,}</td>'
                    f'<td style="{num}">{r["登録者数"]:,}</td>'
                    f'<td style="{num}{style}">{esc(shown)}</td>'
                    "</tr>")
            parts.append("</table>")

        if skipped:
            parts.append('<p style="margin-top:18px;color:#666;font-size:13px;">'
                         f'集計から除外したチャンネル({len(skipped)}件):</p>'
                         '<ul style="color:#666;font-size:13px;margin:4px 0 0;">')
            for url, reason in skipped:
                parts.append(f"<li>{esc(urllib.parse.unquote(url))} — {esc(reason)}</li>")
            parts.append("</ul>")

        parts.append('<p style="margin-top:20px;color:#888;font-size:12px;">'
                     "このメールは main.py により自動送信されました。</p></div>")
        return "\n".join(parts)

    def build_text(self, rows, skipped, extractor, channel_count, suppressed=0):
        """HTMLを表示できない環境向けの代替テキスト"""
        lines = ["Long動画 よい動画レポート", ""]
        lines += [f"{label}: {value}" for label, value in
                  self.summary(rows, skipped, extractor, channel_count, suppressed)]
        lines.append("")
        if not rows:
            lines.append("条件に合う動画はありませんでした。")
        for i, r in enumerate(rows, 1):
            multiplier = r["倍率(vs登録者数)"]
            shown = f"{multiplier}倍" if multiplier not in (None, "") else "—"
            lines += [
                f"{i}. [{self.channel_label(r['チャンネル'])}] {r['動画名']}",
                f"    {r['アップロード日']} / {r['視聴数']:,}回 / 登録{r['登録者数']:,}人 / {shown}"
                f"{' ★よい動画' if r['outlier'] else ''}",
                f"    {r['動画URL']}",
            ]
        if skipped:
            lines += ["", f"除外したチャンネル({len(skipped)}件):"]
            lines += [f"  - {urllib.parse.unquote(url)} — {reason}"
                      for url, reason in skipped]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 4. 実行
    # ------------------------------------------------------------------
    def run(self):
        """取得 → 解析 → 送信 までを実行する"""
        # IPv6が半端に有効な回線で1リクエストごとに待たされるのを防ぐ(long_video_outlier参照)
        prefer_ipv4()

        self._log("=" * 60)
        self._log("[1/4] スプレッドシートからチャンネル一覧を取得")
        self._log("=" * 60)
        channel_urls = self.fetch_channel_urls()
        if not channel_urls:
            raise SheetReaderError("チャンネルURLが1件も取得できませんでした")

        self._log("\n" + "=" * 60)
        self._log(f"[2/4] {len(channel_urls)}チャンネルのLong動画を解析")
        self._log(f"  視聴数 >= max(登録者数, {self.subscriber_floor:,}) × {self.threshold}")
        self._log("=" * 60)
        all_rows, extractor = self.evaluate(channel_urls)

        self._log("\n" + "=" * 60)
        self._log("[3/4] すでに通知した動画を除外")
        self._log("=" * 60)
        store = VideoStore(self.db_path, verbose=self.verbose) if self.use_db else None
        try:
            rows = self.select(all_rows, store)
            rows.sort(key=self.sort_key, reverse=True)
            suppressed = sum(1 for r in all_rows if r["outlier"]) - \
                sum(1 for r in rows if r["outlier"])
            return self._report(rows, extractor, store, len(channel_urls), suppressed)
        finally:
            if store:
                store.close()

    def _report(self, rows, extractor, store, channel_count, suppressed):
        """レポートを組み立てて送信し、送れた動画を通知済みにする"""
        self._log("\n" + "=" * 60)
        self._log("[4/4] レポートを作成して送信")
        self._log("=" * 60)
        body = self.build_text(rows, extractor.skipped, extractor, channel_count, suppressed)
        body_html = self.build_html(rows, extractor.skipped, extractor, channel_count, suppressed)

        attachments = []
        if self.attach_csv and rows:
            attachments.append(extractor.to_csv(rows))
            self._log(f"CSVを作成: {attachments[0]}")

        good = [r for r in rows if r["outlier"]]
        subject = self.subject or (
            f"[Long動画レポート] {datetime.now(JST).strftime('%Y-%m-%d')} "
            f"よい動画{len(good)}本 / 直近{self.days}日")

        if self.dry_run:
            Path(PREVIEW_FILE).write_text(body_html, encoding="utf-8")
            self._log(f"\ndry_run のため送信しません。本文を {PREVIEW_FILE} に出力しました。")
            self._log(f"  件名: {subject}")
            self._log("  通知済みの記録は付けないので、次回の実行でも同じ動画が対象になります。")
            return PREVIEW_FILE

        GmailSender(verbose=self.verbose).send(
            subject=subject, body=body, to=self.to,
            attachments=attachments, html=body_html)

        # 送信できたものだけをDBに記録する(送信失敗時は例外が飛ぶのでここに来ない)
        if store:
            self._log(f"  通知済みとしてDBに記録: {store.mark_notified(good)}本")
        return subject


def main():
    """使い方の例。ここを書き換えて `uv run main.py` で実行する。"""

    # === 設定(ここの値を書き換えて使う) ===============================
    # ※ shorts_outlier.py / sheet_reader.py 側の main() は uv run main.py では
    #    呼ばれない。main.py の実行条件はすべてこのブロックで指定する。
    reporter = TrendReporter(
        # --- 「よい動画」の判定条件 (long_video_outlier) ---
        days=180,            # 直近何日分の動画を対象にするか(180日=半年)
        threshold=10.0,      # 視聴数 ÷ 分母 がこの倍率以上で「よい動画」
        subscriber_floor=100,# 分母の下限。登録者100人未満は1,000回以上で検知される
        include_all=False,   # True なら基準未満の動画も表に載せる
        max_videos=300,      # 1チャンネルあたり遡る最大本数

        # --- 読み取り元 (sheet_reader) ---
        sheet_name=None,    # None なら「①YouTubeベンチマーク_雑学」
        start_cell=None,    # None なら "H6"
        max_channels=None,  # 動作確認用の上限。例: 3 なら先頭3チャンネルだけ処理

        # --- 通知の重複除外 (video_store) ---
        use_db=True,        # 通知した動画をSQLiteに記録し、二度目は通知しない
        db_path=None,       # DBのパス。None なら trend_videos.db

        # --- 送信 (mail_sender) ---
        to=None,            # 宛先。None なら .env の MAIL_TO
        subject=None,       # 件名。None なら日付と件数から自動生成
        attach_csv=True,    # 結果CSVを添付するか

        # コマンドラインで --dry-run を付けても同じ(タスクの動作確認用)
        dry_run="--dry-run" in sys.argv,
        verbose=True,       # False にすると進捗表示を止める
    )
    reporter.run()

    # === 送信せず、まず本文を確認したい場合 =============================
    # uv run main.py --dry-run
    # → report_preview.html をブラウザで開く

    # === 全件を載せたい場合(通知済み・基準未満の動画も表に出す) =========
    # TrendReporter(include_all=True).run()

    # === DBの中身を確認したい場合 =======================================
    # uv run video_store.py


if __name__ == "__main__":
    try:
        main()
    except (SheetReaderError, ShortsOutlierError, MailSenderError) as e:
        raise SystemExit(f"[エラー] {e}")
