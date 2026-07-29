# 구현 요약

현재 프로젝트는 Oracle ITSM 직접조회와 PowerCLI vCenter 직접수집을 운영 기본으로 사용한다.

- Oracle: python-oracledb Thin 기본, 화면 등록 및 연결/조회 테스트
- vCenter: PowerCLI `Connect-VIServer` + `Get-VM`
- 다중 vCenter: 일괄 붙여넣기/CSV/공통계정/개별계정
- 보안: 비밀번호 API 미반환, 명령행 미포함, 로컬 설정파일만 저장
- 보관: vCenter별 JSON 수집 결과와 날짜별 XLSX
- 비교: ITSM 전일/오늘, vCenter 전일/오늘
- 정합성: Hostname/IP/내부 identity map 기반
- 실패보호: 특정 vCenter 실패 시 COLLECTION_GAP, 삭제 확정 금지
- 스케줄: 매일 07:00
