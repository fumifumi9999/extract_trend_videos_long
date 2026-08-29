#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["gspread>=6.0", "python-dotenv>=1.0"]
# ///
"""
sheet_reader.py
Googleスプレッドシートの指定シート・指定列を読み取る。
デフォルトでは「①YouTubeベンチマーク_雑学」シートのH6以降で、
値が入っている行をすべて取得する。

必要なもの:
    - 対象スプレッドシートのURL(.env の SPREADSHEET_URL に記載)
    - 認証情報(下のどちらか)
        A. サービスアカウント(非公開シートでも読める / 推奨)
           1. Google Cloud で Google Sheets API を有効化
           2. サービスアカウントを作成し、JSONキーをダウンロード
           3. .env に GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
           4. スプレッドシートの共有設定で、そのサービスアカウントの
              メールアドレス(xxx@yyy.iam.gserviceaccount.com)に閲覧権限を付与
        B. APIキー(「リンクを知っている全員が閲覧可」のシート限定)
           .env に GOOGLE_API_KEY=... を記載
           (YOUTUBE_API_KEY は流用しない。使うなら明示的に設定すること)

使い方:
    # main() の中の設定を書き換えて実行する
    uv run sheet_reader.py

    # 他のスクリプトから使う場合
    #   from sheet_reader import SheetReader
    #   rows = SheetReader().read_column(sheet_name="①YouTubeベンチマーク_雑学")
    #   values = [r["value"] for r in rows]
"""

import os
import re
from pathlib import Path

import gspread
from dotenv import find_dotenv, load_dotenv

ENV_SPREADSHEET = "SPREADSHEET_URL"
ENV_SERVICE_ACCOUNT = "GOOGLE_SERVICE_ACCOUNT_FILE"
ENV_API_KEY = "GOOGLE_API_KEY"

DEFAULT_SHEET_NAME = "②YouTubeベンチマーク_AI動画"
DEFAULT_START_CELL = "H6"


class SheetReaderError(Exception):
    """このスクリプト由来のエラー(認証情報未設定、シートが見つからない等)"""


