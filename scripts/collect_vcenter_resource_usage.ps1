param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$StartDate,
    [Parameter(Mandatory=$true)][string]$EndDate
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Require-Env([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { throw "Required environment variable is missing: $Name" }
    return $value
}
function Usage-Summary($Stats) {
    $values = @($Stats | Where-Object { $null -ne $_.Value } | Select-Object -ExpandProperty Value)
    if ($values.Count -eq 0) {
        return @{ Max = $null; Avg = $null; Count = 0 }
    }
    $measure = $values | Measure-Object -Average -Maximum
    return @{
        Max = [math]::Round([double]$measure.Maximum, 2)
        Avg = [math]::Round([double]$measure.Average, 2)
        Count = [int]$measure.Count
    }
}

$server = Require-Env 'VCENTER_SERVER'
$portText = [Environment]::GetEnvironmentVariable('VCENTER_PORT')
$port = if ([string]::IsNullOrWhiteSpace($portText)) { 443 } else { [int]$portText }
$authModeValue = [Environment]::GetEnvironmentVariable('VCENTER_AUTH_MODE')
if ([string]::IsNullOrWhiteSpace($authModeValue)) { $authModeValue = 'CREDENTIAL' }
$authMode = $authModeValue.ToUpperInvariant()
$vcenterId = [Environment]::GetEnvironmentVariable('VCENTER_ID')
$vcenterName = [Environment]::GetEnvironmentVariable('VCENTER_NAME')
$ignoreCertificate = ([Environment]::GetEnvironmentVariable('VCENTER_IGNORE_CERT')).ToLowerInvariant() -eq 'true'
$intervalText = [Environment]::GetEnvironmentVariable('VCENTER_RESOURCE_INTERVAL_MINS')
$intervalMins = if ([string]::IsNullOrWhiteSpace($intervalText)) { 120 } else { [int]$intervalText }
$start = [datetime]::ParseExact($StartDate, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
$finish = [datetime]::ParseExact($EndDate, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(1).AddTicks(-1)

Import-Module VMware.VimAutomation.Core -ErrorAction Stop
Set-PowerCLIConfiguration -Scope Session -ParticipateInCEIP:$false -Confirm:$false | Out-Null
if ($ignoreCertificate) { Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -Confirm:$false | Out-Null }

$viServer = $null
try {
    if ($authMode -eq 'PASS_THROUGH') {
        $viServer = Connect-VIServer -Server $server -Port $port -Force -NotDefault -ErrorAction Stop
    } elseif ($authMode -eq 'CREDENTIAL') {
        $username = Require-Env 'VCENTER_USERNAME'
        $password = Require-Env 'VCENTER_PASSWORD'
        $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential($username, $securePassword)
        $viServer = Connect-VIServer -Server $server -Port $port -Credential $credential -Force -NotDefault -ErrorAction Stop
    } else { throw "Unsupported VCENTER_AUTH_MODE: $authMode" }

    $hostCluster = @{}
    foreach ($cluster in Get-Cluster -Server $viServer -ErrorAction SilentlyContinue) {
        foreach ($vmHost in Get-VMHost -Location $cluster -Server $viServer -ErrorAction SilentlyContinue) {
            $hostCluster[$vmHost.Id] = $cluster.Name
        }
    }

    $hostRows = @()
    foreach ($vmHost in Get-VMHost -Server $viServer -ErrorAction Stop | Sort-Object Name) {
        $cpu = Usage-Summary (Get-Stat -Entity $vmHost -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average' -ErrorAction SilentlyContinue)
        $mem = Usage-Summary (Get-Stat -Entity $vmHost -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'mem.usage.average' -ErrorAction SilentlyContinue)
        $hostRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            cluster_name = if ($hostCluster.ContainsKey($vmHost.Id)) { $hostCluster[$vmHost.Id] } else { $null }
            esxi_host = $vmHost.Name
            allocated_cpu_cores = [int]$vmHost.NumCpu
            allocated_memory_mb = [int][math]::Round([double]$vmHost.MemoryTotalGB * 1024, 0)
            cpu_max_pct = $cpu.Max
            cpu_avg_pct = $cpu.Avg
            mem_max_pct = $mem.Max
            mem_avg_pct = $mem.Avg
            sample_count = [math]::Max($cpu.Count, $mem.Count)
        }
    }

    $vmRows = @()
    foreach ($vm in Get-VM -Server $viServer -ErrorAction Stop | Where-Object { -not $_.ExtensionData.Config.Template } | Sort-Object Name) {
        $cpu = @{ Max = $null; Avg = $null; Count = 0 }
        $mem = @{ Max = $null; Avg = $null; Count = 0 }
        if ([string]$vm.PowerState -eq 'PoweredOn') {
            $cpu = Usage-Summary (Get-Stat -Entity $vm -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average' -ErrorAction SilentlyContinue)
            $mem = Usage-Summary (Get-Stat -Entity $vm -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'mem.usage.average' -ErrorAction SilentlyContinue)
        }
        $hostId = if ($null -ne $vm.VMHost) { $vm.VMHost.Id } else { $null }
        $vmRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            cluster_name = if ($hostId -and $hostCluster.ContainsKey($hostId)) { $hostCluster[$hostId] } else { $null }
            esxi_host = if ($null -ne $vm.VMHost) { $vm.VMHost.Name } else { $null }
            vm_uuid = $vm.ExtensionData.Config.InstanceUuid
            vm_name = $vm.Name
            power_state = [string]$vm.PowerState
            allocated_cpu_cores = [int]$vm.NumCpu
            allocated_memory_mb = [int]$vm.MemoryMB
            cpu_max_pct = $cpu.Max
            cpu_avg_pct = $cpu.Avg
            mem_max_pct = $mem.Max
            mem_avg_pct = $mem.Avg
            sample_count = [math]::Max($cpu.Count, $mem.Count)
        }
    }

    $payload = [ordered]@{
        metadata = [ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            period_start = $start.ToString('yyyy-MM-dd')
            period_end = $finish.ToString('yyyy-MM-dd')
            collected_at = (Get-Date).ToString('s')
        }
        hosts = @($hostRows)
        vms = @($vmRows)
    }
    $parent = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Path $OutputPath -Encoding UTF8
    Write-Output ("HOST_COUNT=" + @($hostRows).Count + ";VM_COUNT=" + @($vmRows).Count)
}
finally {
    if ($null -ne $viServer) { Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null }
}
