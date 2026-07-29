# V8 통합서버 자원사용률 자동화 변경사항

## 07:00 자동 처리 흐름

1. vCenter 인벤토리 수집
2. VM 신규·삭제·CPU·Memory·ESXi Host 이동 비교
3. Oracle ITSM 수집 및 비교
4. 동일 일일 배치의 ITSM-vCenter 정합성 분석
5. 동일 vCenter 등록정보로 전일 ESXi/VM 자원사용률 수집
6. 자원사용률 결과를 같은 `daily_batch_id`와 `vcenter_snapshot_id`에 연결
7. 통합기별 VM 대수와 VM 소속을 해당 인벤토리로 확정
8. 대시보드·기간조회·Excel Export에 공통 사용

## 자원사용률 화면

- 임의 시작일/종료일 조회
- vCenter, Cluster, ESXi Host 필터
- HostResourceUsage: 통합기 VM 대수, 실제 CPU/Memory, CPU/MEM Max·Avg
- VMsResource: 통합기별 VM, 전원상태, 실제 할당 CPU/Memory, CPU/MEM Max·Avg
- VM 변경 이력: 신규, 삭제, CPU, Memory, Host 이동, vCenter 이동
- 화면 조건 그대로 Excel Export

## PowerShell

사용자가 제공한 `ResourceUsageExport_new_range.ps1`은 수집 기준 참고자료로 사용했습니다.
실제 자동화용 스크립트는 기존 vCenter 등록정보와 환경변수 인증방식을 재사용하도록
`scripts/collect_vcenter_resource_usage.ps1`로 별도 구현했습니다. 계정·비밀번호·내부 주소는
스크립트나 명령행에 저장하지 않습니다.
