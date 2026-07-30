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

**`config/modules/<module_id>.yaml` 파일 하나를 추가한다.** 공용 파일은 고치지 않는다.
여러 담당자가 `app_config.yaml` 의 목록을 고치면 branch 를 병합할 때마다 같은 줄에서
충돌하기 때문이다.

```yaml
# config/modules/capacity.yaml   ← 파일 이름이 모듈 ID (소문자/숫자/밑줄)
name: 용량 관리                  # 메뉴에 표시되는 이름
icon: 📦
base_url: http://was-capacity:5301
enabled: true
required_role: user             # user | admin
menu_section: 운영
health_path: /api/health
panel_path: /api/dashboard/panel
allowed_prefixes: ['/api/']     # 프록시 허용 경로
timeout_seconds: 8              # 이 모듈만 다르게 줄 때
access: role                    # role | explicit
dashboard:                      # 통합 대시보드에서 차지할 크기 (전체 12칸)
  width: 4                      #   4 = 1/3 폭
  height: 1
children:                       # 소메뉴. 있으면 사이드바가 2단이 된다
- id: trend
  name: 사용률 추이
  icon: 📈
- id: plan
  name: 증설 계획
  icon: 🧮
  required_role: admin          # 관리자에게만 보이는 소메뉴
```

배포 환경마다 주소가 다르면 `config/modules/capacity.local.yaml` 에 그 값만 적는다.
git 에 올라가지 않으므로 개발 장비 주소가 저장소에 섞이지 않는다.

```yaml
# config/modules/capacity.local.yaml
base_url: http://127.0.0.1:8081
```

필드 표와 예시는 `config/modules/README.md` 에 있다.

공통 설정(모든 모듈에 함께 적용)은 `config/app_config.local.yaml` 에 둔다.

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

**소메뉴가 있으면 `?menu=<소메뉴 id>` 가 함께 온다.** 같은 엔드포인트에서 갈라 주면
소메뉴별 화면이 완성된다. 통합 대시보드에서 부를 때는 `menu` 가 없다.

```python
@app.get("/api/dashboard/panel")
def panel():
    menu = request.args.get("menu", "")      # '' = 통합 대시보드용 요약
    if menu == "trend":
        return jsonify({"title": "사용률 추이", "table": {...}})
    return jsonify({"title": "용량 요약", "metrics": [...]})
```

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

### 7.1 push 하기 전에 — 자가 점검 스크립트

```bash
python scripts/check_module_contract.py --module capacity          # 파일만 검사
python scripts/check_module_contract.py --module capacity --live   # WAS 까지 호출
```

확인하는 것: 설정 파일이 레지스트리에 실제로 등록되는가 · 소메뉴 page 중복 · SQLite와
MySQL 스키마 파일 짝과 테이블 이름 규칙 · 마이그레이션 이름 · `page.html` 의 화면 id ·
전역을 덮는 CSS · (`--live`) 헬스체크와 패널 스펙, 소메뉴별 `?menu=` 응답.

**CI 가 push 마다 같은 것을 돌린다**(`.github/workflows/ci.yml`). 여기서 통과하면
병합 후에 깨지지 않는다. CI 는 추가로 MySQL 8 컨테이너에 스키마를 실제로 적용해 본다 —
SQLite 로만 돌리면 MySQL 전용 파일의 문법 오류를 놓치기 때문이다.

### 7.2 붙인 뒤에

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

1. `config/modules/<id>.yaml` 이 있는가 (오타로 건너뛰어졌으면 로그에 남는다)
2. `enabled: true` 인가
3. `required_role` 이 현재 사용자 권한보다 높지 않은가
4. 통합 웹 WAS 에서 `base_url` 로 통신이 되는가
5. **설정 파일을 추가한 뒤 다시 읽었는가** — 아래 참고

### 7.3 대메뉴 추가 후 재적용

레지스트리는 기동할 때 읽는다. 대메뉴를 추가할 때마다 통합 웹을 재시작하면 다른
담당자들의 화면까지 잠깐 끊기므로, **재시작 없이 다시 읽을 수 있다.**

```text
관리 → 연계 설정 → [대메뉴 다시 읽기]
POST /api/modules/reload        (관리자 전용)
```

설정 파일과 화면 파일을 다시 읽고 추가·제거된 대메뉴를 알려준다. 사이드바는 화면을
새로고침해야 다시 그려진다. 재적용이 실패하면 **돌던 대메뉴는 그대로 남는다** — 새
정의를 전부 만든 뒤에 교체하기 때문이다. 재적용 이력은 `audit_log` 에
`action='RELOAD'` 로 남는다.

WAS 자체를 새로 배포했거나 파이썬 코드(`modules_local/`)를 바꿨으면 재시작이 필요하다.

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

## 9. 메뉴와 통합 대시보드 배치

### 9.0 사이드바

