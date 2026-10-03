#!/usr/bin/env bash
# client-send.sh — 自宅PCで実行。1往復のチャットを投げて応答を待つ。
#
#   export TUNNEL_URL=https://xxxx.trycloudflare.com
#   export CLIENT_TOKEN=...
#   export MODEL=<モデル名>
#   ./client-send.sh "1+1は？"
#
# このコマンドは Relay Agent が結果を返すまでブロックする（最大 CLIENT_TIMEOUT 秒）。
# 実行したら、会社Ubuntu 側で agent-once.sh を走らせること。

set -uo pipefail

: "${TUNNEL_URL:?TUNNEL_URL を設定してください}"
: "${CLIENT_TOKEN:?CLIENT_TOKEN を設定してください}"
: "${MODEL:?MODEL を設定してください}"

MSG="${1:-1+1は？ 簡潔に答えて}"
MAXTOK="${MAX_TOKENS:-100}"
WORK=/tmp/papp-relay-manual
mkdir -p "$WORK"

python3 - "$MODEL" "$MSG" "$MAXTOK" > "$WORK/request.json" <<'PY'
import json, sys
model, msg, maxtok = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(json.dumps({"model": model,
                  "messages": [{"role": "user", "content": msg}],
                  "max_tokens": maxtok}, ensure_ascii=False))
PY

echo "=== 送信 ==============================================="
cat "$WORK/request.json"
echo
echo "→ $TUNNEL_URL/v1/chat/completions"
echo "   Agent の応答を待機中... 会社Ubuntu で agent-once.sh を実行してください"
echo

START=$(date +%s)
HTTP=$(curl -sS -m 130 -o "$WORK/response.json" -w "%{http_code}" \
  -X POST "$TUNNEL_URL/v1/chat/completions" \
  -H "Authorization: Bearer $CLIENT_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @"$WORK/request.json")
RC=$?
ELAPSED=$(( $(date +%s) - START ))

echo "=== 受信 (HTTP:$HTTP / ${ELAPSED}s) ====================="
if [ "$RC" -ne 0 ]; then
  echo "curl 失敗 (exit=$RC)。トンネルが落ちていないか確認してください。"
  exit 1
fi

python3 - "$WORK/response.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(open(sys.argv[1]).read()[:500]); sys.exit()
if "detail" in d:
    print("エラー:", d["detail"]); sys.exit()
try:
    print(d["choices"][0]["message"]["content"])
    if d.get("usage"):
        u = d["usage"]
        print("\n--- usage: prompt=%s completion=%s" % (u.get("prompt_tokens"), u.get("completion_tokens")))
except Exception:
    print(json.dumps(d, ensure_ascii=False, indent=2)[:800])
PY

case "$HTTP" in
  200) echo; echo "✅ 往復成功" ;;
  504) echo; echo "❌ タイムアウト: Agent が結果を返していません" ;;
  502) echo; echo "❌ Agent 側でエラー（上の detail を確認）" ;;
  401|403) echo; echo "❌ CLIENT_TOKEN 不一致" ;;
esac
