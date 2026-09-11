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
function Usage-Summary($Values) {
    $values = @($Values)
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

# Get-Stat 결과를 대상별 · 지표별로 한 번에 갈라 담는다.
#
# 지표를 대상마다 따로 물어보면 "대상 수 x 지표 수" 만큼 왕복이 생긴다. VM 이 1,000대면
# 2,000번이다. 한 번에 받아서 여기서 나누면 왕복은 한 번이다.
function Group-Stats($Stats) {
    $grouped = @{}
    foreach ($stat in $Stats) {
        if ($null -eq $stat.Value) { continue }
        $entityId = [string]$stat.Entity.Id
        $metric = [string]$stat.MetricId
        if (-not $grouped.ContainsKey($entityId)) { $grouped[$entityId] = @{} }
        if (-not $grouped[$entityId].ContainsKey($metric)) { $grouped[$entityId][$metric] = [System.Collections.ArrayList]::new() }
        [void]$grouped[$entityId][$metric].Add([double]$stat.Value)
    }
    return $grouped
}

function Stat-Values($Grouped, $EntityId, $Metric) {
    if (-not $Grouped.ContainsKey($EntityId)) { return @() }
    if (-not $Grouped[$EntityId].ContainsKey($Metric)) { return @() }
    return @($Grouped[$EntityId][$Metric])
}

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$marks = [ordered]@{}
function Mark([string]$Name) {
    $script:marks[$Name] = [math]::Round($script:timer.Elapsed.TotalSeconds, 1)
    $script:timer.Restart()
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
Mark 'module'
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

    # 클러스터는 소속 호스트 목록을 갖고 있으므로 한 번의 조회로 끝난다.
    $hostCluster = @{}
    foreach ($view in Get-View -Server $viServer -ViewType ClusterComputeResource -Property Name, Host) {
        foreach ($hostRef in @($view.Host)) {
            $hostCluster[$hostRef.ToString()] = $view.Name
        }
    }
    Mark 'topology'

    $hostEntities = @(Get-VMHost -Server $viServer -ErrorAction Stop | Sort-Object Name)
    $hostStats = @{}
    if ($hostEntities.Count -gt 0) {
        $hostStats = Group-Stats (Get-Stat -Entity $hostEntities -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average', 'mem.usage.average' -ErrorAction SilentlyContinue)
    }
    Mark 'host-stats'

    $hostRows = @()
    foreach ($vmHost in $hostEntities) {
        $cpu = Usage-Summary (Stat-Values $hostStats $vmHost.Id 'cpu.usage.average')
        $mem = Usage-Summary (Stat-Values $hostStats $vmHost.Id 'mem.usage.average')
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

    # VM 메타데이터(소속 호스트·UUID·템플릿 여부)는 한 번에 받는다. $vm.VMHost 를 읽으면
    # VM 하나당 별도 호출이 나가므로 건드리지 않는다.
    $hostNameById = @{}
    foreach ($vmHost in $hostEntities) { $hostNameById[$vmHost.Id] = $vmHost.Name }
    $vmMeta = @{}
    foreach ($view in Get-View -Server $viServer -ViewType VirtualMachine -Property Name, Config.Template, Config.InstanceUuid, Runtime.Host) {
        $hostRef = $null
        if ($null -ne $view.Runtime -and $null -ne $view.Runtime.Host) { $hostRef = $view.Runtime.Host.ToString() }
        $vmMeta[$view.MoRef.ToString()] = @{
            Template = [bool]$view.Config.Template
            InstanceUuid = $view.Config.InstanceUuid
            HostId = $hostRef
        }
    }
    Mark 'vm-meta'

    $vmEntities = @(Get-VM -Server $viServer -ErrorAction Stop | Sort-Object Name)
    $poweredOn = @($vmEntities | Where-Object { [string]$_.PowerState -eq 'PoweredOn' })
    $vmStats = @{}
    if ($poweredOn.Count -gt 0) {
        $vmStats = Group-Stats (Get-Stat -Entity $poweredOn -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average', 'mem.usage.average' -ErrorAction SilentlyContinue)
    }
    Mark 'vm-stats'

    $vmRows = @()
    foreach ($vm in $vmEntities) {
        $meta = $vmMeta[$vm.Id]
        if ($null -ne $meta -and $meta.Template) { continue }
        $cpu = Usage-Summary (Stat-Values $vmStats $vm.Id 'cpu.usage.average')
        $mem = Usage-Summary (Stat-Values $vmStats $vm.Id 'mem.usage.average')
        $hostId = $null
        if ($null -ne $meta) { $hostId = $meta.HostId }
        $vmRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            cluster_name = if ($hostId -and $hostCluster.ContainsKey($hostId)) { $hostCluster[$hostId] } else { $null }
            esxi_host = $(if ($null -ne $hostId -and $hostNameById.ContainsKey($hostId)) { $hostNameById[$hostId] } else { $null })
            vm_uuid = $(if ($null -ne $meta) { $meta.InstanceUuid } else { $null })
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
    Mark 'write'
    Write-Output ("HOST_COUNT=" + @($hostRows).Count + ";VM_COUNT=" + @($vmRows).Count)
    Write-Output ("TIMING=" + (($marks.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)s" }) -join ' '))
}
finally {
    if ($null -ne $viServer) { Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null }
}
