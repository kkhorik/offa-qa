# PAPP Relay

クライアントからのチャットリクエストを受け付け、社内側のRelay Agentへジョブとして渡す小規模な中継サーバーです。FastAPI製のサーバー、ブラウザ用チャット画面、コマンドライン送信スクリプトを含みます。

社内側のAgentが外部サーバーへ接続してジョブを取得し、処理結果を返す構成です。社内へのインバウンド接続を必要としません。サーバー自身はモデルの推論を実行しません。

## 構成

```text
クライアント（ブラウザ／送信スクリプト）
    │ POST /v1/chat/completions → 結果を待機
    ▼
Relay Server（外部公開する中継サーバー）
    ▲
    │ Agentからのロングポーリング・結果送信
Relay Agent（社内側）→ 推論バックエンド
```

| ファイル | 役割 |
| --- | --- |
| `relay_server.py` | ジョブの受付・配信・結果返却、状態確認API、ブラウザ用チャット画面 |
| `client-send.ps1` | Windows PowerShell／PowerShellから単発のチャットを送信 |
| `client-send.sh` | Bashから単発のチャットを送信 |

**Relay Agentと推論バックエンドはこのフォルダに含まれていません。** 送信スクリプト内に出てくる`agent-once.sh`も別途用意する必要があります。Agentが結果を返さない場合、チャットはタイムアウトします。

## 必要な環境

- サーバー：Python 3.10以上、FastAPI 0.110以上、Uvicorn 0.29以上、Pydantic 2.6以上
- Windowsクライアント：Windows PowerShell 5.1またはPowerShell 7（スクリプトの想定環境）
- Bashクライアント：Bash、`python3`、`curl`
- ブラウザクライアント：JavaScriptとlocalStorageが利用できるブラウザ

Pythonの依存関係は`relay_server.py`先頭のスクリプトメタデータにも記載されています。以下の手順では`pip`でインストールします。

## サーバーの起動

このフォルダで仮想環境を作成し、依存関係をインストールします。

### Windows／PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "fastapi>=0.110" "uvicorn[standard]>=0.29" "pydantic>=2.6"

$env:AGENT_TOKEN = "replace-with-agent-secret"
$env:CLIENT_TOKEN = "replace-with-client-secret"
.\.venv\Scripts\python.exe relay_server.py
```

### Linux／Bash

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.29' 'pydantic>=2.6'

export AGENT_TOKEN='replace-with-agent-secret'
export CLIENT_TOKEN='replace-with-client-secret'
.venv/bin/python relay_server.py
```

例のトークンは、十分に長いランダムな値に置き換えてください。クライアント用とAgent用には別々の値を設定します。どちらかが未設定または空文字の場合、サーバーは起動時にエラーになります。

直接実行した場合の待受先は`http://127.0.0.1:8080`です。ブラウザでこのURLを開くとチャット画面が表示されます。

外部インターフェースで待ち受ける場合は、例えば以下で起動します。

```bash
.venv/bin/python -m uvicorn relay_server:app --host 0.0.0.0 --port 8080 --workers 1
```

**ワーカー数は必ず1にしてください。** ジョブとキューはプロセス内メモリに保存されるため、複数ワーカーや複数レプリカでは状態を共有できません。インターネットに公開する際は、HTTPSのリバースプロキシやトンネルなどを用意し、クライアントとAgentの両方から到達できるURLを使います。トンネルの起動設定はこのフォルダにはありません。

## 環境変数

### サーバー

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `AGENT_TOKEN` | なし（必須） | Agent APIのBearerトークン |
| `CLIENT_TOKEN` | なし（必須） | クライアントAPIのBearerトークン |
| `CLIENT_TIMEOUT` | `180` | クライアントリクエストが結果を待つ秒数 |
| `JOB_TTL` | `300` | ジョブ作成からメモリ上の保持期限までの秒数。30秒ごとに期限切れジョブを削除 |
| `MAX_QUEUE` | `32` | 未配信ジョブのキュー上限。配信済みジョブはこの件数に含まれない |
| `MAX_BODY_CHARS` | `20000` | 検証後のペイロードを`str()`で表した文字数の上限。HTTP本文のバイト数ではない |
| `HOST` | `127.0.0.1` | `python relay_server.py`で起動した場合の待受アドレス |
| `PORT` | `8080` | `python relay_server.py`で起動した場合のポート |

