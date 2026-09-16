# Extract Trend Videos Long

YouTubeチャンネルの Long 動画を分析し、チャンネル登録者数に対して視聴数が突出した動画を「よい動画」として抽出し、CSVとメールレポートとして出力するためのスクリプト群です。

主な流れは次の通りです。

1. GoogleスプレッドシートからチャンネルURLを取得
2. 各チャンネルの登録者数と Long 動画を YouTube Data API で取得
3. `視聴数 >= max(登録者数, 100) × 10` を満たす動画を判定
4. すでにメール通知した動画を SQLite で判別し、レポートから除外
5. 結果を CSV に出力し、Gmail でレポート送信

---

## 「よい動画」の定義

**直近180日(半年)以内に公開され、次の式を満たす動画**を「よい動画」とします。

```
視聴数 >= max(登録者数, 100) × 10
```

| 記号 | 設定名 | 既定値 | 意味 |
| --- | --- | --- | --- |
| 180 | `days` | 180 | 対象にする直近日数 |
| 100 | `subscriber_floor` | 100 | 倍率の分母の下限 |
| 10 | `threshold` | 10.0 | 分母に対する倍率の閾値 |

分母を登録者数にすることで、登録者規模の異なるチャンネルを同じ閾値で横並びに比較できます。
そのうえで分母に下限(100)を設けているため、実際の挙動は次の2通りになります。

- **登録者100人以上のチャンネル** … 視聴数が登録者数の10倍以上
- **登録者100人未満のチャンネル** … 視聴数が1,000回以上(`100 × 10`)

つまり登録者99人・50万回再生のような動画も、チャンネルごと切り捨てられることなく拾えます。
同時に分母が1桁まで小さくならないので、極小チャンネルの倍率が青天井に跳ね上がって
ランキング上位を占めてしまうことも防げます。

補足:

- 登録者数を非公開にしているチャンネルは分母を決められないため除外します
  (理由付きでスキップ一覧に載ります)。
- YouTube Data API が返す登録者数は上位3桁に丸められた概数です(例: 1,234人 → 1,230)。

---

## 通知の重複除外 (SQLite)

同じ動画を何度もメールで受け取らないよう、`video_store.py` が
**メールで通知した動画だけ**を `trend_videos.db` (SQLite) に記録します。
毎回の実行は次の順番で動きます。

1. **判定** … 直近180日の動画を、**毎回すべて** YouTube API の最新の視聴数で評価し直す。
2. **絞り込み** … 基準を満たす動画のうち、DBに無いもの(=未通知)だけをメールに載せる。
3. **記録** … メール送信に成功した動画だけをDBに追加する。

判定に過去の記録を一切使わない(毎回まっさらに評価し直す)のがポイントです。
そのため **投稿直後は基準に届かなかった動画が、あとから視聴数が伸びて基準を超えた場合、
その時点の実行で通知されます**。一方、一度通知した動画はその後どれだけ伸びても
再通知されません。DBが覚えているのは「もう通知したか」だけです。

未通知の動画を貯めないので、DBの行数は**通知した本数までしか増えません**。

```
テーブル          列
----------------- ------------------------------------------------------
notified_videos   video_id, channel_id, channel_url, title, published_at,
                  views, subscribers, multiplier, notified_at
```

`views` / `subscribers` / `multiplier` は**通知した時点の値**で、以後は更新しません
(判定には使わず、あとから「どの水準で通知したか」を見返すための記録です)。

DBの中身をざっと確認するには:

```powershell
uv run video_store.py
```

注意点:

- **`dry_run=True` のときはDBに記録しません。** プレビューを見ただけで
  通知が消費されてしまわないようにするためです。
- DBを消すと通知履歴も消えるため、次回の実行で過去の動画が再度通知されます。
  `trend_videos.db` は `.gitignore` に入れていないので、コミットすれば履歴が残ります。
