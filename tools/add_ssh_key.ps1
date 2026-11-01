# One-time: add this PC's public key to a Linux host so later logins need no password.
param(
    [string]$Target = "CUI@192.168.31.61",
    [string]$PubKey = "$env:USERPROFILE\.ssh\id_ed25519.pub"
)
Get-Content $PubKey | ssh $Target "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo KEY-ADDED"
