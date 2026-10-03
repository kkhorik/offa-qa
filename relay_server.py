# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.110",
#   "uvicorn[standard]>=0.29",
#   "pydantic>=2.6",
# ]
# ///
"""
PAPP Relay Server (minimum viable)

インターネット側（VPS / Cloud Run / Render など）に置くジョブブローカー。
- スマホ（クライアント）から /v1/chat/completions を受け取り、キューに積む
- 社内の Relay Agent が /agent/jobs をロングポーリングして取りに来る
- Agent が /agent/jobs/{id}/result で結果を返すと、待機中のクライアントに返す

Inbound 接続は一切社内に入らない。社内 → Internet の HTTPS Outbound のみ。

起動:
    uvicorn relay_server:app --host 0.0.0.0 --port 8080 --workers 1
    ※ジョブキューはプロセス内メモリ。必ず --workers 1 で動かすこと。
"""

import asyncio
import logging
import os
import secrets
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- config

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN", "")
CLIENT_TIMEOUT = float(os.environ.get("CLIENT_TIMEOUT", "180"))   # 秒
JOB_TTL = float(os.environ.get("JOB_TTL", "300"))                 # 秒
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "32"))
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "20000"))

if not AGENT_TOKEN or not CLIENT_TOKEN:
    raise RuntimeError("AGENT_TOKEN と CLIENT_TOKEN を環境変数で設定してください")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relay-server")

# ---------------------------------------------------------------- state


class Job:
    __slots__ = ("id", "payload", "created_at", "dispatched_at", "result",
                 "error", "event", "status")

    def __init__(self, payload: Dict[str, Any]):
        self.id = uuid.uuid4().hex
        self.payload = payload
        self.created_at = time.time()
        self.dispatched_at: Optional[float] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.event = asyncio.Event()
        self.status = "queued"


JOBS: Dict[str, Job] = {}
QUEUE: "asyncio.Queue[str]" = asyncio.Queue()
AGENT_LAST_SEEN: float = 0.0

app = FastAPI(title="PAPP Relay Server", docs_url=None, redoc_url=None)


def _auth(header: Optional[str], expected: str, role: str) -> None:
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer トークンがありません")
    token = header[7:]
    if not secrets.compare_digest(token, expected):
        log.warning("認証失敗 role=%s", role)
        raise HTTPException(status_code=403, detail="トークンが一致しません")


# ---------------------------------------------------------------- client API


class ChatRequest(BaseModel):
    model: str
    messages: list
    temperature: Optional[float] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)
    top_p: Optional[float] = None


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatRequest,
    authorization: Optional[str] = Header(default=None),
):
    """スマホ側が叩く OpenAI 互換エンドポイント（非ストリーミング）。"""
    _auth(authorization, CLIENT_TOKEN, "client")

    payload = req.model_dump(exclude_none=True)
    if len(str(payload)) > MAX_BODY_CHARS:
        raise HTTPException(status_code=413, detail="リクエストが大きすぎます")
    if QUEUE.qsize() >= MAX_QUEUE:
        raise HTTPException(status_code=429, detail="キューが詰まっています。時間をおいて再送してください")

    job = Job(payload)
    JOBS[job.id] = job
    await QUEUE.put(job.id)
    log.info("ジョブ受付 id=%s model=%s queue=%d", job.id, req.model, QUEUE.qsize())

    try:
        await asyncio.wait_for(job.event.wait(), timeout=CLIENT_TIMEOUT)
    except asyncio.TimeoutError:
        job.status = "timeout"
        log.warning("クライアントタイムアウト id=%s", job.id)
        raise HTTPException(
            status_code=504,
            detail="Relay Agent から時間内に応答がありません。Agent の稼働状況を確認してください",
        )

    if job.error:
        raise HTTPException(status_code=502, detail=f"Agent エラー: {job.error}")

    elapsed = time.time() - job.created_at
    log.info("ジョブ完了 id=%s %.1fs", job.id, elapsed)
    return JSONResponse(job.result)


@app.get("/v1/status")
async def status(authorization: Optional[str] = Header(default=None)):
    _auth(authorization, CLIENT_TOKEN, "client")
    age = time.time() - AGENT_LAST_SEEN if AGENT_LAST_SEEN else None
    return {
        "agent_online": age is not None and age < 60,
        "agent_last_seen_sec": round(age, 1) if age is not None else None,
        "queued": QUEUE.qsize(),
    }


