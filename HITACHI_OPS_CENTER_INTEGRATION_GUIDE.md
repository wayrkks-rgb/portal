# Hitachi 스토리지 · SAN 연동 가이드

운영 중인 장비 기준으로 다시 정리한 문서입니다.

> **이전 판 정정** — 처음 문서는 Ops Center **API Configuration Manager(23451)** 를
> 주력으로 잡았습니다. 실제 환경에는 그게 없고 Analyzer(22016) · Analyzer detail
> view(8443) · Administrator(20961) 세 개만 있으므로 전제가 틀렸습니다. 아래는
> 실제 구성 기준입니다.

---

## 0. 먼저 알아야 할 것 — 겪고 계신 문제 중 둘은 설정 문제가 아닙니다

| 겪는 일 | 원인 | 답 |
|---|---|---|
| alert 에 디스크 fault 가 안 나온다 | Analyzer 의 alert 은 **임계값 경보**(IOPS·응답시간·사용률)입니다. 디스크·전원·캐시 고장은 배열이 내는 **SIM** 이고 SNMP trap / syslog / Hi-Track 으로 나갑니다 | **라이선스를 사도 안 나옵니다.** SNMP 로 직접 받아야 합니다 |
| SAN 이벤트가 폭주하거나 아예 없다 | FC 스위치는 Ops Center 기본 관리 대상이 아닙니다. Analyzer 의 SAN 스위치 프로브를 따로 붙여야 하고, 반쯤 걸려 있으면 딱 이 증상입니다 | 스위치에서 **SNMP / 스위치 REST** 로 직접 받습니다 |
| System Tasks 에 create/delete LDEV 는 있는데 상세를 모르겠다 | 태스크 로그는 "무슨 작업을 했다"만 남깁니다. "어느 서버에 몇 TB"는 LDEV → 호스트그룹 → LUN 경로를 이어야 나오는데 태스크 화면은 그걸 안 보여줍니다 | 태스크 로그 대신 **매일 구성을 찍어 어제와 비교**합니다 (아래 4장) |

**SNMP · 배열 REST · CCI 는 Ops Center 라이선스와 무관합니다.** 배열 마이크로코드에
들어 있는 기능이라 추가 비용이 없습니다. 무료 버전의 한계를 우회하는 것이 아니라,
애초에 그쪽이 정품 경로입니다.

---

## 1. 운영 장비별 연동 방법

세대에 따라 REST API 가 들어 있는 위치가 다릅니다.

| 장비 | 구성·용량·포트 | 비고 |
|---|---|---|
| **VSP 5100 / 5500 / 5600** | **SVP 의 REST API** (`https://<SVP IP>/ConfigurationManager/v1`) | SVP 주소로 붙습니다. 배열 IP 가 아닙니다 |
| **VSP E590** | **컨트롤러 내장 REST** (`https://<배열 IP>/ConfigurationManager/v1`) | SVP 없음. 가장 단순합니다 |
| **VSP F350 / G350** | **컨트롤러 내장 REST** | 2018년 세대(G350/G370/G700/G900, F350~F900)부터 내장 |
| **VSP F800 / G800** | **내장 REST 없음** | 2015년 세대. **CCI(raidcom)** 로 갑니다 (3-2 참고) |
| **VSP One File 34** | **별도 API** (NAS) | 블록이 아니라 파일 시스템입니다. 아래 5장 |

**장애(디스크 폴트)는 위 구분과 무관하게 열 대 전부 SNMP 로 받습니다.**

### 1-1. 문서로 따지지 말고 직접 물어보세요

세대 구분은 문서마다 조금씩 달라서, 실제로 물어보는 편이 확실합니다. 확인 스크립트를
같이 넣었습니다.

```powershell
cd C:\portal
.venv\Scripts\python.exe scripts\check_storage_api.py ^
  10.0.0.11 10.0.0.12 10.0.0.13 10.0.0.21 10.0.0.31 10.0.0.32 10.0.0.41 10.0.0.42
```

주소가 많으면 파일로 줍니다.

```
# storages.txt  — 한 줄에 하나, # 뒤는 설명
10.0.0.11    # VSP 5500 SVP
10.0.0.12    # VSP 5100 SVP
10.0.0.13    # VSP 5600 SVP
10.0.0.21    # E590
10.0.0.31    # F350
10.0.0.32    # G350
10.0.0.41    # F800
10.0.0.42    # G800
```

```powershell
.venv\Scripts\python.exe scripts\check_storage_api.py --file storages.txt
```

