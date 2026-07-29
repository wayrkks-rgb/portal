$ErrorActionPreference = 'Stop'
$module = Get-Module -ListAvailable VMware.VimAutomation.Core | Sort-Object Version -Descending | Select-Object -First 1
if ($null -eq $module) {
    Write-Error 'VMware.VimAutomation.Core module was not found in PSModulePath.'
    exit 2
}
[pscustomobject]@{
    status = 'SUCCESS'
    module = $module.Name
    version = [string]$module.Version
    path = $module.Path
    powershell = $PSVersionTable.PSVersion.ToString()
} | ConvertTo-Json -Depth 4