메뉴는 `application/menu.py` 한 곳에서 만든다. 통합 웹 자신의 화면(`PORTAL_MENU`)과
모듈 레지스트리를 같은 형태로 합치므로, 대메뉴를 추가할 때 `base.html` 을 고치지 않는다.

```text
운영            ← PORTAL_MENU (통합 웹 화면)
  🏠 통합 대시보드
  📝 보고서
자산 관리        ← menu_section 이 같은 모듈이 이 묶음에 들어온다
  🔗 자산 정합성
    📋 정합성 현황        ← children (소메뉴)
    🔍 ITSM↔vCenter 비교
    🗃️ 수집 이력
연계 모듈
  📊 용량 관리
    📈 사용률 추이
    🧮 증설 계획          ← required_role: admin 이면 관리자에게만
설정
  ⚙️ 관리
```

- 묶음 이름은 `menu_section` 이다. 같은 이름을 쓰면 한 묶음으로 합쳐진다.
  순서는 `menu.py` 의 `SECTION_ORDER`, 거기 없는 이름은 뒤에 사전순으로 붙는다.
- 소메뉴가 가리키는 화면이 없으면 대메뉴 공통 화면에서 `?menu=<id>` 로 패널을 다시
  받아 그린다(2-2 참고). 그래서 **화면 파일 없이 WAS 코드만으로도 소메뉴가 된다.**
- 대메뉴 권한이 없으면 소메뉴도 통째로 사라진다(8장).

### 9.1 통합 대시보드 배치

통합 대시보드는 **12칸 격자**다. 담당자는 `dashboard.width` 로 자기 몫을 정한다.

| `width` | 폭 | 쓰임 |
| --- | --- | --- |
| 3 | 1/4 | 지표 2~3개짜리 작은 위젯 |
| 4 | 1/3 | 기본값. 지표 + 짧은 표 |
| 6 | 1/2 | 표가 넓을 때 |
| 12 | 전체 | 한 줄 전부 |

`height` 는 세로 칸(최대 4)이고, 표가 길어 아래로 넘칠 때만 쓴다.
`dashboard.enabled: false` 로 두면 대메뉴 화면에만 나오고 통합 대시보드에서 빠진다.

화면이 좁아지면(1280px 이하) 1/2 미만 위젯은 반폭으로, 760px 이하에서는 전부 전체
폭으로 접힌다. **담당자가 격자 CSS 를 다룰 일은 없다** — 패널 스펙이든
`widget.html` 이든 통합 웹이 설정값으로 span 을 씌운다.

대메뉴 화면(소메뉴 포함)에서는 패널을 전체 폭으로 그린다. 축소는 통합 대시보드에서만
일어난다.

### 9.2 화면 파일 구조

대메뉴가 늘어도 팀별로 파일이 갈리도록 템플릿을 분할했다.

```text
templates/
  base.html                        공통 셸: 스타일, 사이드바, 공통 스크립트
  main.html                        통합 웹 화면 · 통합 대시보드(모듈 조합)
  pages/<name>.html                통합 웹이 직접 담당하는 화면
  modules/_generic.html            원격 모듈의 기본 화면(패널 렌더링)
  modules/<module_id>/page.html    담당자가 만든 대메뉴 화면
  modules/<module_id>/scripts.html 그 화면용 <script>
  modules/<module_id>/widget.html  통합 대시보드에 넣을 축소 위젯
  partials/modals.html             공용 모달
  partials/js/<name>.html          화면별 스크립트
```

**담당자는 `main.html` 을 고치지 않는다.** `modules/<module_id>/` 에 파일을 두면
기동할 때 발견되어 자동으로 include 된다. 세 파일 모두 선택이고, 있는 것만 쓰인다.

| 파일 | 어디에 그려지나 |
| --- | --- |
| `page.html` | 대메뉴 화면. 없으면 `_generic.html` 이 패널 스펙으로 그린다 |
| `scripts.html` | 공통 스크립트 뒤에 온다. `callModule()` 등을 그대로 쓸 수 있다 |
| `widget.html` | 통합 대시보드의 축소 위젯 영역 |

- **통합 대시보드는 `main.html` 에 남겨 두었다.** 여러 모듈을 조합해 보여주는
  화면이므로 통합 웹의 책임이다.
- **화면 파일이 있다는 것이 접근 권한을 주지는 않는다.** 권한 있는 사용자에게만
  include 된다(8장). `access: explicit` 인 모듈은 부여받지 않은 사용자에게 markup
  자체가 내려가지 않는다.
- `page.html` 의 최상위 요소는 `<div class="page" id="page-<page 키>">` 로 둔다.
  사이드바가 그 id 로 화면을 전환한다.
- 공통 헬퍼(`escapeHtml`, `integrationFetch`, `setActionResult`, `setButtonBusy`,
  `integrationToast`, 모달 제어)는 `partials/js/common.html` 에 있다. 화면
  스크립트에서 그대로 호출한다.
