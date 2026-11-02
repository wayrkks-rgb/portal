# 통합기(vCenter) 1대에 붙어 지정한 VM 들의 '지금' 전원 상태만 읽는다.
#
# 일간 보고는 아침 배치 시점의 전원 상태다. 하루에도 꺼졌다 켜지는 VM 은 낮에 보면
# 다르다. 그래서 전원이 바뀐 VM 에 한해 화면에서 이 스크립트를 부른다.
#
# 빠르게 끝내는 것이 목적이다.
#   * 통합기 1대만 붙는다(파이썬이 통합기별로 나눠 부른다).
#   * Get-View 로 Name · 전원 · UUID 세 속성만 한 번에 받는다. VM 수와 무관하게 왕복 1번.
#   * 단계별 시간을 돌려준다. 느리면 어디가 느린지(대개 모듈 로딩) 바로 보인다.
#
# 입력(환경 변수): VCENTER_SERVER, VCENTER_PORT, VCENTER_AUTH_MODE, VCENTER_USERNAME,
#                  VCENTER_PASSWORD, VCENTER_IGNORE_CERT,
#                  VM_KEYS (쉼표로 구분한 MoRef · UUID. 이 VM 들만 돌려준다)
# 출력(표준 출력): RESULT_JSON={"timings":{...},"vms":[{"moref","name","power","uuid","instance_uuid"}]}

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# VM 이름에 한글이 있어도 파이썬이 그대로 읽게 UTF-8 로 낸다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$timings = [ordered]@{}
function Mark([string]$Name) {
    $script:timings[$Name] = [math]::Round($script:timer.Elapsed.TotalSeconds, 2)
    $script:timer.Restart()
}

function Env([string]$Name, [string]$Default = '') {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

Import-Module VMware.VimAutomation.Core -ErrorAction Stop
Set-PowerCLIConfiguration -Scope Session -ParticipateInCEIP:$false -Confirm:$false | Out-Null
if ((Env 'VCENTER_IGNORE_CERT' 'false').ToLowerInvariant() -eq 'true') {
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -Confirm:$false | Out-Null
}
Mark 'module'

$server = Env 'VCENTER_SERVER'
$port = [int](Env 'VCENTER_PORT' '443')
$mode = (Env 'VCENTER_AUTH_MODE' 'CREDENTIAL').ToUpperInvariant()
$viServer = $null
try {
    if ($mode -eq 'PASS_THROUGH') {
        $viServer = Connect-VIServer -Server $server -Port $port -Force -NotDefault -ErrorAction Stop
    }
    else {
        $secure = ConvertTo-SecureString (Env 'VCENTER_PASSWORD') -AsPlainText -Force
        $credential = [System.Management.Automation.PSCredential]::new((Env 'VCENTER_USERNAME'), $secure)
        $viServer = Connect-VIServer -Server $server -Port $port -Credential $credential -Force -NotDefault -ErrorAction Stop
    }
    Mark 'connect'

    # 찾을 VM. 받는 것은 전체를 한 번에 받고(그쪽이 빠르다) 내보내는 것만 거른다.
    $wanted = @{}
    foreach ($key in (Env 'VM_KEYS').Split(',')) {
        $clean = $key.Trim().ToLowerInvariant()
        if ($clean) { $wanted[$clean] = $true }
    }
    $rows = @()
    foreach ($view in Get-View -Server $viServer -ViewType VirtualMachine -Property Name, Runtime.PowerState, Config.Uuid, Config.InstanceUuid) {
        $moref = $view.MoRef.Value
        if ($wanted.Count -gt 0) {
            $keys = @("$moref", "$($view.Config.Uuid)", "$($view.Config.InstanceUuid)") | ForEach-Object { $_.ToLowerInvariant() }
            if (-not ($keys | Where-Object { $wanted.ContainsKey($_) })) { continue }
        }
        $rows += [pscustomobject]@{
            moref         = $moref
            name          = $view.Name
            power         = [string]$view.Runtime.PowerState
            uuid          = $view.Config.Uuid
            instance_uuid = $view.Config.InstanceUuid
        }
    }
    Mark 'query'

    $result = [ordered]@{ timings = $timings; vms = $rows }
    Write-Output ('RESULT_JSON=' + ($result | ConvertTo-Json -Depth 4 -Compress))
}
finally {
    if ($null -ne $viServer) {
        Disconnect-VIServer -Server $viServer -Confirm:$false -Force -ErrorAction SilentlyContinue | Out-Null
    }
}
