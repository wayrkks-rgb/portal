# 대메뉴 연동 안내 (담당자용)

통합 웹 하나에 팀별 화면을 대메뉴로 붙이는 구조다. **이 문서만 보면 연동이 된다.**
세부 규격은 `MODULE_INTEGRATION_GUIDE.md`, 설정 필드는 `config/modules/README.md`.

---

## 1. 어떤 구조인가

```
              ┌──────────────────────────────────────┐
  브라우저 ──→│          통합 웹 (Flask)             │
              │  로그인 · 메뉴 · 통합 대시보드       │
              └───────────────┬──────────────────────┘
                              │  서버가 대신 호출 (BFF)
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          WAS A           WAS B           WAS C
        (A팀 대메뉴)    (B팀 대메뉴)    (C팀 대메뉴)
              └───────────────┼───────────────┘
                              ▼
                    MySQL (공용 · 팀별 테이블)
```

**브라우저는 통합 웹만 호출한다.** 각 팀 WAS는 통합 웹이 서버에서 대신 부른다.

이 구조라서 좋은 점:

- WAS를 **사설망에 둬도 된다**. 외부에 열 필요가 없다
- **CORS 설정이 필요 없다**
- 로그인은 통합 웹이 한 번만 처리한다. 팀별로 로그인 화면을 만들지 않는다

---

## 2. 무엇을 만들면 되나

### 최소 구성 — API 2개 + 설정파일 1개

이것만 있으면 메뉴에 뜨고 대시보드에 나옵니다. 화면 파일은 안 만들어도 됩니다.

**① 헬스체크**

```
GET /api/health   →   {"status": "UP"}
```

**② 대시보드 패널** — 통합 웹이 이 JSON을 보고 화면을 그립니다

```json
GET /api/dashboard/panel
{
  "title": "용량 현황",
  "metrics": [
    {"label": "총 용량", "value": 812,  "unit": "TB", "state": "info"},
    {"label": "사용률",  "value": 73.4, "unit": "%",  "state": "warning"}
  ],
  "table": {
    "columns": ["스토리지", "사용률"],
    "rows": [["SAN-01", 81], ["SAN-02", 66]]
  },
  "note": "07:00 수집 기준"
}
```

- `state`: `info` / `success` / `warning` / `error` — 숫자 색이 달라집니다
- 전부 선택입니다. 지표만, 표만 돌려줘도 됩니다
- 지표 8개 · 표 50행 · 열 12개까지 표시됩니다

**③ 등록 파일** — 저장소에 `config/modules/<모듈ID>.yaml` 추가

```yaml
# config/modules/capacity.yaml   ← 파일 이름이 모듈 ID (소문자/숫자/밑줄)
name: 용량 관리
icon: 📦
menu_section: 연계 모듈      # 사이드바 묶음 이름
base_url: http://was-capacity:5301
required_role: user          # user | admin
dashboard:                   # 통합 대시보드에서 차지할 크기 (전체 12칸)
  width: 4                   #   4 = 화면 1/3
```

**끝입니다.** 통합 웹에 push하면 메뉴와 대시보드에 나옵니다.

---

## 3. 사용자 확인 — 서명 토큰

통합 웹이 WAS를 부를 때 헤더에 단기 토큰을 붙입니다. WAS는 이걸로 **누가 요청했는지**
알 수 있습니다. 세션을 공유하지 않아도 됩니다.

```
X-Portal-Token: <base64url(payload)>.<hmac-sha256>
```

payload 내용:

```json
{"sub": "hong", "uid": 7, "role": "admin", "name": "홍길동",
 "mod": "capacity", "perm": "VIEW", "exp": 1785000060}
```

검증 코드는 **`application/modules/tokens.py`의 `verify_token`을 그대로 복사**해 쓰면
됩니다. 표준 라이브러리만 써서 추가 설치가 없습니다.

```python
payload = verify_token(request.headers.get("X-Portal-Token"), SECRET, module_id="capacity")
if payload is None:
    return jsonify({"error": "unauthorized"}), 401
user_id, role = payload["sub"], payload["role"]
```

- 공유 비밀키(`MODULE_SHARED_SECRET`)는 **`.env`로 받습니다.** 소스에 넣지 마세요
- 유효기간 60초, A모듈 토큰을 B모듈에 재사용할 수 없습니다
- 쓰기 요청은 `perm == "MANAGE"`인지 WAS에서 한 번 더 확인하시길 권합니다

---

## 4. 그 외 API는 자유

대메뉴 화면에서 쓰는 나머지 API는 마음대로 만드시면 됩니다. 통합 웹이 프록시합니다.

```
브라우저 → GET /api/modules/capacity/proxy/api/detail?page=1
통합 웹  → GET http://was-capacity:5301/api/detail?page=1
```

기본적으로 `/api/`로 시작하는 경로만 통과합니다 (`allowed_prefixes`로 조정).

---

## 5. DB를 쓸 때

MySQL을 팀들이 함께 씁니다. **테이블 이름은 반드시 `<모듈ID>_`로 시작**하세요.
이름이 겹치면 서로의 테이블을 덮어씁니다.

저장소에 **두 파일을 짝으로** 추가합니다 (한쪽만 두면 기동할 때 실패합니다).

