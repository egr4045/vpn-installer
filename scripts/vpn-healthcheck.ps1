<#
  __BRAND__ VPN — client healthcheck (Windows / PowerShell 5+)
  Проверяет каждый протокол, подтверждает падение 3 ретраями (без ложных срабатываний),
  меряет YouTube latency + скорость, пишет лог локально и шлёт отчёт в админку (/health).

  Разово:  powershell -ExecutionPolicy Bypass -File .\vpn-healthcheck.ps1
  Цикл:    powershell -ExecutionPolicy Bypass -File .\vpn-healthcheck.ps1 -Loop
#>
param(
  [string]$AdminUrl  = "__ADMIN_BASE__/api/health/report",
  [string]$Token     = "__HEALTH_TOKEN__",
  [string]$Server    = "__SERVER_IP__",
  [string]$Direct    = "__DIRECT_DOMAIN__",
  [string]$Cdn       = "__CDN_DOMAIN__",
  [int]$Retries      = 3,            # подтверждений падения подряд (внутри одного прогона)
  [int]$RetryGapMs   = 4000,
  [switch]$Loop,
  [int]$IntervalSec  = 300
)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$LogFile = Join-Path $env:LOCALAPPDATA "__BRAND__VPN\healthcheck.log"

function Test-Tcp([string]$h,[int]$p,[int]$timeoutMs=4000){
  $sw=[Diagnostics.Stopwatch]::StartNew()
  try{
    $c=New-Object Net.Sockets.TcpClient
    $iar=$c.BeginConnect($h,$p,$null,$null)
    if(-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)){ $c.Close(); return @{ok=$false;ms=$null} }
    $c.EndConnect($iar); $c.Close()
    return @{ok=$true; ms=[int]$sw.ElapsedMilliseconds}
  }catch{ return @{ok=$false; ms=$null} }
}
function Test-Tls([string]$h,[int]$p,[string]$sni,[int]$timeoutMs=6000){
  $sw=[Diagnostics.Stopwatch]::StartNew()
  try{
    $c=New-Object Net.Sockets.TcpClient
    $iar=$c.BeginConnect($h,$p,$null,$null)
    if(-not $iar.AsyncWaitHandle.WaitOne($timeoutMs)){ $c.Close(); return @{ok=$false;ms=$null} }
    $c.EndConnect($iar)
    $cb=[Net.Security.RemoteCertificateValidationCallback]{ param($s,$cert,$chain,$err) $true }
    $ssl=New-Object Net.Security.SslStream($c.GetStream(),$false,$cb)
    $ssl.AuthenticateAsClient($sni); $ok=$ssl.IsAuthenticated
    $ssl.Close(); $c.Close()
    return @{ok=$ok; ms=[int]$sw.ElapsedMilliseconds}
  }catch{ return @{ok=$false; ms=$null} }
}
function Test-Udp([string]$h,[int]$p,[int]$timeoutMs=2500){
  try{
    $u=New-Object Net.Sockets.UdpClient; $u.Connect($h,$p); $u.Client.ReceiveTimeout=$timeoutMs
    [void]$u.Send([byte[]](1..32),32)
    try{ $ep=New-Object Net.IPEndPoint([Net.IPAddress]::Any,0); [void]$u.Receive([ref]$ep); $u.Close(); return @{ok=$true;ms=$null} }
    catch [Net.Sockets.SocketException]{ $code=$_.Exception.SocketErrorCode; $u.Close()
      if($code -eq 'ConnectionReset'){ return @{ok=$false;ms=$null} }; return @{ok=$true;ms=$null} }
  }catch{ return @{ok=$false; ms=$null} }
}
function Test-Https([string]$url,[int]$timeoutSec=8){
  $sw=[Diagnostics.Stopwatch]::StartNew()
  try{
    [void](Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop)
    return @{ok=$true; ms=[int]$sw.ElapsedMilliseconds}
  }catch{ if($_.Exception.Response){ return @{ok=$true; ms=[int]$sw.ElapsedMilliseconds} }; return @{ok=$false; ms=$null} }
}

