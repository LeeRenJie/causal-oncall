# Pre-warm the Causal On-Call Cloud Run instance before a demo recording.
#
# Hits GET /warmup every 30 seconds for 5 minutes, then exits. The
# /warmup endpoint is lightweight by design (no LLM, no MCP, no Mongo)
# so the warmup itself never becomes the bottleneck.
#
# Usage:
#   .\scripts\prewarm.ps1                         # defaults to live URL
#   .\scripts\prewarm.ps1 https://other.run.app   # override base URL
#
# Cancel anytime with Ctrl+C; partial warmup is still useful.

param(
    [string]$BaseUrl = "https://causal-oncall-856589756095.us-central1.run.app"
)

$TotalSeconds = 300        # 5 minutes
$IntervalSeconds = 30

Write-Host "Pre-warming $BaseUrl/warmup for ${TotalSeconds}s every ${IntervalSeconds}s."

$elapsed = 0
while ($elapsed -lt $TotalSeconds) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/warmup" -Method GET -UseBasicParsing -TimeoutSec 30
        $body = $resp.Content
        Write-Host "[$stamp] http=$($resp.StatusCode) body=$body"
    } catch {
        Write-Host "[$stamp] http=err body=$($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
    $elapsed += $IntervalSeconds
}

Write-Host "Pre-warm complete. Container should be hot for the next recording window."
