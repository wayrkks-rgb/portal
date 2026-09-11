param(
    [string]$OutputPath = '',
    [string]$OutputDir = ''
)

# vCenter VM 인벤토리를 수집한다.
#
# 속도가 중요하다. 세 가지를 지킨다.
#
#   1. Get-View 로 필요한 속성만 '한 번에' 받는다. VM 수와 무관하게 왕복이 일정하다.
#   2. PowerCLI 객체의 지연 속성($vm.VMHost 등)을 건드리지 않는다. 이것을 읽는 순간
#      VM 하나당 별도 호출이 나간다.
#   3. 한 프로세스가 여러 통합기를 처리한다. PowerCLI 모듈 로딩이 6~15초인데
#      통합기마다 프로세스를 띄우면 그 비용을 통합기 수만큼 낸다. 한 번만 낸다.
#
# 출력 JSON 의 컬럼 이름은 파이썬 쪽이 그대로 읽으므로 바꾸지 않는다.
#
# 두 가지 호출 방식을 받는다.
#   * 통합기 1대 : VCENTER_SERVER 등 + -OutputPath
#   * 여러 대    : VCENTER_COUNT, VCENTER_1_SERVER ... + -OutputDir
#                  통합기당 <OutputDir>/<id>.json 을 쓰고 RESULT= 줄을 한 줄씩 낸다.
#
# VCENTER_COLLECT_MODE=COMPAT 으로 두면 예전 방식(Get-VM)으로 돌린다. 새 방식이
# 환경에 맞지 않을 때 소스를 고치지 않고 빠져나갈 길을 남긴다.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

function Lookup($map, $key) {
    if ($null -ne $key -and $map.ContainsKey($key)) { return $map[$key] }
    return $null
}

$collectMode = (Get-EnvOrDefault 'VCENTER_COLLECT_MODE' 'BULK').ToUpperInvariant()

