# 대메뉴 모듈 연동 가이드

통합 웹 하나에 여러 담당자의 WAS 를 붙이는 구조다. 새 대메뉴를 추가하는 쪽과
통합 웹을 운영하는 쪽이 각각 무엇을 하면 되는지 정리한다.

```text
브라우저 ──→ 통합 웹 (이 소스)
                 │  서버에서 호출 (BFF)
                 ├──→ WAS A · 대메뉴 A
                 ├──→ WAS B · 대메뉴 B
                 └──→ WAS C · 대메뉴 C
                          ↑
                  MySQL (공유 · 계정/자산 데이터)
```

브라우저는 **항상 통합 웹만** 호출한다. 각 WAS 는 사설망에 두어도 되고, CORS 설정이
필요 없고, 인증 경계가 통합 웹 한 곳으로 모인다.

## 1. 통합 웹에 대메뉴 등록

`config/app_config.local.yaml` 의 `modules.registry` 에 항목을 추가한다.
**통합 웹 소스는 고치지 않는다.**

```yaml
modules:
  request_timeout_seconds: 5      # 호출별 타임아웃
  retry_count: 1                  # GET 만 재시도한다
  dashboard_budget_seconds: 12    # 통합 대시보드 전체 응답 예산
  max_response_bytes: 4194304
  verify_tls: true
  auth:
    token_ttl_seconds: 60
    header: X-Portal-Token
  registry:
    - id: capacity                # 소문자/숫자/밑줄
      name: 용량 관리              # 메뉴에 표시되는 이름
      icon: 📦
      base_url: http://was-capacity:5301
      enabled: true
      required_role: user         # user | admin
      menu_section: 운영
      health_path: /api/health
      panel_path: /api/dashboard/panel
      allowed_prefixes: ['/api/']  # 프록시 허용 경로
      timeout_seconds: 8           # 이 모듈만 다르게 줄 때
```

| 항목 | 의미 |
|---|---|
| `base_url` 비움 | 통합 웹 안에 있는 내부 모듈. HTTP 를 거치지 않는다 |
| `required_role: admin` | 관리자에게만 메뉴와 API 가 보인다 |
| `allowed_prefixes` | 이 접두어로 시작하는 경로만 프록시한다. 통합 웹이 임의 주소로 가는 통로가 되지 않게 하는 장치다 |
| `enabled: false` | 메뉴·대시보드·프록시에서 모두 제외된다 |

설정 항목 하나가 잘못돼도 그 모듈만 건너뛰고 통합 웹은 정상 기동한다. 건너뛴 항목은
로그에 남는다.

공유 시크릿은 파일에 쓰지 않는다.

```text
MODULE_SHARED_SECRET=<모든 WAS 가 같은 값>
```

## 2. WAS 담당자가 만들 것 — 2개 엔드포인트

### 2-1. 헬스체크 (`health_path`)

```json
{"status": "UP"}
```

### 2-2. 대시보드 패널 (`panel_path`)

통합 대시보드에 표시할 내용을 아래 스펙으로 돌려준다. **통합 웹은 도메인 지식 없이
이 스펙만 보고 그린다.** 새 대메뉴가 늘어도 통합 웹 렌더링 코드는 바뀌지 않는다.

```json
{
  "title": "용량 관리 현황",
  "metrics": [
    {"label": "총 용량", "value": 812,  "unit": "TB", "state": "info"},
    {"label": "사용률",  "value": 73.4, "unit": "%",  "state": "warning"}
  ],
  "table": {
    "columns": ["스토리지", "사용률"],
    "rows": [["SAN-01", 81], ["SAN-02", 66]]
  },
  "note": "07:00 수집 기준",
  "updated_at": "2026-07-30T07:00:00"
}
```

- `state` — `info` / `success` / `warning` / `error`. 숫자 색이 달라진다.
- `metrics` 는 8개, `table.rows` 는 50행, `columns` 는 12개까지만 표시된다. 넘으면
  통합 웹이 잘라낸다.
- `table.rows` 는 배열 또는 객체(`{"스토리지": "SAN-01"}`) 모두 받는다.
- 모든 항목은 생략 가능하다. 지표만, 표만 돌려줘도 된다.

참조 구현은 `application/local_panels.py` 의 `asset_sync_panel` 이다.

### 2-3. 그 외 API

대메뉴 화면에서 쓰는 나머지 API 는 자유롭게 만들면 된다. 통합 웹은 프록시로 전달한다.

