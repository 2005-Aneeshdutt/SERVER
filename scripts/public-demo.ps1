# Share the running stack publicly through a Cloudflare quick tunnel (free, no account).
#   powershell -ExecutionPolicy Bypass -File scripts\public-demo.ps1
# Prints an https://<random>.trycloudflare.com URL. It changes on every start, and only works
# while this machine is on. Stop with: Stop-Process -Name cloudflared
#
# Before sharing:
# - Demo keys must be free-tier only (the default DEMO_MODELS: Groq and Gemini). Anyone can mint
#   one, and the budget is $0.
# - The admin API needs ADMIN_TOKEN; make it a long random value.
# - Get cloudflared (pin a version, check its SHA-256 against the release notes) from
#   https://github.com/cloudflare/cloudflared/releases and set $Cloudflared below.
param([string]$Cloudflared = "$env:USERPROFILE\tools\cloudflared.exe")

$log = Join-Path $env:TEMP "cloudflared-demo.log"
Remove-Item $log -ErrorAction SilentlyContinue
$args = "tunnel", "--url", "http://localhost:8000", "--no-autoupdate", "--logfile", $log
Start-Process -FilePath $Cloudflared -ArgumentList $args -WindowStyle Hidden | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep 2
    $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($m) { "Public demo: $($m.Matches[0].Value)"; exit 0 }
}
"No URL yet; see $log"
exit 1
