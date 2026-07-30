# 처음부터 실행하고 검증하는 방법

## 1단계: Python 확인

```bat
python --version
```

정상 기준:

```text
Python 3.13.x
```

## 2단계: 인터넷 PC에서 Python 패키지 준비

프로젝트 루트에서:

```bat
scripts\build_offline_wheels.bat
```

완료 후 `wheels` 폴더가 생성된다.

## 3단계: PowerCLI 오프라인 모듈 준비

RVTools는 설치하지 않는다. 인터넷 가능한 Windows PC에서 VMware PowerCLI 모듈을 오프라인 폴더로 내려받는다.

```powershell
Save-Module -Name VMware.PowerCLI -Path <OFFLINE_DOWNLOAD_FOLDER> -Force
```

`<OFFLINE_DOWNLOAD_FOLDER>` 아래의 모듈 폴더 전체를 폐쇄망으로 반입한다. 관리자 PowerShell에서 작업 스케줄러 실행계정도 읽을 수 있는 공용 모듈 경로에 복사한다.

```powershell
$target = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'
Copy-Item '<OFFLINE_DOWNLOAD_FOLDER>\*' $target -Recurse -Force
Get-ChildItem $target -Recurse | Unblock-File
Get-Module -ListAvailable VMware.VimAutomation.Core |
  Sort-Object Version -Descending |
  Select-Object -First 1 Name,Version,Path
```

기존 운영 서버에 이미 PowerCLI가 설치되어 있다면 위 다운로드·복사 단계는 생략하고 모듈 조회만 확인한다.

## 4단계: 폐쇄망 프로젝트 설치

```bat
scripts\install_offline.bat
```

PowerCLI 확인:

```bat
.venv\Scripts\python.exe scripts\check_powercli_installation.py
```

## 5단계: DEMO로 애플리케이션 확인

```bat
scripts\run_demo.bat
scripts\run_flask.bat
```

브라우저에서 WAS 주소와 Flask 포트로 접속한다.

## 6단계: Oracle 등록

`관리 → 연동정보 관리`에서 Oracle Host/VIP, Port, Service Name/SID, 조회 계정, 비밀번호를 입력하고 저장한다.

`접속 테스트`를 눌러 TCP 통신과 로그인이 성공하는지 확인한다.

이어서 `Oracle 자산 테이블 찾기`에서 `자산 테이블 추천` 또는 `테이블 목록 조회`를 실행해 해당 DB의 자산 테이블을 찾고, 컬럼과 샘플 데이터를 확인한 뒤 `자산 원본으로 적용`을 누른다. 선택한 테이블의 컬럼에 맞춰 자산 조회 SQL이 자동 생성된다.

마지막으로 `자산 조회 테스트`를 눌러 SQL 실행과 샘플 조회가 성공하는지 확인한다.

## 7단계: vCenter 등록

PowerCLI 공통 설정은 기본값을 우선 사용한다.

```text
PowerShell 실행파일: powershell.exe
PowerCLI 수집 스크립트: scripts/collect_vcenter_inventory.ps1
스냅샷 폴더: data/archive/vcenter
임시 JSON 폴더: data/temp/powercli
XLSX 자동보관: 사용
```

공통 vCenter 사용자명과 비밀번호를 입력한다. 아래 목록에 각 vCenter의 IP/FQDN을 한 줄씩 등록한다.

`전체 테스트`를 눌러 모든 vCenter의 TCP·로그인·VM 조회를 확인한다.

화면 등록 전 PowerCLI 자체를 수동 확인할 때는 평문 비밀번호를 명령행에 넣지 말고 다음처럼 테스트한다.

```powershell
Import-Module VMware.VimAutomation.Core
$cred = Get-Credential
$vc = Connect-VIServer -Server <VCENTER_IP_OR_FQDN> -Credential $cred -ErrorAction Stop
Get-VM -Server $vc | Select-Object -First 3 Name,PowerState,NumCpu,MemoryMB
Disconnect-VIServer -Server $vc -Confirm:$false
```

## 8단계: 전체 자동수집 수동 실행

```bat
.venv\Scripts\python.exe scripts\check_auto_configuration.py
scripts\run_auto_now.bat
```

확인 위치:

```text
data/archive/vcenter/YYYYMMDD/*.xlsx
logs/asset_sync.log
SQLite snapshot / collection_run / change_event
웹 화면의 전일 대비 및 정합성 메뉴
```

## 9단계: 전일 비교 검증

첫 정상 실행:

```text
ITSM: NO_BASELINE
vCenter: NO_BASELINE
```

다음날 또는 두 번째 테스트 데이터 수집 후:

- ITSM: CM_ID 기준 추가·삭제·상태·CPU·Memory·IP·OS·EOS 변경
- vCenter: VM UUID 중심 추가·삭제·전원·CPU·Memory·IP·DNS·ESXi Host 변경
- ITSM ↔ vCenter: MATCHED, IP_CHANGE_CANDIDATE, HOSTNAME_REVIEW, ITSM_ONLY, RVTOOLS_ONLY, COLLECTION_GAP

## 10단계: 오전 7시 작업 등록

관리자 PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1 `
  -RunTime "07:00" `
  -RunAsUser "<TASK_SCHEDULER_SERVICE_ACCOUNT>"
```

작업 스케줄러 실행계정은 프로젝트·로그·스냅샷 폴더 쓰기 권한, Oracle 네트워크 권한, PowerCLI 모듈 접근권한, vCenter 인증 권한을 가져야 한다.