결과는 이렇게 나옵니다.

```
주소                      REST   사유                        내용
----------------------------------------------------------------------------
10.0.0.11               YES    OK                          VSP 5500 S/N 60123 (ID 900000060123, ...)
10.0.0.21               YES    HTTP_401_AUTH_REQUIRED      계정을 넣으면 조회됩니다.
10.0.0.41               NO     TCP_CLOSED                  [WinError 10061] ...
```

- **YES / OK** → 바로 붙습니다. 모델·시리얼·마이크로코드까지 찍힙니다
- **YES / HTTP_401** → REST 는 살아 있고 계정만 넣으면 됩니다. 판단에는 충분합니다
- **NO / TCP_CLOSED** → 방화벽이 막혔거나 그 세대에 REST 가 없습니다
- **NO / NOT_JSON** → 그 주소는 웹 콘솔입니다. SVP 주소를 확인하세요

**이 결과를 그대로 보내 주시면** 장비별로 어느 경로를 쓸지 확정하고 수집기를
만들겠습니다. 계정 없이 도는 확인이라 지금 바로 해 보셔도 됩니다.

---

## 2. 장애 · 폴트 — 열 대 공통 (제일 먼저 하시길 권합니다)

지금 **디스크 폴트를 아무 데서도 못 보고 계신 상태**입니다. 이게 가장 급하고,
세대와 무관하게 한 가지 방법으로 열 대 전부 덮입니다.

### 2-1. 배열에서 SNMP trap 보내기 (스토리지 담당자 작업)

```
Storage Navigator / Maintenance Utility
  → Administration → Alert Notifications
     Notification Alert : All  (또는 Host Report)
     SNMP Agent         : Enable
     SNMP Version       : v2c (또는 v3)
     Community          : public 외 별도 값 권장
     Trap 수신 주소     : <통합 웹 WAS IP>  포트 162
```

받게 되는 것 (SIM):

| 분류 | 예 |
|---|---|
| 드라이브 | 디스크 폐쇄, 예비 디스크 전환, 카피백 |
| 캐시 | 캐시 장애, 캐시 폐쇄, 배터리 이상 |
| 전원·냉각 | PS 고장, 팬 고장, 온도 이상 |
| 경로 | FC 포트 링크 다운, 경로 장애 |
| 풀 | 사용률 임계 초과, 풀 고갈 |

### 2-2. SAN 스위치도 같은 곳으로

Brocade / Cisco 스위치에서도 같은 WAS 로 trap 을 보냅니다.

```
# Brocade FOS
snmpconfig --set snmpv1
snmpconfig --add mibcapability -mib_name SW-MIB
# trap 수신지에 <WAS IP> 추가

# Cisco MDS
snmp-server host <WAS IP> traps version 2c <community>
snmp-server enable traps
```

포트 에러(CRC, link failure, loss of sync)와 포트 다운이 여기로 들어옵니다.
지금 Analyzer 에서 이상하게 나오던 SAN 이벤트를 이걸로 대체합니다.

### 2-3. 방화벽

| 출발 | 목적지 | 포트 | 용도 |
|---|---|---|---|
| 배열 / SVP / SAN 스위치 | 통합 웹 WAS | **UDP 162** | SNMP trap (수신) |
| 통합 웹 WAS | 배열 / SVP | TCP 443 | REST (3장) |
| 통합 웹 WAS | 배열 / SAN 스위치 | UDP 161 | SNMP 폴링 (선택) |

**162 는 들어오는 방향**입니다. 지금까지 신청하신 것과 방향이 반대라 따로 신청이
필요할 수 있습니다.

### 2-4. 통합 웹 쪽 준비 — 휠 하나 반입 필요

SNMP 는 파이썬 표준 라이브러리로 안 됩니다. 휠 두 개를 반입해야 합니다.

| 파일 | 설명 |
|---|---|
| `pysnmp-*.whl` | SNMP 처리. 순수 파이썬이라 컴파일 없음 |
| `pyasn1-*.whl` | pysnmp 가 쓰는 인코딩 라이브러리 |

지금까지 "설치할 것 없다"고 말씀드린 것과 다른 부분이라 미리 말씀드립니다.
HMC 연동과 배열 REST 는 여전히 추가 설치가 없습니다.

---

## 3. 용량 · 할당 · 포트

### 3-1. REST 가 되는 장비 (VSP 5000 계열, E590, F350, G350)

