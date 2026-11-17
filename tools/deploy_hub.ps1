# 把 HomeHub 后台服务（hub/）部署或更新到香橙派：拷代码、证书和令牌，构建镜像，重建容器。
# 数据（洗衣记录 SQLite、App 安装包）在香橙派 SD 卡的 /mnt/nas-sd/homehub/data，重建不会丢。SD 卡没挂载时容器不会启动（bind mount 要求目录存在）。
# 令牌：每次都从电脑的 .env（HA_TOKEN、HUB_TOKEN）重新生成香橙派上的 hub.env（权限 600）。
# 证书：hub\certs\（tools\gen_secrets.py 生成）拷到 data/tls/。
param(
    [string]$Pi = "",
    [string]$PiDir = "/home/CUI/homehub/hub",
    [string]$DataDir = "/mnt/nas-sd/homehub/data",   # 数据在 SD 卡上；固态盘只放系统
    [switch]$OpsOnly                                  # 只更新运维容器（homehub-ops），不动 hub：主站在 NAS 时也能用
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$PiIp = (& python "$root\tools\mget.py" pi address).Trim()   # 地址表在 hub/app/machines.py（可被 machines.json 覆盖）
if (-not $Pi) { $Pi = "$((& python "$root\tools\mget.py" pi ssh_user).Trim())@$PiIp" }
$ssh = @("-o", "BatchMode=yes")

$envVals = @{}
Get-Content "$root\.env" | ForEach-Object { if ($_ -match '^(\w+)=(.*)$') { $envVals[$Matches[1]] = $Matches[2] } }
foreach ($k in "HA_TOKEN", "HUB_TOKEN", "SENTINEL_TOKEN") { if (-not $envVals[$k]) { throw "$k missing in $root\.env (save_ha_token.ps1 / gen_secrets.py)" } }
# 主站不在香橙派时，这里的 hub 是停着的（哨兵管着）；重建容器会把它启动，变成两个 hub 同时运行，所以先拦住
try {
    $st = & curl.exe -sk -m 6 -H "Authorization: Bearer $($envVals.SENTINEL_TOKEN)" "https://${PiIp}:8210/status" | ConvertFrom-Json
    if ($st.primary -ne "pi" -and -not $OpsOnly) { throw "现在主站在 $($st.primary)，香橙派上的 hub 是停着的；先在手机服务器页把主站切回香橙派再部署" }
} catch { if ($_.Exception.Message -like "现在主站*") { throw } }
if (-not (Test-Path "$root\hub\certs\server.crt")) { throw "no cert; run python tools\gen_secrets.py" }

# 迁移服务正在搬东西时重建运维容器会让任务中断；先看有没有
try {
    $act = & curl.exe -sk -m 6 -H "Authorization: Bearer $($envVals.HUB_TOKEN)" "https://${PiIp}:8220/api/monitor/migrate/active" | ConvertFrom-Json
    if ($act | Where-Object { -not $_.done }) { throw "有迁移任务正在进行，等它结束再部署" }
} catch { if ($_.Exception.Message -like "有迁移任务*") { throw } }
# 数据回传（迁走服务的数据传回 NAS）正在跑时重建运维容器会把它打断
try {
    $reg = & curl.exe -sk -m 8 -H "Authorization: Bearer $($envVals.HUB_TOKEN)" "https://${PiIp}:8220/api/registry" | ConvertFrom-Json
    if ($reg.replica.busy) { throw "数据回传正在进行，等它结束再部署" }
} catch { if ($_.Exception.Message -like "数据回传*") { throw } }
& ssh @ssh $Pi "mkdir -p /home/CUI/apps '$PiDir/src' && rm -rf '$PiDir/src/app' && test -d /mnt/nas-sd/homehub && mkdir -p '$DataDir/tls' '$DataDir/updates'"
& scp @ssh -r "$root\hub\app" "$root\hub\Dockerfile" "$root\hub\requirements.txt" "${Pi}:$PiDir/src/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
& scp @ssh "$root\hub\certs\server.crt" "$root\hub\certs\server.key" "${Pi}:$DataDir/tls/"
$tmp = New-TemporaryFile
try {
    $nas = ""   # 资源监控页（/monitor）用 SSH 读 NAS；没配就只显示香橙派
    if ($envVals.NAS_USER) { $nas = "NAS_USER=$($envVals.NAS_USER)`nNAS_PASSWORD=$($envVals.NAS_PASSWORD)`n" }
    [IO.File]::WriteAllText($tmp, "HA_URL=http://127.0.0.1:8123`nHA_TOKEN=$($envVals.HA_TOKEN)`nHUB_TOKEN=$($envVals.HUB_TOKEN)`nSENTINEL_URL=https://127.0.0.1:8210`nSENTINEL_TOKEN=$($envVals.SENTINEL_TOKEN)`nMACHINES_FILE=/data/machines.json`nJOIN_SECRET=$($envVals.JOIN_SECRET)`nENDPOINTS_TOKEN=$($envVals.ENDPOINTS_TOKEN)`nIDEA_TOKEN=$($envVals.IDEA_TOKEN)`nIDEA_DL=$($envVals.IDEA_DL)`n$nas")
    & scp @ssh $tmp "${Pi}:$PiDir/hub.env"
} finally { Remove-Item $tmp }
& ssh @ssh $Pi "chmod 600 '$PiDir/hub.env' '$DataDir/tls/server.key'"
# 机器地址表的覆盖文件（换了机器/IP 时写在仓库根目录 machines.json）：hub 和 ops 都从 /data/machines.json 读
if (Test-Path "$root\machines.json") {
    $mj = Get-Content "$root\machines.json" -Raw
    foreach ($d in $DataDir, "/mnt/nas-sd/homehub/ops") {      # these folders may belong to root (a container made them): write through docker
        $mj | & ssh @ssh $Pi "docker run --rm -i -v ${d}:/d alpine sh -c 'mkdir -p /d && cat > /d/machines.json'"
    }
}

# 镜像也放进香橙派仓库（会同步到 NAS），NAS 接管时从那里拉
$hubRun = "docker rm -f homehub-hub >/dev/null 2>&1; " +
       "docker run -d --name homehub-hub --restart unless-stopped --network host " +
       "--env-file '$PiDir/hub.env' --mount type=bind,source='$DataDir',target=/data -v /var/run/docker.sock:/var/run/docker.sock -v /home/CUI/apps:/apps --mount type=bind,source=/mnt/nas-sd/registry/data,target=/registry-data,readonly -v /mnt/nas-sd:/mnt/nas-sd -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket homehub-hub && " +
       "sleep 4 && docker ps --filter name=homehub-hub --format '{{.Names}} {{.Status}}' && docker logs --tail 5 homehub-hub"
$build = "cd '$PiDir/src' && docker build -q -t homehub-hub . && docker tag homehub-hub ${PiIp}:5000/homehub-hub:latest && docker push -q ${PiIp}:5000/homehub-hub:latest >/dev/null"
$run = if ($OpsOnly) { $build } else { "$build && $hubRun" }
& ssh @ssh $Pi $run
if ($LASTEXITCODE -ne 0) { throw "build/run failed" }

# 运维容器：同一个镜像，另一条启动命令。迁移、镜像仓库同步、数据回传都在它里面，跟着主站走（哨兵管启停）
$ops = "docker run --rm -v /mnt/nas-sd/homehub:/h alpine mkdir -p /h/ops && docker rm -f homehub-ops >/dev/null 2>&1; " +
       "docker run -d --name homehub-ops --restart unless-stopped --network host --env-file '$PiDir/hub.env' " +
       "--mount type=bind,source=/mnt/nas-sd/homehub/ops,target=/data -v /var/run/docker.sock:/var/run/docker.sock -v /home/CUI/apps:/apps -v /home/CUI/learnsync:/home/CUI/learnsync -v /home/CUI/plan:/home/CUI/plan -v /mnt/nas-sd/homehub/keys:/keys:ro " +
       "--mount type=bind,source=/mnt/nas-sd/registry/data,target=/registry-data,readonly -v /mnt/nas-sd:/mnt/nas-sd homehub-hub python -m app.ops && " +
       "sleep 6 && docker ps --filter name=homehub-ops --format '{{.Names}} {{.Status}}' && docker logs --tail 4 homehub-ops"
& ssh @ssh $Pi $ops
if ($LASTEXITCODE -ne 0) { throw "ops run failed" }
# 主站不在香橙派时，运维服务跟着主站在那台机器上；这里重建出来的是多余的，停掉免得两个同时跑
if ($st -and $st.primary -ne "pi") {
    & ssh @ssh $Pi "docker update --restart no homehub-ops >/dev/null; docker stop homehub-ops >/dev/null; echo '主站在 $($st.primary)：香橙派上的 ops 已更新镜像但保持停止'"
}
Write-Host "HomeHub: https://${PiIp}:8200"

# 新镜像同步到 NAS 的备用仓库：NAS 接管主站时从那里拉 hub 镜像，不同步的话 NAS 起来的是旧版 hub
try {
    $h = @("-H", "Authorization: Bearer $($envVals.HUB_TOKEN)")
    & curl.exe -sk -m 10 -X POST @h "https://${PiIp}:8200/api/registry/mirror" | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        $r = & curl.exe -sk -m 10 @h "https://${PiIp}:8200/api/registry" | ConvertFrom-Json
        if (-not $r.mirror.busy) { break }
    }
    Write-Host "镜像已同步到 NAS 备用仓库"
} catch { Write-Warning "同步到 NAS 备用仓库失败，手机服务器页的镜像仓库卡片点同步" }
