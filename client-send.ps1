<#
.SYNOPSIS
    client-send.ps1 — 自宅PC(Windows)で実行。1往復のチャットを投げて応答を待つ。

.DESCRIPTION
    このコマンドは Relay Agent が結果を返すまでブロックする（最大 CLIENT_TIMEOUT 秒）。
    実行したら、会社Ubuntu 側で agent-once.sh を走らせること。

.EXAMPLE
    $env:TUNNEL_URL   = "https://xxxx.trycloudflare.com"
    $env:CLIENT_TOKEN = "..."
    $env:MODEL        = "<モデル名>"
    .\client-send.ps1 "1+1は？"

.NOTES
    実行できない場合:
      powershell -ExecutionPolicy Bypass -File .\client-send.ps1 "1+1は？"
    Windows PowerShell 5.1 / PowerShell 7 の両方で動作する。
#>

param(
    [Parameter(Position = 0)]
    [string]$Message = "1+1は？ 簡潔に答えて"
)

$ErrorActionPreference = 'Stop'

# 日本語の入出力が化けないようにする（5.1 対策）
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
# 5.1 は既定で TLS1.0 のため明示指定
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
} catch {}

# ---------------------------------------------------------------- 設定
$TunnelUrl   = $env:TUNNEL_URL
$ClientToken = $env:CLIENT_TOKEN
$Model       = $env:MODEL
$MaxTokens   = if ($env:MAX_TOKENS) { [int]$env:MAX_TOKENS } else { 100 }

if (-not $TunnelUrl)   { Write-Host "TUNNEL_URL を設定してください"   -ForegroundColor Red; exit 1 }
if (-not $ClientToken) { Write-Host "CLIENT_TOKEN を設定してください" -ForegroundColor Red; exit 1 }
if (-not $Model)       { Write-Host "MODEL を設定してください"        -ForegroundColor Red; exit 1 }

$TunnelUrl = $TunnelUrl.TrimEnd('/')

# ---------------------------------------------------------------- リクエスト組み立て
$payload = @{
    model      = $Model
    messages   = @(@{ role = 'user'; content = $Message })
    max_tokens = $MaxTokens
}
$json  = $payload | ConvertTo-Json -Depth 5 -Compress
# ConvertTo-Json は日本語を \uXXXX にエスケープするため、そのまま UTF-8 バイト列にする
$bytes = [Text.Encoding]::UTF8.GetBytes($json)

Write-Host "=== 送信 ===============================================" -ForegroundColor Cyan
Write-Host $json
Write-Host ""
Write-Host "-> $TunnelUrl/v1/chat/completions"
Write-Host "   Agent の応答を待機中... 会社Ubuntu で agent-once.sh を実行してください" -ForegroundColor Yellow
Write-Host ""

# ---------------------------------------------------------------- 送信
$sw       = [Diagnostics.Stopwatch]::StartNew()
$status   = 0
$bodyText = $null
$failed   = $false

try {
    $resp = Invoke-WebRequest -Uri "$TunnelUrl/v1/chat/completions" `
        -Method Post `
        -Headers @{ Authorization = "Bearer $ClientToken" } `
        -ContentType 'application/json; charset=utf-8' `
        -Body $bytes `
        -TimeoutSec 130 `
        -UseBasicParsing
    $status = [int]$resp.StatusCode
    # Content-Type に charset が無いと 5.1 が誤デコードするため生バイトから復号
    try {
        $bodyText = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
    } catch {
        $bodyText = $resp.Content
    }
}
catch {
    $failed = $true
    if ($_.Exception.Response) {
        try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
    }
    # PowerShell 7 系
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        $bodyText = $_.ErrorDetails.Message
    }
    # Windows PowerShell 5.1 系
    elseif ($_.Exception.Response -and $_.Exception.Response.PSObject.Methods['GetResponseStream']) {
        try {
            $reader   = New-Object IO.StreamReader(
                            $_.Exception.Response.GetResponseStream(), [Text.Encoding]::UTF8)
            $bodyText = $reader.ReadToEnd()
            $reader.Close()
        } catch {}
    }
    if (-not $bodyText) { $bodyText = $_.Exception.Message }
}
$sw.Stop()
$elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)

# ---------------------------------------------------------------- 結果表示
Write-Host "=== 受信 (HTTP:$status / ${elapsed}s) =====================" -ForegroundColor Cyan

$parsed = $null
try { $parsed = $bodyText | ConvertFrom-Json } catch {}

if ($parsed -and $parsed.PSObject.Properties['detail']) {
    Write-Host ("エラー: " + $parsed.detail) -ForegroundColor Red
}
elseif ($parsed -and $parsed.choices) {
    Write-Host $parsed.choices[0].message.content
    if ($parsed.usage) {
        Write-Host ""
        Write-Host ("--- usage: prompt={0} completion={1}" -f `
            $parsed.usage.prompt_tokens, $parsed.usage.completion_tokens) -ForegroundColor DarkGray
    }
}
else {
    if ($bodyText) { Write-Host $bodyText.Substring(0, [Math]::Min(800, $bodyText.Length)) }
}

Write-Host ""
switch ($status) {
    200     { Write-Host "[OK] 往復成功" -ForegroundColor Green }
    504     { Write-Host "[NG] タイムアウト: Agent が結果を返していません" -ForegroundColor Red }
    502     { Write-Host "[NG] Agent 側でエラー（上の detail を確認）" -ForegroundColor Red }
    401     { Write-Host "[NG] CLIENT_TOKEN が設定されていません" -ForegroundColor Red }
    403     { Write-Host "[NG] CLIENT_TOKEN 不一致" -ForegroundColor Red }
    429     { Write-Host "[NG] キューが詰まっています" -ForegroundColor Red }
    default {
        if ($failed) {
            Write-Host "[NG] 接続失敗。トンネルが落ちていないか確認してください。" -ForegroundColor Red
        }
    }
}