class SheetReader:
    """Googleスプレッドシートから指定列の値を読み取る。

    Attributes:
        spreadsheet: スプレッドシートのURLまたはID(NoneならSPREADSHEET_URL)
        skip_blank:  Trueなら途中の空セルを飛ばし、値のある行だけ返す
        verbose:     進捗をprintするか
    """

    def __init__(self, spreadsheet=None, service_account_file=None, api_key=None,
                 skip_blank=True, env_file=None, verbose=True):
        self.load_env(env_file)
        self.spreadsheet = spreadsheet or os.environ.get(ENV_SPREADSHEET)
        self.service_account_file = (service_account_file
                                     or os.environ.get(ENV_SERVICE_ACCOUNT))
        # APIキーは明示指定 or GOOGLE_API_KEY のみ。YOUTUBE_API_KEY は流用しない
        self.api_key = api_key or os.environ.get(ENV_API_KEY)
        self.skip_blank = skip_blank
        self.verbose = verbose
        self._client = None

    # ------------------------------------------------------------------
    # セットアップ
    # ------------------------------------------------------------------
    @staticmethod
    def load_env(env_file=None):
        """.env を読み込む(既存の環境変数は上書きしない)。

        探索順: env_file 指定 → スクリプトと同じディレクトリ → カレント以上の親階層。
        """
        if env_file:
            if not Path(env_file).is_file():
                raise SheetReaderError(f".envファイルが見つかりません: {env_file}")
            env_path = env_file
        else:
            script_env = Path(__file__).resolve().parent / ".env"
            env_path = str(script_env) if script_env.is_file() else find_dotenv(usecwd=True)

        if env_path:
            load_dotenv(env_path, encoding="utf-8")
        return env_path

    def _log(self, message):
        if self.verbose:
            print(message)

    @property
    def client(self):
        """gspreadクライアント(サービスアカウント優先、なければAPIキー)"""
        if self._client is not None:
            return self._client

        if self.service_account_file:
            path = Path(self.service_account_file)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent / path
            if not path.is_file():
                raise SheetReaderError(
                    f"サービスアカウントのJSONキーが見つかりません: {path}\n"
                    f"  .env の {ENV_SERVICE_ACCOUNT} のパスを確認してください")
            self._log(f"認証: サービスアカウント ({path.name})")
            self._client = gspread.service_account(filename=str(path))
        elif self.api_key:
            self._log("認証: APIキー(公開シートのみ読み取り可)")
            self._client = gspread.api_key(self.api_key)
        else:
            raise SheetReaderError(
                f"認証情報が未設定です。.env に次のどちらかを記載してください。\n"
                f"  {ENV_SERVICE_ACCOUNT}=service_account.json  (非公開シート対応 / 推奨)\n"
                f"  {ENV_API_KEY}=...                           (リンク公開シートのみ)")
        return self._client

    # ------------------------------------------------------------------
    # 読み取り
    # ------------------------------------------------------------------
    @staticmethod
    def parse_cell(cell):
        """"H6" → ("H", 8, 6) のように 列名・列番号・開始行 に分解する"""
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", cell.strip())
        if not m:
            raise SheetReaderError(f"セル参照の形式が不正です: {cell} (例: H6)")
        letters, row = m.group(1).upper(), int(m.group(2))
        index = 0
        for ch in letters:
            index = index * 26 + (ord(ch) - ord("A") + 1)
        return letters, index, row

    @staticmethod
    def build_rows(col_values, column_letter, start_row, skip_blank=True):
        """列の全値リストから、start_row以降の行だけを取り出して整形する。

        col_values は1行目から並んだ値のリスト(gspreadのcol_values相当)。
        戻り値は [{"row": 6, "cell": "H6", "value": "..."}, ...]
        """
        rows = []
        for offset, value in enumerate(col_values[start_row - 1:]):
            row_number = start_row + offset
            value = (value or "").strip()
            if skip_blank and not value:
                continue
            rows.append({
                "row": row_number,
                "cell": f"{column_letter}{row_number}",
                "value": value,
            })
        return rows

    def read_column(self, spreadsheet=None, sheet_name=DEFAULT_SHEET_NAME,
                    start_cell=DEFAULT_START_CELL):
        """指定シートの指定列を start_cell 以降で読み取り、行データのリストを返す"""
        target = spreadsheet or self.spreadsheet
        if not target:
            raise SheetReaderError(
                f"スプレッドシートが未指定です。\n"
                f"  .env に {ENV_SPREADSHEET}=https://docs.google.com/spreadsheets/d/... "
                f"を記載するか、read_column(spreadsheet=\"...\") で渡してください")

        column_letter, column_index, start_row = self.parse_cell(start_cell)

        self._log(f"スプレッドシートを開いています: {target}")
        try:
            if re.match(r"https?://", target):
                book = self.client.open_by_url(target)
            else:
                book = self.client.open_by_key(target)
        except gspread.exceptions.SpreadsheetNotFound as e:
            raise SheetReaderError(
                f"スプレッドシートが見つかりません(URL/ID、または共有権限を確認してください)"
                f": {target}") from e
        except gspread.exceptions.APIError as e:
            raise SheetReaderError(self._api_error_message(e)) from e

        self._log(f"  → {book.title}")

        try:
            worksheet = book.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound as e:
            available = ", ".join(ws.title for ws in book.worksheets())
            raise SheetReaderError(
                f"シートが見つかりません: {sheet_name}\n"
                f"  このスプレッドシート内のシート: {available}") from e

        self._log(f"シート「{sheet_name}」の{column_letter}列を{start_row}行目から取得中...")
        try:
            col_values = worksheet.col_values(column_index)
        except gspread.exceptions.APIError as e:
            raise SheetReaderError(self._api_error_message(e)) from e

        rows = self.build_rows(col_values, column_letter, start_row, self.skip_blank)
        self._log(f"  → {len(rows)}件を取得({column_letter}{start_row}以降)")
        return rows

    def read_values(self, spreadsheet=None, sheet_name=DEFAULT_SHEET_NAME,
                    start_cell=DEFAULT_START_CELL):
        """read_column() と同じだが、値の文字列リストだけを返す"""
        return [r["value"] for r in
                self.read_column(spreadsheet, sheet_name, start_cell)]

    @staticmethod
    def _api_error_message(error):
        """Google APIのエラーに、原因の当たりを付けるヒントを添える"""
        text = str(error)
        hint = ""
        if "403" in text and "PERMISSION_DENIED" in text:
            hint = ("\n  ヒント: サービスアカウントのメールアドレスにシートを共有したか、"
                    "Google Sheets API が有効か確認してください")
        elif "API_KEY" in text or "401" in text:
            hint = ("\n  ヒント: APIキーでは「リンクを知っている全員が閲覧可」のシートしか"
                    "読めません。非公開シートはサービスアカウントを使ってください")
        return f"Google Sheets APIエラー: {text}{hint}"


def main():
    """使い方の例。ここを書き換えて `uv run sheet_reader.py` で実行する。"""

    # === 設定(すべてデフォルト値を明記。ここの値を書き換えて使う) =======
    sheet_name = "①YouTubeベンチマーク_雑学"   # 読み取るシート(タブ)名
    start_cell = "H6"                          # 読み始めるセル(列と開始行)

    reader = SheetReader(
        spreadsheet=None,           # URLまたはID。None なら .env の SPREADSHEET_URL
        service_account_file=None,  # JSONキーのパス。None なら .env の GOOGLE_SERVICE_ACCOUNT_FILE
        api_key=None,               # APIキー。None なら .env の GOOGLE_API_KEY
        skip_blank=True,            # False にすると途中の空行も空文字のまま返す
        env_file=None,              # .envのパス。None ならスクリプト位置→カレントの順に自動探索
        verbose=True,               # False にすると進捗表示を止める
    )
    rows = reader.read_column(sheet_name=sheet_name, start_cell=start_cell)

    for r in rows:
        print(f"{r['cell']}\t{r['value']}")
    print(f"\n合計 {len(rows)}件")

    # === 値の文字列リストだけ欲しい場合 ================================
    # values = reader.read_values(sheet_name=sheet_name, start_cell=start_cell)
    # print(values)

    # === 別のシート・別の列を読む場合 ==================================
    # rows = reader.read_column(sheet_name="別のシート名", start_cell="B2")

    # === 取得したチャンネルURLをそのまま long_video_outlier に渡す例 ========
    # from long_video_outlier import ShortsOutlierExtractor
    # extractor = ShortsOutlierExtractor(days=7, video_type="long", verbose=False)
    # for url in reader.read_values(sheet_name=sheet_name, start_cell=start_cell):
    #     print(url, "→ outlier", len(extractor.analyze(url)), "本")


if __name__ == "__main__":
    try:
        main()
    except SheetReaderError as e:
        raise SystemExit(f"[エラー] {e}")
