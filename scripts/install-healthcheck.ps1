<#
  __BRAND__ VPN - установщик healthcheck в Планировщик задач (запуск полностью невидимый, без кражи фокуса).
  Поставить:  iwr __ADMIN_BASE__/health/install -OutFile install.ps1
              powershell -ExecutionPolicy Bypass -File .\install.ps1
  Удалить:    schtasks /Delete /TN "__BRAND__VPN-Healthcheck" /F
#>
param(
  [string]$ScriptUrl = "__ADMIN_BASE__/health/script",
  [int]$IntervalMin  = 10,
  [string]$TaskName  = "__BRAND__VPN-Healthcheck"
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$dir = Join-Path $env:LOCALAPPDATA "__BRAND__VPN"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ps  = Join-Path $dir "vpn-healthcheck.ps1"
$vbs = Join-Path $dir "run-hidden.vbs"

Write-Host ("Качаю healthcheck -> " + $ps) -ForegroundColor Cyan
Invoke-WebRequest -Uri $ScriptUrl -OutFile $ps -UseBasicParsing

# VBS-лаунчер: запускает PowerShell скрыто (окно 0) и не ждёт -> ноль мелькания, ноль фокуса
$q = [char]34
$vbsBody = "CreateObject(" + $q + "WScript.Shell" + $q + ").Run " + $q + `
           "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " + $q + $q + $ps + $q + $q + $q + ", 0, False"
Set-Content -Path $vbs -Value $vbsBody -Encoding ASCII

# Планировщик запускает wscript (windowless host) -> ничего не всплывает
$tr = "wscript.exe //B //Nologo " + $q + $vbs + $q
$out = schtasks /Create /TN $TaskName /TR $tr /SC MINUTE /MO $IntervalMin /F 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host ("Не удалось создать задачу: " + ($out -join ' ')) -ForegroundColor Red
  Write-Host "Запусти PowerShell от имени администратора и повтори." -ForegroundColor Yellow
  exit 1
}
Write-Host ("Задача '" + $TaskName + "' создана: каждые " + $IntervalMin + " мин, невидимо.") -ForegroundColor Green
schtasks /Run /TN $TaskName | Out-Null
Write-Host "Запустил разово - через минуту смотри отчёт в админке на /health." -ForegroundColor Green
Write-Host ("Лог на клиенте: " + (Join-Path $dir "healthcheck.log")) -ForegroundColor DarkGray
Write-Host ("Удалить: schtasks /Delete /TN " + $q + $TaskName + $q + " /F") -ForegroundColor DarkGray
