# 发布 Idea 手机 App 的新版本：编译 release 包，连同 version.json 传到香橙派 SD 卡 /mnt/nas-sd/homehub/data/idea_updates/。
# 手机上的 Idea 打开时检查到新版本会提示更新（在外面走 pi / nas 域名也行）。
# 第一次安装用的下载链接：<外网地址>/idea-dl/<.env 里的 IDEA_DL>/Idea.apk（外网地址见 .env 的 IDEA_WAN）
# 发布前：改 idea_app\app\build.gradle.kts 的 versionCode（+1）和 versionName，-Notes 写这一版改了什么。
param(
    [string]$Notes = "",
    [switch]$NoBuild
)
$ErrorActionPreference = "Stop"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Pi = "$((& python "$root\tools\mget.py" pi ssh_user).Trim())@$((& python "$root\tools\mget.py" pi address).Trim())"
$PiDir = "/mnt/nas-sd/homehub/data/idea_updates"
$ssh = @("-o", "BatchMode=yes")

if (-not $NoBuild) {
    Push-Location "$root\idea_app"
    try { & .\gradlew.bat assembleRelease --console=plain -q; if ($LASTEXITCODE -ne 0) { throw "build failed" } }
    finally { Pop-Location }
}
$gradle = Get-Content "$root\idea_app\app\build.gradle.kts" -Raw
$code = [int]([regex]::Match($gradle, 'versionCode\s*=\s*(\d+)').Groups[1].Value)
$name = [regex]::Match($gradle, 'versionName\s*=\s*"([^"]+)"').Groups[1].Value
$apk = "$root\idea_app\app\build\outputs\apk\release\app-release.apk"
$apkName = "Idea-v$name.apk"
$json = [ordered]@{ versionCode = $code; versionName = $name; apk = $apkName; size = (Get-Item $apk).Length; notes = @($Notes -split "\|" | Where-Object { $_ }) } | ConvertTo-Json -Depth 3
$tmp = New-TemporaryFile
try {
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    # the folder may belong to root (the hub container made /data): write through docker, like deploy_hub does
    & ssh @ssh $Pi "docker run --rm -v /mnt/nas-sd/homehub/data:/d alpine mkdir -p /d/idea_updates && docker run --rm -v /mnt/nas-sd/homehub/data:/d alpine chmod 777 /d/idea_updates"
    & scp @ssh $apk "${Pi}:$PiDir/$apkName"
    if ($LASTEXITCODE -ne 0) { throw "scp apk failed" }
    & scp @ssh $tmp "${Pi}:$PiDir/version.json"      # last, so a phone never sees a version whose apk isn't there yet
} finally { Remove-Item $tmp }
& ssh @ssh $Pi "cd '$PiDir' && ls -t Idea-v*.apk | tail -n +3 | xargs -r rm -f; ls -la"
Write-Host "已发布 Idea v$name (versionCode $code)"
