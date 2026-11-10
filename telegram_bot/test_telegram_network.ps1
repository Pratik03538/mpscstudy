$ErrorActionPreference = "Continue"

Write-Host "=== DNS lookup ===" -ForegroundColor Cyan
try {
    Resolve-DnsName api.telegram.org -ErrorAction Stop |
        Select-Object Name, Type, IPAddress |
        Format-Table -AutoSize
} catch {
    Write-Host "DNS lookup failed: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== TCP port 443 ===" -ForegroundColor Cyan
Test-NetConnection api.telegram.org -Port 443 -InformationLevel Detailed

Write-Host ""
Write-Host "=== HTTPS/TLS handshake (no bot token used) ===" -ForegroundColor Cyan
curl.exe -I --max-time 20 https://api.telegram.org

Write-Host ""
Write-Host "A returned HTTP status (even 404/405 at the API root) means DNS/TCP/TLS connected."
Write-Host "A timeout or connection failure means local network, ISP, firewall, antivirus, or proxy needs checking."
