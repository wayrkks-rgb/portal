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
- DatastoreUsage: 데이터스토어 용량·사용·사용률, VM 할당(프로비저닝)·할당률, 과할당 여부
- VM 변경 이력: 신규, 삭제, CPU, Memory, Host 이동, vCenter 이동
- 화면 조건 그대로 Excel Export

## 디스크는 두 가지 숫자다

CPU·메모리와 같은 구분이 디스크에도 있고, 디스크에서는 차이가 더 크다.

| 값 | 뜻 | 계산 |
| --- | --- | --- |
| 사용률 | 지금 실제로 차 있는 양 | (용량 − 여유) ÷ 용량 |
| 할당률 | VM 에게 약속한 양 | (사용 + 미사용 씬 몫) ÷ 용량 |

씬 프로비저닝(Thin)은 VMDK 를 100GB 로 만들어도 실제로 쓴 만큼만 데이터스토어를
차지한다. 그래서 **할당률이 100% 를 넘을 수 있다**(과할당). 사용률만 보고 "아직
반이 남았다" 고 읽으면, VM 들이 약속받은 만큼 채우는 순간 데이터스토어가 꽉 차고
그 위의 VM 이 전부 멈춘다. 두 값을 같이 보아야 한다.

미사용 씬 몫은 vCenter 의 데이터스토어 요약(`Summary.Uncommitted`)에 이미 계산돼
있어 VM 을 하나하나 더하지 않는다. VM 단위 값은 `Summary.Storage` 의
`Committed`(쓴 양)와 `Uncommitted`(아직 안 쓴 씬 몫)에서 받는다. 두 값 모두 일괄
조회 속성이라 VM 수만큼 왕복이 늘지 않는다.

데이터스토어는 ESXi 여러 대가 함께 쓰므로 통합기(클러스터)에 매달지 않는다. 한
클러스터만 마운트한 경우에만 클러스터명을 붙인다. 통합기·ESXi 줄의 디스크 칸은
용량 비율이 아니라 **그 위 VM 이 차지한 양**(할당 / 사용)이며, 남은 공간은
데이터스토어 표에서 본다.

디스크는 평균이 쓸모 없다. 기간 조회에서는 **마지막 날의 값**을 지금 모습으로 쓰고,
기간 중 가장 찼을 때를 따로 적는다.

## PowerShell

사용자가 제공한 `ResourceUsageExport_new_range.ps1`은 수집 기준 참고자료로 사용했습니다.
실제 자동화용 스크립트는 기존 vCenter 등록정보와 환경변수 인증방식을 재사용하도록
`scripts/collect_vcenter_resource_usage.ps1`로 별도 구현했습니다. 계정·비밀번호·내부 주소는
스크립트나 명령행에 저장하지 않습니다.