- **CSS 는 자동으로 그 화면 안에 갇힌다.** `page.html` / `widget.html` 의 `<style>`
  안 선택자에는 통합 웹이 범위를 붙인다. `.card { border:0 }` 를 써도 다른 팀 화면은
  바뀌지 않는다.

  ```css
  .card { gap: 0 }        →  #page-capacity .card { gap: 0 }        (page.html)
  .card { gap: 0 }        →  #module-widget-capacity .card { gap:0 } (widget.html)
  ```

  `html` · `body` · `:root` · `*` 는 범위를 붙여도 전역을 덮으므로 **렌더링에서
  제외되고 로그에 남는다.** CI 가 이것을 실패로 잡는다. 배경색 같은 것이 필요하면
  자기 화면의 최상위 요소에 준다.

  `@keyframes` 이름과 `scripts.html` 의 전역 변수·함수 이름은 자동으로 격리되지
  않는다. `capacity-` / `capacity_` 접두어를 붙인다.

### 9.3 내부 모듈 코드

담당 WAS 를 따로 두지 않고 통합 웹 안에서 돌리는 경우, 파일 위치만 맞추면 등록된다.

```text
application/modules_local/<module_id>/routes.py   bp (Blueprint) 를 노출
application/modules_local/<module_id>/panel.py    panel(user, params) 를 노출
```

`routes.py` 의 `bp` 는 `create_app()` 이 자동으로 `register_blueprint` 하고,
`panel.py` 의 `panel` 은 통합 대시보드 패널 제공자로 등록된다. 한 모듈의 import 가
실패하면 그 모듈만 건너뛰고 로그에 남는다 — 통합 웹은 정상 기동한다.

패널 스펙의 참조 구현은 `application/local_panels.py` 를 본다.

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

## 12. 담당자가 추가하는 파일 한눈에

전부 **새 파일 추가**다. 통합 웹의 공용 파일을 고칠 일이 없으므로 branch 를 병합할 때
충돌하지 않는다.

| 필요한 것 | 파일 |
| --- | --- |
| 대메뉴 등록 · 소메뉴 · 위젯 크기 | `config/modules/<id>.yaml` |
| 환경별 주소 | `config/modules/<id>.local.yaml` (git 제외) |
| 대메뉴 화면 | `templates/modules/<id>/page.html` |
| 화면 스크립트 | `templates/modules/<id>/scripts.html` |
| 통합 대시보드 위젯 | `templates/modules/<id>/widget.html` |
| DB 테이블 | `asset_sync/db/modules/<id>.sql` + `<id>.mysql.sql` |
| DB 변경 이력 | `asset_sync/db/modules/<id>_migrations.py` |
| 내부 모듈 라우트 | `application/modules_local/<id>/routes.py` |
| 내부 모듈 패널 | `application/modules_local/<id>/panel.py` |

담당 WAS 를 따로 두는 경우 마지막 두 개는 필요 없다. 그 대신 WAS 가 2장의 두
엔드포인트를 제공한다.

## 13. 경계 — 일부러 막아둔 것

- **WAS 응답으로 받은 HTML 은 그리지 않는다.** 패널 스펙(JSON)만 렌더링한다. 남의
  WAS 가 보낸 HTML 을 그대로 넣으면 통합 웹의 세션과 다른 팀 화면까지 그 코드의
  사정권이 된다. 고유 markup 은 저장소의 템플릿 파일로 받는다(12장 표).
- **모듈 CSS 는 자기 화면 밖으로 나가지 못한다.** `html` · `body` · `:root` 규칙은
  렌더링에서 빠진다(9.2).
- **모듈 스키마 파일은 자기 테이블만 다룬다.** 다른 이름의 테이블을 만들거나
  `ALTER` / `DROP` 을 쓰면 기동할 때 멈춘다(11장).
- **프록시는 대용량 파일에 맞지 않다.** 요청 본문을 메모리에 모두 읽고 응답은
  `max_response_bytes`(기본 4MB)로 제한된다. 대용량 업로드·다운로드가 필요하면
  별도 설계가 필요하다.

## 14. 아직 없는 것

필요해지면 그때 만든다. 지금 막히는 담당자가 있으면 알려주면 된다.

- **3단 메뉴** — 대메뉴 + 소메뉴까지다. 그 아래는 `page.html` 안에서 탭으로 만든다.
- **격자 좌표 지정** — 폭·높이는 정할 수 있지만 "3열 2행의 이 자리" 는 안 된다.
  순서는 등록 순서(= 설정 파일 이름 사전순)를 따른다.
- **권한 변경의 즉시 반영** — 화면을 새로 열 때 갱신된다. 세션에 캐시하지 않고 매
  요청 조회하므로 API 는 즉시 반영된다.
- **파이썬 코드 변경의 무중단 반영** — 설정과 화면 파일은 재시작 없이 다시 읽지만
  (7.3), `modules_local/` 의 파이썬 코드는 재시작이 필요하다.
