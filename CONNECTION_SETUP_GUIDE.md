# Oracle 및 PowerCLI vCenter 연동 설정 가이드

## 수집 구조

```text
WAS Python → PowerShell 실행 → Connect-VIServer → Get-VM
          → JSON 원본 저장 → SQLite 스냅샷 → 전일 비교
          → 자체 XLSX 보관본 생성(선택)
```

RVTools 실행파일이나 RVTools XLSX는 필요하지 않다. 화면의 XLSX 자동보관은 PowerCLI 조회 결과를 이 프로젝트가 자체 생성하는 기능이다.

## 1. WAS에서 필요한 통신

Oracle:

```powershell
Test-NetConnection <ORACLE_HOST_OR_VIP> -Port <ORACLE_PORT>
```

각 vCenter:

```powershell
Test-NetConnection <VCENTER_IP_OR_FQDN> -Port 443
```

두 테스트 모두 `TcpTestSucceeded : True`여야 한다.

## 2. Oracle 화면 입력값

`관리 → 연동정보 관리 → Oracle ITSM`에서 입력한다.

| 항목 | 입력값 |
|---|---|
| 사용 | 사용 |
| ITSM 수집모드 | 자동수집 - Oracle 직접조회 |
| Oracle 모드 | Thin 권장 |
| Host 또는 VIP | Oracle 접속 주소 |
| DSN | RAC·이중화 연결 기술자가 필요할 때 사용 |
| Port | Listener 포트 |
| Service Name 또는 SID | 실제 접속 서비스 |
| 조회 계정 | 자산 Table/View SELECT 권한이 있는 읽기 전용 계정 |
| 비밀번호 | 해당 DB 계정 비밀번호 |

Table/View와 SQL 파일, CPU·Memory·EOS 컬럼은 이 화면에서 직접 입력하지 않는다.
Table/View는 아래 `2-1`의 자산 테이블 찾기에서 선택하고, SQL 파일
(`config/oracle_query.local.sql`)은 그때 자동 생성된다.

`Oracle 연결 테스트`는 다음을 확인한다.

```text
TCP 통신 → DB 로그인 → SQL 실행 → 샘플 조회 → 필수 컬럼 검증
```

## 2-1. 자산 테이블 찾기

자산 조회 테스트가 `ORA-00942`(테이블 또는 뷰가 없습니다)나
`서버 내부의 Oracle 자산 조회 SQL이 준비되지 않았습니다`로 실패하면 조회 대상
객체가 등록되지 않았거나 실제 DB의 객체명과 다른 경우다. 접속정보를 입력한 뒤
`관리 → 연동정보 관리 → Oracle 자산 테이블 찾기`에서 해결한다.

| 버튼 | 동작 |
|---|---|
| 자산 테이블 추천 | `CM_ID`, `CM_HOSTNAME` 등 자산 컬럼이 가장 많이 포함된 객체를 순서대로 제시 |
| 테이블 목록 조회 | 검색어·소유자 조건으로 조회 계정이 접근 가능한 테이블/뷰 목록 조회 |
| 컬럼 보기 | 선택 객체의 컬럼 정의와 최대 10행 샘플 데이터 확인 |
| 매핑 미리보기 | 선택 객체 기준으로 생성될 자산 조회 SQL과 컬럼 매핑 결과만 확인 |
| 자산 원본으로 적용 | `asset_source` 저장 + 조회 SQL 생성 + 자산 조회 재검증 |

조회는 모두 `ALL_TAB_COMMENTS`, `ALL_TAB_COLUMNS` 등 읽기 전용 데이터 딕셔너리
뷰를 사용하며 조회 계정에 보이는 객체만 나타난다. 목록이 비어 있으면 DBA에게
해당 객체의 `SELECT` 권한을 요청한다.

컬럼 매핑은 다음 순서로 자동 결정된다.

```text
1. 컬럼명이 완전히 같은 경우            예: CM_HOSTNAME → CM_HOSTNAME
2. CM_ 접두어를 제외하고 같은 경우      예: HOSTNAME    → CM_HOSTNAME
3. 사이트별 관용 표기 사전              예: IP_ADDR     → CM_IP
4. 위 어느 것도 없으면 NULL로 조회
```

원본 테이블에 없는 컬럼을 `NULL`로 조회하기 때문에 필수 컬럼 검증이 통과하며,
그 항목은 화면의 매핑 결과에 `❌ 없음`으로 표시된다. 실제 컬럼으로 교체하거나
`WHERE` 조건(예: `CM_CAT_CD IN ('HW0101','HW0102','HW0104')`)을 추가하려면 생성된
`config/oracle_query.local.sql`을 직접 수정한다. 이 파일은 `.gitignore` 대상이며
폐쇄망 서버에만 존재한다.

