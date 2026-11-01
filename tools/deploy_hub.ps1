# 把 HomeHub 后台服务（hub/）部署或更新到香橙派：拷代码、证书和令牌，构建镜像，重建容器。
# 数据（洗衣记录 SQLite、App 安装包）在香橙派的 ~/homehub/hub/data，重建不会丢。
# 令牌：每次都从电脑的 .env（HA_TOKEN、HUB_TOKEN）重新生成香橙派上的 hub.env（权限 600）。
# 证书：hub\certs\（tools\gen_secrets.py 生成）拷到 data/tls/。
param(
    [string]$Pi = "CUI@192.168.31.61",
    [string]$PiDir = "/home/CUI/homehub/hub"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$ssh = @("-o", "BatchMode=yes")

$envVals = @{}
Get-Content "$root\.env" | ForEach-Object { if ($_ -match '^(\w+)=(.*)$') { $envVals[$Matches[1]] = $Matches[2] } }
foreach ($k in "HA_TOKEN", "HUB_TOKEN") { if (-not $envVals[$k]) { throw "$k missing in $root\.env (save_ha_token.ps1 / gen_secrets.py)" } }
if (-not (Test-Path "$root\hub\certs\server.crt")) { throw "no cert; run python tools\gen_secrets.py" }

& ssh @ssh $Pi "mkdir -p '$PiDir/src' '$PiDir/data/tls' '$PiDir/data/updates' && rm -rf '$PiDir/src/app'"
& scp @ssh -r "$root\hub\app" "$root\hub\Dockerfile" "$root\hub\requirements.txt" "${Pi}:$PiDir/src/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
& scp @ssh "$root\hub\certs\server.crt" "$root\hub\certs\server.key" "${Pi}:$PiDir/data/tls/"
$tmp = New-TemporaryFile
try {
    [IO.File]::WriteAllText($tmp, "HA_URL=http://127.0.0.1:8123`nHA_TOKEN=$($envVals.HA_TOKEN)`nHUB_TOKEN=$($envVals.HUB_TOKEN)`n")
    & scp @ssh $tmp "${Pi}:$PiDir/hub.env"
} finally { Remove-Item $tmp }
& ssh @ssh $Pi "chmod 600 '$PiDir/hub.env' '$PiDir/data/tls/server.key'"

$run = "cd '$PiDir/src' && docker build -q -t homehub-hub . && " +
       "docker rm -f homehub-hub >/dev/null 2>&1; " +
       "docker run -d --name homehub-hub --restart unless-stopped --network host " +
       "--env-file '$PiDir/hub.env' -v '$PiDir/data:/data' homehub-hub && " +
       "sleep 4 && docker ps --filter name=homehub-hub --format '{{.Names}} {{.Status}}' && docker logs --tail 5 homehub-hub"
& ssh @ssh $Pi $run
if ($LASTEXITCODE -ne 0) { throw "build/run failed" }
Write-Host "HomeHub: https://192.168.31.61:8200"
