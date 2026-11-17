# 开发闯关地图的电脑端收集器：登记成"登录时自动启动"的计划任务，并马上启动。
# 它在后台跑 collect.py run：每 15 秒看有没有"一键推送"任务，每 10 分钟扫一遍 D:\Github 的仓库，每小时整理新对话。
# 日志：%LOCALAPPDATA%\Journey\collector.log；缓存：%LOCALAPPDATA%\Journey\levels.json
# 卸载：Unregister-ScheduledTask -TaskName "HomeHub Journey Collector" -Confirm:$false
$ErrorActionPreference = "Stop"
$name = "HomeHub Journey Collector"
$script = Join-Path $PSScriptRoot "collect.py"
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`" run" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "已登记并启动：$name（$pyw $script run）"