```
asset_sync/db/modules/capacity.sql         ← SQLite용 (개발·테스트)
asset_sync/db/modules/capacity.mysql.sql   ← MySQL용 (운영)
```

```sql
-- capacity.sql
CREATE TABLE IF NOT EXISTS capacity_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_name TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capacity_snapshot_time ON capacity_snapshot(captured_at DESC);
```

- `CREATE TABLE IF NOT EXISTS`로 쓰세요. 기동할 때마다 실행됩니다
- 이미 배포된 테이블을 바꿀 때는 `capacity_migrations.py`를 씁니다 (아래 8번)
- 공용 테이블(`app_user`, `audit_log` 등)은 읽어도 됩니다

---

## 6. 화면을 직접 만들고 싶을 때

패널 JSON으로 부족하면 저장소에 화면 파일을 추가합니다. **전부 선택입니다.**

```
templates/modules/capacity/page.html      대메뉴 화면
templates/modules/capacity/scripts.html   그 화면용 <script>
templates/modules/capacity/widget.html    통합 대시보드에 넣을 축소 위젯
```

`page.html`의 최상위는 이 형태여야 합니다 (사이드바가 이 id로 화면을 전환합니다):

```html
<div class="page {% if page=='capacity' %}active{% endif %}" id="page-capacity">
  ...
</div>
```

**CSS는 자동으로 그 화면 안에 갇힙니다.** `.card { border:0 }`을 써도 다른 팀 화면은
안 바뀝니다. 다만 `html` · `body` · `:root`는 전역을 덮으므로 렌더링에서 제외됩니다.
`@keyframes` 이름과 JS 전역 함수명은 자동 격리가 안 되니 `capacity-` 접두어를 붙이세요.

---

## 7. 소메뉴가 필요하면

`config/modules/capacity.yaml`에 추가:

```yaml
children:
- id: trend
  name: 사용률 추이
  icon: 📈
- id: plan
  name: 증설 계획
  required_role: admin      # 관리자에게만 보임
```

소메뉴를 누르면 **`?menu=trend`가 붙어 패널 API가 다시 호출**됩니다.
WAS에서 갈라주면 화면 파일 없이 소메뉴가 완성됩니다.

```python
@app.get("/api/dashboard/panel")
def panel():
    menu = request.args.get("menu", "")   # '' = 통합 대시보드용 요약
    if menu == "trend":
        return jsonify({"title": "사용률 추이", "table": {...}})
    return jsonify({"title": "용량 요약", "metrics": [...]})
```

---

## 8. 추가하는 파일 한눈에

**전부 새 파일 추가입니다.** 통합 웹의 공용 파일을 고칠 일이 없어 병합 충돌이 없습니다.

| 필요한 것 | 파일 |
| --- | --- |
| 대메뉴 등록 · 소메뉴 · 위젯 크기 | `config/modules/<id>.yaml` |
| 환경별 주소 (git 제외) | `config/modules/<id>.local.yaml` |
| 대메뉴 화면 | `templates/modules/<id>/page.html` |
| 화면 스크립트 | `templates/modules/<id>/scripts.html` |
| 대시보드 위젯 | `templates/modules/<id>/widget.html` |
| DB 테이블 | `asset_sync/db/modules/<id>.sql` + `<id>.mysql.sql` |
| DB 변경 이력 | `asset_sync/db/modules/<id>_migrations.py` |

---

## 9. push 전에 확인

```bash
python scripts/check_module_contract.py --module capacity          # 파일만
python scripts/check_module_contract.py --module capacity --live   # WAS까지 호출
```

설정 등록 여부, 테이블 이름 규칙, 화면 id, 전역 CSS, 헬스체크와 패널 응답까지
한 번에 봅니다. **CI가 push마다 같은 것을 돌리므로 여기서 통과하면 병합 후에
깨지지 않습니다.**

---

## 10. 주의할 점

| | |
| --- | --- |
| **WAS가 HTML을 돌려줘도 안 그립니다** | 패널 JSON만 렌더링합니다. 고유 화면은 6번의 템플릿 파일로 |
| **다른 팀 테이블은 못 건드립니다** | `ALTER` · `DROP`은 기동할 때 막힙니다 |
| **큰 파일 전송에는 안 맞습니다** | 응답 4MB 제한. 대용량은 별도 협의 |
| **모듈 하나가 죽어도 괜찮습니다** | 대시보드는 나머지를 그리고, 그 자리만 오류로 표시됩니다 |
| **설정 추가 후 반영** | `관리 → 연계 설정 → [대메뉴 다시 읽기]` (재시작 불필요) |

---

## 11. 진행 순서

1. 모듈 ID를 정하고 통합 웹 담당자에게 알린다 (중복 방지)
2. 저장소를 branch로 받는다
3. WAS에 헬스체크·패널 API 2개를 만든다
4. `config/modules/<id>.yaml`을 추가한다
5. 필요하면 DB 파일·화면 파일을 추가한다
6. `check_module_contract.py`로 확인하고 push한다
7. 통합 웹 담당자가 검토 후 병합·배포한다

막히면 `MODULE_INTEGRATION_GUIDE.md`를 보시고, 그래도 안 되면 통합 웹 담당자에게
`check_module_contract.py` 실행 결과와 함께 문의해 주세요.