UvicornのCLIで起動する場合、待受先は`--host`と`--port`で指定します。

### 送信スクリプト

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `TUNNEL_URL` | なし（必須） | Relay ServerのベースURL。ローカルURLも指定可能 |
| `CLIENT_TOKEN` | なし（必須） | サーバーと一致するクライアントトークン |
| `MODEL` | なし（必須） | Agent／推論バックエンドが扱うモデル名 |
| `MAX_TOKENS` | `100` | リクエストの`max_tokens`。1以上の整数を指定 |

両スクリプトのHTTP待機時間は**130秒固定**です。サーバーの既定値180秒より先に接続が切れる場合があります。運用時は処理時間に合わせてスクリプト内の`-TimeoutSec 130`／`-m 130`やサーバーの`CLIENT_TIMEOUT`を調整してください。プロキシやトンネルの待機時間にも整合させます。

## クライアントの使い方

### ブラウザ

1. Relay ServerのURLを開きます。
2. 接続設定に`CLIENT_TOKEN`とモデル名を入力して保存します。
3. メッセージを入力し、「送信」を押します。

画面は20秒ごとにAgentの状態を確認します。Agentが最後にジョブ取得または結果送信を行ってから60秒未満なら「接続中」と表示します。これは推論バックエンドの正常性を保証するものではありません。

トークンとモデル名はブラウザのlocalStorageに保存されます。共有端末での利用は避けてください。会話履歴はページ内メモリにのみ保持され、再読み込みで失われます。送信する履歴は最新12メッセージです。

### Windows／PowerShell

```powershell
$env:TUNNEL_URL = "http://127.0.0.1:8080"
$env:CLIENT_TOKEN = "replace-with-client-secret"
$env:MODEL = "your-model-name"
$env:MAX_TOKENS = "100"
.\client-send.ps1 "1+1は？ 簡潔に答えて"
```

実行ポリシーでスクリプトがブロックされる場合、スクリプト内では次の実行方法が案内されています。

```powershell
powershell -ExecutionPolicy Bypass -File .\client-send.ps1 "1+1は？"
```

### Linux／Bash

```bash
export TUNNEL_URL='http://127.0.0.1:8080'
export CLIENT_TOKEN='replace-with-client-secret'
export MODEL='your-model-name'
export MAX_TOKENS=100
bash client-send.sh '1+1は？ 簡潔に答えて'
```

Bash版のURLには末尾の`/`を付けないでください。リクエストとレスポンスは`/tmp/papp-relay-manual/request.json`と`response.json`に保存され、次回実行時に上書きされます。同時実行でも同じファイルを使用します。

両スクリプトとも1回の実行につき1件のユーザーメッセージを送り、回答・使用トークン数（応答に含まれる場合）・HTTPステータス・経過時間を表示します。引数省略時のメッセージは「1+1は？ 簡潔に答えて」です。

## API

認証が必要なAPIには`Authorization: Bearer <token>`を付けます。

| メソッド | パス | 認証 | 動作 |
| --- | --- | --- | --- |
| `GET` | `/` | 不要 | ブラウザ用チャット画面 |
| `GET` | `/healthz` | 不要 | `ok`、キュー件数`queued`、保持ジョブ件数`jobs`を返す |
| `GET` | `/v1/status` | CLIENT_TOKEN | `agent_online`、`agent_last_seen_sec`、`queued`を返す |
| `POST` | `/v1/chat/completions` | CLIENT_TOKEN | チャットをジョブ化し、結果が届くまで待機 |
| `GET` | `/agent/jobs?wait=25` | AGENT_TOKEN | ジョブを1件取得。待機秒数は0〜55、既定値25。取得できなければ204 |
| `POST` | `/agent/jobs/{job_id}/result` | AGENT_TOKEN | Agentの処理結果またはエラーを登録 |

### チャットリクエスト

