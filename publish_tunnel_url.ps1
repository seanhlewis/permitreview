# Reads the current quick-tunnel URL from logs\tunnel.err.log (or takes -Url),
# writes it into static\api-config.js, rebuilds the GitHub Pages files,
# commits, and pushes so https://seanhlewis.github.io/permitreview/ points
# at the live backend.
#
#   .\publish_tunnel_url.ps1                 # auto-detect URL from the tunnel log
#   .\publish_tunnel_url.ps1 -Url https://x.trycloudflare.com
#   .\publish_tunnel_url.ps1 -NoPush         # update + build only
param(
    [string]$Url,
    [switch]$NoPush
)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not $Url) {
    $log = Get-Content (Join-Path $root 'logs\tunnel.err.log') -Raw -ErrorAction Stop
    if ($log -notmatch 'https://[a-z0-9-]+\.trycloudflare\.com') {
        Write-Error 'No tunnel URL in logs\tunnel.err.log. Run .\start_backend_and_tunnel.ps1 first.'
        exit 1
    }
    $Url = $Matches[0]
}
$Url = $Url.TrimEnd('/')

$cfg = Join-Path $root 'static\api-config.js'
$text = Get-Content $cfg -Raw
$updated = $text -replace 'https://[a-z0-9-]+\.trycloudflare\.com', $Url
Set-Content -Path $cfg -Value $updated -NoNewline -Encoding utf8
Write-Host "api-config.js -> $Url"

foreach ($i in 1..6) {
    try {
        $h = (Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 15).Content
        Write-Host "tunnel health: $h"
        break
    } catch {
        Write-Host "waiting for tunnel ($i): $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}

py -3 build_github_pages.py
if ($NoPush) { Write-Host 'Skipping git push (-NoPush).'; exit 0 }
git add -A
git commit -m "Update Cloudflare tunnel endpoint to $Url" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push origin main
Write-Host 'Published. Check https://seanhlewis.github.io/permitreview/api/health/ in about a minute.'
