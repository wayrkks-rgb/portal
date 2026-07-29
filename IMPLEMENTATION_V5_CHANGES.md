# v5 변경사항

## 1. 여러 vCenter 일괄 관리 UI

- RVTools 공통 실행경로, 기본 포트, 인증방식, 계정, SSL 정책을 상단에서 한 번만 설정
- vCenter는 목록형 표로 일괄 관리
- 빈 행 추가, 다중 행 붙여넣기, CSV/TXT 불러오기, 선택 삭제 지원
- 공통 인증 프로필과 vCenter별 개별 인증 프로필 지원
- vCenter ID 및 주소 중복 저장 차단
- 각 행 개별 테스트와 활성 vCenter 전체 테스트 제공

## 2. Oracle 직접수집 검증

화면에 저장한 Oracle 설정을 사용하여 다음 경로를 실행한다.

1. TCP 통신
2. Oracle 로그인
3. 설정 SQL 실행
4. 샘플 레코드 조회
5. 설정된 CPU/Memory/EOS 포함 필수 컬럼 검증
6. 전체 수집 후 SQLite 스냅샷 저장
7. 변경 이벤트 생성

## 3. vCenter → RVTools → XLSX 검증

vCenter 주소는 각 목록 행에 저장하고 프로그램이 RVTools 실행 인자를 자동 생성한다. `command_template` 입력은 사용하지 않는다.

1. vCenter TCP 통신
2. RVTools subprocess 실행(`shell=False`)
3. vCenter별 XLSX 생성
4. `vInfo` 시트 확인
5. VM, Powerstate, CPUs, Memory 필수 컬럼 확인
6. 데이터 행 확인
7. 여러 vCenter 결과 통합
8. SQLite 스냅샷 저장 및 변경 이벤트 생성

## 4. 전일 대비 화면

- ITSM과 RVTools를 별도 패널로 표시
- 최신 정상 스냅샷과 **이전 날짜의 마지막 정상 스냅샷**을 비교
- 전일 자료가 없을 때만 동일 일자의 직전 실행을 검증용 기준으로 사용하고 화면에 명시
- 비교 날짜, 건수, 순증감 표시
- 추가, 삭제, 일반 변경, 상태 변경, COLLECTION_GAP 집계
- 자산별 이벤트 유형, 필드, 이전값, 현재값 표시

일일 수집 중 생성되는 변경 이력은 연속 실행 간 비교로 유지하고, `전일 대비` 전용 화면은 날짜가 다른 스냅샷을 명시적으로 다시 비교한다.

## 5. 자동화 검증

비식별 Synthetic Oracle 모듈과 가짜 RVTools 실행파일로 실제 코드 호출 경로를 검증했다.

- Oracle connect → SQL → schema validation → 정규화 → SQLite snapshot → diff
- vCenter 2대 → subprocess → 실제 XLSX 생성 → vInfo parse → 통합 snapshot → diff
- 최신일자와 이전 날짜 스냅샷 선택 로직
- 공통/개별 vCenter 인증 설정 및 비밀정보 마스킹