## 3. PowerCLI 설치 확인

프로젝트는 `RVTools.exe`를 사용하지 않는다. PowerCLI 모듈이 필요하다.

PowerShell에서 확인:

```powershell
Get-Module -ListAvailable VMware.VimAutomation.Core |
  Sort-Object Version -Descending |
  Select-Object -First 1 Name,Version,Path
```

결과가 없으면 PowerCLI 오프라인 ZIP을 PowerShell 모듈 경로 중 하나에 압축 해제한다.

```powershell
$env:PSModulePath -split ';'
Get-ChildItem '<POWERCLI_MODULE_FOLDER>' -Recurse | Unblock-File
```

프로젝트 점검:

```bat
.venv\Scripts\python.exe scripts\check_powercli_installation.py
```

## 4. PowerCLI 공통 설정

`관리 → 연동정보 관리 → PowerCLI / vCenter 일괄 관리`에서 입력한다.

| 항목 | 권장값 |
|---|---|
| 수집모드 | `POWERCLI` |
| PowerShell 실행파일 | `powershell.exe` 또는 실제 전체 경로 |
| PowerCLI 수집 스크립트 | `scripts/collect_vcenter_inventory.ps1` |
| 원본 스냅샷 보관 폴더 | `data/archive/vcenter` |
| 임시 JSON 폴더 | `data/temp/powercli` |
| XLSX 자동보관 | 사용 |
| 기본 포트 | 443 |
| Timeout | 1800초 |
| 재시도 | 1 |

## 5. vCenter 인증방식

### vCenter 계정

일반적인 운영 방식이다.

- 사용자명: vCenter 조회 전용 계정
- 비밀번호: 해당 계정 비밀번호
- 최소한 VM 인벤토리를 조회할 수 있는 읽기 권한 필요

비밀번호는 PowerCLI 명령행 인자에 넣지 않는다. Python이 PowerShell 자식 프로세스의 환경변수로 전달하고 PowerShell에서 `PSCredential`로 변환한다.

### Pass-through

작업 스케줄러 실행 Windows 계정 자체가 vCenter 인증에 사용되는 환경에서만 선택한다. 일반 AD 계정으로 vCenter에 로그인했던 기존 운영 방식과 일치하는지 먼저 수동 검증해야 한다.

## 6. 여러 vCenter 등록

공통계정을 사용하는 경우 상단 공통 사용자명과 비밀번호를 한 번 입력한다. 아래 표에는 vCenter별 주소만 등록한다.

```text
ID,화면명,주소,사이트,사용여부,포트,인증프로필
vc_001,운영 VC 1,<VCENTER_ADDRESS_001>,PROD,Y,443,COMMON
vc_002,운영 VC 2,<VCENTER_ADDRESS_002>,PROD,Y,443,COMMON
vc_003,DR VC,<VCENTER_ADDRESS_003>,DR,Y,443,CUSTOM
```

`CUSTOM` 행은 해당 행에 개별 사용자명·비밀번호를 입력한다.

## 7. vCenter 테스트가 확인하는 단계

```text
1. WAS → vCenter TCP 443
2. PowerShell 실행파일 확인
3. VMware.VimAutomation.Core 모듈 Import
4. Connect-VIServer 로그인
5. Get-VM 조회
6. JSON 생성
7. 필수 필드 확인
8. XLSX 보관본 생성
```

필수 필드:

```text
VM
Powerstate
CPUs
Memory
```

주요 식별·비교 필드:

```text
VM UUID
SMBIOS UUID
VM ID
DNS Name
Primary IP Address
Host
Cluster
Datacenter
VI SDK Server
```

## 8. 자동수집 검증

설정 준비검사:

```bat
.venv\Scripts\python.exe scripts\check_auto_configuration.py
```

전체 연결 테스트:

```bat
.venv\Scripts\python.exe scripts\test_all_connections.py
```

전체 수집·비교:

```bat
scripts\run_auto_now.bat
```

첫 실행은 비교 기준이 없으므로 `NO_BASELINE`이 정상이다. 다음 정상 수집부터 추가·삭제·CPU·Memory·IP·DNS·전원·Host 이동 등의 이벤트가 생성된다.

특정 vCenter 수집에 실패하면 해당 vCenter의 기존 VM을 삭제로 확정하지 않고 `COLLECTION_GAP`으로 보호한다.
