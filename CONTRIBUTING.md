# 이 저장소에서 작업하는 법

통합 웹 하나에 팀별 화면을 **대메뉴**로 붙이는 구조다. 각 팀은 branch 를 따서
자기 파일만 추가하고 push 한다. **공용 파일은 건드리지 않는다** — 그래야 여러 팀이
동시에 작업해도 충돌이 나지 않는다.

이 문서는 저장소 전체를 어떻게 이해하고 쓰면 되는지 설명한다.
연동 규격 자체는 [MODULE_ONBOARDING.md](MODULE_ONBOARDING.md) 를 본다.

---

## 1. 무엇을 하는 프로그램인가

Oracle ITSM(자산관리대장)과 vCenter(실제 VM)를 매일 자동으로 읽어와서

- 자산이 **새로 생겼는지 · 없어졌는지 · 자원이 바뀌었는지** 찾아내고
- 두 쪽 정보가 **서로 맞는지** 비교한다

여기에 팀별 화면을 대메뉴로 얹는 것이 이 저장소의 목적이다.

---

## 2. 폴더 지도

**✅ 표시된 곳에만 파일을 추가한다.** 나머지는 통합 웹 담당이 관리한다.

```
portal/
├── config/
│   ├── app_config.yaml          공용 설정 (git 추적)
│   ├── app_config.local.yaml    실제 접속값 — git 에 없음. 서버마다 다름
│   └── modules/  ✅             config/modules/<내모듈>.yaml 을 여기 둔다
│
├── templates/
│   ├── base.html                레이아웃·CSS·사이드바
│   ├── main.html                화면 껍데기
│   ├── pages/                   통합 웹이 원래 갖고 있는 화면
│   ├── partials/js/             화면별 스크립트
│   └── modules/  ✅             templates/modules/<내모듈>/{page,scripts,widget}.html
│
├── application/                 웹 뼈대 (로그인·메뉴·모듈 등록)
│   └── modules_local/  ✅       application/modules_local/<내모듈>/{routes,panel}.py
│
├── asset_sync/                  ITSM·vCenter 수집과 비교 (핵심 로직)
│   ├── collectors/              Oracle · PowerCLI 에서 읽어오는 부분
│   ├── services/                비교·집계·정합성 판정
│   ├── routes/                  API
│   └── db/
│       ├── schema.sql           공용 테이블
│       ├── migrations.py        공용 스키마 변경
│       └── modules/  ✅         asset_sync/db/modules/<내모듈>.sql + .mysql.sql
│
├── scripts/                     실행·점검 스크립트
└── tests/  ✅                   내 모듈 테스트도 여기 둔다
```

---

## 3. 작업 순서

```bash
git clone <저장소 주소>
cd portal
git checkout -b feature/<내모듈>          # 예: feature/capacity

# ... ✅ 위치에만 파일 추가 ...

python scripts/check_module_contract.py --module <내모듈>   # 자가 점검
python -m pytest -q                                        # 전체 테스트

git add .
git commit -m "용량 관리 대메뉴 추가"
git push -u origin feature/<내모듈>
```

push 하면 CI 가 자동으로 같은 점검을 돌린다. 통과하면 PR 을 올린다.

> `git status` 에 **`M`(수정)이 찍힌 공용 파일이 있으면 멈추고 확인한다.**
> 정상적인 모듈 추가는 전부 `??`(새 파일)이어야 한다.

---

## 4. 로컬에서 띄우기

### 처음 한 번

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

폐쇄망이면 `scripts\install_offline.bat` 을 쓴다 (미리 받아둔 wheel 로 설치).

### 실행

```bash
scripts\run_flask.bat            # http://localhost:5100
```

초기 계정은 첫 기동 때 자동으로 만들어진다 (`admin` / `user`).
**운영에 올리기 전에 반드시 비밀번호를 바꾼다.**

Oracle·vCenter 접속 없이도 화면은 뜬다. 수집만 안 될 뿐이다.
내 모듈만 확인할 거라면 접속정보를 채울 필요가 없다.

---

## 5. 내 모듈 붙이는 두 가지 방법

### 방법 A — 우리 팀 WAS 를 따로 운영한다 (권장)

