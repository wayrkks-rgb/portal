# AIX(HMC) · Hitachi 스토리지 · Brocade SAN 연동 검토

목표: 통합서버 자원사용현황에 AIX 를 넣고, SAN 스토리지 · SAN 스위치를 일 · 주 · 월로 점검한다.
기존 원칙(서버 1대 = 한 곳, 매일 스냅샷 → 어제와 비교, 폐쇄망 = 새 패키지 최소화)을 그대로 쓴다.

---

## 1. AIX(HMC) → 통합서버 자원사용현황

### 1-1. 지금 상태

| 항목 | 상태 |
| --- | --- |
| 연결 테스트 · 미리보기 | 있음 (관리 → 연계 설정 → HMC) |
| 저장 · 일별 스냅샷 | **없음** — 그래서 통합서버 자원사용현황에 안 나온다 |
| 오류 | 아래 1-3 의 원인 4가지를 고쳤다(2026-11-04). 남은 오류는 `check_hmc_api.py` 로 단계를 확인 |

### 1-2. 넣는 방법 (설계)

통합서버 자원사용현황 표(`host_resource_usage_daily`)는 "호스트 1줄 = 그 위 VM 묶음" 구조다.
AIX 는 **프레임 = 호스트, LPAR = VM** 으로 같은 표에 넣으면 화면 · 월간 보고서 · Excel 을 새로 만들 필요가 없다.

| 표의 칸 | vCenter(ESXi) | AIX(HMC) |
| --- | --- | --- |
| 통합기 | vCenter | `HMC:<id>` |
| 호스트 | ESXi | 프레임(ManagedSystem) 이름 |
| VM 수 | VM 수 | 실행 중 LPAR 수(VIOS 는 따로 표시) |
| 할당 CPU | vCPU 합 | LPAR **처리 장치(proc units) 합** (dedicated 는 코어 수) |
| 할당 메모리 | VM 메모리 합 | LPAR 메모리 합 |
| 총 용량 | 호스트 코어 · 메모리 | 프레임 설치 proc units · 메모리 |
| CPU 사용률(최대/평균) | vCenter 성능 | **PCM(성능 데이터)** — HMC 에서 켜져 있어야 함. 꺼져 있으면 비움 |

주의할 점
- **CPU 단위가 다르다.** vCPU 와 proc units 는 더하면 안 된다. 같은 표에 두되 합계는 x86 / AIX 를 나눈다.
- **AIX 메모리는 나눠 주면 그만큼 쓰는 것**(dedicated)이라 메모리 "사용률" 대신 할당률을 쓴다.
- LPAR ↔ ITSM 자산 짝짓기: LPAR 이름 · RMC IP ↔ CM_HOSTNAME · CM_IP (vCenter 와 같은 엔진).
- 수집: 07:00 일일 배치에 붙인다. HMC 는 REST(표준 라이브러리 urllib)라 새 패키지가 없다.

### 1-3. "방화벽 · 계정은 맞는데 오류" — 고친 것과 확인할 것

| 원인 | 증상 | 조치 |
| --- | --- | --- |
| Windows 시스템(IE) 프록시를 urllib 이 저절로 씀 | 403 · Tunnel failed · 시간 초과 | **고침**: 기본 직접 연결(`hmc.use_proxy: false`) |
| 오래된 HMC TLS 를 파이썬 3.13(OpenSSL 3)이 막음 | `unsafe legacy renegotiation disabled` · `dh key too small` | **옵션 추가**: `hmc.legacy_tls: true` |
| LPAR 없는 프레임이 HTTP 204(빈 본문) | "응답을 읽지 못했습니다" | **고침**: 빈 목록 |
| 로그온 Accept 헤더 | HTTP 406 | **고침**: `type=LogonResponse` |
| TLS · 시간 초과도 "방화벽 확인" 으로 안내 | 엉뚱한 곳을 봄 | **고침**: 원인별 안내 |
| HMC 사용자 속성 'Allow remote access via the web' 꺼짐 | 401 | HMC 관리자 확인 |
| 프레임 · LPAR 이 많아 느림 | 시간 초과 | `hmc.timeout_seconds` 늘림 |

```bat
.venv\Scripts\python.exe scripts\check_hmc_api.py
```
프록시 → 이름 · TCP → TLS → 로그온 → 프레임 → LPAR → PCM(사용률) 순으로 어디서 막히는지 찍는다.
**실패한 단계의 메시지를 그대로 주시면** 다음 조치를 정할 수 있다.

---

## 2. Hitachi 스토리지 (블록 · NAS)