```text
브라우저 → GET /api/modules/capacity/proxy/api/detail?page=1
통합 웹  → GET http://was-capacity:5301/api/detail?page=1
```

## 3. 사용자 인증 — 서명 토큰

통합 웹이 세션으로 사용자를 인증하고, WAS 호출 시 헤더에 단기 토큰을 붙인다.
WAS 는 세션 저장소를 공유하지 않고도 요청자를 확인할 수 있다.

```text
X-Portal-Token: <base64url(payload)>.<hmac-sha256>
```

payload:

```json
{"v":1, "sub":"hong", "uid":7, "role":"admin", "name":"홍길동",
 "mod":"capacity", "iat":1785000000, "exp":1785000060, "jti":"..."}
```

WAS 쪽 검증 코드는 `application/modules/tokens.py` 의 `verify_token` 을 그대로
복사해 쓰면 된다. 표준 라이브러리만 사용하므로 추가 설치가 없다.

```python
from tokens import verify_token   # 복사해 둔 파일

payload = verify_token(request.headers.get("X-Portal-Token"), SECRET, module_id="capacity")
if payload is None:
    return jsonify({"error": "unauthorized"}), 401
user_id, role = payload["sub"], payload["role"]
```

검증이 보장하는 것:

- **위조 불가** — 공유 시크릿을 모르면 서명을 만들 수 없다.
- **재사용 제한** — `exp` 기본 60초. 시계 오차 30초까지 허용한다.
- **모듈 간 전용** — `mod` 가 달라 A 모듈 토큰을 B 모듈에 쓸 수 없다.

`MODULE_SHARED_SECRET` 이 비어 있으면 토큰을 붙이지 않는다. WAS 쪽 검증이 준비되기
전에도 연동을 시험할 수 있게 한 것이며, **운영에서는 반드시 설정한다.**

## 4. 통합 웹이 제공하는 엔드포인트

| 엔드포인트 | 용도 |
|---|---|
| `GET /api/modules` | 현재 사용자에게 보이는 모듈 목록. `base_url` 은 내려주지 않는다 |
| `GET /api/modules/health` | 모듈별 도달 여부 집계. 하나라도 죽으면 `DEGRADED` |
| `GET /api/modules/dashboard` | 모든 모듈의 패널을 병렬로 수집. `?modules=capacity` 로 일부만 |
| `GET/POST/PUT/DELETE /api/modules/<id>/proxy/<path>` | 모듈 API 대리 호출 |

## 5. 부분 실패와 지연

통합 대시보드는 모듈을 **병렬로** 호출한다. 순차 호출이면 응답시간이 합산된다.

- 모듈 하나가 죽어도 나머지 패널은 그대로 표시된다. 화면 전체가 오류로 바뀌지 않는다.
- 패널별 상태: `SUCCESS` / `FAILED` / `TIMEOUT` / `UNREACHABLE` / `SKIPPED`
- 호출별 타임아웃은 전체 예산으로 제한된다. 모듈 하나의 긴 타임아웃이 화면을 붙잡지
  못한다.
- 응답에 `degraded` 와 `counts` 가 함께 온다. 화면 상단에 "모듈 3개 중 2개 정상"
  형태로 표시된다.

## 6. 프록시가 차단하는 것

| 항목 | 처리 |
|---|---|
| 허용 접두어 밖의 경로 | 거부 |
| 경로에 `..` 포함 | 거부 |
| `PATCH` 등 목록 밖 메서드 | 거부 |
| 권한 없는 모듈 | 403 |
| 등록되지 않은/중지된 모듈 | 404 |
| 브라우저 쿠키·Authorization 헤더 | WAS 로 전달하지 않는다 |
| WAS 의 `Set-Cookie` | 브라우저로 전달하지 않는다 |
| 응답 크기 초과 | 거부 (`max_response_bytes`) |
| 리다이렉트 | 따라가지 않는다 |
| 프록시 환경변수 | 사내 호출이므로 무시한다 |

호출 대상 주소는 항상 설정의 `base_url` 이다. 사용자 입력이 주소가 되는 경로는 없다.

## 7. 검증

```bat
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements-bff.txt
```

브라우저에서 통합 대시보드를 열면 모듈별 패널과 상단 요약이 보인다. API 로 확인할
때는 아래를 쓴다.

```text
GET /api/modules/health      → 모듈별 도달 여부
GET /api/modules/dashboard   → 패널과 counts/degraded
```

