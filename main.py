#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["gspread>=6.0", "python-dotenv>=1.0"]
# ///
"""
main.py
3つのスクリプトを繋いだ全体の実行役。

    sheet_reader   スプレッドシートからチャンネルURL一覧を取得
        ↓
    shorts_outlier 各チャンネルのLong動画を解析し、伸びている動画を抽出
        ↓
    mail_sender    結果を見やすい表(HTMLメール)にして送信

必要なもの:
    .env に以下がすべて設定されていること(詳しくは .env.example を参照)
      - YOUTUBE_API_KEY                (shorts_outlier)
      - SPREADSHEET_URL / GOOGLE_SERVICE_ACCOUNT_FILE  (sheet_reader)
      - GMAIL_ADDRESS / GMAIL_APP_PASSWORD / MAIL_TO   (mail_sender)

使い方:
    # main() の中の設定を書き換えて実行する
    uv run main.py

    # いきなり送信せず、メール本文をブラウザで確認したい場合は
    # main() の dry_run=True にする → report_preview.html が出力される
"""

import html as html_lib
import urllib.parse
from datetime import datetime
from pathlib import Path

from mail_sender import GmailSender, MailSenderError
from sheet_reader import SheetReader, SheetReaderError
from long_video_outlier import JST, ShortsOutlierError, ShortsOutlierExtractor

PREVIEW_FILE = "report_preview.html"


