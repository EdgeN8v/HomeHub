# 取出你自己小米账号下所有设备的本地密钥（token）和 IP，存成 .keys\mi_tokens.json（.keys 已被 git 忽略）。
# 用法：powershell -NoProfile -File tools\get_tokens.ps1
# 登录时选「二维码」，用手机米家 App 扫码即可，不用输入密码。服务器选 cn。
# 这个工具是开源的 PiotrMachowski/Xiaomi-cloud-tokens-extractor，只连小米自己的域名，
# 脚本副本在 .keys\token_extractor\（已通读过，没有向别处发送数据）。
$root = Split-Path $PSScriptRoot
$dir = Join-Path $root ".keys\token_extractor"
$venv = Join-Path $dir "venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    python -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install -q -r "$dir\requirements.txt"
}
$out = Join-Path $root ".keys\mi_tokens.json"
& "$venv\Scripts\python.exe" "$dir\token_extractor.py" -o $out
Write-Host "`n完成后告诉我一声。结果在 $out ，我只在本机读取，不会上传或写进仓库。"
