# AIX HMC 연동 가이드

IBM Power 서버(AIX)의 프레임과 LPAR 정보를 **HMC REST API** 로 직접 읽어 옵니다.

vCenter 연동과 비교하면 훨씬 간단합니다.

| | vCenter | AIX HMC |
|---|---|---|
| 연결 방법 | PowerCLI(별도 설치) → vCenter | HTTPS 직접 호출 |
| WAS 에 설치할 것 | VMware PowerCLI 모듈 | **없음** |
| 포트 | 443 | **12443** |
| 방식 | PowerShell 프로세스 실행 | 파이썬 표준 라이브러리 |
| 계정 권한 | 읽기 전용 역할 | `hmcviewer` 역할 |

**설치할 파일이 없습니다.** HMC 가 주고받는 것은 HTTPS 와 XML 뿐이고 둘 다
파이썬에 기본으로 들어 있습니다. 폐쇄망에 휠(wheel)을 추가로 반입할 필요가
없습니다.

---

## 1. 준비 (HMC 관리자에게 요청할 것)

### 1-1. REST API 사용 확인

HMC V8 R8.4.0 이상이면 REST API 가 기본으로 켜져 있습니다. 확인 방법:

```
HMC 콘솔 → HMC Management → Console Settings → Change Performance Monitoring Settings
```

또는 HMC 에 SSH 로 붙어서:

```bash
lshmc -V            # 버전 확인 (V9 R2 이상 권장)
lshmc -r            # 원격 접속 설정. "remote web access" 가 enabled 여야 함
```

`remote web access` 가 꺼져 있으면:

```bash
chhmc -c ssh -s enable
# 웹 접근은 HMC 콘솔의 "Remote Operation" 설정에서 켭니다.
```

### 1-2. 조회 전용 계정 만들기

운영 계정(`hscroot`)을 쓰지 마세요. 조회만 되는 계정을 따로 만듭니다.

```
HMC 콘솔 → Users and Security → Manage User Profiles and Access
  → Create User
     User ID        : portal_ro
     Task Role      : hmcviewer          ← 조회 전용 역할
     Resource Role  : AllSystemResources
     Password expiration: 사용 안 함(만료되면 수집이 멈춥니다)
```

CLI 로 만든다면:

```bash
mkhmcusr -u portal_ro -a hmcviewer -d "통합 웹 조회 전용"
chhmcusr -u portal_ro -t passwd -v '<비밀번호>'
```

> `hmcviewer` 역할은 `lssyscfg`, `lshwres` 계열 조회만 허용합니다. LPAR 을
> 만들거나 끄는 명령은 허용되지 않으므로 운영 사고 위험이 없습니다.

### 1-3. 방화벽

| 출발 | 목적지 | 포트 | 용도 |
|---|---|---|---|
| 통합 웹 WAS | HMC | **TCP 12443** | REST API (필수) |
| 통합 웹 WAS | HMC | TCP 443 | 웹 콘솔 (선택, 확인용) |

**12443 입니다.** 443(웹 콘솔)이나 22(SSH)가 아닙니다. 여기서 막히는 경우가
가장 많습니다.

확인:

```powershell
Test-NetConnection -ComputerName <HMC주소> -Port 12443
```

`TcpTestSucceeded : True` 가 나와야 합니다.

### 1-4. 인증서

HMC 는 보통 자체 서명 인증서를 씁니다. 화면의 **인증서 검증** 을
`건너뜀(자체 서명)` 으로 두면 됩니다. 사내 CA 로 교체한 HMC 라면 `검증` 으로
바꾸세요.

---

## 2. 통합 웹에서 등록하기

```
관리 → 연계 설정 → 🖧 AIX HMC 연결 관리
```

1. **[HMC 추가]** 를 누릅니다.
2. 한 줄을 채웁니다.

