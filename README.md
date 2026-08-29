# Extract Trend Videos Long

YouTubeチャンネルの Long 動画を分析し、過去の平均視聴数に対して突出した動画を抽出して、CSVとメールレポートとして出力するためのスクリプト群です。

主な流れは次の通りです。

1. GoogleスプレッドシートからチャンネルURLを取得
2. 各チャンネルの Long 動画を YouTube Data API で取得
3. 直近N本の中央値と比較して outlier を判定
4. 結果を CSV に出力し、Gmail でレポート送信

---

## できること

- チャンネル一覧をスプレッドシートから読み取る
- Long動画の投稿日・視聴数・中央値比を計算する
- 閾値と最小視聴数でノイズ除去を行う
- `outlier` 判定を含む CSV を出力する
- HTMLメール本文を生成して送信する
- `dry_run=True` でメール送信せずにプレビューHTMLを出力する

---

## 必要なもの

- Python 3.9 以上
- YouTube Data API v3 の API キー
- Google Sheets の参照対象と認証情報
- Gmail の送信用アカウントとアプリパスワード

推奨:

- `uv` (依存関係管理・実行用)

---

## セットアップ

1. 依存関係をインストールする

```powershell
uv sync
```

または

```powershell
pip install -r requirements.txt
```

2. `.env.example` を `.env` にコピーする

```powershell
Copy-Item .env.example .env
```

3. `.env` を自分の環境に合わせて編集する

```env
YOUTUBE_API_KEY=...
SPREADSHEET_URL=...
GOOGLE_SERVICE_ACCOUNT_FILE=spreadsheet_key/service_account.json
GMAIL_ADDRESS=...
GMAIL_APP_PASSWORD=...
MAIL_TO=...
```

必要な設定の詳細は `.env.example` を参照してください。

### Google連携のポイント

- `SPREADSHEET_URL`: 読み取り元のスプレッドシートURL
- `GOOGLE_SERVICE_ACCOUNT_FILE`: サービスアカウントのJSONファイル
  - JSONの `client_email` をスプレッドシートに共有する必要があります
- `YOUTUBE_API_KEY`: YouTube Data API 用
- `GMAIL_APP_PASSWORD`: Gmailのアプリパスワード

---

## 実行方法

メインの実行は `main.py` です。

```powershell
uv run main.py
```

または

```powershell
python main.py
```

### 実行前の設定変更

`main.py` の `TrendReporter(...)` 部分で、解析条件や送信先を調整します。

```python
reporter = TrendReporter(
    days=3,
    threshold=3.0,
    baseline=30,
    include_all=False,
    min_views=100,
    max_channels=None,
    attach_csv=True,
    dry_run=False,
    verbose=True,
)
```

- `days`: 対象期間の直近日数
- `threshold`: 中央値に対する倍率閾値
- `baseline`: 中央値計算に使う動画数
- `min_views`: 低視聴数のノイズ除去
- `max_channels`: 動作確認用の上限
- `dry_run=True`: 送信せずに `report_preview.html` を生成

---

## 送信前の確認

メール送信をしたくない場合は、`dry_run=True` にして実行します。

```python
TrendReporter(dry_run=True, max_channels=3).run()
```

これで `report_preview.html` が生成され、ブラウザで本文を確認できます。

---

## 出力ファイル

- `report_preview.html`: dry run 時のメール本文プレビュー
- CSV出力: 解析結果をCSVファイルとして保存

---

## ファイル構成

```text
.
├── .env.example
├── .env
├── main.py
├── long_video_outlier.py
├── sheet_reader.py
├── mail_sender.py
├── tests/
│   └── test_long_video_support.py
├── pyproject.toml
├── requirements.txt
├── report_preview.html
└── spreadsheet_key/
    └── service_account.json
```

---

## 注意事項

- `.env` には秘密情報が入るため、Git管理対象にしないでください
- スプレッドシートの共有権限がないと読み取りに失敗します
- Gmail は通常のパスワードでは送信できず、アプリパスワードが必要です
- YouTube Data API の利用枠とレート制限に注意してください

---

## テスト

```powershell
pytest
```

既存のテストでは、Long動画のデフォルト設定やゼロ中央値・最小視聴数フィルタの挙動を確認しています。

---

## 例：簡単な使い方

```python
from main import TrendReporter

TrendReporter(
    days=7,
    threshold=3.0,
    baseline=30,
    include_all=False,
    min_views=100,
    dry_run=True,
    max_channels=5,
).run()
```

このように使うと、まず動作確認用の少数チャンネルで結果を確認できます。
