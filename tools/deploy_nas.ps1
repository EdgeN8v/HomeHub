# 部署或更新 NAS 上的公网转发容器 homehub-forward（nas\nginx.conf）：公网 8095 → 香橙派 8200。
param(
    # user@host of the NAS; kept out of the repo: put NAS_TARGET=user@192.168.x.x in .env
    [string]$Nas = "",
    [int]$NasPort = 10000,
    [string]$NasDir = "/data_s001/docker_appstore/homehub-forward",
    [string]$Key = "$env:USERPROFILE\.ssh\id_ed25519_nas"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $Nas) {
    $line = Get-Content "$root\.env" -ErrorAction SilentlyContinue | Where-Object { $_ -match '^NAS_TARGET=' } | Select-Object -First 1
    if ($line) { $Nas = $line -replace '^NAS_TARGET=', '' }
}
if (-not $Nas) { throw "give -Nas user@host, or set NAS_TARGET in .env" }
$ssh = @("-p", $NasPort, "-o", "BatchMode=yes", "-i", $Key)
$scp = @("-O", "-P", $NasPort, "-o", "BatchMode=yes", "-i", $Key)

& ssh @ssh $Nas "sudo mkdir -p '$NasDir' && sudo chown `$(id -u):`$(id -g) '$NasDir'"
& scp @scp "$root\nas\nginx.conf" "${Nas}:$NasDir/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

# nginx:alpine 已在 NAS 本地（claude-remote-forward 在用），不用从 Docker Hub 拉
$run = "sudo docker rm -f homehub-forward >/dev/null 2>&1; " +
       "sudo docker run -d --name homehub-forward --restart unless-stopped -p 8095:8095 --memory 32m " +
       "-v $NasDir/nginx.conf:/etc/nginx/nginx.conf:ro nginx:alpine && sleep 2 && " +
       "sudo docker ps --filter name=homehub-forward --format '{{.Names}} {{.Status}} {{.Ports}}'"
& ssh @ssh $Nas $run
if ($LASTEXITCODE -ne 0) { throw "docker run failed" }
