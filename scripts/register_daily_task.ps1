param(
    [string]$TaskName = "AssetDailyCollection",
    [string]$RunTime = "07:00",
    [Parameter(Mandatory=$true)]
    [string]$RunAsUser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BatchFile = Join-Path $ProjectRoot "scripts\run_daily_batch.bat"
if (-not (Test-Path $BatchFile)) { throw "배치 파일이 없습니다: $BatchFile" }

$At = [datetime]::ParseExact($RunTime, "HH:mm", $null)
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchFile`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$Credential = Get-Credential -UserName $RunAsUser -Message "작업 스케줄러 실행 계정 암호 입력"
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
Write-Host "등록 완료: $TaskName / 매일 $RunTime"
Write-Host "작업 스케줄러에서 수동 실행 후 마지막 실행 결과 0x0을 확인하세요."
