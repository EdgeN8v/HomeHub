# 标准部署：把一个服务构建成镜像，推进香橙派的镜像仓库，在香橙派上拉起，清掉旧容器和旧版本镜像。
# 之后手机上 HomeHub 服务器页的镜像仓库卡片会提示“有新镜像”，点一下“同步”就复制到 NAS 的备用仓库。
#
# 例：
#   .\ship_service.ps1 -Name gongzhang -Context D:\Github\Gongzhang\server -Tag v12 `
#       -RunArgs "-p 8100:8080 -v /home/CUI/apps/gongzhang/data:/data" -Health http://192.168.31.61:8100/health
# 数据放 /home/CUI/apps/<名字>/ 下（HomeHub 才能迁移它）。-Context 里要有 Dockerfile。
param(
    [Parameter(Mandatory)] [string]$Name,
    [Parameter(Mandatory)] [string]$Context,
    [Parameter(Mandatory)] [string]$Tag,
    [string]$RunArgs = "",
    [string]$Service = "",       # 地址表里的名字：给了它，HomeHub 会自动把这个服务登记进地址表，并随服务搬家自动调整顺序
    [string]$CheckPath = "/",    # App 用来确认地址能通的路径
    [string]$Expect = "",        # 该路径返回内容里应包含的文字（可空）
    [int]$ServicePort = 0,       # 对外端口；不给就取容器发布的第一个端口
    [string]$Health = "",
    [string]$Pi = "",
    [string]$Registry = ""
)
$ErrorActionPreference = "Stop"
$mget = Join-Path $PSScriptRoot "mget.py"
$PiIp = (& python $mget pi address).Trim()
if (-not $Pi) { $Pi = "$((& python $mget pi ssh_user).Trim())@$PiIp" }
if (-not $Registry) { $Registry = "${PiIp}:$((& python $mget pi registry_port).Trim())" }
if (-not (Test-Path (Join-Path $Context "Dockerfile"))) { throw "$Context 里没有 Dockerfile" }
$image = "$Registry/${Name}:$Tag"
$dir = "~/build/$Name"
$ssh = @("-o", "BatchMode=yes")
$labels = ""
if ($Service) {
    $labels = "--label homehub.service=$Service --label homehub.check=$CheckPath --label homehub.expect=$Expect"
    if ($ServicePort) { $labels += " --label homehub.port=$ServicePort" }
}

Write-Host "1/5 拷贝代码到香橙派"
& ssh @ssh $Pi "rm -rf $dir && mkdir -p $dir"
& tar -C $Context --exclude=.git --exclude=node_modules -cf - . | & ssh @ssh $Pi "tar -xf - -C $dir"
if ($LASTEXITCODE -ne 0) { throw "拷贝失败" }

Write-Host "2/5 构建并推进仓库 $image"
& ssh @ssh $Pi "cd $dir && docker build -q -t $image . && docker push $image | tail -1" 2>&1 | Where-Object { $_ -notmatch "DEPRECATED|BuildKit|legacy|docs.docker" }
if ($LASTEXITCODE -ne 0) { throw "构建或推送失败" }

Write-Host "3/5 在香橙派上拉起新容器（旧的先改名留作回滚）"
$run = "docker rm -f ${Name}-prev >/dev/null 2>&1; " +
       "if docker inspect $Name >/dev/null 2>&1; then docker stop $Name >/dev/null && docker rename $Name ${Name}-prev; fi; " +
       "docker run -d --name $Name --restart unless-stopped $labels $RunArgs $image"
& ssh @ssh $Pi $run
if ($LASTEXITCODE -ne 0) {
    & ssh @ssh $Pi "docker rm -f $Name >/dev/null 2>&1; docker rename ${Name}-prev $Name 2>/dev/null && docker start $Name"
    throw "新容器没起来，已恢复旧的"
}

Write-Host "4/5 检查"
Start-Sleep -Seconds 5
$ok = $true
if ($Health) {
    try { [void](Invoke-WebRequest -UseBasicParsing -SkipCertificateCheck:$false -TimeoutSec 8 $Health) } catch { $ok = $false }
}
$state = (& ssh @ssh $Pi "docker inspect -f '{{.State.Running}}' $Name").Trim()
if (-not $ok -or $state -ne "true") {
    & ssh @ssh $Pi "docker rm -f $Name >/dev/null 2>&1; docker rename ${Name}-prev $Name 2>/dev/null && docker start $Name"
    throw "新版本检查没通过，已恢复旧的"
}

Write-Host "5/5 清掉旧容器和旧版本镜像"
& ssh @ssh $Pi "docker rm -f ${Name}-prev >/dev/null 2>&1; docker images --format '{{.Repository}}:{{.Tag}}' | grep '^$Registry/${Name}:' | grep -v ':$Tag$' | xargs -r docker rmi >/dev/null 2>&1; true"
Write-Host "完成：$Name 已在香橙派运行。打开手机 HomeHub 服务器页，镜像仓库卡片点“同步”，NAS 备用仓库就会更新。"
