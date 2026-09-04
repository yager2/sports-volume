# Registers the daily sports-volume pull with Windows Task Scheduler.
# Run once, from an ordinary (non-admin) PowerShell:
#     powershell -ExecutionPolicy Bypass -File install_schedule.ps1
#
# Re-running updates the existing task. To remove it:
#     Unregister-ScheduledTask -TaskName "SportsVolumeTracker" -Confirm:$false

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat  = Join-Path $here "run_all.bat"
$taskName = "SportsVolumeTracker"

# Two triggers so a missed run gets a same-day second chance:
#   1:30 PM local (Central) ~= 2:30 ET, after DKeX finalises the prior day.
#   7:30 PM local - catch-up if the laptop was asleep/off at 1:30. IgnoreNew
#   (below) means if the 1:30 run is still going, the 7:30 one is skipped; if
#   1:30 already finished, 7:30 re-runs (idempotent - same code path).
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At 1:30PM),
    (New-ScheduledTaskTrigger -Daily -At 7:30PM)
)
$action   = New-ScheduledTaskAction -Execute $bat

# WakeToRun     - wake the laptop from sleep to run (needs "Allow wake timers"
#                 on in the power plan; the 7:30 trigger is the fallback if not).
# StartWhenAvailable - run ASAP after a missed start once the machine is back.
# RestartCount/Interval - if the run dies (e.g. killed mid-Kalshi), retry.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Trigger $triggers -Action $action `
    -Settings $settings -Description "Daily sports prediction-market volume pull (run_all.py)" `
    -Force

# Best-effort: let scheduled tasks wake the machine (may need admin; harmless if it fails).
try {
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1
    powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1
    powercfg /SETACTIVE SCHEME_CURRENT
    Write-Host "Wake timers enabled for the current power plan."
} catch {
    Write-Host "Could not change power plan (not admin?) - the 7:30 PM catch-up trigger covers this."
}

Write-Host "Registered '$taskName' - runs daily at 1:30 PM, with a 7:30 PM catch-up."
Write-Host "Run now to test:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Then watch run_all.log in $here"