class TrendReporter:
    """シート → 解析 → メール送信 を一気通貫で実行する。

    Attributes:
        days/threshold/baseline/include_all: Long動画解析条件
        sheet_name/start_cell:               読み取るシートと開始セル
        max_channels: 動作確認用に処理するチャンネル数を制限する(Noneで全件)
        attach_csv:   Trueなら結果CSVをメールに添付する
        dry_run:      Trueなら送信せず report_preview.html に本文を書き出す
    """

    def __init__(self, days=7, threshold=3.0, baseline=30, include_all=False,
                 sheet_name=None, start_cell=None, max_channels=None,
                 attach_csv=True, to=None, subject=None,
                 dry_run=False, verbose=True, min_views=100):
        self.days = days
        self.threshold = threshold
        self.baseline = baseline
        self.include_all = include_all
        self.min_views = min_views
        self.sheet_name = sheet_name
        self.start_cell = start_cell
        self.max_channels = max_channels
        self.attach_csv = attach_csv
        self.to = to
        self.subject = subject
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
    def analyze(self, channel_urls):
        """各チャンネルを解析する。存在しないチャンネルは自動で除外される"""
        extractor = ShortsOutlierExtractor(
            days=self.days,
            threshold=self.threshold,
            baseline=self.baseline,
            include_all=self.include_all,
            video_type="long",
            min_views=self.min_views,
            verbose=self.verbose,
        )
        rows = extractor.analyze_many(channel_urls, skip_missing=True)
        rows.sort(key=self.sort_key, reverse=True)
        return rows, extractor

    @staticmethod
    def sort_key(row):
        """倍率の降順に並べる(中央値0で倍率が出せない行は最上位に)"""
        value = row.get("倍率(vs中央値)")
        return float("inf") if value == "" else float(value)

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

    def summary(self, rows, skipped, channel_count):
        """メール冒頭に出す集計情報"""
        outliers = sum(1 for r in rows if r["outlier"])
        return [
            ("集計日時", datetime.now(JST).strftime("%Y-%m-%d %H:%M")),
            ("対象期間", f"直近{self.days}日"),
            ("outlier基準", f"直近{self.baseline}本の視聴数中央値の{self.threshold}倍以上"),
            ("対象チャンネル", f"{channel_count}件(うち除外 {len(skipped)}件)"),
            ("抽出動画", f"{len(rows)}本(うちoutlier {outliers}本)"),
        ]

    def build_html(self, rows, skipped, channel_count):
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
            '<h2 style="font-size:18px;margin:0 0 12px;">Long動画 好調動画レポート</h2>',
            '<table style="border-collapse:collapse;margin-bottom:18px;">',
        ]
        for label, value in self.summary(rows, skipped, channel_count):
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
                f'<th style="{th}">倍率</th>'
                "</tr>")
            for i, r in enumerate(rows, 1):
                multiplier = r["倍率(vs中央値)"]
                # outlierは倍率を赤字・太字にして目立たせる
                style = ("color:#c0392b;font-weight:700;" if r["outlier"] else "")
                shown = f"{multiplier}倍" if multiplier != "" else "—"
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

    def build_text(self, rows, skipped, channel_count):
        """HTMLを表示できない環境向けの代替テキスト"""
        lines = ["Long動画 好調動画レポート", ""]
        lines += [f"{label}: {value}" for label, value in
                  self.summary(rows, skipped, channel_count)]
        lines.append("")
        if not rows:
            lines.append("条件に合う動画はありませんでした。")
        for i, r in enumerate(rows, 1):
            multiplier = r["倍率(vs中央値)"]
            shown = f"{multiplier}倍" if multiplier != "" else "—"
            lines += [
                f"{i}. [{self.channel_label(r['チャンネル'])}] {r['動画名']}",
                f"    {r['アップロード日']} / {r['視聴数']:,}回 / {shown}"
                f"{' ★outlier' if r['outlier'] else ''}",
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
        self._log("=" * 60)
        self._log("[1/3] スプレッドシートからチャンネル一覧を取得")
        self._log("=" * 60)
        channel_urls = self.fetch_channel_urls()
        if not channel_urls:
            raise SheetReaderError("チャンネルURLが1件も取得できませんでした")

        self._log("\n" + "=" * 60)
        self._log(f"[2/3] {len(channel_urls)}チャンネルのLong動画を解析")
        self._log(f"  min_views={self.min_views}, threshold={self.threshold}, baseline={self.baseline}")
        self._log("=" * 60)
        rows, extractor = self.analyze(channel_urls)

        self._log("\n" + "=" * 60)
        self._log("[3/3] レポートを作成して送信")
        self._log("=" * 60)
        body = self.build_text(rows, extractor.skipped, len(channel_urls))
        body_html = self.build_html(rows, extractor.skipped, len(channel_urls))

        attachments = []
        if self.attach_csv and rows:
            attachments.append(extractor.to_csv(rows))
            self._log(f"CSVを作成: {attachments[0]}")

        subject = self.subject or (
            f"[Long動画レポート] {datetime.now(JST).strftime('%Y-%m-%d')} "
            f"好調動画{len(rows)}本 / 直近{self.days}日")

        if self.dry_run:
            Path(PREVIEW_FILE).write_text(body_html, encoding="utf-8")
            self._log(f"\ndry_run のため送信しません。本文を {PREVIEW_FILE} に出力しました。")
            self._log(f"  件名: {subject}")
            return PREVIEW_FILE

        GmailSender(verbose=self.verbose).send(
            subject=subject, body=body, to=self.to,
            attachments=attachments, html=body_html)
        return subject


def main():
    """使い方の例。ここを書き換えて `uv run main.py` で実行する。"""

    # === 設定(ここの値を書き換えて使う) ===============================
    # ※ shorts_outlier.py / sheet_reader.py 側の main() は uv run main.py では
    #    呼ばれない。main.py の実行条件はすべてこのブロックで指定する。
    reporter = TrendReporter(
        # --- 解析条件 (shorts_outlier) ---
        days=3,             # 直近何日分の動画を対象にするか
        threshold=3.0,      # 視聴数 ÷ 中央値 がこの倍率以上でoutlier判定
        baseline=30,        # 中央値の算出に使う直近Shorts本数
        include_all=False,  # True なら基準未満の動画も表に載せる
        min_views=100,      # 100回未満はノイズとして除外

        # --- 読み取り元 (sheet_reader) ---
        sheet_name=None,    # None なら「①YouTubeベンチマーク_雑学」
        start_cell=None,    # None なら "H6"
        max_channels=None,  # 動作確認用の上限。例: 3 なら先頭3チャンネルだけ処理

        # --- 送信 (mail_sender) ---
        to=None,            # 宛先。None なら .env の MAIL_TO
        subject=None,       # 件名。None なら日付と件数から自動生成
        attach_csv=True,    # 結果CSVを添付するか

        dry_run=False,      # True なら送信せず report_preview.html に本文を出力
        verbose=True,       # False にすると進捗表示を止める
    )
    reporter.run()

    # === 送信せず、まず本文を確認したい場合 =============================
    # TrendReporter(dry_run=True, max_channels=3).run()
    # → report_preview.html をブラウザで開く

    # === 全件を載せたい場合(outlier以外も表に出す) =====================
    # TrendReporter(include_all=True, days=14).run()


if __name__ == "__main__":
    try:
        main()
    except (SheetReaderError, ShortsOutlierError, MailSenderError) as e:
        raise SystemExit(f"[エラー] {e}")
