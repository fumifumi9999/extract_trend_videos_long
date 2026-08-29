#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.9"
# dependencies = ["python-dotenv>=1.0"]
# ///
"""
mail_sender.py
Gmailのアプリパスワードを使って、SMTPでメールを送信する。
CSVなどのファイル添付、複数宛先、CC/BCCに対応。

必要なもの:
    - Gmailアドレスと「アプリパスワード」(16桁)
      発行手順:
        1. Googleアカウントの「セキュリティ」で2段階認証プロセスをONにする
           https://myaccount.google.com/security
        2. https://myaccount.google.com/apppasswords を開く
        3. アプリ名を入力(例: extract_trend_videos)して作成
        4. 表示された16桁を .env の GMAIL_APP_PASSWORD に貼り付ける
           (スペース区切りで表示されるが、そのまま貼り付けてよい)
    - .env に GMAIL_ADDRESS(送信元) と GMAIL_APP_PASSWORD を記載
      ※ 通常のGoogleアカウントのパスワードでは送信できない

使い方:
    # main() の中の設定を書き換えて実行する
    uv run mail_sender.py

    # 他のスクリプトから使う場合
    #   from mail_sender import GmailSender
    #   GmailSender().send("件名", "本文", to="xxx@example.com",
    #                      attachments=["long_videos_20260812.csv"])
"""

import mimetypes
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

ENV_ADDRESS = "GMAIL_ADDRESS"
ENV_APP_PASSWORD = "GMAIL_APP_PASSWORD"
ENV_DEFAULT_TO = "MAIL_TO"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465          # SSL接続用のポート


class MailSenderError(Exception):
    """このスクリプト由来のエラー(認証情報未設定、添付ファイル不明など)"""


class GmailSender:
    """Gmailのアプリパスワードでメールを送信する。

    Attributes:
        address:    送信元Gmailアドレス
        default_to: to を省略したときの既定の宛先(リスト)
        verbose:    進捗をprintするか
    """

    def __init__(self, address=None, app_password=None, default_to=None,
                 env_file=None, verbose=True):
        self.load_env(env_file)
        self.address = address or os.environ.get(ENV_ADDRESS)
        # アプリパスワードは4桁区切りで表示されるため、空白を除去して扱う
        password = app_password or os.environ.get(ENV_APP_PASSWORD) or ""
        self.app_password = re.sub(r"\s+", "", password)
        self.default_to = self.normalize_recipients(
            default_to or os.environ.get(ENV_DEFAULT_TO))
        self.verbose = verbose

        if not self.address or not self.app_password:
            raise MailSenderError(
                f"送信元の情報が未設定です。.env に次の2行を記載してください。\n"
                f"  {ENV_ADDRESS}=あなたのアドレス@gmail.com\n"
                f"  {ENV_APP_PASSWORD}=16桁のアプリパスワード\n"
                f"  (アプリパスワードは https://myaccount.google.com/apppasswords で発行。\n"
                f"   通常のGoogleアカウントのパスワードでは送信できません)")
        if "@" not in self.address:
            raise MailSenderError(
                f"{ENV_ADDRESS} がメールアドレスの形式ではありません: {self.address}")
        # プレースホルダのまま/通常のパスワードを入れた場合に、送信前に気づけるようにする
        if not re.fullmatch(r"[A-Za-z0-9]{16}", self.app_password):
            raise MailSenderError(
                f"{ENV_APP_PASSWORD} がアプリパスワードの形式(英数16桁)ではありません。\n"
                f"  https://myaccount.google.com/apppasswords で発行した16桁を\n"
                f"  .env に貼り付けてください(表示どおりスペース入りでも構いません)")

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
                raise MailSenderError(f".envファイルが見つかりません: {env_file}")
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

    @staticmethod
    def normalize_recipients(value):
        """宛先を文字列("a@x, b@y")でもリストでも受け取れるようにして、リストで返す"""
        if not value:
            return []
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        return [addr.strip() for addr in value if addr and addr.strip()]

    # ------------------------------------------------------------------
    # メール組み立て / 送信
    # ------------------------------------------------------------------
    def resolve_recipients(self, to=None, cc=None, bcc=None):
        """To / Cc / Bcc を正規化して (to, cc, bcc) のリストの組で返す"""
        to_list = self.normalize_recipients(to) or self.default_to
        if not to_list:
            raise MailSenderError(
                f"宛先が未指定です。send(to=\"xxx@example.com\") で渡すか、\n"
                f"  .env に {ENV_DEFAULT_TO}=xxx@example.com を記載してください")
        return (to_list,
                self.normalize_recipients(cc),
                self.normalize_recipients(bcc))

    def build_message(self, subject, body, to=None, cc=None, attachments=None,
                      html=None):
        """送信するメールを組み立てて EmailMessage を返す(送信はしない)。

        html を渡すとHTMLメールになる。その場合 body はHTMLを表示できない環境向けの
        代替テキストとして一緒に送られる。
        BccはヘッダーにするとBcc相手が全受信者に見えてしまうため、ここには含めず
        送信時のRCPT TO(send()の宛先リスト)にのみ渡す。
        """
        to_list, cc_list, _ = self.resolve_recipients(to, cc)

        message = EmailMessage()
        message["From"] = self.address
        message["To"] = ", ".join(to_list)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")

        for path in self.normalize_attachments(attachments):
            maintype, subtype = self.guess_type(path)
            message.add_attachment(path.read_bytes(), maintype=maintype,
                                   subtype=subtype, filename=path.name)
        return message

    @staticmethod
    def normalize_attachments(attachments):
        """添付ファイルのパスを検証して Path のリストで返す"""
        if not attachments:
            return []
        if isinstance(attachments, (str, Path)):
            attachments = [attachments]
        paths = []
        for item in attachments:
            path = Path(item)
            if not path.is_absolute() and not path.is_file():
                # カレントに無ければスクリプトと同じディレクトリも探す
                candidate = Path(__file__).resolve().parent / path
                if candidate.is_file():
                    path = candidate
            if not path.is_file():
                raise MailSenderError(f"添付ファイルが見つかりません: {item}")
            paths.append(path)
        return paths

    @staticmethod
    def guess_type(path):
        """拡張子からMIMEタイプを推定する(不明なら汎用のバイナリ扱い)"""
        mime, _ = mimetypes.guess_type(path.name)
        if not mime:
            return "application", "octet-stream"
        maintype, _, subtype = mime.partition("/")
        return maintype, subtype

    def send(self, subject, body, to=None, cc=None, bcc=None, attachments=None,
             html=None):
        """メールを送信し、実際の宛先リストを返す"""
        to_list, cc_list, bcc_list = self.resolve_recipients(to, cc, bcc)
        message = self.build_message(subject, body, to_list, cc_list, attachments,
                                     html)
        recipients = to_list + cc_list + bcc_list

        self._log(f"送信中: {subject}")
        self._log(f"  From: {self.address}")
        self._log(f"  To  : {', '.join(recipients)}")
        for part in message.iter_attachments():
            self._log(f"  添付: {part.get_filename()}")

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                  context=ssl.create_default_context(),
                                  timeout=30) as smtp:
                smtp.login(self.address, self.app_password)
                smtp.send_message(message, from_addr=self.address,
                                  to_addrs=recipients)
        except smtplib.SMTPAuthenticationError as e:
            raise MailSenderError(
                f"Gmailの認証に失敗しました: {e.smtp_code} {e.smtp_error}\n"
                f"  ・{ENV_APP_PASSWORD} が「アプリパスワード」か確認してください\n"
                f"    (通常のアカウントパスワードでは送信できません)\n"
                f"  ・2段階認証プロセスがONになっているか確認してください\n"
                f"  ・{ENV_ADDRESS} がアプリパスワードを発行したアカウントか確認してください") from e
        except smtplib.SMTPRecipientsRefused as e:
            raise MailSenderError(f"宛先が拒否されました: {e.recipients}") from e
        except (smtplib.SMTPException, OSError) as e:
            raise MailSenderError(f"送信に失敗しました: {type(e).__name__}: {e}") from e

        self._log(f"  → 送信完了({len(recipients)}件)")
        return recipients