| 항목 | 예시 | 설명 |
|---|---|---|
| 식별자 | `hmc01` | 내부 구분용. 영문·숫자로 짧게. 나중에 바꾸지 마세요 |
| 표시명 | `본사 HMC` | 화면·보고서에 나오는 이름 |
| 주소 | `10.0.0.10` | HMC 의 IP 또는 FQDN |
| 포트 | `12443` | 기본값 그대로 |
| 조회 계정 | `portal_ro` | 1-2 에서 만든 계정 |
| 비밀번호 | – | 저장 후에는 화면으로 내려오지 않습니다 |
| 사용 | 사용 | 잠시 빼려면 `중지` |

3. **[테스트]** 를 눌러 확인합니다. 성공하면 읽어 온 프레임 이름이 표시됩니다.
4. **[미리보기]** 로 실제 값을 확인합니다. 프레임의 CPU/메모리, LPAR 의 OS
   버전과 RMC IP 까지 나옵니다.
5. **[HMC 설정 저장]** 을 누릅니다.

### 테스트가 실패하면

응답에 **어느 단계에서 막혔는지** 가 함께 나옵니다.

| 단계 | 뜻 | 확인할 것 |
|---|---|---|
| `CONFIG` | 입력값이 비었음 | 주소·계정·비밀번호 |
| `CONNECT` | TCP 연결 실패 | 방화벽 12443, HMC 전원/주소 |
| `AUTH` | 로그인 거부 | 계정·비밀번호, 계정 잠김/만료 |
| `LOGON` | 응답을 못 읽음 | REST API 가 켜져 있는지, 포트가 12443 인지 |
| `QUERY` | 로그인은 됐는데 조회 실패 | 계정의 Resource Role 이 `AllSystemResources` 인지 |

---

## 3. 읽어 오는 값

### ManagedSystem (물리 프레임) — vCenter 의 통합기에 해당

| 우리 이름 | HMC 항목 | 설명 |
|---|---|---|
| `system_name` | `SystemName` | 프레임 이름 |
| `machine_type` / `model` | `MachineType` / `Model` | 예: 9080-M9S |
| `serial_number` | `SerialNumber` | 시리얼 (ITSM 대조 키) |
| `state` | `State` | operating / standby 등 |
| `installed_proc_units` | `InstalledSystemProcessorUnits` | 장착 CPU |
| `available_proc_units` | `CurrentAvailableSystemProcessorUnits` | **남은** CPU |
| `installed_memory_mb` | `InstalledSystemMemory` | 장착 메모리(MB) |
| `available_memory_mb` | `CurrentAvailableSystemMemory` | **남은** 메모리(MB) |

### LogicalPartition (LPAR) — vCenter 의 VM 에 해당

| 우리 이름 | HMC 항목 | 설명 |
|---|---|---|
| `partition_name` | `PartitionName` | LPAR 이름 |
| `partition_id` | `PartitionID` | 프레임 안 번호 |
| `partition_state` | `PartitionState` | running / not activated |
| `os_version` | `OperatingSystemVersion` | 예: `AIX 7.2 7200-05-04-2220` |
| `rmc_ip` | `ResourceMonitoringIPAddress` | RMC 로 잡힌 IP. **ITSM 대조에 씁니다** |
| `memory_mb` | `CurrentMemory` | 할당 메모리(MB) |
| `proc_units` | `CurrentProcessingUnits` | 공유 CPU 할당량 |
| `virtual_procs` | `CurrentMaximumVirtualProcessors` | 가상 프로세서 수 |
| `dedicated_procs` | `CurrentDedicatedProcessors` | 전용 CPU (전용 모드일 때) |

> **`rmc_ip` 가 비어 있으면** 그 LPAR 의 RMC 연결이 끊긴 상태입니다. LPAR 안에서
> `lsrsrc IBM.MCP` 로 확인하고, 필요하면 `/usr/sbin/rsct/bin/rmcctrl -z; rmcctrl -A`
> 로 되살립니다. RMC 가 끊기면 OS 버전과 IP 를 읽을 수 없습니다.

### 호출하는 주소