우리 서버는 그대로 두고, 통합 웹이 **서버끼리** 호출한다.
브라우저는 통합 웹만 부르므로 우리 WAS 를 외부에 열 필요가 없다.

만들 것은 **API 2개 + 설정파일 1개**뿐이다.

```
GET /api/health           →  {"status": "UP"}
GET /api/dashboard/panel  →  {"title": ..., "metrics": [...]}
```

```yaml
# config/modules/capacity.yaml     ← 파일 이름이 곧 모듈 ID
id: capacity
name: 용량 관리
icon: 📊
base_url: https://was-a.example:8443
enabled: true
required_role: user
menu_section: 운영
```

자세한 규격 → [MODULE_ONBOARDING.md](MODULE_ONBOARDING.md)

### 방법 B — 통합 웹 안에 직접 넣는다

별도 서버 없이 이 저장소 안에서 돈다.

```
config/modules/capacity.yaml                  base_url 을 비워 둔다
application/modules_local/capacity/routes.py  bp = Blueprint(...)
application/modules_local/capacity/panel.py   def panel(user, params)
templates/modules/capacity/page.html          id="page-capacity"
asset_sync/db/modules/capacity.sql            테이블 이름은 capacity_ 로 시작
asset_sync/db/modules/capacity.mysql.sql      같은 테이블의 MySQL 판
```

**이름이 정확해야 한다.** `routes.py` 는 `bp`, `panel.py` 는 `panel(user, params)` 다.
틀리면 통합 웹은 정상 기동하고 **내 모듈만 조용히 빠진다.**
`check_module_contract.py` 가 이걸 잡아주므로 push 전에 꼭 돌린다.

---

## 6. 건드리면 안 되는 것과 그 이유

| 하지 말 것 | 왜 | 대신 |
|---|---|---|
| `config/app_config.yaml` 의 `modules.registry` 에 내 모듈 추가 | 모든 팀이 같은 줄을 고쳐 충돌난다 | `config/modules/<id>.yaml` 파일 추가 |
| `templates/main.html` 에 내 화면 `include` | 위와 같음 | `templates/modules/<id>/page.html` 두면 자동 include |
| `templates/base.html` 에 메뉴 항목 추가 | 위와 같음 | yaml 의 `menu_section` · `children` |
| `asset_sync/db/schema.sql` 에 내 테이블 추가 | 공용 스키마다 | `asset_sync/db/modules/<id>.sql` |
| 다른 팀 테이블 조회·수정 | 남의 데이터다 | 그 팀 API 를 호출 |
| `<style>` 에 `.card { ... }` 같은 전역 선택자 | 다른 팀 화면까지 바뀐다 | 통합 웹이 자동으로 내 화면 안으로 가둔다(아래 참고) |

**모든 자동 인식은 "파일을 두면 알아서 읽는다"** 방식이다. 목록에 이름을 적는 곳이
한 군데도 없기 때문에 팀이 늘어도 같은 줄을 고칠 일이 없다.

CSS 는 템플릿을 읽는 시점에 선택자 앞에 `#page-<내모듈>` 이 자동으로 붙는다.
`.card{border-left:3px solid #4a9}` 이라고 쓰면 실제로는
`#page-capacity .card{...}` 가 되어 다른 팀 화면에 영향이 없다.

---

## 7. DB 규칙

- 테이블 이름은 **`<모듈ID>_` 로 시작**한다. `capacity_daily` (O) / `daily` (X)
- **SQLite 와 MySQL 파일을 짝으로** 만든다. 하나만 있으면 점검에서 실패한다
  (개발은 SQLite, 운영은 MySQL 을 쓴다)
- 이미 배포된 테이블을 바꿀 때는 `.sql` 을 고치지 말고 **마이그레이션**을 추가한다.
  이름은 `<모듈ID>/설명` 형식이다 — `capacity/add_note` (O), `capacity/3` (X).
  **숫자 이름은 다른 팀과 겹쳐서 조용히 건너뛰어진다.**

자세히 → [asset_sync/db/modules/README.md](asset_sync/db/modules/README.md)

---

## 8. 비밀값

**비밀번호·키를 git 에 올리지 않는다.** 실수하면 이력에 영원히 남는다.

