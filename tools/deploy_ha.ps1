# 把 Home Assistant 部署或更新到香橙派（Orange Pi 5），重建容器。HA 的配置和数据在香橙派的 config/ 里，重建不会丢。
# HA 是设备层：所有品牌的设备都接到这里，我们自己的 App / 语音 / 自动化只通过它的 API 控制设备。
# 用 host 网络：米家等设备的局域网发现（mDNS/SSDP）和局域网直连控制需要它。网页端口 8123。
# 香橙派用户 CUI 在 docker 组里，不需要 sudo；登录用电脑的 ~/.ssh/id_ed25519（tools\add_ssh_key.ps1 加的）。
param(
    [string]$Pi = "CUI@192.168.31.61",
    [string]$PiDir = "/home/CUI/homehub/homeassistant",
    [string]$Image = "ghcr.nju.edu.cn/home-assistant/home-assistant:stable"
)
$ErrorActionPreference = "Stop"
$ssh = @("-o", "BatchMode=yes")

$run = "mkdir -p '$PiDir/config' && " +
       "docker rm -f homeassistant >/dev/null 2>&1; " +
       "docker run -d --name homeassistant --restart unless-stopped --network host " +
       "-e TZ=Asia/Shanghai -v '$PiDir/config:/config' -v /etc/localtime:/etc/localtime:ro $Image && " +
       "sleep 3 && docker ps --filter name=homeassistant --format '{{.Names}} {{.Status}}'"
& ssh @ssh $Pi $run
if ($LASTEXITCODE -ne 0) { throw "docker run failed" }
Write-Host "HA: http://192.168.31.61:8123"