```
PUT    https://<HMC>:12443/rest/api/web/Logon              ← 세션 토큰 발급
GET    https://<HMC>:12443/rest/api/uom/ManagedSystem      ← 프레임 목록
GET    https://<HMC>:12443/rest/api/uom/ManagedSystem/<uuid>/LogicalPartition
DELETE https://<HMC>:12443/rest/api/web/Logon              ← 세션 정리
```

세션은 매번 정리합니다. 남겨 두면 HMC 의 동시 세션 수 제한에 걸립니다.

---

## 4. 값이 비어 보일 때

HMC 는 **펌웨어 버전마다 XML 의 항목 이름과 위치가 조금씩 다릅니다.** 그래서
경로가 아니라 **항목 이름** 으로 찾습니다. 그래도 버전이 많이 다르면 일부 값이
비어 나올 수 있습니다.

[미리보기] 에서 특정 열만 `-` 로 나온다면, 그 HMC 의 원본 XML 을 확인해 주세요.

```bash
# HMC 에 SSH 로 붙어서 (또는 WAS 에서 curl 로)
curl -k -c /tmp/c -X PUT -H "Content-Type: application/vnd.ibm.powervm.web+xml; type=LogonRequest" \
  -d '<LogonRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/" schemaVersion="V1_0"><UserID>portal_ro</UserID><Password>비번</Password></LogonRequest>' \
  https://<HMC>:12443/rest/api/web/Logon
```

돌려받은 `<X-API-Session>` 값을 헤더에 넣어:

```bash
curl -k -H "X-API-Session: <토큰>" https://<HMC>:12443/rest/api/uom/ManagedSystem
```

그 XML 을 보내 주시면 항목 이름을 맞춰 넣겠습니다
(`asset_sync/collectors/hmc_collector.py` 의 `FRAME_FIELDS` / `LPAR_FIELDS` 한 줄씩 추가).

---

## 5. 지금 되는 것과 다음 단계

**이번에 들어간 것**

- 연계 설정에 AIX HMC 등록 화면 (추가·수정·삭제·사용 중지)
- 연결 테스트 (단계별 실패 사유 표시)
- 미리보기 (프레임·LPAR 실제 값 확인)
- HMC REST 클라이언트와 수집기 (`HMCCollector.collect_all()`)

**아직 안 된 것 — 실제 값으로 매핑을 확인한 뒤에 합니다**

- 07 시 일일 배치에 HMC 수집을 넣고 DB 에 쌓기
- 일간·주간·월간 점검 화면에 AIX 프레임/LPAR 표시
- ITSM ↔ LPAR 정합성 비교 (`rmc_ip` 와 `serial_number` 를 키로)

먼저 **[미리보기] 결과를 한 번 보내 주세요.** 항목 이름이 맞는지 확인하고 나서
배치와 화면을 붙이는 게 안전합니다. 매핑이 틀린 채로 쌓으면 나중에 전부 다시
받아야 합니다.

---

## 6. 자주 막히는 곳

| 증상 | 원인 | 조치 |
|---|---|---|
| `CONNECT` 로 실패 | 방화벽 12443 미개방 | `Test-NetConnection -Port 12443` 로 확인 후 신청 |
| `CONNECT` 인데 방화벽은 열림 | 포트를 443 으로 입력 | 포트를 `12443` 으로 |
| `AUTH` 로 실패 | 비밀번호 만료 | HMC 에서 계정 만료 정책을 끄고 재설정 |
| `QUERY` 로 실패 | Resource Role 이 좁음 | `AllSystemResources` 로 변경 |
| 프레임은 나오는데 LPAR 이 0 | Resource Role 에 해당 프레임 없음 | 역할에 프레임 추가 |
| `os_version`·`rmc_ip` 만 빔 | LPAR 의 RMC 끊김 | LPAR 안에서 `rmcctrl -z; rmcctrl -A` |
| 간헐적으로 실패 | HMC 동시 세션 수 초과 | HMC 콘솔에서 유휴 세션 정리 |
