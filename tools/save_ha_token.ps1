# One-time: save a Home Assistant long-lived access token to .env (gitignored) so scripts can call the HA API.
# Create the token in HA: click your name (bottom left) -> Security -> Long-lived access tokens -> Create token.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$secure = Read-Host "Paste the HA token (input is hidden), then press Enter" -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if ($token.Length -lt 100) { throw "That does not look like an HA token (too short). Nothing saved." }
$lines = @("HA_URL=http://192.168.31.61:8123", "HA_TOKEN=$token")
[IO.File]::WriteAllLines("$root\.env", $lines)
try {
    $r = Invoke-RestMethod "http://192.168.31.61:8123/api/" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 10
    Write-Host "Saved to $root\.env and verified: $($r.message)" -ForegroundColor Green
} catch {
    Write-Host "Saved to $root\.env, but the test call failed: $($_.Exception.Message)" -ForegroundColor Red
}