### 2-1. 수집 경로 (기존 안내서 HITACHI_OPS_CENTER_INTEGRATION_GUIDE.md 와 같음)

| 장비 | 경로 | 비고 |
| --- | --- | --- |
| VSP 5000 계열 · E590 · F350/G350 | 배열(또는 SVP) **REST** `ConfigurationManager/v1` | 조회 계정, TCP 443 |
| F800 / G800 | **CCI(raidcom)** | 커맨드 디바이스 · CCI 설치 호스트 필요 |
| NAS(VSP One File 등) | 별도 관리 API | 모델 · API 확인 필요 |

### 2-2. 보고 싶은 것 → 어떻게 만드나

| 보고 항목 | 데이터 | 계산 |
| --- | --- | --- |
| 총 용량 · 사용량 · 할당량 · 여유량 | 풀(pool) | 여유 = 총 − 사용. 할당(located)은 씬 프로비저닝이라 총보다 클 수 있음 → 할당률 따로 |
| 일 · 주 · 월 증감 | 풀 일별 스냅샷 | 기간 첫날 대비 끝날 (서버 대수와 같은 방식) |
| 단위 GB/TB 선택 | 표시만 바꿈 | **10진(GB=10⁹) / 2진(GiB=2³⁰) 도 고르게** — Hitachi 화면은 2진, 보고서는 10진인 경우가 많아 숫자가 7% 어긋남 |
| 할당 · 회수 내역 | LDEV + 경로(포트 · 호스트그룹 · LUN) 일별 스냅샷 | 어제와 비교: 경로가 새로 생기면 **할당**, 사라지면 **회수**, 크기만 바뀌면 **확장** |
| 어떤 서버에 | 호스트그룹 이름 · WWN | WWN ↔ Brocade name server(포트맵) ↔ alias ↔ ITSM 자산으로 이음 |

할당 · 회수 한 줄 예:

| 날짜 | 구분 | 스토리지 | LDEV | 크기 | 포트 / 호스트그룹 / LUN | 서버(추정) | 근거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 11-03 | 할당 | VSP5500-01 | 00:2B | 8 TB | CL3-B / ESXI-CL01 / 12 | ESXI-CL01 (ITSM CM0123) | 호스트그룹 WWN = SAN alias esxi01_hba0 |
| 11-04 | 회수 | VSP5500-01 | 00:1A | 2 TB | (없음) ← CL1-A / AIX-WAS-01 | AIX-WAS-01 | 어제 경로가 오늘 없음 |

"서버 1대 = 한 곳" 처럼 **LDEV 1개 = 기간 중 한 곳**(할당 · 회수 · 변경)으로 접는다.

---

## 3. Brocade SAN

### 3-1. 수집 경로 — REST 우선, 안 되면 SSH CLI

| 방식 | 조건 | 새 패키지 |
| --- | --- | --- |
| **FOS REST API** (`/rest/running/...`) | FOS 8.2 이상 · REST 사용 켜짐(`mgmtapp --show`) | 없음(urllib) |
| SSH CLI (`switchshow`, `cfgactvshow` …) | SSH 키 로그인 허용 | 없음(Windows 기본 ssh.exe, HMC 진단과 같은 방식) |

스위치마다 FOS 버전이 다를 수 있으므로 **스위치별로 REST/CLI 를 고르는 구조**로 만든다.

### 3-2. 항목별

| 보고 항목 | CLI | REST(FOS 8.2+) | 저장 · 판정 |
| --- | --- | --- | --- |
| 포트맵 | `switchshow`, `nsshow` | `brocade-interface/fibrechannel`, `brocade-name-server` | 일별 스냅샷. 포트 ↔ WWPN ↔ alias ↔ 서버 |
| 현재 조닝 | `cfgactvshow` | `brocade-zone/effective-configuration` | 일별 스냅샷 → 어제와 비교(존 · 멤버 추가/삭제) |
| 포트에 걸린 조닝 스크립트 | (위 결과로 만듦) | (위 결과로 만듦) | 포트 WWPN 이 속한 존을 `alicreate` · `zonecreate` · `cfgadd` 형태로 재구성해 보여줌 |
| errdump | `errdump -r` | FOS 버전에 따라 RASlog REST 유무가 다름 → 확인 필요 | 마지막으로 읽은 시각 이후만. ERROR/CRITICAL 대시보드 |
| SFP 광량 | `sfpshow -all` | `brocade-media/media-rdp` | 기준 미만이면 대시보드. 기준은 아래 3-3 |
| 포트 오류(CRC 등) | `porterrshow` | `brocade-interface/fibrechannel-statistics` | **누적값이라 하루 증가분으로 판정**(아래 3-3) |

