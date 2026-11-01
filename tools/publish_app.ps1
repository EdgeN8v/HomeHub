# 发布手机 App 的新版本：编译 release 包，连同 version.json 传到香橙派 ~/homehub/hub/data/updates/。
# 手机上的 App 打开时会检查到新版本，点"更新"即可，在外面走公网也行。
# 发布前：改 android\app\build.gradle.kts 的 versionCode（+1）和 versionName，在 CHANGELOG.md 顶部写这一版的说明。
param(
    [string]$Pi = "CUI@192.168.31.61",
    [string]$PiDir = "/home/CUI/homehub/hub/data/updates",
    [switch]$NoBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$ssh = @("-o", "BatchMode=yes")

if (-not $NoBuild) {
    Push-Location "$root\android"
    try { & .\gradlew.bat assembleRelease --console=plain -q; if ($LASTEXITCODE -ne 0) { throw "build failed" } }
    finally { Pop-Location }
}
$gradle = Get-Content "$root\android\app\build.gradle.kts" -Raw
$code = [int]([regex]::Match($gradle, 'versionCode\s*=\s*(\d+)').Groups[1].Value)
$name = [regex]::Match($gradle, 'versionName\s*=\s*"([^"]+)"').Groups[1].Value
$apk = "$root\android\app\build\outputs\apk\release\app-release.apk"
$apkName = "HomeHub-v$name.apk"

$notes = @(); $inFirst = $false
foreach ($line in Get-Content "$root\CHANGELOG.md" -Encoding UTF8) {
    if ($line -match '^## ') { if ($inFirst) { break }; $inFirst = $true; continue }
    if ($inFirst -and $line -match '^- (.+)$') { $notes += $Matches[1] }
}
$json = [ordered]@{ versionCode = $code; versionName = $name; apk = $apkName; size = (Get-Item $apk).Length; notes = $notes } | ConvertTo-Json -Depth 3
$tmp = New-TemporaryFile
try {
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    & ssh @ssh $Pi "mkdir -p '$PiDir'"
    & scp @ssh $apk "${Pi}:$PiDir/$apkName"
    if ($LASTEXITCODE -ne 0) { throw "scp apk failed" }
    # version.json last, so a phone never sees a version whose apk isn't there yet
    & scp @ssh $tmp "${Pi}:$PiDir/version.json"
} finally { Remove-Item $tmp }
& ssh @ssh $Pi "cd '$PiDir' && ls -t HomeHub-v*.apk | tail -n +3 | xargs -r rm -f; ls"
Write-Host "已发布 v$name (versionCode $code)"
