# 실환경 준비 체크리스트

## Windows / Python

- [ ] Windows Server 2019
- [ ] Python 3.13.x
- [ ] 프로젝트 서비스계정 권한
- [ ] `wheels` 오프라인 패키지

## Oracle

- [ ] Host/VIP 또는 DSN
- [ ] Port
- [ ] Service Name 또는 SID
- [ ] SELECT 전용 사용자명·비밀번호
- [ ] 자산 Table/View · 화면의 [자산 테이블 찾기]에서 선택하면 SQL 자동 생성
- [ ] 조회 계정의 `ALL_TAB_COLUMNS` 등 데이터 딕셔너리 조회 가능 여부
- [ ] CPU/Memory/EOS 실제 컬럼명
- [ ] WAS → Oracle 포트 통신

## PowerCLI / vCenter

- [ ] PowerShell 실행 가능
- [ ] `VMware.VimAutomation.Core` 모듈 확인
- [ ] 각 vCenter IP/FQDN
- [ ] 각 vCenter TCP 443 통신
- [ ] 조회 전용 계정 또는 검증된 Pass-through 계정
- [ ] 인증서 오류 환경의 Ignore 정책 결정
- [ ] 작업 스케줄러 계정에서 동일한 PowerCLI 테스트

## 폴더

- [ ] `data/archive/vcenter` 쓰기
- [ ] `data/temp/powercli` 쓰기
- [ ] `logs` 쓰기
- [ ] SQLite DB 쓰기
