param(
    [Parameter(Mandatory=$true)][string]$Server,
    [string]$Python = (Get-Command python -ErrorAction Stop).Source
)
$ErrorActionPreference = 'Stop'
if ($Server -notmatch '^[a-zA-Z0-9.-]+$') { throw 'Expected a server hostname or IPv4 address' }
$pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is required' }
$directory = Join-Path $env:LOCALAPPDATA 'SafeChillVPN'
$script = Join-Path $directory 'vpn-client-probe.py'
$launcher = Join-Path $directory 'run-hidden.vbs'
$taskName = 'SafeChillVPN-Healthcheck'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or
        $existing.Actions.Execute -notmatch '(^|\\)wscript.exe$' -or
        $existing.Actions.Arguments -notlike ('*' + $launcher + '*'))) {
    throw 'Existing task has a different action; inspect it before changing anything'
}
New-Item -ItemType Directory -Path $directory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'vpn-client-probe.py') -Destination $script
$content = 'CreateObject("WScript.Shell").Run """' + $pythonw + '"" ""' + $script + '"" --server ' + $Server + '", 0, False'
[IO.File]::WriteAllText($launcher, $content, [Text.Encoding]::ASCII)
if (-not $existing) {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B //Nologo "{0}"' -f $launcher)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Minutes 10)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Limited | Out-Null
}
Start-ScheduledTask -TaskName $taskName
Write-Output 'Client probe installed; existing task schedule preserved.'