OpenAI形式に合わせた非ストリーミングのエンドポイントです。対応フィールドは`model`（必須文字列）、`messages`（必須リスト）、`temperature`、`max_tokens`（1以上）、`top_p`です。`stream`など、それ以外のフィールドは転送されません。モデル名の解決と推論はAgent側で行います。

```json
{
  "model": "your-model-name",
  "messages": [{"role": "user", "content": "1+1は？"}],
  "max_tokens": 100
}
```

成功時はAgentが登録した`result`をJSONとして返します。ブラウザと送信スクリプトは、回答が`choices[0].message.content`にある応答形式を想定しています。

### Agent側の処理

Agentは以下の流れを実装する必要があります。

1. `AGENT_TOKEN`を使って`GET /agent/jobs?wait=25`を繰り返します。
2. 204なら再度ポーリングし、200なら`id`と`payload`を取得します。
3. `payload`を推論バックエンドへ渡します。
4. 同じ`id`に対して結果またはエラーをPOSTします。

ジョブ取得時の応答例：

```json
{
  "id": "job-id",
  "payload": {
    "model": "your-model-name",
    "messages": [{"role": "user", "content": "1+1は？"}],
    "max_tokens": 100
  }
}
```

成功時の`POST /agent/jobs/job-id/result`本文例：

```json
{
  "result": {
    "choices": [{"message": {"role": "assistant", "content": "2です。"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3}
  }
}
```

失敗時の本文例：

```json
{"error": "推論バックエンドへの接続に失敗しました"}
```

結果登録の応答は`{"ok": true}`です。既に確定したジョブへの再送は、結果を上書きせず注記付きで成功を返します。保持期限切れなどでジョブが存在しない場合は404です。

## エラーと確認事項

| 状態 | 主な原因・確認事項 |
| --- | --- |
| 起動時の`RuntimeError` | `AGENT_TOKEN`と`CLIENT_TOKEN`を設定しているか |
| 401 | Bearer形式の認証ヘッダーがあるか |
| 403 | 対象APIのトークンがサーバー設定と一致しているか |
| 413 | ペイロードの文字数が`MAX_BODY_CHARS`を超えていないか |
| 422 | 必須フィールド・型・`max_tokens`・`wait`の範囲が正しいか |
| 429 | 未配信ジョブが`MAX_QUEUE`に達していないか。Agentが取得しているか |
| 502 | Agentが返した`error`の内容を確認 |
| 504 | `CLIENT_TIMEOUT`以内に結果が返っているか。Agentとバックエンドの稼働を確認 |
| 接続失敗・クライアント側タイムアウト | URL、サーバー、トンネル、通信経路、130秒の待機上限を確認 |

認証なしでサーバーの応答を確認する例：

```bash
curl http://127.0.0.1:8080/healthz
```

`/healthz`が成功しても、Agentが接続しているとは限りません。Agentの状態は認証付きの`/v1/status`で確認します。

## 現在の実装上の制約

- ジョブは永続化されず、サーバー再起動ですべて失われます。
- 配信済みジョブの自動再配信・リトライはありません。Agentが処理途中で停止すると、クライアントはタイムアウトします。
- TTLの削除処理はジョブ辞書から削除するだけで、キュー内のIDは即時削除しません。そのため、キュー件数には期限切れやタイムアウト済みのIDが残る場合があります。Agentがこれらを取得した場合も204を返します。
- `JOB_TTL`は作成時刻から計算されます。結果を返す前に削除されないよう、処理時間と`CLIENT_TIMEOUT`を考慮して設定してください。
- クライアントがタイムアウトした後でも、保持中のジョブへの結果登録は受け付けますが、終了したリクエストへ回答を届ける機能はありません。
- ストリーミング、ユーザーごとの認証、会話履歴の永続化は実装されていません。
- HTTPS終端・トンネル・Agent・推論バックエンドの設定や、自動テストはこのフォルダには含まれていません。

サーバーログにはジョブID、モデル名、キュー件数、処理時間、認証失敗などが出力されます。送信スクリプトはリクエスト本文を画面に表示し、Bash版は本文と応答を一時ファイルにも保存するため、利用端末でのデータの扱いに注意してください。
