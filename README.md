# ITSM–vCenter 자산 정합성 및 변경이력 시스템 — PowerCLI 버전

Windows Server 2019 폐쇄망에서 다음 작업을 자동화한다.

```text
매일 07:00
→ Oracle ITSM 자산 직접 조회
→ 등록된 모든 vCenter에 PowerCLI 연결
→ VM 인벤토리 수집
→ 원본 JSON 및 XLSX 보관
→ SQLite 오늘 스냅샷 저장
→ ITSM 전일 대비 비교
→ vCenter 전일 대비 비교
→ 당일 ITSM ↔ vCenter 정합성 분석
→ 웹 화면 갱신
```

RVTools 프로그램은 사용하지 않는다. vCenter 수집은 PowerShell의 `VMware.VimAutomation.Core` 모듈로 수행한다.

## 운영 기본 모드

- ITSM: `ORACLE`
- vCenter: `POWERCLI`
- Python: 3.13
- DB: SQLite (단일 호스트) / MySQL (여러 WAS가 하나의 DB를 공유할 때)
- 기본 실행 시각: 07:00

`DEMO`는 설치 확인용이며 `FILE_ONLY`는 장애 분석이나 과거 스냅샷 재처리용 보조 모드다.

## PowerCLI 수집과 파일 비교 방식

```text
vCenter → PowerCLI 조회 → JSON 원본 → Python 정규화 → SQLite 스냅샷
                                      └→ 자체 XLSX 보관본(선택)
```

전일 비교는 XLSX 파일명이나 수동 업로드를 기준으로 하지 않는다. SQLite에 저장된 최신 정상 스냅샷과 직전 정상 스냅샷을 비교한다. XLSX는 운영 확인과 감사용 보관본이며 RVTools 형식의 필수 설치파일이 아니다.

## 주요 디렉터리

```text
application/                         Flask 기존 기능
asset_sync/collectors/powercli_collector.py
scripts/collect_vcenter_inventory.ps1
jobs/collect_vcenter.py
jobs/daily_batch.py
config/app_config.yaml               안전한 기본값
config/app_config.local.yaml         폐쇄망 로컬값, Git 제외
config/vcenters.local.yaml           vCenter 주소·계정, Git 제외
.env                                 Oracle 접속정보, Git 제외
data/archive/vcenter/YYYYMMDD/       날짜별 vCenter XLSX
```

## 설치 개요

1. 인터넷 PC의 Python 3.13 환경에서 `scripts\build_offline_wheels.bat` 실행
2. 프로젝트와 `wheels` 폴더를 폐쇄망으로 반입
3. PowerCLI 오프라인 ZIP을 폐쇄망으로 반입하고 PowerShell 모듈 경로에 설치
4. 폐쇄망에서 `scripts\install_offline.bat` 실행
5. `scripts\run_flask.bat` 실행
6. 관리 화면에서 Oracle과 vCenter 정보 등록
7. Oracle 연결 테스트와 vCenter 전체 테스트 실행
8. `scripts\run_auto_now.bat`로 전체 배치 검증
9. 작업 스케줄러 등록

상세 절차는 `BEGINNER_RUN_AND_VALIDATION_GUIDE.md`와 `CONNECTION_SETUP_GUIDE.md`를 참고한다.
여러 WAS를 붙여 운영하려면 `MULTI_WAS_MYSQL_GUIDE.md`를 함께 본다.