```http
POST   {base}/objects/storages/{id}/sessions              ← Basic 인증, 토큰 발급
GET    {base}/objects/storages/{id}/pools                 ← 풀 용량·할당
GET    {base}/objects/storages/{id}/ldevs?ldevOption=defined&count=16384
GET    {base}/objects/storages/{id}/ports
GET    {base}/objects/storages/{id}/host-groups?portId={portId}
GET    {base}/objects/storages/{id}/luns?portId={portId}&hostGroupNumber={n}
DELETE {base}/objects/storages/{id}/sessions/{sessionId}  ← 반드시 정리
```

`{base}` = `https://<배열 또는 SVP>/ConfigurationManager/v1`

풀에서 얻는 값:

| 값 | 항목 | 의미 |
|---|---|---|
| 총 용량 | `totalPoolCapacity` | 풀의 물리 용량 |
| 사용량 | `usedPoolCapacity` | 실제 쓴 양 |
| **사용률** | `usedCapacityRate` | 증설 판단 |
| 할당량 | `totalLocatedCapacity` | 볼륨에 나눠 준 양 |
| **할당률** | `locatedCapacityRate` | 볼륨 추가 가능 여부 판단 |
| 임계 | `thresholdWarning` / `thresholdDepletion` | 경고·고갈 |

> 통합기 자원 화면과 같은 구분입니다. 씬 프로비저닝에서는 **할당률 200% 인데
> 사용률 40%** 가 흔합니다. 둘을 같은 줄에 나란히 놓고 봐야 판단이 됩니다.

세션은 장비당 동시 개수 제한(기본 64)이 있으므로 끝나면 반드시 지웁니다.
HMC 연동과 같은 방식으로 처리합니다.

### 3-2. REST 가 없는 장비 (F800, G800)

**CCI(Command Control Interface, `raidcom`)** 를 씁니다. 배열에 포함된 도구라
추가 비용이 없고, F800/G800 을 포함해 모든 블록 장비에서 동작합니다.

필요한 것:

1. **커맨드 디바이스** — 50MB 짜리 작은 LUN 을 한 대의 호스트에 매핑합니다
   (스토리지 담당자 작업)
2. 그 호스트에 **CCI 설치** — 배열 미디어에 포함
3. 통합 웹이 그 호스트의 결과를 읽어 옵니다

```bash
raidcom get pool     -I<instance>     # 풀 용량·사용량
raidcom get ldev -ldev_list defined -I<instance>
raidcom get port     -I<instance>
raidcom get host_grp -port CL1-A -I<instance>
raidcom get lun      -port CL1-A-0   -I<instance>
```

커맨드 디바이스를 어느 호스트에 붙일지가 관건입니다. 이미 LUN 이 붙어 있는
AIX/Linux 호스트가 있으면 거기가 가장 간단합니다. 통합 웹 WAS 에 직접 붙이려면
WAS 에 FC/iSCSI 연결이 있어야 합니다.

> **F800/G800 은 나중으로 미뤄도 됩니다.** REST 가 되는 장비부터 붙이고, 커맨드
> 디바이스 이야기는 그 다음에 하는 편이 진행이 빠릅니다.

---

## 4. "어느 서버에 몇 TB 할당했나" — 태스크 로그 말고 차분으로

System Tasks 로는 원하는 답이 안 나옵니다. 대신 **매일 구성을 통째로 찍어 두고
어제와 비교**합니다. ITSM·vCenter 에 이미 쓰고 있는 방식 그대로입니다.

```
2026-09-21  LDEV 00:1A  2TB  →  CL1-A / 호스트그룹 AIX-WAS-01
2026-09-22  LDEV 00:1A  2TB  →  (없음)                              ← 회수 2TB
            LDEV 00:2B  8TB  →  CL3-B / 호스트그룹 ESXI-CLUSTER-01   ← 신규 8TB
```

태스크 로그보다 정확합니다. 태스크가 실패했거나 나중에 수동으로 바뀐 것까지
**실제 구성 기준**으로 잡히기 때문입니다.

LDEV 의 `label` 에 업무명을 적어 두셨다면 그것도 같이 나옵니다. 안 적혀 있으면
호스트그룹 이름으로 서버를 찾습니다. 호스트그룹 → WWN → 서버를 이으면 ITSM 자산과
연결되고, 그러면 "이 서버가 스토리지를 얼마나 쓰고 있나" 가 자산 화면에 붙습니다.

---

## 5. VSP One File 34 는 따로 봅니다

