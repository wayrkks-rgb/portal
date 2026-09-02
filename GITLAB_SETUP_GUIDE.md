# 사내 GitLab 으로 옮겨서 계속 개발하기

지금 이 저장소는 GitHub(`wayrkks-rgb/portal`)에 있다. 이걸 사내 GitLab 에 붙여서
수정·배포까지 하려면 **사내망이 어디까지 열려 있느냐**에 따라 방법이 갈린다.
먼저 아래 세 줄만 확인하면 어느 경로인지 정해진다.

대상 저장소는 이것이다.

```text
https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service
```

| 확인할 것 | 확인 방법 |
|---|---|
| ① 내 PC 에서 사내 GitLab 이 열리나 | 위 주소를 브라우저로 접속 |
| ② 그 PC 에서 인터넷(github.com)이 되나 | 브라우저로 `https://github.com` 접속 |
| ③ 그 PC 에서 `api.anthropic.com` 이 되나 | Claude Code 를 그 PC 에서 쓸 거면 필요 |

- ①만 된다 → **경로 A**. 사내 GitLab 을 주 저장소로 쓴다. 가장 깔끔하다.
- ①②가 다 된다 → **경로 B**. GitHub 에서 개발하고 GitLab 에 동기화한다.
- ①과 ②가 서로 다른 PC 다(망분리) → **경로 C**. 파일로 반입한다.

> **이 Claude 세션(claude.ai/code)에서는 위 주소에 붙지 못한다.** 실제로 붙여 보면
> egress 정책에서 `403` 으로 막는다(`ito-ax-gitlab.apps.dev.honecloud.co.kr` 이
> 허용 목록에 없음). 지금 세션은 인터넷상의 격리된 컨테이너에서 돌기 때문이다.
>
> 뚫는 방법은 둘 중 하나다.
>
> 1. **그 GitLab 이 인터넷에서 열리는 주소라면** — Claude Code 환경(environment)의
>    network policy 허용 목록에 이 도메인을 넣으면 웹 세션에서도 직접 붙는다.
>    설정은 <https://code.claude.com/docs/en/claude-code-on-the-web> 참고.
>    여기서는 정책에 막혀서 열리는 주소인지 아닌지까지는 확인할 수 없다.
> 2. **사내망에서만 열리는 주소라면** — ③이 열린 **사내망 PC 에 Claude Code CLI 를
>    설치**해서 거기서 쓴다. 이게 말씀하신 "다이렉트로 수정·배포" 에 해당한다.

---

## 경로 A — 사내 GitLab 을 주 저장소로 (권장)

### A-1. 저장소 올리기

인터넷 PC 에서 이력까지 통째로 받아 두고(→ 경로 C의 반입 파일), 사내에서 올린다.
①②가 같은 PC 에서 되면 그냥 아래처럼 하면 된다.

```bash
git clone https://github.com/wayrkks-rgb/portal.git
cd portal
git remote add gitlab https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
git push gitlab --all
git push gitlab --tags
```

그다음 `origin` 을 GitLab 으로 바꿔 끼운다. 앞으로 `git push` 는 사내로 간다.

```bash
git remote rename origin github
git remote rename gitlab origin
```

### A-2. 작업 순서

`CONTRIBUTING.md` 의 순서와 같다. PR 이라는 말만 MR(Merge Request)로 바뀐다.

```bash
git checkout -b feature/<내모듈>
# ... ✅ 표시된 위치에만 파일 추가 ...
python scripts/check_module_contract.py --module <내모듈>
python -m pytest -q
git push -u origin feature/<내모듈>
```

push 하면 GitLab 이 링크를 찍어 준다. 그 링크로 MR 을 만든다.

### A-3. CI 켜기

`.gitlab-ci.yml` 이 이미 저장소에 있다. GitHub Actions 와 **같은 검사**를 돌린다.

1. 사내에 GitLab Runner 가 이미 있으면 그냥 push 하면 파이프라인이 돈다.
2. Runner 가 없으면 등록해야 한다 (Settings → CI/CD → Runners).
3. 폐쇄망이라 `python:3.11` 이미지를 못 받거나 docker executor 가 아니면,
   `.gitlab-ci.yml` 아래쪽 주석의 **shell executor 방식**으로 바꾼다.
4. 사내 PyPI 미러가 있으면 Settings → CI/CD → Variables 에 `PIP_INDEX_URL` 을 넣는다.
   미러도 없으면 runner 서버에 venv 를 미리 만들어 두고 3번 방식을 쓴다.

> 지금 `tests/test_oracle_diagnostics.py` 의 2건은 `oracledb` 가 없으면 실패한다.
> GitHub CI 도 이 조합(dev·mysql·bff)이라 같은 상태다. GitLab 파이프라인을 켜면
> 처음부터 빨간불로 시작하므로, runner 에 `requirements-oracle.txt` 를 같이 깔든지
> 해당 테스트를 고치든지 먼저 정리하고 켜는 편이 낫다.

Runner 를 못 붙이는 상황이면 CI 없이 가되, **MR 올리기 전에 사람이 두 줄을 돌린다.**

```bash
python scripts/check_module_contract.py
python -m pytest -q
```

### A-4. 배포까지 GitLab 에서

지금 배포는 손으로 한다 — 서버에서 `scripts\install_offline.bat` 하고
`scripts\run_flask.bat`. 이걸 GitLab 에 넘기려면 **WAS 서버 자체에 shell
executor runner** 를 깔고 `.gitlab-ci.yml` 에 stage 를 하나 더 붙인다.

