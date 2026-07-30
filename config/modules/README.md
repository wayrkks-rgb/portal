# 대메뉴(모듈) 등록 파일

대메뉴를 추가할 때 `config/app_config.yaml` 의 `modules.registry` 를 고치지 않는다.
여러 담당자가 그 목록을 고치면 branch 를 병합할 때마다 같은 줄에서 충돌한다.
**이 폴더에 파일 하나를 추가**하면 기동할 때 자동으로 등록된다.

```text
config/modules/<module_id>.yaml        git 에 올리는 정의 (필수)
config/modules/<module_id>.local.yaml  배포 환경별 덮어쓰기 (선택, git 제외)
```

파일 이름이 모듈 ID 다. 파일 안에 `id` 를 적으면 파일 이름과 같아야 한다.

## 예시

```yaml
# config/modules/capacity.yaml
name: 용량 관리
icon: 📊
menu_section: 운영
# 담당 WAS 주소. 비워 두면 통합 웹 안에서 도는 내부 모듈로 본다.
base_url: http://10.10.20.31:8081
enabled: true
required_role: user
# role: required_role 로 판정 · explicit: 사용자별 명시 부여가 있어야 접근
access: role
# 통합 웹이 호출할 경로
health_path: /api/health
panel_path: /api/dashboard/panel
# 통합 웹이 프록시해 줄 경로 접두어. 좁게 둘수록 안전하다.
allowed_prefixes:
- /api/
timeout_seconds: 5
show_in_menu: true
```

```yaml
# config/modules/capacity.local.yaml  (git 제외)
# 개발 장비에서만 다른 주소를 쓸 때
base_url: http://127.0.0.1:8081
```

## 필드

| 필드 | 기본값 | 설명 |
| --- | --- | --- |
| `name` | 모듈 ID | 메뉴에 보이는 이름 |
| `icon` | `🧩` | 메뉴 아이콘 (이모지) |
| `base_url` | `''` | 담당 WAS 주소. 비우면 내부 모듈 |
| `enabled` | `true` | `false` 면 메뉴·API 모두에서 사라진다 |
| `required_role` | `user` | `user` / `admin` |
| `menu_section` | `운영` | 사이드바 묶음 이름 |
| `page` | 모듈 ID | 화면 전환에 쓰는 페이지 키 |
| `health_path` | `/api/health` | 상태 확인 경로 |
| `panel_path` | `/api/dashboard/panel` | 통합 대시보드 패널 경로 |
| `timeout_seconds` | 공통값(5초) | 이 모듈만 다르게 줄 때 |
| `allowed_prefixes` | `['/api/']` | 프록시 허용 접두어 |
| `show_in_menu` | `true` | 대시보드에만 넣고 메뉴에서 감출 때 `false` |
| `access` | `role` | `explicit` 이면 관리자가 사용자별로 부여해야 보인다 |

`base_url` 은 통합 웹 서버만 알면 되는 내부 주소다. 화면에는 내려가지 않는다.
공유 비밀키(`MODULE_SHARED_SECRET`)는 이 파일이 아니라 `.env` 에 둔다.

## 같이 두면 되는 것

| 필요한 것 | 파일 |
| --- | --- |
| 대메뉴 화면 | `templates/modules/<module_id>/page.html` |
| 그 화면용 스크립트 | `templates/modules/<module_id>/scripts.html` |
| 통합 대시보드 축소 위젯 | `templates/modules/<module_id>/widget.html` |
| DB 테이블 | `asset_sync/db/modules/<module_id>.sql` + `.mysql.sql` |
| DB 변경 이력 | `asset_sync/db/modules/<module_id>_migrations.py` |
| 내부 모듈 라우트 | `application/modules_local/<module_id>/routes.py` (`bp`) |
| 내부 모듈 패널 | `application/modules_local/<module_id>/panel.py` (`panel`) |

전부 선택이다. 있는 것만 쓰인다. 어느 것도 통합 웹의 공용 파일을 고치게 하지 않는다.

## 확인

```bash
python -c "from asset_sync.config import load_config; \
print([m['id'] for m in load_config().modules['registry']])"
```

레지스트리는 기동할 때 한 번 읽는다. 파일을 추가했으면 통합 웹을 재시작한다.