새 모듈이 목록에 없으면 순서대로 확인한다.

1. `modules.registry` 에 항목이 있는가 (오타로 건너뛰어졌으면 로그에 남는다)
2. `enabled: true` 인가
3. `required_role` 이 현재 사용자 권한보다 높지 않은가
4. 통합 웹 WAS 에서 `base_url` 로 통신이 되는가

## 8. 대메뉴별 권한

`user_module_permission` 테이블에 사용자-대메뉴 단위로 부여한다. 판정 규칙은 세 줄이다.

```text
1. admin 은 모든 대메뉴에 MANAGE 권한을 갖는다
2. 명시 부여가 있으면 그 값을 쓴다 (VIEW | MANAGE)
3. 없으면 모듈의 access 설정으로 판정한다
     access: role     (기본) → required_role 로 판정. 기존 동작과 같다
     access: explicit        → 명시 부여가 없으면 접근 불가
```

기본값이 `role` 인 이유는 이 테이블을 도입해도 기존 계정이 갑자기 대메뉴를 잃지
않게 하기 위한 것이다. **엄격히 통제할 대메뉴만 `access: explicit` 로 바꾼다.**

| 권한 | 의미 |
|---|---|
| `VIEW` | 메뉴 노출 + 조회(GET) 프록시 허용 |
| `MANAGE` | 추가로 `POST`/`PUT`/`DELETE` 프록시 허용 |

부여는 `관리 → 사용자 관리 → 권한 설정` 에서 한다. 변경은 `audit_log` 에
`action='PERMISSION'` 으로 남는다.

WAS 쪽에서는 토큰의 `perm` 값으로 쓰기 권한을 다시 확인할 수 있다. 통합 웹이
이미 막지만, WAS 가 자체적으로 판단할 수 있어야 다른 경로로 들어온 요청도 안전하다.

```python
payload = verify_token(request.headers.get("X-Portal-Token"), SECRET, module_id="capacity")
if request.method != "GET" and payload.get("perm") != "MANAGE":
    return jsonify({"error": "forbidden"}), 403
```

## 9. 화면 파일 구조

대메뉴가 늘어도 팀별로 파일이 갈리도록 템플릿을 분할했다.

```text
templates/
  base.html                 공통 셸: 스타일, 사이드바, 공통 스크립트
  main.html                 통합 웹 화면 · 통합 대시보드(모듈 조합)
  pages/<name>.html         통합 웹이 직접 담당하는 화면
  modules/_generic.html     등록된 원격 모듈의 기본 화면(패널 렌더링)
  partials/modals.html      공용 모달
  partials/js/<name>.html   화면별 스크립트
```

- **통합 대시보드는 `main.html` 에 남겨 두었다.** 여러 모듈을 조합해 보여주는
  화면이므로 통합 웹의 책임이다.
- 새 대메뉴는 기본적으로 `modules/_generic.html` 이 패널 스펙만으로 그린다.
  모듈 고유 화면이 필요하면 `templates/modules/<module_id>.html` 을 만들고
  `main.html` 의 include 를 바꾼다. **다른 팀 파일을 건드리지 않는다.**
- 공통 헬퍼(`escapeHtml`, `integrationFetch`, `setActionResult`, `setButtonBusy`,
  `integrationToast`, 모달 제어)는 `partials/js/common.html` 에 있다. 화면
  스크립트에서 그대로 호출한다.

## 10. 공유 감사로그

`audit_log` 는 통합 웹과 각 WAS 가 함께 쓰는 테이블이다. `module_id` 로 출처를
구분한다.

```sql
INSERT INTO audit_log(module_id, user_id, action, target_type, target_id, reason,
                      before_json, after_json, created_at)
VALUES ('capacity', 'hong', 'UPDATE', 'storage_pool', '12', '증설 승인',
        '{"size":700}', '{"size":812}', '2026-07-30T09:12:00');
```

| 컬럼 | 값 |
|---|---|
| `module_id` | 자기 모듈 id. 통합 웹 자체 기록은 `portal`, 자산 정합성은 `asset_sync` |
| `user_id` | 토큰의 `sub` 값을 그대로 쓴다. 그래야 통합 웹 기록과 이어진다 |
| `action` / `target_type` / `target_id` | 모듈이 정한다. `target_type` 은 모듈 안에서만 고유하면 된다 |

조회 API:

```text
GET /api/admin/audit-log                  전체
GET /api/admin/audit-log?module_id=capacity  특정 모듈만
GET /api/admin/audit-log/modules           모듈별 건수와 마지막 기록 시각
```

기존 DB 에는 이 컬럼이 없으므로 기동 시 자동으로 추가된다(아래 참고).

## 11. 스키마 변경 규칙

### 11.1 담당자는 공용 파일을 고치지 않는다

각자 자기 파일만 추가한다. 공용 `schema.sql` / `migrations.py` 를 여러 명이 고치면
branch 를 병합할 때마다 같은 줄에서 충돌한다.

```text
asset_sync/db/modules/<module_id>.sql            SQLite 용 테이블·인덱스
asset_sync/db/modules/<module_id>.mysql.sql      MySQL 용 같은 것
asset_sync/db/modules/<module_id>_migrations.py  이미 배포된 DB 를 바꾸는 단계 (선택)
```

두 SQL 파일은 짝으로 둔다(한쪽만 있으면 기동 시 실패한다). 만드는 객체 이름은
`<module_id>_` 로 시작해야 한다 — 하나의 DB 를 공유하므로 이름이 겹치면 서로의
테이블을 덮어쓴다. 규칙을 어기면 무엇이 잘못됐는지 알려주며 멈춘다.

전체 계약과 예시는 **`asset_sync/db/modules/README.md`** 에 있다.

### 11.2 마이그레이션 이름은 숫자가 아니다

이력은 `schema_migration.name` 에 `capacity/add_memory_column` 형태로 남는다.
버전 숫자를 쓰면 담당자 A 와 B 가 각자 branch 에서 같은 번호를 쓰고, 병합에서 한쪽만
남거나 **이미 그 번호가 찍힌 DB 에서 다른 쪽이 오류 없이 영구히 스킵된다.** 이름은
서로 겹치지 않으므로 그런 일이 없다. 한 번 배포한 이름은 바꾸지 않는다(바꾸면 다시
실행된다).

### 11.3 기존 DB 를 깨지 않는 두 가지

**기존 테이블에 컬럼을 추가할 때 그 컬럼을 참조하는 인덱스를 `.sql` 파일에 넣으면
안 된다.** 기존 DB 에서는 `CREATE TABLE IF NOT EXISTS` 가 no-op 이라 컬럼 없이
인덱스를 만들려 해 스키마 적용 자체가 실패한다. 컬럼은 `CREATE TABLE` 에 두고
인덱스는 마이그레이션에서 만든다.

**각 마이그레이션 단계는 적용 여부를 스스로 확인한다.** 새로 만든 DB 는 최신 스키마를
이미 갖고 있고, 여러 WAS 가 동시에 기동해 같은 DDL 을 돌릴 수도 있다.
`column_exists`, `table_exists`, `apply_step` 이 그 용도로 있다.

### 11.4 적용 순서

기동할 때 `DatabaseManager.initialize()` 가 이 순서로 돈다.

1. 공용 `schema.sql` (또는 `schema_mysql.sql`)
2. `db/modules/` 의 모듈 SQL — 모듈 ID 사전순
3. 미적용 마이그레이션 — 코어 먼저, 그 뒤 모듈 파일 이름순

모듈 간 순서는 보장하지 않는다. 다른 모듈의 테이블에 의존하는 단계는 두지 않는다.
공용 테이블(`app_user`, `audit_log` 등)은 1번에서 만들어지므로 참조해도 된다.

## 12. 아직 남은 것

- **권한 변경이 메뉴에 즉시 반영되지 않는다.** 화면을 새로 열 때 갱신된다.
  세션에 캐시하지 않고 매 요청 조회하므로 API 는 즉시 반영된다.
- **모듈이 자기 HTML 을 직접 제공할 수는 없다.** 지금은 패널 스펙(JSON)만 통합
  웹이 렌더링한다. 모듈 고유 화면이 필요하면 `templates/modules/<id>.html` 을
  통합 웹 저장소에 추가해야 한다.
- **프록시는 대용량 파일에 맞지 않다.** 요청 본문을 메모리에 모두 읽고 응답은
  `max_response_bytes`(기본 4MB)로 제한된다. 대용량 업로드·다운로드가 필요하면
  별도 설계가 필요하다.
- **레지스트리는 기동 시 1회 읽는다.** 대메뉴를 추가하면 통합 웹을 재시작해야
  한다.