# ---------------------------------------------------------------- agent API


class ResultIn(BaseModel):
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@app.get("/agent/jobs")
async def get_job(
    wait: int = Query(default=25, ge=0, le=55),
    authorization: Optional[str] = Header(default=None),
):
    """Agent がロングポーリングでジョブを1件取得する。無ければ 204。"""
    global AGENT_LAST_SEEN
    _auth(authorization, AGENT_TOKEN, "agent")
    AGENT_LAST_SEEN = time.time()

    try:
        job_id = await asyncio.wait_for(QUEUE.get(), timeout=wait)
    except asyncio.TimeoutError:
        return Response(status_code=204)

    job = JOBS.get(job_id)
    if job is None or job.status != "queued":
        return Response(status_code=204)

    job.status = "dispatched"
    job.dispatched_at = time.time()
    AGENT_LAST_SEEN = time.time()
    return {"id": job.id, "payload": job.payload}


@app.post("/agent/jobs/{job_id}/result")
async def post_result(
    job_id: str,
    body: ResultIn,
    authorization: Optional[str] = Header(default=None),
):
    global AGENT_LAST_SEEN
    _auth(authorization, AGENT_TOKEN, "agent")
    AGENT_LAST_SEEN = time.time()

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません（TTL 切れの可能性）")
    if job.event.is_set():
        return {"ok": True, "note": "結果は既に確定済みです"}

    job.result = body.result
    job.error = body.error
    job.status = "done" if body.error is None else "failed"
    job.event.set()
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "queued": QUEUE.qsize(), "jobs": len(JOBS)}


# ---------------------------------------------------------------- housekeeping


async def _sweeper():
    while True:
        await asyncio.sleep(30)
        now = time.time()
        stale = [k for k, j in JOBS.items() if now - j.created_at > JOB_TTL]
        for k in stale:
            JOBS.pop(k, None)
        if stale:
            log.info("期限切れジョブを削除 %d件", len(stale))


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_sweeper())
    log.info("Relay Server 起動 client_timeout=%.0fs job_ttl=%.0fs", CLIENT_TIMEOUT, JOB_TTL)


# ---------------------------------------------------------------- mobile UI

INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>PAPP Relay</title>
<style>
  :root{
    --ink:#e8ecf1; --dim:#7b8896; --line:#1e2733;
    --bg:#0b0f14; --panel:#111820; --wire:#2a3644;
    --live:#4fd1a5; --down:#e0605e; --me:#1b2836;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:var(--bg); color:var(--ink);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    display:flex; flex-direction:column;
    padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }
  header{
    padding:12px 16px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:10px;
  }
  .wire{
    font:11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    letter-spacing:.08em; color:var(--dim); white-space:nowrap;
    display:flex; align-items:center; gap:6px;
  }
  .dot{width:7px;height:7px;border-radius:50%;background:var(--down);flex:none}
  .dot.on{background:var(--live);box-shadow:0 0 0 3px rgba(79,209,165,.15)}
  .spacer{flex:1}
  button.link{background:none;border:0;color:var(--dim);font-size:12px;padding:4px}
  main{flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:12px}
  .msg{max-width:88%; padding:10px 13px; border-radius:14px; white-space:pre-wrap; word-break:break-word}
  .msg.user{align-self:flex-end; background:var(--me); border-bottom-right-radius:4px}
  .msg.bot{align-self:flex-start; background:var(--panel); border:1px solid var(--line); border-bottom-left-radius:4px}
  .msg.err{align-self:stretch; background:none; border:1px dashed var(--down); color:var(--down); font-size:13px}
  .meta{
    font:10px/1 ui-monospace,Menlo,monospace; color:var(--dim);
    letter-spacing:.06em; margin-top:8px
  }
  footer{border-top:1px solid var(--line); padding:10px 12px; display:flex; gap:8px; align-items:flex-end}
  textarea{
    flex:1; resize:none; background:var(--panel); color:var(--ink);
    border:1px solid var(--wire); border-radius:12px; padding:10px 12px;
    font:15px/1.5 inherit; max-height:120px;
  }
  textarea:focus,button:focus-visible{outline:2px solid var(--live); outline-offset:1px}
  .send{
    background:var(--live); color:#04140e; border:0; border-radius:12px;
    padding:11px 16px; font-weight:700; font-size:14px;
  }
  .send:disabled{opacity:.4}
  dialog{
    background:var(--panel); color:var(--ink); border:1px solid var(--wire);
    border-radius:14px; padding:20px; width:min(92vw,380px);
  }
  dialog h2{margin:0 0 4px; font-size:16px}
  dialog p{margin:0 0 14px; font-size:13px; color:var(--dim)}
  dialog input{
    width:100%; background:var(--bg); color:var(--ink); border:1px solid var(--wire);
    border-radius:10px; padding:10px; font:14px/1.4 ui-monospace,Menlo,monospace; margin-bottom:10px;
  }
  dialog .send{width:100%}
