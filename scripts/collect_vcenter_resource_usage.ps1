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
# 바이트를 MB 로. **Int64 로 돌려준다.**
#
# [int] 는 Int32 다. 2,147,483,647 을 넘으면 "값이 너무 크거나 작아 Int32 형식에
# 맞지 않습니다" 로 스크립트 전체가 죽는다. 디스크는 그 한계를 쉽게 넘는다 --
# 데이터스토어 용량, 특히 씬 프로비저닝 합계(Uncommitted)가 그렇다. 통합기 한 대
# 때문에 그 vCenter 전체 수집을 잃을 이유가 없다.
function To-Mb($Bytes) {
    if ($null -eq $Bytes) { return $null }
    try {
        $value = [double]$Bytes
    } catch {
        return $null
    }
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value)) { return $null }
    if ($value -lt 0) { return $null }
    return [long][math]::Round($value / 1MB, 0)
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

    # PowerCLI 의 Get-VMHost 는 태깅 때문에 Inventory Service(/invsvc) 에 붙으려
    # 한다. 그 서비스가 막혀 있으면 호스트 목록조차 못 받고 vCenter 하나가 통째로
    # 빠진다. 그때는 vSphere API 만 쓰는 Get-View 로 떨어진다 -- 사용률(Get-Stat)은
    # 포기하되 용량·VM 대수·디스크는 그대로 남는다. 없는 것보다 낫다.
    $hostEntities = @()
    $hostFallback = $false
    try {
        $hostEntities = @(Get-VMHost -Server $viServer -ErrorAction Stop | Sort-Object Name)
    } catch {
        $hostFallback = $true
        $reason = [string]$_.Exception.Message -replace '\s+', ' '
        if ($reason.Length -gt 200) { $reason = $reason.Substring(0, 200) }
        Write-Output ("HOST_FALLBACK=" + $reason)
    }

    # 두 경로가 같은 모양을 내놓게 한다. 아래 코드는 어느 쪽인지 몰라도 된다.
    $hostInfo = @()
    if ($hostFallback) {
        foreach ($view in Get-View -Server $viServer -ViewType HostSystem -Property Name, Hardware.CpuInfo.NumCpuCores, Hardware.MemorySize) {
            $hostInfo += [pscustomobject]@{
                Id = $view.MoRef.ToString()
                Name = $view.Name
                Cores = [int]$view.Hardware.CpuInfo.NumCpuCores
                MemoryMb = (To-Mb $view.Hardware.MemorySize)
            }
        }
    } else {
        foreach ($vmHost in $hostEntities) {
            $hostInfo += [pscustomobject]@{
                Id = $vmHost.Id
                Name = $vmHost.Name
                Cores = [int]$vmHost.NumCpu
                MemoryMb = (To-Mb ([double]$vmHost.MemoryTotalGB * 1GB))
            }
        }
    }

    $hostStats = @{}
    if (-not $hostFallback -and $hostEntities.Count -gt 0) {
        $hostStats = Group-Stats (Get-Stat -Entity $hostEntities -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average', 'mem.usage.average' -ErrorAction SilentlyContinue)
    }
    Mark 'host-stats'

    $hostRows = @()
    foreach ($item in $hostInfo) {
        $cpu = Usage-Summary (Stat-Values $hostStats $item.Id 'cpu.usage.average')
        $mem = Usage-Summary (Stat-Values $hostStats $item.Id 'mem.usage.average')
        $hostRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            cluster_name = if ($hostCluster.ContainsKey($item.Id)) { $hostCluster[$item.Id] } else { $null }
            esxi_host = $item.Name
            allocated_cpu_cores = $item.Cores
            allocated_memory_mb = $item.MemoryMb
            cpu_max_pct = $cpu.Max
            cpu_avg_pct = $cpu.Avg
            mem_max_pct = $mem.Max
            mem_avg_pct = $mem.Avg
            sample_count = [math]::Max($cpu.Count, $mem.Count)
        }
    }

    $hostNameById = @{}
    foreach ($item in $hostInfo) { $hostNameById[$item.Id] = $item.Name }

    # 데이터스토어. 디스크는 '쓴 양' 과 '나눠준 양' 이 다르다. 씬 프로비저닝이면
    # 나눠준 양이 용량을 넘을 수 있고(과할당), 그때는 VM 이 실제로 채우는 순간
    # 데이터스토어가 꽉 찬다. 그래서 두 값을 따로 담는다.
    #
    #   사용   = Capacity - FreeSpace            (지금 실제로 차 있는 양)
    #   할당   = 사용 + Uncommitted              (VM 에게 약속한 전체 양)
    #
    # Uncommitted 는 씬 디스크가 아직 안 쓴 몫이다. vCenter 가 데이터스토어
    # 요약으로 이미 계산해 두므로 VM 을 하나하나 더하지 않는다.
    $datastoreRows = @()
    foreach ($view in Get-View -Server $viServer -ViewType Datastore -Property Name, Summary, Host) {
      # 데이터스토어 하나가 이상해도 나머지는 건진다. 한 건 때문에 vCenter
      # 전체 수집을 잃지 않는다.
      try {
        $summary = $view.Summary
        if ($null -eq $summary) { continue }
        $mountedHosts = @()
        $mountedClusters = @()
        foreach ($mount in @($view.Host)) {
            if ($null -eq $mount -or $null -eq $mount.Key) { continue }
            $hostId = $mount.Key.ToString()
            if ($hostNameById.ContainsKey($hostId)) { $mountedHosts += $hostNameById[$hostId] }
            if ($hostCluster.ContainsKey($hostId)) { $mountedClusters += $hostCluster[$hostId] }
        }
        $mountedClusters = @($mountedClusters | Sort-Object -Unique)
        $capacity = [double]$summary.Capacity
        $free = [double]$summary.FreeSpace
        $uncommitted = if ($null -eq $summary.Uncommitted) { 0 } else { [double]$summary.Uncommitted }
        $usedBytes = $capacity - $free
        $datastoreRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            # 여러 클러스터가 함께 쓰는 데이터스토어는 한 클러스터에 매달 수 없다.
            cluster_name = if ($mountedClusters.Count -eq 1) { $mountedClusters[0] } else { $null }
            datastore_name = $view.Name
            datastore_type = [string]$summary.Type
            accessible = [bool]$summary.Accessible
            capacity_mb = (To-Mb $capacity)
            free_mb = (To-Mb $free)
            used_mb = (To-Mb $usedBytes)
            provisioned_mb = (To-Mb ($usedBytes + $uncommitted))
            host_count = @($mountedHosts).Count
            host_names = @($mountedHosts | Sort-Object -Unique)
            cluster_names = $mountedClusters
        }
      } catch {
        $reason = [string]$_.Exception.Message -replace '\s+', ' '
        if ($reason.Length -gt 160) { $reason = $reason.Substring(0, 160) }
        Write-Output ("DATASTORE_SKIP=" + $view.Name + " : " + $reason)
      }
    }
    Mark 'datastore'

    # VM 메타데이터(소속 호스트·UUID·템플릿 여부·디스크)는 한 번에 받는다.
    # $vm.VMHost 나 $vm.ProvisionedSpaceGB 를 읽으면 VM 하나당 별도 호출이
    # 나가므로 건드리지 않는다. Summary.Storage 에 이미 다 들어 있다.
    $vmMeta = @{}
    foreach ($view in Get-View -Server $viServer -ViewType VirtualMachine -Property Name, Config.Template, Config.InstanceUuid, Runtime.Host, Summary.Storage, Runtime.PowerState, Config.Hardware.NumCPU, Config.Hardware.MemoryMB) {
        $hostRef = $null
        if ($null -ne $view.Runtime -and $null -ne $view.Runtime.Host) { $hostRef = $view.Runtime.Host.ToString() }
        $committed = 0
        $provisioned = 0
        $storage = $null
        if ($null -ne $view.Summary) { $storage = $view.Summary.Storage }
        if ($null -ne $storage) {
            $committed = [double]$storage.Committed
            # 할당 = 쓴 양 + 아직 안 쓴 씬 몫. VMDK 로 약속한 전체 크기다.
            $provisioned = $committed + [double]$storage.Uncommitted
        }
        $vmMeta[$view.MoRef.ToString()] = @{
            Name = $view.Name
            Template = [bool]$view.Config.Template
            InstanceUuid = $view.Config.InstanceUuid
            HostId = $hostRef
            PowerState = [string]$view.Runtime.PowerState
            Cores = [int]$view.Config.Hardware.NumCPU
            MemoryMb = [long]$view.Config.Hardware.MemoryMB
            UsedDiskMb = (To-Mb $committed)
            ProvisionedDiskMb = (To-Mb $provisioned)
        }
    }
    Mark 'vm-meta'

    # Get-VM 도 호스트와 같은 이유(Inventory Service)로 막힐 수 있다. 막히면
    # 위에서 이미 받아 둔 Get-View 결과만으로 줄을 만든다 -- 사용률만 빠진다.
    $vmEntities = @()
    $vmFallback = $hostFallback
    if (-not $vmFallback) {
        try {
            $vmEntities = @(Get-VM -Server $viServer -ErrorAction Stop | Sort-Object Name)
        } catch {
            $vmFallback = $true
            $reason = [string]$_.Exception.Message -replace '\s+', ' '
            if ($reason.Length -gt 200) { $reason = $reason.Substring(0, 200) }
            Write-Output ("VM_FALLBACK=" + $reason)
        }
    }
    $vmStats = @{}
    if (-not $vmFallback) {
        $poweredOn = @($vmEntities | Where-Object { [string]$_.PowerState -eq 'PoweredOn' })
        if ($poweredOn.Count -gt 0) {
            $vmStats = Group-Stats (Get-Stat -Entity $poweredOn -Server $viServer -Start $start -Finish $finish -IntervalMins $intervalMins -Stat 'cpu.usage.average', 'mem.usage.average' -ErrorAction SilentlyContinue)
        }
    }
    Mark 'vm-stats'

    # 두 경로를 같은 모양(키, 메타)으로 맞춘다.
    $vmKeys = @()
    if ($vmFallback) {
        $vmKeys = @($vmMeta.Keys | Sort-Object { $vmMeta[$_].Name })
    } else {
        $vmKeys = @($vmEntities | ForEach-Object { $_.Id })
    }

    $vmRows = @()
    foreach ($vmKey in $vmKeys) {
        $meta = $vmMeta[$vmKey]
        if ($null -ne $meta -and $meta.Template) { continue }
        $cpu = Usage-Summary (Stat-Values $vmStats $vmKey 'cpu.usage.average')
        $mem = Usage-Summary (Stat-Values $vmStats $vmKey 'mem.usage.average')
        $hostId = $null
        if ($null -ne $meta) { $hostId = $meta.HostId }
        $vmRows += [pscustomobject][ordered]@{
            vcenter_id = $vcenterId
            service_name = $vcenterName
            cluster_name = if ($hostId -and $hostCluster.ContainsKey($hostId)) { $hostCluster[$hostId] } else { $null }
            esxi_host = $(if ($null -ne $hostId -and $hostNameById.ContainsKey($hostId)) { $hostNameById[$hostId] } else { $null })
            vm_uuid = $(if ($null -ne $meta) { $meta.InstanceUuid } else { $null })
            vm_name = $(if ($null -ne $meta) { $meta.Name } else { $null })
            power_state = $(if ($null -ne $meta) { $meta.PowerState } else { $null })
            allocated_cpu_cores = $(if ($null -ne $meta) { $meta.Cores } else { $null })
            allocated_memory_mb = $(if ($null -ne $meta) { $meta.MemoryMb } else { $null })
            provisioned_disk_mb = $(if ($null -ne $meta) { $meta.ProvisionedDiskMb } else { $null })
            used_disk_mb = $(if ($null -ne $meta) { $meta.UsedDiskMb } else { $null })
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
        datastores = @($datastoreRows)
    }
    $parent = Split-Path -Parent $OutputPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Path $OutputPath -Encoding UTF8
    Mark 'write'
    Write-Output ("HOST_COUNT=" + @($hostRows).Count + ";VM_COUNT=" + @($vmRows).Count + ";DATASTORE_COUNT=" + @($datastoreRows).Count)
    Write-Output ("TIMING=" + (($marks.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)s" }) -join ' '))
}
finally {
    if ($null -ne $viServer) { Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null }
}