```yaml
배포:
  stage: deploy
  tags: [portal-was]          # WAS 서버에 붙인 runner tag
  when: manual                # 버튼을 눌러야 나간다. 자동 배포는 하지 않는다.
  rules:
    - if: $CI_COMMIT_BRANCH == "master"
  script:
    - git pull
    - scripts\install_offline.bat
    - powershell -File scripts\restart_flask.ps1   # 재기동 스크립트는 따로 만든다
```

> **자동 배포는 켜지 말 것.** 이 시스템은 07:00 배치가 물려 있어서 아무 때나
> 재기동하면 수집이 끊긴다. `when: manual` 로 두고 배치 시간을 피해서 누른다.

---

## 경로 B — GitHub 에서 개발, GitLab 에 사본 유지

개발은 지금처럼 GitHub 에서 하고(Claude Code 도 계속 쓸 수 있다), 사내 GitLab 에는
읽기용 사본만 둔다. 두 저장소에 모두 닿는 PC 에서 한 줄로 맞춘다.

```bash
scripts\sync_gitlab.bat master
```

처음 한 번만 remote 를 등록해 둔다.

```bash
git remote add gitlab https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
```

사람이 안 돌려도 되게 하려면 GitLab 의 **Pull mirroring** 을 쓴다
(Settings → Repository → Mirroring repositories, `Pull` 방향, GitHub URL 등록).
단 **사내 GitLab 서버가 github.com 으로 나갈 수 있어야** 한다. 못 나가면
`sync_gitlab.bat` 을 작업 스케줄러에 걸어 두는 쪽이 확실하다.

> 이 경로에서는 **GitLab 쪽을 직접 고치지 않는다.** 양쪽에서 고치면 다음
> 동기화 때 push 가 거부된다. 거부는 정상이다 — 덮어쓰지 말고 사람이 본다.

---

## 경로 C — 망분리 (인터넷 PC ↔ 폐쇄망 PC)

wheel 반입하듯이 저장소도 파일 하나로 반입한다. **ZIP 으로 복사하면 안 된다.**
commit 이력이 사라져서 사내에서 이어서 작업할 수가 없다. `git bundle` 을 쓴다.

```text
[인터넷 PC]  scripts\export_git_bundle.bat master
             → data\export\portal-master-<sha>.bundle

             ── USB 등으로 반입 ──

[폐쇄망 PC]  (사내 GitLab 을 clone 한 폴더에서)
             scripts\import_git_bundle.bat D:\반입\portal-master-<sha>.bundle master
```

두 스크립트 다 반입 전후로 `git bundle verify` 를 돌린다. 깨진 파일을 들고
들어갔다가 다시 나오는 일이 없게 한다.

폐쇄망 GitLab 을 **처음** 채울 때는 clone 할 것이 없으므로 이렇게 한다.

```bash
git clone portal-master-<sha>.bundle hli-syscheck-service
cd hli-syscheck-service
git remote set-url origin https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
git push -u origin master --tags
```

반대 방향(사내에서 고친 것을 GitHub 로 내보내기)도 같은 방식이다. 폐쇄망 PC 에서
`git bundle create` 로 묶어 반출하면 된다. 다만 **양쪽에서 동시에 고치면 반드시
충돌**하므로, 어느 쪽이 원본인지 팀에서 하나로 정해 두는 편이 낫다.

---

## 어느 경로든 공통

### 절대 GitLab 에 올라가면 안 되는 것

`.gitignore` 가 막고 있지만, 저장소를 옮길 때 손으로 복사하다 딸려 들어가기 쉽다.
사내 GitLab 은 사람이 더 많이 본다.

```text
.env                            Oracle 계정
config/app_config.local.yaml    실제 접속값
config/vcenters.local.yaml      vCenter 주소·계정
config/oracle_query.local.sql
scripts/env_local.bat
data/                           수집 원본과 DB
```

옮기고 나서 한 번 확인한다.

```bash
git log --all --name-only --pretty=format: | sort -u | findstr /i "\.env local\.yaml local\.sql"
```

한 줄이라도 나오면 이미 이력에 박혀 있는 것이다. 그 상태로 사내에 올리기 전에
계정을 먼저 바꾼다.

### CI 는 양쪽을 같이 고친다

`.github/workflows/ci.yml` 과 `.gitlab-ci.yml` 은 같은 것을 검사한다.
한쪽에 검사를 추가하면 다른 쪽도 같이 넣는다. 안 그러면 "GitHub 에서는 통과했는데
GitLab 에서 깨진다" 가 생긴다. 경로 A 로 완전히 넘어가서 GitHub 을 안 쓰게 되면
그때 `.github/` 를 지운다. 지우기 전에는 둘 다 유지한다.

### 사내망 PC 에서 Claude Code 쓰기

경로 A 로 가면서 Claude 로 계속 개발하려면, 그 PC 에서 `api.anthropic.com` 이
열려야 한다(프록시 허용 목록에 추가). 이건 방화벽 담당자에게 요청할 사항이고,
안 열리면 코드 작성은 인터넷 PC 에서 하고 경로 C 로 반입하는 수밖에 없다.

---

## 처음 한 번 체크리스트

- [ ] 사내 GitLab `26-project-hli-syscheck-gitlab/hli-syscheck-service` 프로젝트 확인 (private)
- [ ] 위 ①②③ 확인해서 경로 A / B / C 중 하나 결정
- [ ] 저장소 올리기 (`git push --all` 또는 bundle 반입)
- [ ] 비밀값이 이력에 없는지 확인
- [ ] Runner 등록하고 `.gitlab-ci.yml` 파이프라인 한 번 통과시키기
- [ ] Settings → Repository → Protected branches 에서 `master` 보호
- [ ] 팀에 `CONTRIBUTING.md` 와 이 문서 링크 공지