# Ретраим ТОЛЬКО при падении: здоровый протокол проходит с 1-й попытки без задержек.
function Confirm([scriptblock]$check){
  for($i=1; $i -le $Retries; $i++){
    $r = & $check
    if($r.ok){ return $r }
    if($i -lt $Retries){ Start-Sleep -Milliseconds $RetryGapMs }
  }
  return @{ok=$false; ms=$null}
}

function Measure-Net(){
  $lat = Test-Tcp "www.youtube.com" 443
  $mbps=$null
  # тяжёлый замер скорости — только раз в час (минута < 10), чтобы не жечь лимит трафика
  if((Get-Date).Minute -lt 10){
    try{
      $sw=[Diagnostics.Stopwatch]::StartNew()
      $data=(New-Object Net.WebClient).DownloadData("https://speed.cloudflare.com/__down?bytes=10000000")
      $sw.Stop(); $sec=$sw.Elapsed.TotalSeconds
      if($sec -gt 0){ $mbps=[math]::Round(($data.Length*8)/($sec*1000000),1) }
    }catch{}
  }
  $loss=$null; $png=$null
  try{ $p=Test-Connection -ComputerName $Server -Count 4 -ErrorAction Stop
       $png=[int](($p | Measure-Object ResponseTime -Average).Average); $loss=0 }
  catch{ $loss=100 }
  return @{ latency_ms=$lat.ms; mbps=$mbps; ping_ms=$png; loss_pct=$loss }
}

function Log([string]$line){
  try{
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $line)
    $all=Get-Content $LogFile -ErrorAction SilentlyContinue
    if($all.Count -gt 1000){ $all[-800..-1] | Set-Content $LogFile }  # трим лога
  }catch{}
}

function Run-Once(){
  $results=[ordered]@{
    "Reality"     = (Confirm { Test-Tls  $Server 8443 "www.microsoft.com" })
    "httpUpgrade" = (Confirm { Test-Https "https://$Cdn/" })
    "Hy2"         = (Confirm { Test-Udp  $Direct 443 })
    "Hy2-2"       = (Confirm { Test-Udp  $Server 8444 })
    "TUIC"        = (Confirm { Test-Udp  $Direct 9443 })
    "TCP"         = (Confirm { Test-Tcp  $Server 2080 })
  }
  $net=Measure-Net
  $payload=[ordered]@{ host=$env:COMPUTERNAME; results=$results; youtube=$net; ts=(Get-Date).ToString("o") } | ConvertTo-Json -Depth 6

  $summary = ($results.Keys | ForEach-Object { "{0}:{1}" -f $_, $(if($results[$_].ok){'ok'}else{'FAIL'}) }) -join " "
  Log ("$summary | ping=$($net.ping_ms)ms loss=$($net.loss_pct)% mbps=$($net.mbps)")

  Write-Host ""
  Write-Host "== VPN healthcheck @ $(Get-Date -Format 'HH:mm:ss') ==" -ForegroundColor Cyan
  foreach($k in $results.Keys){
    $r=$results[$k]; $ms=if($r.ms -ne $null){"$($r.ms) ms"}else{""}; $col=if($r.ok){'Green'}else{'Red'}
    Write-Host ("  {0,-12} {1,-5} {2}" -f $k, $(if($r.ok){'OK'}else{'FAIL'}), $ms) -ForegroundColor $col
  }
  Write-Host ("  {0,-12} ping {1} ms, loss {2}%, {3} Mbps" -f "Сеть", $net.ping_ms, $net.loss_pct, $net.mbps) -ForegroundColor Gray
  try{
    [void](Invoke-RestMethod -Uri $AdminUrl -Method Post -Headers @{Authorization="Bearer $Token"} `
      -ContentType "application/json" -Body $payload -TimeoutSec 10)
    Write-Host "  -> отправлено в админку" -ForegroundColor Green
  }catch{ Write-Host "  -> отправка не удалась: $($_.Exception.Message)" -ForegroundColor Yellow }
}

if($Loop){ Write-Host "Loop: каждые $IntervalSec сек. Ctrl+C для выхода." -ForegroundColor Cyan; while($true){ Run-Once; Start-Sleep -Seconds $IntervalSec } }
else { Run-Once }