</style>
</head>
<body>
<header>
  <strong style="font-size:14px">PAPP Relay</strong>
  <span class="wire"><span class="dot" id="dot"></span><span id="wire">確認中</span></span>
  <span class="spacer"></span>
  <button class="link" id="cfgBtn">設定</button>
</header>

<main id="log"></main>

<footer>
  <textarea id="input" rows="1" placeholder="メッセージを入力" enterkeyhint="send"></textarea>
  <button class="send" id="send">送信</button>
</footer>

<dialog id="cfg">
  <h2>接続設定</h2>
  <p>この端末のブラウザにのみ保存されます。共有端末では使わないでください。</p>
  <input id="tok" type="password" placeholder="クライアントトークン" autocomplete="off">
  <input id="mdl" type="text" placeholder="モデル名（例: gemma4-31b）" autocomplete="off">
  <button class="send" id="saveCfg">保存</button>
</dialog>

<script>
const $ = (id) => document.getElementById(id);
const logEl = $("log");
let history = [];

function cfg(){ return { tok: localStorage.getItem("relay_tok")||"", mdl: localStorage.getItem("relay_mdl")||"" }; }

function add(text, cls, meta){
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  if(meta){
    const m = document.createElement("div");
    m.className = "meta"; m.textContent = meta;
    d.appendChild(m);
  }
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
  return d;
}

async function ping(){
  const {tok} = cfg();
  if(!tok){ $("wire").textContent = "トークン未設定"; return; }
  try{
    const r = await fetch("/v1/status", {headers:{Authorization:"Bearer "+tok}});
    if(!r.ok) throw new Error(r.status);
    const s = await r.json();
    $("dot").classList.toggle("on", s.agent_online);
    $("wire").textContent = s.agent_online
      ? "AGENT 接続中 · " + s.agent_last_seen_sec + "s"
      : "AGENT 応答なし";
  }catch(e){
    $("dot").classList.remove("on");
    $("wire").textContent = "サーバ到達不可";
  }
}

async function send(){
  const {tok, mdl} = cfg();
  const text = $("input").value.trim();
  if(!text) return;
  if(!tok || !mdl){ $("cfg").showModal(); return; }

  $("input").value = ""; $("input").style.height = "auto";
  add(text, "user");
  history.push({role:"user", content:text});

  $("send").disabled = true;
  const t0 = performance.now();
  const placeholder = add("…", "bot");

  try{
    const r = await fetch("/v1/chat/completions", {
      method:"POST",
      headers:{"Content-Type":"application/json", Authorization:"Bearer "+tok},
      body: JSON.stringify({model: mdl, messages: history.slice(-12)})
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
    const msg = data.choices?.[0]?.message?.content ?? JSON.stringify(data);
    const ms = Math.round(performance.now() - t0);
    const usage = data.usage ? ` · ${data.usage.completion_tokens} tok` : "";
    placeholder.remove();
    add(msg, "bot", `${(ms/1000).toFixed(1)}s 往復${usage}`);
    history.push({role:"assistant", content:msg});
  }catch(e){
    placeholder.remove();
    add(String(e.message||e), "err");
  }finally{
    $("send").disabled = false;
    ping();
  }
}

$("send").onclick = send;
$("input").addEventListener("input", (e)=>{
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
});
$("cfgBtn").onclick = ()=>{
  const c = cfg(); $("tok").value = c.tok; $("mdl").value = c.mdl; $("cfg").showModal();
};
$("saveCfg").onclick = ()=>{
  localStorage.setItem("relay_tok", $("tok").value.trim());
  localStorage.setItem("relay_mdl", $("mdl").value.trim());
  $("cfg").close(); ping();
};

const c0 = cfg();
if(!c0.tok || !c0.mdl) $("cfg").showModal();
ping();
setInterval(ping, 20000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn

    # 単一プロセスで起動する（ジョブキューがプロセス内メモリのため workers 増設は不可）
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )
