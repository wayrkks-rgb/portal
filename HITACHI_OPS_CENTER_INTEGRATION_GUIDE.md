# Hitachi Ops Center 연동 가이드 (SAN · Storage)

Hitachi 스토리지의 **포트 정보, 용량·사용량·할당량, 알람/에러** 를 통합 웹으로
가져오기 위한 준비와 연동 방법입니다.

> 이 문서는 **연동 전 검토·요청용** 입니다. 화면 구현은 아래 1~3 장을 확인해
> 어느 API 를 쓸지 정한 뒤에 진행합니다. 제품 구성에 따라 필요한 서버가 다르므로
> 먼저 **어떤 제품이 깔려 있는지** 확인하는 것이 첫걸음입니다.

---

## 1. 먼저 확인할 것 — 어느 API 를 쓸 수 있는가

"Hitachi Ops Center" 는 한 제품이 아니라 묶음입니다. 우리가 원하는 값을 주는
창구가 셋인데, **깔려 있는 것이 무엇이냐** 에 따라 붙는 곳이 달라집니다.

| 제품 | 기본 포트 | 무엇을 주는가 | 우리가 원하는 값 |
|---|---|---|---|
| **Ops Center API Configuration Manager** (CM REST API) | 23450 / 23451(SSL) | LDEV, 풀, 포트, 호스트그룹, 볼륨 | **용량·할당량·포트 (핵심)** |
| **Ops Center Analyzer** (REST API) | 22015 / 22016(SSL) | 성능 지표, 임계값 알람 | **사용률·알람** |
| **Ops Center Administrator** (REST API) | 443 | 프로비저닝 중심 | 보조 |
| (구) Hitachi Device Manager / HDvM REST | 2001 / 2443(SSL) | CM REST 의 전신 | CM 이 없을 때 대안 |

**스토리지 담당자에게 물어볼 것 — 이 5가지면 충분합니다.**

1. Ops Center **API Configuration Manager** 가 설치되어 있습니까? 주소와 포트는?
2. Ops Center **Analyzer** 가 설치되어 있습니까? 주소와 포트는?
3. 대상 스토리지의 **장비 모델과 시리얼(Storage Device ID)** 은?
   (VSP 5000 / VSP E / VSP G·F 계열에 따라 API 지원 범위가 다릅니다)
4. **조회 전용 계정**을 받을 수 있습니까? (아래 2장 참고)
5. SVP 경유입니까, 컨트롤러 직결(REST on controller)입니까?

---

## 2. 계정과 권한

조회만 하는 계정을 따로 받으세요. 프로비저닝 권한은 필요 없습니다.

| 제품 | 필요한 역할 |
|---|---|
| API Configuration Manager | `Storage Administrator (View Only)` |
| Analyzer | `Viewer` (또는 `Ops Center Viewer`) |

계정 등록(스토리지 담당자 작업):

```
Ops Center Administrator → Administration → Users
  → Create User → Role: Storage Administrator (View Only)
```

> **주의:** CM REST API 는 스토리지 장비마다 **세션을 따로** 엽니다. 장비 하나당
> 동시 세션 수 제한(기본 64)이 있으므로, 수집이 끝나면 반드시 로그아웃해야
> 합니다. 통합 웹은 HMC 연동과 같은 방식으로 세션을 정리합니다.

---

## 3. 방화벽

| 출발 | 목적지 | 포트 | 용도 |
|---|---|---|---|
| 통합 웹 WAS | Ops Center CM 서버 | **TCP 23451** (SSL) | 구성·용량·포트 조회 |
| 통합 웹 WAS | Ops Center CM 서버 | TCP 23450 | 비 SSL (SSL 미사용 시) |
| 통합 웹 WAS | Ops Center Analyzer | **TCP 22016** (SSL) | 성능·알람 조회 |
| (CM 서버) | 스토리지 SVP | TCP 443 / 1099 등 | CM 서버가 쓰는 경로 (우리 쪽 작업 아님) |

확인:

```powershell
Test-NetConnection -ComputerName <OpsCenter주소> -Port 23451
Test-NetConnection -ComputerName <Analyzer주소>  -Port 22016
```

---

## 4. 설치할 것

**통합 웹 쪽에는 설치할 것이 없습니다.** HMC 연동과 마찬가지로 HTTPS + JSON
이므로 파이썬 표준 라이브러리로 처리합니다. 폐쇄망에 휠을 반입할 필요가 없습니다.

스토리지 쪽에 **API Configuration Manager 가 아직 없다면** 그것만 설치가
필요합니다. Hitachi 에서 제공하는 설치 파일은 다음과 같습니다.

| 파일 | 설명 | 받는 곳 |
|---|---|---|
| `Hitachi Ops Center API Configuration Manager` 설치 미디어 | Windows/Linux 서버에 설치 | Hitachi 지원 포털 또는 담당 엔지니어 |
| 라이선스 키 | 제품 라이선스 | 구매 계약에 포함 |

설치는 스토리지 담당 엔지니어 작업이며, 통합 웹은 설치된 서버의 REST 주소만
있으면 됩니다.

---

## 5. 가져올 값과 호출하는 주소

아래는 API Configuration Manager REST API(v1) 기준입니다.
`{base}` = `https://<OpsCenter>:23451/ConfigurationManager/v1`,
`{id}` = Storage Device ID (예: `800000012345`).

### 5-1. 세션

```http
POST {base}/objects/storages/{id}/sessions        ← Basic 인증, 세션 토큰 발급
  응답: { "token": "...", "sessionId": 3 }
이후 요청 헤더: Authorization: Session <token>
DELETE {base}/objects/storages/{id}/sessions/{sessionId}   ← 반드시 정리
```

### 5-2. 포트 정보

```http
GET {base}/objects/storages/{id}/ports
GET {base}/objects/storages/{id}/ports/{portId}?detailInfoType=logins
```

| 원하는 값 | 응답 항목 |
|---|---|
| 포트 ID | `portId` (예: `CL1-A`) |
| 포트 종류 | `portType` (FIBRE / ISCSI) |
| 속도 | `portSpeed` (`AUT`, `8G`, `16G`, `32G`) |
| WWN | `wwn` |
| 접속 모드 | `portConnection` (FCAL / PtoP) |
| 로그인 수 | `loginWWNs` (상세 조회 시) |

### 5-3. 용량 · 할당량 · 사용률

**풀(Pool) 단위 — 가장 중요합니다.**

```http
GET {base}/objects/storages/{id}/pools
```

| 원하는 값 | 응답 항목 | 비고 |
|---|---|---|
| 총 용량 | `totalPoolCapacity` (MB) | 풀의 물리 용량 |
| 사용량 | `usedPoolCapacity` (MB) | 실제 쓴 양 |
| 사용률 | `usedCapacityRate` (%) | 사용량 ÷ 총 용량 |
| **할당량** | `totalLocatedCapacity` (MB) | 볼륨에 **나눠 준** 양 |
| **할당률** | `locatedCapacityRate` (%) | 오버프로비저닝 판단 기준 |
| 임계 | `thresholdWarning` / `thresholdDepletion` (%) | 경고·고갈 임계 |
| 상태 | `poolStatus` (`POLN` 정상 / `POLF` 만료 / `POLS` 축소 중) | |

> **사용률과 할당률은 다릅니다.** 통합기 자원 화면과 같은 구분입니다. 씬
> 프로비저닝에서는 할당률이 200% 를 넘어도 사용률은 40% 일 수 있습니다. 증설
> 판단은 **사용률**, 볼륨 추가 가능 여부는 **할당률**로 봅니다.

**볼륨(LDEV) 단위**

```http
GET {base}/objects/storages/{id}/ldevs?ldevOption=defined&count=16384
```

| 원하는 값 | 응답 항목 |
|---|---|
| 볼륨 번호 | `ldevId` / `ldevIdHex` |
| 라벨 | `label` (업무명이 들어 있는 경우가 많습니다) |
| 용량 | `blockCapacity` (블록) / `byteFormatCapacity` |
| 소속 풀 | `poolId` |
| 속성 | `attributes` (`CVS`, `HDP`, `HTI` 등) |
| 상태 | `status` (`NML` 정상 / `BLK` 차단) |

**호스트 그룹 — 어느 서버에 붙어 있는지**

```http
GET {base}/objects/storages/{id}/host-groups?portId={portId}
GET {base}/objects/storages/{id}/host-wwns?portId={portId}&hostGroupNumber={n}
GET {base}/objects/storages/{id}/luns?portId={portId}&hostGroupNumber={n}
```

이 세 개를 묶으면 **서버 WWN ↔ 포트 ↔ 볼륨** 이 이어집니다. ITSM 의 서버와
스토리지 볼륨을 연결하는 열쇠이고, SAN 구성도를 그릴 수 있는 근거가 됩니다.

### 5-4. 알람 · 에러

CM REST API 에는 알람 조회가 없습니다. 두 가지 중 하나를 씁니다.

**(A) Ops Center Analyzer REST API — 권장**

```http
POST https://<Analyzer>:22016/Analytics/v1/services/auth/login   ← 토큰 발급
GET  https://<Analyzer>:22016/Analytics/v1/services/alerts
GET  https://<Analyzer>:22016/Analytics/v1/services/resources/storages
```

| 원하는 값 | 응답 항목 |
|---|---|
| 알람 종류 | `alertType` |
| 심각도 | `severity` (Critical / Warning / Info) |
| 대상 | `resourceName`, `resourceType` |
| 발생 시각 | `occurrenceTime` |
| 상태 | `status` (Open / Closed) |

**(B) SNMP Trap — Analyzer 가 없을 때**

스토리지에서 통합 웹 서버로 SNMP Trap 을 보내도록 설정합니다. 다만 Trap 수신기를
따로 띄워야 하고 폐쇄망 라우팅 작업이 늘어나므로, Analyzer 가 있으면 (A) 를
권장합니다.

---

## 6. 화면에 어떻게 넣을 것인가 (제안)

통합기 자원 화면과 같은 구조로 맞추면 읽는 사람이 헷갈리지 않습니다.

```
운영 → 시스템 파트 → 월간 점검 → [스토리지 현황] 탭 (신설)

  ┌ 스토리지 용량 현황 ───────────────────────────────────────┐
  │ 장비 | 풀 | 총 용량 | 사용량 | 사용률 | 할당량 | 할당률 | 임계 │
  │ VSP-01 | Pool-00 | 300TB | 120TB | 40% | 610TB | 203% | 80% │
  └──────────────────────────────────────────────────────────┘

  ┌ SAN 포트 현황 ───────────────────────────────────────────┐
  │ 장비 | 포트 | 종류 | 속도 | WWN | 로그인 수 | 상태          │
  └──────────────────────────────────────────────────────────┘

  ┌ 알람 · 에러 ─────────────────────────────────────────────┐
  │ 시각 | 장비 | 심각도 | 종류 | 대상 | 내용                  │
  └──────────────────────────────────────────────────────────┘
```

전월 대비 증감(용량이 얼마나 늘었는지)은 통합기 화면과 같은 방식으로
일별 값을 쌓아 두고 월말 값을 비교하면 됩니다.

---

## 7. 연동을 시작하기 위해 주셔야 할 것

아래 표를 채워 주시면 바로 붙일 수 있습니다.

| 항목 | 값 |
|---|---|
| API Configuration Manager 주소 : 포트 | |
| Analyzer 주소 : 포트 (없으면 "없음") | |
| Storage Device ID (장비별) | |
| 장비 모델 / 시리얼 | |
| 조회 전용 계정 ID | |
| SSL 인증서 (사내 CA / 자체 서명) | |
| 방화벽 신청 완료 여부 | |

계정·비밀번호는 화면(연계 설정)에서 직접 입력받습니다. 문서나 메일로 보내지
마세요. 저장된 비밀번호는 `config/app_config.local.yaml` 에만 들어가며 화면으로
다시 내려오지 않습니다.

---

## 8. 확인용 curl (담당자가 미리 해 볼 수 있습니다)

```bash
# 1) 세션 발급
curl -k -u portal_ro:'<비번>' -X POST \
  -H "Content-Type: application/json" \
  https://<OpsCenter>:23451/ConfigurationManager/v1/objects/storages/<id>/sessions

# 2) 풀 용량 (위 응답의 token 사용)
curl -k -H "Authorization: Session <token>" \
  https://<OpsCenter>:23451/ConfigurationManager/v1/objects/storages/<id>/pools

# 3) 포트
curl -k -H "Authorization: Session <token>" \
  https://<OpsCenter>:23451/ConfigurationManager/v1/objects/storages/<id>/ports

# 4) 세션 정리 (꼭 하세요)
curl -k -X DELETE -H "Authorization: Session <token>" \
  https://<OpsCenter>:23451/ConfigurationManager/v1/objects/storages/<id>/sessions/<sessionId>
```

2·3번의 **응답 JSON 을 그대로 보내 주시면** 그 값에 맞춰 화면과 수집기를
만들겠습니다. 마이크로코드 버전에 따라 항목이 조금씩 다르므로, 실제 응답을 보고
맞추는 편이 확실합니다.