| 값 | 두는 곳 |
|---|---|
| Oracle·MySQL 비밀번호 | `.env` |
| 서버 주소·계정 | `config/app_config.local.yaml` |
| 내 모듈의 비밀값 | `config/modules/<id>.local.yaml` |

`.local.yaml` 과 `.env` 는 `.gitignore` 에 들어 있다. 커밋 전에
`git status` 에 이 파일들이 안 보이는지 확인한다.

---

## 9. push 전 자가 점검

```bash
python scripts/check_module_contract.py --module <내모듈>
```

확인하는 것:

- `config/modules/<id>.yaml` 이 실제로 대메뉴로 등록되는가
- 소메뉴 `page` 가 서로 겹치지 않는가
- SQLite/MySQL 스키마가 짝을 이루고 테이블 이름 규칙을 지키는가
- 마이그레이션 이름이 충돌하지 않는가
- `page.html` 에 `id="page-<page값>"` 이 있는가 *(없으면 메뉴는 뜨는데 빈 화면이 된다)*
- `routes.py` 의 `bp`, `panel.py` 의 `panel()` 이 제대로 있는가
- 전역 CSS 선택자를 쓰지 않았는가

같은 점검을 CI 가 push 마다 자동으로 돌린다. MySQL 스키마는 실제 MySQL 8 을
띄워서 적용해 본다 — SQLite 로만 확인하면 MySQL 에서만 나는 오류를 놓친다.

---

## 10. 어떤 문서를 읽어야 하나

| 알고 싶은 것 | 문서 |
|---|---|
| 내 모듈 붙이는 법 (제일 먼저) | [MODULE_ONBOARDING.md](MODULE_ONBOARDING.md) |
| 연동 규격 상세 · 서명 토큰 · 권한 | [MODULE_INTEGRATION_GUIDE.md](MODULE_INTEGRATION_GUIDE.md) |
| yaml 필드 하나하나 | [config/modules/README.md](config/modules/README.md) |
| DB 파일·마이그레이션 규칙 | [asset_sync/db/modules/README.md](asset_sync/db/modules/README.md) |
| 여러 WAS + MySQL 공용 구성 | [MULTI_WAS_MYSQL_GUIDE.md](MULTI_WAS_MYSQL_GUIDE.md) |
| 처음 설치·실행 | [BEGINNER_RUN_AND_VALIDATION_GUIDE.md](BEGINNER_RUN_AND_VALIDATION_GUIDE.md) |
| Oracle·vCenter 접속 설정 | [CONNECTION_SETUP_GUIDE.md](CONNECTION_SETUP_GUIDE.md) |

---

## 11. 자주 막히는 곳

**메뉴는 보이는데 화면이 비어 있다**
`page.html` 의 `id="page-..."` 와 yaml 의 `page` 값이 다르다.
사이드바가 이 id 로 화면을 전환한다. 점검 스크립트가 잡아준다.

**내 모듈만 안 뜬다. 오류는 없다**
`routes.py` 의 `bp` 나 `panel.py` 의 `panel()` 이름이 틀렸다.
통합 웹은 한 모듈이 깨져도 전체가 멈추지 않도록 그 모듈만 건너뛰고 로그만 남긴다.
`logs/asset_sync.log` 에서 `내부 모듈 ... 등록을 건너뜁니다` 를 찾는다.

**파일을 고쳤는데 화면이 그대로다**
Flask 를 껐다 켜지 않았다. 운영 모드는 파일 변경을 자동으로 다시 읽지 않는다.
기동 로그의 `통합 웹 기동 · 소스=...` 줄로 지금 도는 소스를 확인할 수 있다.

**내 CSS 가 다른 팀 화면까지 바꿨다**
`html`, `body`, `:root`, `*` 는 범위를 가두지 않는다. 이 넷만 피하면 된다.

**마이그레이션이 적용되지 않았다**
같은 이름이 이미 적용된 것으로 기록돼 있다. 이름을 바꾸면 다시 돈다.
단, **이미 배포된 이름은 바꾸지 않는다** — 다른 서버에서 두 번 실행된다.

---

## 12. 도움 요청할 때

이 세 가지를 같이 주면 대부분 바로 답이 나온다.

1. `python scripts/check_module_contract.py --module <내모듈>` 출력 전체
2. `logs/asset_sync.log` 의 마지막 50줄
3. 브랜치 이름과 `git status --short`