### 3-3. 판정에서 조심할 것

- **porterrshow 는 누적값이다.** 스위치 재기동 · `portstatsclear` 전까지 계속 쌓이므로 "오늘 CRC 가 있다" 가
  아니라 **어제보다 늘었나**로 본다. 값이 줄었으면(초기화) 그날은 기준점만 다시 잡는다.
  대상: crc_err · enc_out · link_fail · loss_sync · loss_sig · disc_c3 등, 칸별 임계값은 설정.
- **SFP 기준치**: 두 가지 중 고른다.
  1. **MAPS 위반을 그대로 쓴다**(권장) — Brocade 가 SFP 종류별 기준으로 이미 판정한다(`mapsdb --show`).
  2. 직접 기준 — 속도 · SFP 종류별 RX/TX dBm 하한을 설정. 사양서 값을 담당자가 정한다.
- **errdump 중복**: 같은 메시지 반복은 묶어서 횟수로 보인다.
- **가짜 경보**: 스위치 하나가 응답하지 않으면 그 스위치 전체를 "수집 공백"으로 두고(이번에 vCenter 에
  적용한 방식) 포트 다운 · 존 삭제로 판정하지 않는다.

---

## 4. 화면 · 데이터 구조 (제안)

- 새 대메뉴 **스토리지 · SAN** (내부 모듈, 지식 저장소와 같은 방식 — 공용 파일을 거의 건드리지 않음)
  - 일간: 어제 대비 할당/회수 · 풀 증감 · 조닝 변경 · 새 SAN 오류
  - 주간 · 월간: 풀별 총/사용/할당/여유 추이, 할당 · 회수 목록, 조닝 변경 이력
  - 포트맵: 스위치 · 포트 · WWPN · alias · 서버 · 존 · 조닝 스크립트, SFP 광량, 오류 증가분
- 통합 대시보드 위젯: **SFP 기준 미만 N · CRC 증가 포트 N · errdump ERROR N · 풀 임계 N**
- DB(모듈 스키마 `storage_*`): 장비 · 수집 실행, 풀 일별, LDEV/경로 스냅샷, 할당 · 회수 이벤트,
  스위치 · 포트 스냅샷, 존 스냅샷, 포트 오류 카운터(일별), SFP 값(일별), errdump 이벤트
- 수집 시각: 07:00 일일 배치에 붙인다. SAN 오류 · SFP 는 수시 수집(업무 시간)에도 붙일 수 있다.

---

## 5. 순서 제안

| 순서 | 할 일 | 이유 |
| --- | --- | --- |
| 1 | HMC 오류 확인 → AIX 를 통합서버 자원사용현황에 | 거의 다 돼 있음. `check_hmc_api.py` 결과만 있으면 됨 |
| 2 | Brocade: 포트맵 · 조닝 · porterrshow · SFP · errdump | 장애 징후(CRC · 광량)를 바로 보는 효과가 크고, REST/CLI 둘 다 추가 설치가 없다 |
| 3 | Hitachi 블록: 풀 용량 · LDEV 할당/회수 | 2 의 WWN/alias 가 있어야 "어느 서버" 가 정확해진다 |
| 4 | F800/G800(CCI), NAS | 별도 준비물(커맨드 디바이스 · NAS API) 필요 |

## 6. 받아야 할 것

**HMC**
1. `scripts\check_hmc_api.py` 실행 결과(실패한 단계 메시지)

**Brocade** (스위치별)
1. `version` — FOS 버전
2. `mgmtapp --show` — REST 사용 여부
3. SSH 키 로그인 가능 여부(REST 가 안 되는 스위치용)
4. 패브릭 · 스위치 목록(이름 · 주소)
5. 아래 명령 출력 예시 한 벌(이름 · WWN 은 가려도 됨) — 우리 FOS 버전 출력 형식에 맞춰 읽기 위해
   `switchshow`, `nsshow`, `cfgactvshow`, `porterrshow`, `sfpshow -all`(포트 2~3개분), `errdump -r`(몇 줄), `mapsdb --show`
6. SFP 기준: MAPS 를 따를지, 직접 기준을 둘지

**Hitachi**
1. `scripts\check_storage_api.py` 실행 결과(장비별 REST 가능 여부)
2. REST 되는 장비 하나의 `pools` · `ldevs` 응답 한 벌(이름은 가려도 됨)
3. 단위 기본값: TB/GB, 10진/2진 중 무엇
4. NAS 모델과 관리 API 유무