- `use_db=False` にするとDBを使わず、毎回すべてのよい動画を通知します。
- 未通知も貯めていた旧バージョンのDBを開いた場合は、通知済みの行だけを
  自動で引き継ぎ、残りは削除します。

---

## できること

- チャンネル一覧をスプレッドシートから読み取る
- Long動画の投稿日・視聴数・登録者数比を計算する
- 閾値と最小視聴数でノイズ除去を行う
- 「よい動画」判定(`outlier` 列)を含む CSV を出力する
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
    days=180,
    threshold=10.0,
    subscriber_floor=100,
    include_all=False,
    max_videos=300,
    max_channels=None,
    attach_csv=True,
    use_db=True,
    db_path=None,
    dry_run=False,
    verbose=True,
)
```

- `days`: 対象期間の直近日数(180=半年)
- `threshold`: 分母に対する倍率閾値
- `subscriber_floor`: 倍率の分母の下限(小規模チャンネルの足切りラインを兼ねる)
- `max_videos`: 1チャンネルあたり遡る最大本数
- `max_channels`: 動作確認用の上限
- `use_db` / `db_path`: 通知済みの動画を除外するSQLite(`None` で `trend_videos.db`)
- `include_all=True`: 通知済み・基準未満の動画も表に載せる(`outlier` 列で判別)
- `dry_run=True`: 送信せずに `report_preview.html` を生成(通知済みの印は付かない)

---

## 送信前の確認

メールを送らずに本文だけ見たい場合は `--dry-run` を付けて実行します。

```powershell
uv run main.py --dry-run
```

`report_preview.html` が生成され、ブラウザで本文を確認できます。
このとき**通知済みの記録は付かない**ので、何度でも試せます。

---

## タスクスケジューラへの登録 (毎日自動実行)

タスクスケジューラは PATH も作業ディレクトリも文字コードも引き継ぎません。
それらを固めたラッパー `run_daily.cmd` を用意してあるので、**タスクからは
これだけを呼びます**。設定をGUIに散らかさず、リポジトリ側に残せます。

`run_daily.cmd` がやっていること:

- `PYTHONUTF8=1` を立てる … ログをファイルに書くとき、既定のcp932だと動画名の
  絵文字などで `UnicodeEncodeError` になり処理ごと落ちるため
- スクリプトのあるフォルダへ `cd` … CSVとDBの出力先を固定する
- `uv.exe` を絶対パスで呼ぶ … タスクスケジューラのPATHには載っていない
- `logs\YYYY-MM-DD.log` に標準出力とエラーを追記し、30日より古いログを削除
- `uv run main.py` の終了コードをそのまま返す … タスクの「前回の実行結果」に出る
  (`0` が成功)

### 1. まず手動で確認する

```powershell
cd C:\Users\fumi9\.cursor\extract_trend_videos_long
.\run_daily.cmd --dry-run
type logs\*.log
```

ログの最後が `exit=0` になり、`report_preview.html` ができていればOKです。
`.env` の値やサービスアカウントのJSONキーが揃っていないとここで失敗します。

### 2. タスクを登録する

PowerShellで実行します(管理者権限は不要)。`-At` に好きな時刻を指定してください。

```powershell
$dir = "C:\Users\fumi9\.cursor\extract_trend_videos_long"

$action   = New-ScheduledTaskAction -Execute "$dir\run_daily.cmd" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -Daily -At 08:00
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "YouTube-LongVideoReport" `
    -Description "Long動画のよい動画レポートを毎日メール送信する" `
    -Action $action -Trigger $trigger -Settings $settings
```

指定しているオプションの意味:

| オプション | 効果 |
| --- | --- |
| `-StartWhenAvailable` | PCが落ちていて実行時刻を逃しても、起動後にすぐ実行する |
| `-MultipleInstances IgnoreNew` | 前回の実行が終わっていなければ新しく起動しない(二重送信とDB競合を防ぐ) |
| `-ExecutionTimeLimit 1時間` | ハングしたまま居座らないよう打ち切る |
| `-AllowStartIfOnBatteries` ほか | ノートPCでバッテリー駆動でも実行する |

### 3. 動作確認

```powershell
Start-ScheduledTask   -TaskName "YouTube-LongVideoReport"
Get-ScheduledTaskInfo -TaskName "YouTube-LongVideoReport" |
    Select-Object LastRunTime, LastTaskResult
