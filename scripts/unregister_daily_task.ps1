param([string]$TaskName = "AssetDailyCollection")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "삭제 완료: $TaskName"