# 어느 단계에서 시간이 걸리는지 남긴다. 느릴 때 추측하지 않으려면 필요하다.
$timer = [System.Diagnostics.Stopwatch]::StartNew()
$marks = [ordered]@{}
function Mark([string]$Name) {
    $script:marks[$Name] = [math]::Round($script:timer.Elapsed.TotalSeconds, 1)
    $script:timer.Restart()
}
function Timing-Text() {
    return (($script:marks.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)s" }) -join ' ')
}

function Get-GuestIpList($guest) {
    $list = @()
    if ($null -ne $guest) {
        if (-not [string]::IsNullOrWhiteSpace($guest.IpAddress)) { $list += $guest.IpAddress }
        if ($null -ne $guest.Net) {
            foreach ($net in $guest.Net) {
                if ($null -ne $net.IpAddress) {
                    $list += @($net.IpAddress | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                }
            }
        }
    }
    return @($list | Select-Object -Unique)
}

# 한 줄을 만든다. 컬럼 이름은 파이썬이 읽는 계약이므로 여기 한 곳에서만 정한다.
# 통합기별로 달라지는 세 값은 Collect-One 이 script 범위에 미리 넣어둔다.
function New-Row([hashtable]$Values) {
    $ips = @($Values.IpList)
    $primaryIp = $null
    if ($ips.Count -gt 0) { $primaryIp = $ips[0] }
    $record = [ordered]@{
        'VM' = $Values.Name
        'Powerstate' = [string]$Values.PowerState
        'Template' = [bool]$Values.Template
        'SRM Placeholder' = [bool]$Values.SrmPlaceholder
        'DNS Name' = $Values.DnsName
        'Primary IP Address' = $primaryIp
        'CPUs' = [int]$Values.Cpus
        'Memory' = [int]$Values.MemoryMb
        'OS according to the configuration file' = $Values.OsConfig
        'OS according to the VMware Tools' = $Values.OsTools
        'Datacenter' = $Values.Datacenter
        'Cluster' = $Values.Cluster
        'Host' = $Values.EsxiHost
        'VM ID' = $Values.VmId
        'SMBIOS UUID' = $Values.SmbiosUuid
        'VM UUID' = $Values.VmUuid
        'VI SDK Server' = $script:sdkServer
        'vCenter Display Name' = $script:vcenterName
        'Collection Server' = $script:server
    }
    for ($i = 1; $i -le 8; $i++) {
        $value = $null
        if ($ips.Count -ge $i) { $value = $ips[$i - 1] }
        $record["Network #$i"] = $value
    }
    return [pscustomobject]$record
}

function Collect-Bulk($viServer) {
    # ESXi 이름을 한 번에 받는다.
    $hostName = @{}
    foreach ($view in Get-View -Server $viServer -ViewType HostSystem -Property Name) {
        $hostName[$view.MoRef.ToString()] = $view.Name
    }
    # 클러스터는 소속 호스트 목록을 갖고 있으므로 한 번으로 끝난다.
    $hostCluster = @{}
    foreach ($view in Get-View -Server $viServer -ViewType ClusterComputeResource -Property Name, Host) {
        foreach ($hostRef in @($view.Host)) {
            $hostCluster[$hostRef.ToString()] = $view.Name
        }
    }
    # 데이터센터는 보통 1~2개다. 그 안의 호스트만 찾으면 된다.
    $hostDatacenter = @{}
    foreach ($dc in Get-View -Server $viServer -ViewType Datacenter -Property Name) {
        foreach ($view in Get-View -Server $viServer -ViewType HostSystem -SearchRoot $dc.MoRef -Property Name) {
            $hostDatacenter[$view.MoRef.ToString()] = $dc.Name
        }
    }
    Mark 'topology'

    $properties = @(
        'Name',
        'Config.Template', 'Config.Uuid', 'Config.InstanceUuid', 'Config.GuestFullName',
        'Config.ManagedBy', 'Config.Hardware.NumCPU', 'Config.Hardware.MemoryMB',
        'Guest.HostName', 'Guest.GuestFullName', 'Guest.IpAddress', 'Guest.Net',
        'Runtime.PowerState', 'Runtime.Host'
    )
    $vmViews = @(Get-View -Server $viServer -ViewType VirtualMachine -Property $properties)
    Mark 'fetch'

    $rows = foreach ($view in $vmViews) {
        $hostRef = $null
        if ($null -ne $view.Runtime -and $null -ne $view.Runtime.Host) {
            $hostRef = $view.Runtime.Host.ToString()
        }
        # SRM 예비 VM 은 실제로 도는 장비가 아니다. 확장 키로 구분한다.
        $isPlaceholder = $false
        if ($null -ne $view.Config -and $null -ne $view.Config.ManagedBy) {
            $isPlaceholder = ($view.Config.ManagedBy.ExtensionKey -eq 'com.vmware.vcDr')
        }
        New-Row @{
            Name           = $view.Name
            PowerState     = $view.Runtime.PowerState
            Template       = $view.Config.Template
            SrmPlaceholder = $isPlaceholder
            DnsName        = $view.Guest.HostName
            IpList         = (Get-GuestIpList $view.Guest)
            Cpus           = $view.Config.Hardware.NumCPU
            MemoryMb       = $view.Config.Hardware.MemoryMB
            OsConfig       = $view.Config.GuestFullName
            OsTools        = $view.Guest.GuestFullName
            Datacenter     = (Lookup $hostDatacenter $hostRef)
            Cluster        = (Lookup $hostCluster $hostRef)
            EsxiHost       = (Lookup $hostName $hostRef)
            VmId           = $view.MoRef.ToString()
            SmbiosUuid     = $view.Config.Uuid
            VmUuid         = $view.Config.InstanceUuid
        }
    }
    return @($rows)
}

function Collect-Compat($viServer) {
    # 예전 방식. VM 하나당 왕복이 생겨 느리지만, 새 방식이 막혔을 때 쓸 수 있어야 한다.
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
    Mark 'topology'

    $rows = foreach ($vm in Get-VM -Server $viServer -ErrorAction Stop) {
        $view = $vm.ExtensionData
        $vmHostId = $null
        $vmHostName = $null
        if ($null -ne $vm.VMHost) {
            $vmHostId = $vm.VMHost.Id
            $vmHostName = $vm.VMHost.Name
        }
        New-Row @{
            Name           = $vm.Name
            PowerState     = $vm.PowerState
            Template       = $view.Config.Template
            SrmPlaceholder = $false
            DnsName        = $view.Guest.HostName
            IpList         = (Get-GuestIpList $view.Guest)
            Cpus           = $vm.NumCpu
            MemoryMb       = $vm.MemoryMB
            OsConfig       = $view.Config.GuestFullName
            OsTools        = $view.Guest.GuestFullName
            Datacenter     = (Lookup $hostDatacenter $vmHostId)
            Cluster        = (Lookup $hostCluster $vmHostId)
            EsxiHost       = $vmHostName
            VmId           = $vm.Id
            SmbiosUuid     = $view.Config.Uuid
            VmUuid         = $view.Config.InstanceUuid
        }
    }
    Mark 'fetch'
    return @($rows)
}

# 통합기 한 대를 수집해 JSON 으로 쓴다. 결과를 해시테이블로 돌려준다.
function Collect-One([hashtable]$Target, [string]$JsonPath) {
    $script:marks = [ordered]@{}
    $script:timer.Restart()
    $script:server = $Target.Server
    $script:vcenterName = $Target.Name
    $script:sdkServer = $Target.Server
    if (-not [string]::IsNullOrWhiteSpace($Target.Id)) { $script:sdkServer = $Target.Id }

    if ($Target.IgnoreCert) {
        Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -Confirm:$false | Out-Null
    }

    $viServer = $null
    try {
        if ($Target.AuthMode -eq 'PASS_THROUGH') {
            $viServer = Connect-VIServer -Server $Target.Server -Port $Target.Port -Force -NotDefault -ErrorAction Stop
        }
        elseif ($Target.AuthMode -eq 'CREDENTIAL') {
            if ([string]::IsNullOrWhiteSpace($Target.Username) -or [string]::IsNullOrWhiteSpace($Target.Password)) {
                throw "vCenter username/password is missing"
            }
            $securePassword = ConvertTo-SecureString $Target.Password -AsPlainText -Force
            $credential = [System.Management.Automation.PSCredential]::new($Target.Username, $securePassword)
            $viServer = Connect-VIServer -Server $Target.Server -Port $Target.Port -Credential $credential -Force -NotDefault -ErrorAction Stop
        }
        else {
            throw "Unsupported VCENTER_AUTH_MODE: $($Target.AuthMode)"
        }
        Mark 'connect'

        $usedMode = $collectMode
        if ($collectMode -eq 'COMPAT') {
            $rows = Collect-Compat $viServer
        }
        else {
            try {
                $rows = Collect-Bulk $viServer
            }
            catch {
                # 새 방식이 막혔으면 수집을 포기하지 않고 예전 방식으로 한 번 더 시도한다.
                Write-Output ("BULK_FALLBACK=" + $_.Exception.Message)
                $usedMode = 'COMPAT'
                $rows = Collect-Compat $viServer
            }
        }

        $parent = Split-Path -Parent $JsonPath
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        @($rows) | ConvertTo-Json -Depth 8 | Set-Content -Path $JsonPath -Encoding UTF8
        Mark 'write'

        return @{ Status = 'SUCCESS'; Count = @($rows).Count; Mode = $usedMode; Timing = (Timing-Text) }
    }
    finally {
        if ($null -ne $viServer) {
            Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null
        }
    }
}

function Read-Target([string]$Prefix) {
    $port = 443
    $portText = [Environment]::GetEnvironmentVariable($Prefix + 'PORT')
    if (-not [string]::IsNullOrWhiteSpace($portText)) { $port = [int]$portText }
    return @{
        Id         = [Environment]::GetEnvironmentVariable($Prefix + 'ID')
        Name       = [Environment]::GetEnvironmentVariable($Prefix + 'NAME')
        Server     = [Environment]::GetEnvironmentVariable($Prefix + 'SERVER')
        Port       = $port
        AuthMode   = (Get-EnvOrDefault ($Prefix + 'AUTH_MODE') 'CREDENTIAL').ToUpperInvariant()
        Username   = [Environment]::GetEnvironmentVariable($Prefix + 'USERNAME')
        Password   = [Environment]::GetEnvironmentVariable($Prefix + 'PASSWORD')
        IgnoreCert = (Get-EnvOrDefault ($Prefix + 'IGNORE_CERT') 'false').ToLowerInvariant() -eq 'true'
    }
}

# 모듈 로딩은 한 번만. 이 프로세스가 맡은 통합기 전부가 이 비용을 나눠 쓴다.
Import-Module VMware.VimAutomation.Core -ErrorAction Stop
Set-PowerCLIConfiguration -Scope Session -ParticipateInCEIP:$false -Confirm:$false | Out-Null
$moduleSeconds = [math]::Round($timer.Elapsed.TotalSeconds, 1)
Write-Output ("MODULE_SECONDS=" + $moduleSeconds)

$countText = [Environment]::GetEnvironmentVariable('VCENTER_COUNT')
if ([string]::IsNullOrWhiteSpace($countText)) {
    # 통합기 1대. 예전 호출 방식과 같다(연결 테스트가 이 길을 쓴다).
    if ([string]::IsNullOrWhiteSpace($OutputPath)) { throw "-OutputPath is required" }
    $target = Read-Target 'VCENTER_'
    if ([string]::IsNullOrWhiteSpace($target.Server)) { throw "Required environment variable is missing: VCENTER_SERVER" }
    $result = Collect-One $target $OutputPath
    Write-Output ("COLLECTED_COUNT=" + $result.Count)
    Write-Output ("COLLECT_MODE=" + $result.Mode)
    Write-Output ("TIMING=" + $result.Timing)
    exit 0
}

# 여러 대. 한 대가 실패해도 나머지는 계속한다.
if ([string]::IsNullOrWhiteSpace($OutputDir)) { throw "-OutputDir is required when VCENTER_COUNT is set" }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$count = [int]$countText
$failures = 0
for ($index = 1; $index -le $count; $index++) {
    $target = Read-Target ("VCENTER_" + $index + "_")
    $vcId = $target.Id
    if ([string]::IsNullOrWhiteSpace($vcId)) { $vcId = "vc$index" }
    $jsonPath = Join-Path $OutputDir ($vcId + '.json')
    try {
        $result = Collect-One $target $jsonPath
        Write-Output ("RESULT=" + $vcId + "|SUCCESS|" + $result.Count + "|" + $result.Mode + "|" + $result.Timing)
    }
    catch {
        $failures++
        $message = ($_.Exception.Message -replace '[\r\n|]', ' ')
        Write-Output ("RESULT=" + $vcId + "|FAILED|0||" + $message)
    }
}
Write-Output ("TIMING=module=" + $moduleSeconds + "s vcenters=" + $count + " failed=" + $failures)
exit 0
