# setup_scheduler.ps1
# Run this script as Administrator to register both scheduled tasks.
# Right-click this file → "Run with PowerShell" OR open PowerShell as Admin and run it.

$folder = "c:\Users\monce\Desktop\facture app 2EME trim\HotelRunner Daily Summary"

Write-Host ""
Write-Host "Setting up HotelRunner scheduled tasks..." -ForegroundColor Cyan

# ── Task 1: Morning full briefing at 07:00 ────────────────────────────────────
$actionMorning = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"`"$folder\run_morning.bat`"`"" `
    -WorkingDirectory $folder

$triggerMorning = New-ScheduledTaskTrigger -Daily -At "07:00"

$settingsMorning = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "HotelRunner Morning Briefing" `
    -Action $actionMorning `
    -Trigger $triggerMorning `
    -Settings $settingsMorning `
    -Description "Full daily check-in/check-out briefing to Telegram at 07:00" `
    -RunLevel Highest `
    -Force

Write-Host "Task 1 done: Morning briefing every day at 07:00" -ForegroundColor Green

# ── Task 2: Hourly change check 08:00 → 00:00 ────────────────────────────────
$actionCheck = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"`"$folder\run_check.bat`"`"" `
    -WorkingDirectory $folder

# Repeat every 1 hour for 16 hours starting at 08:00 (ends at 00:00)
$triggerCheck = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 16) `
    -Once -At "08:00"

$settingsCheck = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "HotelRunner Hourly Check" `
    -Action $actionCheck `
    -Trigger $triggerCheck `
    -Settings $settingsCheck `
    -Description "Change check every hour from 08:00 to 00:00 — alerts only if something changed" `
    -RunLevel Highest `
    -Force

Write-Host "Task 2 done: Hourly check-in 08:00 to 00:00" -ForegroundColor Green

Write-Host ""
Write-Host "All done! Both tasks are now active." -ForegroundColor Cyan
Write-Host "You can view them in Task Scheduler (search 'Task Scheduler' in Start menu)." -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to close"
