# Restarts the staticver backend and a Cloudflare quick tunnel as detached
# background processes (they survive closing this terminal).
# Logs: logs\backend.*.log and logs\tunnel.*.log
# Afterwards run .\publish_tunnel_url.ps1 to push the new URL to GitHub Pages.
param(
    [int]$Port = 8873
)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

# Stop any previous instances started by this script.
Get-CimInstance Win32_Process -Filter "name='cloudflared.exe'" |
    Where-Object { $_.CommandLine -match "127\.0\.0\.1:$Port" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if ($py) { $pyArgs = @('-3', 'server.py') } else { $py = (Get-Command python).Source; $pyArgs = @('server.py') }
$env:STATICVER_PORT = "$Port"
$srv = Start-Process -FilePath $py -ArgumentList $pyArgs -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'logs\backend.out.log') `
    -RedirectStandardError (Join-Path $root 'logs\backend.err.log') -PassThru
Write-Host "backend pid $($srv.Id) on port $Port"

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = 'C:\Users\sean\AppData\Local\Programs\cloudflared\bin\cloudflared.exe' }
$tunnelLog = Join-Path $root 'logs\tunnel.err.log'
# Truncate the old log so we never read a stale URL.
Set-Content -Path $tunnelLog -Value '' -NoNewline
$tun = Start-Process -FilePath $cf -ArgumentList 'tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate' `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'logs\tunnel.out.log') `
    -RedirectStandardError $tunnelLog -PassThru
Write-Host "tunnel pid $($tun.Id)"

$url = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    $log = Get-Content $tunnelLog -Raw -ErrorAction SilentlyContinue
    if ($log -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $url = $Matches[0]; break }
}
if (-not $url) { Write-Error 'Tunnel URL not found in logs\tunnel.err.log'; exit 1 }
Write-Host "tunnel url: $url"
try {
    $health = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5).Content
    Write-Host "local health: $health"
} catch { Write-Warning "local health check failed: $($_.Exception.Message)" }
Write-Host 'Next: .\publish_tunnel_url.ps1'
