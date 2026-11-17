# 把香橙派系统镜像写进 SD 卡（会清空整张卡）。必须在[以管理员身份运行]的 PowerShell 里执行：
#   powershell -ExecutionPolicy Bypass -File D:\Github\HomeHub\tools\flash_sd.ps1
# 安全做法：只列出 USB / SD 的可移动磁盘（不含系统盘），让你输入磁盘号和 YES 才动手；写完逐字节读回校验。
param(
    [string]$Image = "E:\Baidu\Orangepizero3_1.0.2_ubuntu_jammy_server_linux6.1.31\Orangepizero3_1.0.2_ubuntu_jammy_server_linux6.1.31.img"
)
$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请用[以管理员身份运行]的 PowerShell 执行（开始菜单搜 PowerShell，右键，以管理员身份运行）"
}
if (-not (Test-Path $Image)) { throw "找不到镜像：$Image" }
$size = (Get-Item $Image).Length
$cands = Get-Disk | Where-Object { $_.BusType -in "USB", "SD", "MMC" -and -not $_.IsSystem -and -not $_.IsBoot -and $_.Size -le 300GB -and $_.Size -ge 4GB -and $_.OperationalStatus -eq "Online" }
if (-not $cands) { throw "没有找到可写的 SD 卡 / U 盘。把读卡器插好，卡放进去，再试" }
Write-Host "可选的磁盘（只有这些会被考虑）：" -ForegroundColor Yellow
$cands | Format-Table Number, FriendlyName, BusType, @{n = "GB"; e = { [math]::Round($_.Size / 1GB, 1) } }, PartitionStyle -AutoSize | Out-String | Write-Host
$n = Read-Host "输入要写入的磁盘号（上表的 Number），这张卡上的所有内容都会被清除"
$d = $cands | Where-Object { $_.Number -eq [int]$n }
if (-not $d) { throw "磁盘号不在上面的列表里，已取消" }
if ((Read-Host "确认把 $($d.FriendlyName)（$([math]::Round($d.Size/1GB,1)) GB）整张清空并写入系统？输入 YES") -ne "YES") { throw "已取消" }
Write-Host "清空磁盘…"
Clear-Disk -Number $d.Number -RemoveData -RemoveOEM -Confirm:$false
Start-Sleep 2
$path = "\\.\PhysicalDrive$($d.Number)"
$buf = New-Object byte[] (4MB)
$in = [IO.File]::OpenRead($Image)
$out = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 4MB, [IO.FileOptions]::WriteThrough)
$sha = [Security.Cryptography.SHA256]::Create()
$done = 0
try {
    while (($r = $in.Read($buf, 0, $buf.Length)) -gt 0) {
        $sha.TransformBlock($buf, 0, $r, $null, 0) | Out-Null
        $w = [int]([math]::Ceiling($r / 512.0) * 512)          # raw disks take whole sectors
        if ($w -gt $r) { [Array]::Clear($buf, $r, $w - $r) }
        $out.Write($buf, 0, $w)
        $done += $r
        Write-Progress -Activity "写入" -Status ("{0:N0} / {1:N0} MB" -f ($done / 1MB), ($size / 1MB)) -PercentComplete ($done * 100 / $size)
    }
    $out.Flush()
} finally { $out.Close(); $in.Close() }
$sha.TransformFinalBlock($buf, 0, 0) | Out-Null
$want = [BitConverter]::ToString($sha.Hash)
Write-Host "读回校验…"
$rd = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$sha2 = [Security.Cryptography.SHA256]::Create()
$left = $size
while ($left -gt 0) {
    $k = $rd.Read($buf, 0, [int][math]::Min($buf.Length, $left))
    if ($k -le 0) { break }
    $sha2.TransformBlock($buf, 0, $k, $null, 0) | Out-Null
    $left -= $k
}
$rd.Close()
$sha2.TransformFinalBlock($buf, 0, 0) | Out-Null
if ([BitConverter]::ToString($sha2.Hash) -ne $want) { throw "读回校验失败：卡可能有问题，换一张再写" }
Write-Host "写入并校验成功。Windows 若弹出[需要格式化磁盘]，点取消，不要格式化。拔卡，插进 Zero3。" -ForegroundColor Green
