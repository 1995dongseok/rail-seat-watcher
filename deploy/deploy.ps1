# 로컬 PC 에서 실행: 프로젝트를 Lightsail 인스턴스로 복사하고 서비스를 재시작한다.
#   .\deploy\deploy.ps1 -HostName 1.2.3.4 -KeyPath $HOME\.ssh\license_tutor_lightsail
param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$User = "ubuntu",
    [string]$KeyPath = "$HOME\.ssh\license_tutor_lightsail",
    [string]$RemoteDir = "/opt/rail-seat-watcher"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = "$User@$HostName"

# venv, data, .env 는 서버 것을 유지한다. 코드와 설정 예시만 올린다.
$items = @("app", "deploy", "requirements.txt", ".env.example", "README.md")
ssh -i $KeyPath $target "sudo mkdir -p $RemoteDir; sudo chown $User`:$User $RemoteDir"
foreach ($i in $items) {
    scp -i $KeyPath -r (Join-Path $root $i) "$target`:$RemoteDir/"
}
ssh -i $KeyPath $target "cd $RemoteDir; find app -name __pycache__ -type d -exec rm -rf {} +; if [ -d venv ]; then ./venv/bin/pip install -q -r requirements.txt; sudo systemctl restart rail-seat-watcher; sudo systemctl --no-pager --lines=5 status rail-seat-watcher; else echo '최초 설치: sudo bash deploy/setup-server.sh <도메인>'; fi"