```

`LastTaskResult` が `0` なら成功です。詳細は `logs\` の当日のログを見てください。

### ログオフ中も実行したい場合

既定では「ログオンしているときだけ」実行されます。ログオフ中も動かすなら、
`Register-ScheduledTask` に `-User $env:USERNAME -LogonType S4U` を足します。
S4U ならパスワードの保存が不要で、YouTube/Gmail への通信(HTTPS)は問題なく通ります。

### タスクが1時間経っても終わらない / 途中で強制終了される

ログ(`logs\YYYY-MM-DD.log`)に `exit=` の行が無く、更新時刻が開始のちょうど1時間後なら、
`-ExecutionTimeLimit` で強制終了されています。2026-09-14〜16 に実際に起きた原因は
**回線のIPv6が半端に有効になったこと**でした。

- PCにISPのIPv6アドレスが配られ、DNSも `googleapis.com` のIPv6アドレスを返すようになった
- しかしIPv6では外に出られず、Pythonは「IPv6アドレスを順に試して全滅 → IPv4」と動く
- 結果、APIリクエスト1回ごとに約17秒待たされ、1実行(約2,000リクエスト)が終わらなくなった

対処として `long_video_outlier.prefer_ipv4()` を実行の入口で呼び、名前解決の結果を
IPv4優先に並べ替えています(IPv6は捨てずに後ろへ回すだけ)。同じ症状が出たら、
まず次のコマンドでIPv6の接続状態を確認してください。

```powershell
Get-Process python | ForEach-Object { Get-NetTCPConnection -OwningProcess $_.Id } |
    Select-Object RemoteAddress, RemotePort, State
```

`RemoteAddress` が `2001:...` で `State` が `SynSent` のまま動かない接続があればこの症状です。

あわせて `run_daily.cmd` では `PYTHONUNBUFFERED=1` を立てています。これが無いと
出力が8KBずつしかファイルに書かれず、強制終了時にログが実際の停止位置よりずっと
手前で切れてしまい、原因の切り分けができません。

### 注意点

- **`run_daily.cmd` はASCIIのみ・CRLF改行で書いてあります。** cmd.exe はバッチ
  ファイルをコンソールのコードページで解釈するため、日本語コメントやLF改行を
  入れると解析が壊れます(日本語の説明はこのREADMEに置いています)。
- 毎日実行してもAPIクォータには余裕があります。1チャンネルあたり約12ユニット
  (チャンネル解決1 + 登録者数1 + 動画一覧と詳細を50件ずつ)なので、198チャンネルで
  1日あたり2,400ユニット程度。既定の上限は10,000ユニット/日です。
- `long_videos_YYYYMMDD.csv` は実行ごとに増えていきます。不要なら
  `attach_csv=False` にしてください。

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
├── run_daily.cmd
├── video_store.py
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

既存のテストでは次を確認しています。

- `tests/test_long_video_support.py` … `視聴数 >= max(登録者数, 100) × 10` の境界
  (ちょうど10倍/1,000回、1回足りないケース)、既定値、半年の期間フィルタ、
  登録者0人・非公開の扱い
- `tests/test_video_store.py` … 通知した動画だけがDBに残ること、通知済み動画の
  再通知抑止、あとから基準に届いた動画が次回通知されること、DB再オープン後も
  履歴が残ること、旧スキーマからの移行

---

## 例：簡単な使い方

```python
from main import TrendReporter

TrendReporter(
    days=180,
    threshold=10.0,
    subscriber_floor=100,
    include_all=False,
    dry_run=True,
    max_channels=5,
).run()
```

このように使うと、まず動作確認用の少数チャンネルで結果を確認できます。