이건 블록 배열이 아니라 **NAS(파일)** 입니다. LDEV·풀·호스트그룹 개념이 없고,
파일 시스템·쿼터·공유 단위로 봅니다.

| 블록 배열 | VSP One File |
|---|---|
| 풀 용량·사용률 | 스토리지 풀 / 파일 시스템 용량 |
| LDEV | 파일 시스템 |
| 호스트그룹 / LUN | CIFS·NFS 공유(export) |
| WWN | 클라이언트 IP / 액세스 권한 |

관리 API 도 다릅니다. 담당자에게 **VSP One File 관리 주소와 REST API 사용 가능
여부**를 확인해 주세요. 블록 장비를 먼저 붙이고 이건 그 다음 단계로 잡는 것을
권합니다 — 화면 구성이 아예 다르기 때문에 같이 하면 둘 다 늦어집니다.

---

## 6. Analyzer 와 Administrator 는 어디에 쓰나

있는 걸 버릴 이유는 없습니다. 역할만 바꿉니다.

| 제품 | 포트 | 쓸 곳 |
|---|---|---|
| Analyzer detail view | 8443 | **사용률·성능 추이**. 세밀한 지표는 여기 있습니다 |
| Analyzer | 22016 | 임계값 경보(용량·성능). fault 는 여기가 아닙니다 |
| Administrator | 20961 | 보조 확인. REST 가 없는 장비의 구성 조회 대안 |

**fault 와 구성 스냅샷은 2·3장 경로로, 성능·사용률 추이는 Analyzer 로.**
이렇게 나누면 지금 Analyzer 에서 겪는 "많거나 없거나" 문제를 피할 수 있습니다.

---

## 7. 권하는 순서

| 순서 | 할 일 | 얻는 것 | 필요한 것 |
|---|---|---|---|
| **1** | SNMP trap 수신 (배열 10대 + SAN 스위치) | **디스크 폴트가 보이기 시작** | UDP 162 개방, pysnmp 휠 |
| **2** | REST 되는 장비 구성·용량 수집 | 풀 사용률·할당률, 포트 현황 | 조회 계정, TCP 443 |
| **3** | 일별 스냅샷 차분 | "어느 서버에 몇 TB" 자동 집계 | 2번이 돌면 자동 |
| **4** | Analyzer detail view 연동 | 성능 추이, 월간 보고서 그래프 | 8443 개방, 계정 |
| **5** | F800/G800 CCI | 남은 두 대 | 커맨드 디바이스 |
| **6** | VSP One File 34 | NAS 용량·공유 | 별도 검토 |

1번을 먼저 하시길 권합니다. 지금 **아무 데서도 디스크 폴트를 못 보고 계신 게
가장 위험**하고, 세대와 무관하게 열 대가 한 번에 덮이기 때문입니다.

---

## 8. 지금 주시면 되는 것

1. `scripts\check_storage_api.py` **실행 결과** (계정 불필요, 지금 바로 가능)
2. 위 결과에서 REST 가 되는 장비 하나의 **풀·포트 응답 JSON**
   ```powershell
   # 세션 발급
   curl.exe -k -u <조회계정>:<비번> -X POST -H "Content-Type: application/json" ^
     https://<배열>/ConfigurationManager/v1/objects/storages/<id>/sessions
   # 풀 (위 응답의 token 사용)
   curl.exe -k -H "Authorization: Session <token>" ^
     https://<배열>/ConfigurationManager/v1/objects/storages/<id>/pools
   # 포트
   curl.exe -k -H "Authorization: Session <token>" ^
     https://<배열>/ConfigurationManager/v1/objects/storages/<id>/ports
   # 세션 정리 (꼭)
   curl.exe -k -X DELETE -H "Authorization: Session <token>" ^
     https://<배열>/ConfigurationManager/v1/objects/storages/<id>/sessions/<sessionId>
   ```
3. SAN 스위치 **제조사와 모델** (Brocade / Cisco, FOS·NX-OS 버전)
4. UDP 162 **방화벽 신청 가능 여부**

마이크로코드 버전에 따라 응답 항목이 조금씩 다릅니다. 실제 값을 보고 맞추는 편이
확실하고, 틀린 매핑으로 쌓으면 나중에 전부 다시 받아야 합니다.

계정·비밀번호는 문서나 메일로 보내지 마세요. 화면(연계 설정)에서 직접 입력받고,
저장된 값은 `config/app_config.local.yaml` 에만 들어가며 화면으로 다시 내려오지
않습니다.
