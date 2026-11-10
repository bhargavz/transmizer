<#
Registers (or removes) the logon task that runs `p1_sync.py --watch` in the
background, so recordings sync whenever the HiDock P1 is plugged in.

  .\install_p1_watcher.ps1             install / update and start now
  .\install_p1_watcher.ps1 -Uninstall  stop and remove
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'Transmizer P1 Sync'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    # Wait for the old watcher to release its lock, or the new one exits at once.
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and (Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object CommandLine -match 'p1_sync\.py"? --watch')) {
        Start-Sleep -Milliseconds 500
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'."
}
if ($Uninstall) { return }

$pythonw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to python.exe" }
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$PSScriptRoot\p1_sync.py`" --watch" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Syncs HiDock P1 recordings into transmizer when the device is plugged in.' | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started '$TaskName'. Logs: $PSScriptRoot\logs\"