def main():
    """使い方の例。ここを書き換えて `uv run mail_sender.py` で実行する。"""

    # === 設定(すべてデフォルト値を明記。ここの値を書き換えて使う) =======
    subject = "Long動画アウトライア抽出結果"      # 件名
    body = "自動送信のテストです。\n添付のCSVをご確認ください。"   # 本文
    to = None            # 宛先。None なら .env の MAIL_TO を使う
    cc = None            # CC。"a@x.com, b@y.com" のように複数指定可
    bcc = None           # BCC
    attachments = None   # 添付ファイル。例: ["long_videos_20260812.csv"]

    sender = GmailSender(
        address=None,       # 送信元。None なら .env の GMAIL_ADDRESS
        app_password=None,  # アプリパスワード。None なら .env の GMAIL_APP_PASSWORD
        default_to=None,    # 既定の宛先。None なら .env の MAIL_TO
        env_file=None,      # .envのパス。None ならスクリプト位置→カレントの順に自動探索
        verbose=True,       # False にすると進捗表示を止める
    )
    sender.send(subject, body, to=to, cc=cc, bcc=bcc, attachments=attachments)

    # === CSVを添付して送る例 ===========================================
    # sender.send("Long動画集計", "添付をご確認ください。",
    #             to="someone@example.com",
    #             attachments=["long_videos_20260812.csv"])

    # === 複数宛先に送る例(文字列でもリストでもよい) ===================
    # sender.send("お知らせ", "本文", to=["a@example.com", "b@example.com"])
    # sender.send("お知らせ", "本文", to="a@example.com, b@example.com")

    # === shorts_outlier の結果をそのまま送る例 ==========================
    # from long_video_outlier import ShortsOutlierExtractor
    # from sheet_reader import SheetReader
    # extractor = ShortsOutlierExtractor(days=7, video_type="long")
    # csv_path = extractor.run_many(SheetReader(verbose=False).read_values())
    # sender.send(
    #     subject="今週のLong動画アウトライア",
    #     body=f"対象チャンネルの集計結果です。\n除外: {len(extractor.skipped)}件",
    #     attachments=[csv_path],
    # )


if __name__ == "__main__":
    try:
        main()
    except MailSenderError as e:
        raise SystemExit(f"[エラー] {e}")
