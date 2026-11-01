# Generate a strong random password on this PC, put it on the clipboard, then run passwd on the Orange Pi.
# The password is shown only in this terminal; it is never written to a file.
param(
    [string]$Target = "CUI@192.168.31.61",
    [int]$Length = 20
)
$ErrorActionPreference = "Stop"

# No look-alike characters (0 O o 1 l I) and no symbols that are awkward to type or quote.
$lower = "abcdefghijkmnpqrstuvwxyz"
$upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
$digit = "23456789"
$symbol = "!@#%^*-_=+?"
$all = $lower + $upper + $digit + $symbol

$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
function Pick([string]$set) {
    $b = New-Object byte[] 4
    do { $rng.GetBytes($b); $n = [BitConverter]::ToUInt32($b, 0) } while ($n -ge [uint32]::MaxValue - ([uint32]::MaxValue % $set.Length))
    $set[[int]($n % $set.Length)]
}
do {
    $pw = -join (1..$Length | ForEach-Object { Pick $all })
} until ($pw -cmatch "[a-z]" -and $pw -cmatch "[A-Z]" -and $pw -match "[2-9]" -and $pw -match "[!@#%^*=+?_-]")

Set-Clipboard -Value $pw
Write-Host ""
Write-Host "New password (already copied to clipboard):" -ForegroundColor Cyan
Write-Host "    $pw" -ForegroundColor Yellow
Write-Host "Save it in your password manager NOW, then continue." -ForegroundColor Cyan
Write-Host "passwd will ask: current password -> new password (right-click to paste) -> new password again."
Write-Host ""
Read-Host "Press Enter when saved"
ssh -t $Target passwd
if ($LASTEXITCODE -eq 0) { Write-Host "Password changed." -ForegroundColor Green } else { Write-Host "passwd failed (exit $LASTEXITCODE). Nothing changed; run this script again." -ForegroundColor Red }
Set-Clipboard -Value " "
Write-Host "Clipboard cleared."
