param(
    [Parameter(Mandatory=$true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Require-Env([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Required environment variable is missing: $Name"
    }
    return $value
}

$server = Require-Env 'VCENTER_SERVER'
$portText = [Environment]::GetEnvironmentVariable('VCENTER_PORT')
$port = if ([string]::IsNullOrWhiteSpace($portText)) { 443 } else { [int]$portText }
$authModeValue = [Environment]::GetEnvironmentVariable('VCENTER_AUTH_MODE')
if ([string]::IsNullOrWhiteSpace($authModeValue)) { $authModeValue = 'CREDENTIAL' }
$authMode = $authModeValue.ToUpperInvariant()
$vcenterId = [Environment]::GetEnvironmentVariable('VCENTER_ID')
$vcenterName = [Environment]::GetEnvironmentVariable('VCENTER_NAME')
$ignoreCertificateValue = [Environment]::GetEnvironmentVariable('VCENTER_IGNORE_CERT')
if ([string]::IsNullOrWhiteSpace($ignoreCertificateValue)) { $ignoreCertificateValue = 'false' }
$ignoreCertificate = $ignoreCertificateValue.ToLowerInvariant() -eq 'true'

Import-Module VMware.VimAutomation.Core -ErrorAction Stop
Set-PowerCLIConfiguration -Scope Session -ParticipateInCEIP:$false -Confirm:$false | Out-Null
if ($ignoreCertificate) {
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -Confirm:$false | Out-Null
}

$viServer = $null
try {
    if ($authMode -eq 'PASS_THROUGH') {
        $viServer = Connect-VIServer -Server $server -Port $port -Force -NotDefault -ErrorAction Stop
    }
    elseif ($authMode -eq 'CREDENTIAL') {
        $username = Require-Env 'VCENTER_USERNAME'
        $password = Require-Env 'VCENTER_PASSWORD'
        $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
        $credential = [System.Management.Automation.PSCredential]::new($username, $securePassword)
        $viServer = Connect-VIServer -Server $server -Port $port -Credential $credential -Force -NotDefault -ErrorAction Stop
    }
    else {
        throw "Unsupported VCENTER_AUTH_MODE: $authMode"
    }

    $hostCluster = @{}
    $hostDatacenter = @{}
    foreach ($cluster in Get-Cluster -Server $viServer -ErrorAction SilentlyContinue) {
        foreach ($vmHost in Get-VMHost -Location $cluster -Server $viServer -ErrorAction SilentlyContinue) {
            $hostCluster[$vmHost.Id] = $cluster.Name
        }
    }
    foreach ($dc in Get-Datacenter -Server $viServer -ErrorAction SilentlyContinue) {
        foreach ($vmHost in Get-VMHost -Location $dc -Server $viServer -ErrorAction SilentlyContinue) {
            $hostDatacenter[$vmHost.Id] = $dc.Name
        }
    }

    $rows = foreach ($vm in Get-VM -Server $viServer -ErrorAction Stop) {
        $view = $vm.ExtensionData
        $guestIpList = @()
        if ($null -ne $view.Guest.IpAddress) {
            $guestIpList += @($view.Guest.IpAddress | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        }
        if ($null -ne $view.Guest.Net) {
            foreach ($net in $view.Guest.Net) {
                if ($null -ne $net.IpAddress) {
                    $guestIpList += @($net.IpAddress | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                }
            }
        }
        $guestIpList = @($guestIpList | Select-Object -Unique)
        $networkValues = @{}
        for ($i = 1; $i -le 8; $i++) {
            $networkValues["Network #$i"] = if ($guestIpList.Count -ge $i) { $guestIpList[$i - 1] } else { $null }
        }

        $vmHostId = if ($null -ne $vm.VMHost) { $vm.VMHost.Id } else { $null }
        $record = [ordered]@{
            'VM' = $vm.Name
            'Powerstate' = [string]$vm.PowerState
            'Template' = [bool]$view.Config.Template
            'SRM Placeholder' = $false
            'DNS Name' = $view.Guest.HostName
            'Primary IP Address' = if ($guestIpList.Count -gt 0) { $guestIpList[0] } else { $null }
            'CPUs' = [int]$vm.NumCpu
            'Memory' = [int]$vm.MemoryMB
            'OS according to the configuration file' = $view.Config.GuestFullName
            'OS according to the VMware Tools' = $view.Guest.GuestFullName
            'Datacenter' = if ($vmHostId -and $hostDatacenter.ContainsKey($vmHostId)) { $hostDatacenter[$vmHostId] } else { $null }
            'Cluster' = if ($vmHostId -and $hostCluster.ContainsKey($vmHostId)) { $hostCluster[$vmHostId] } else { $null }
            'Host' = if ($null -ne $vm.VMHost) { $vm.VMHost.Name } else { $null }
            'VM ID' = $vm.Id
            'SMBIOS UUID' = $view.Config.Uuid
            'VM UUID' = $view.Config.InstanceUuid
            'VI SDK Server' = if ([string]::IsNullOrWhiteSpace($vcenterId)) { $server } else { $vcenterId }
            'vCenter Display Name' = $vcenterName
            'Collection Server' = $server
        }
        foreach ($key in $networkValues.Keys) {
            $record[$key] = $networkValues[$key]
        }
        [pscustomobject]$record
    }

    $parent = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    @($rows) | ConvertTo-Json -Depth 8 | Set-Content -Path $OutputPath -Encoding UTF8
    Write-Output ("COLLECTED_COUNT=" + @($rows).Count)
}
finally {
    if ($null -ne $viServer) {
        Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null
    }
}
