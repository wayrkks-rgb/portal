# 사내 GitLab 에서 개발하기

`hli-syscheck-service`(일일점검 자동화)는 **별개 신규 프로젝트**다. 이 저장소(`portal`)는
GitHub 에 그대로 두고 옮기지 않는다. 이 문서는 사내 GitLab 쪽 작업을 어떻게 시작하고
이어가는지 정리한 것이다.

```text
portal                  GitHub (wayrkks-rgb/portal)          — 그대로 둔다
hli-syscheck-service    사내 GitLab, 신규                     — 여기서 새로 시작한다
```

대상 저장소:

```text
https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service
```

---

## 0. 먼저 알아야 할 것 — 웹 세션에서는 이 GitLab 에 못 붙는다

claude.ai/code 세션에서 위 주소로 실제로 붙여 보면 egress 정책이 `403` 으로 막는다
(`ito-ax-gitlab.apps.dev.honecloud.co.kr` 이 허용 목록에 없음). 웹 세션은 인터넷상의
격리된 컨테이너에서 돌기 때문이다. 뚫는 방법은 둘 중 하나다.

1. **그 GitLab 이 인터넷에서 열리는 주소라면** — Claude Code 환경(environment)의
   network policy 허용 목록에 이 도메인을 넣으면 웹 세션에서도 직접 붙는다.
   <https://code.claude.com/docs/en/claude-code-on-the-web> 참고. 정책에 막혀 있어서
   여기서는 이 주소가 인터넷에서 열리는지 아닌지까지는 확인할 수 없다.
2. **사내망에서만 열리는 주소라면** — 사내망 PC 에 **Claude Code CLI 를 설치**해서
   거기서 쓴다. 이게 "다이렉트로 수정·배포" 에 해당한다. 그 PC 에서
   `api.anthropic.com` 이 열려야 하므로 방화벽 담당자에게 요청해 둔다.

---

## 경로 A — 사내망 PC 에서 다이렉트 (권장)

가장 단순하다. clone 하고, 고치고, push 한다. GitLab 이 곧 원본이다.

```bash
git clone https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
cd hli-syscheck-service
git checkout -b feature/<작업이름>
# ... 작업 ...
git push -u origin feature/<작업이름>
```

push 하면 GitLab 이 MR 링크를 찍어 준다. 그 링크로 MR 을 만든다.

필요한 것은 두 가지뿐이다.

- **저장소 접근** — Settings → Access Tokens 에서 프로젝트 토큰(`read_repository`,
  `write_repository`)을 만들어 쓰거나, SSH 키를 등록한다.
- **Claude 를 쓸 거면** 그 PC 에서 `api.anthropic.com` 이 열려야 한다.

## 경로 B — 망분리라서 사내망 PC 에 인터넷이 없을 때

코드 작성은 인터넷 PC 에서 하고, 저장소를 **파일 하나로 반입**한다.
**ZIP 으로 복사하면 안 된다.** commit 이력이 사라져서 사내에서 이어서 작업할 수 없다.

```bash
# [인터넷 PC] 묶는다
git bundle create syscheck-main.bundle main --tags
git bundle verify syscheck-main.bundle      # 들고 들어가기 전에 확인한다

#   ── USB 등으로 반입 ──

# [폐쇄망 PC] 사내 GitLab clone 폴더에서
git bundle verify D:\반입\syscheck-main.bundle
git checkout main
git pull D:\반입\syscheck-main.bundle main   # merge 로 받는다. 충돌은 여기서 드러난다
git fetch D:\반입\syscheck-main.bundle "refs/tags/*:refs/tags/*"
git push origin main --tags
```

반대 방향(사내에서 고친 것을 밖으로)도 같은 방식이다. 다만 **양쪽에서 동시에 고치면
반드시 충돌**하므로 어느 쪽이 원본인지 하나로 정해 둔다.

---

## 빈 프로젝트에 처음 올리기

GitLab 프로젝트가 비어 있으면 이렇게 시작한다.

```bash
mkdir hli-syscheck-service && cd hli-syscheck-service
git init -b main
# ... 첫 파일들 ...
git remote add origin https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
git add . && git commit -m "프로젝트 뼈대를 만든다"
git push -u origin main
```

반입 파일에서 시작하는 경우는 이렇게 한다.

```bash
git clone syscheck-main.bundle hli-syscheck-service
cd hli-syscheck-service
git remote set-url origin https://ito-ax-gitlab.apps.dev.honecloud.co.kr/26-project-hli-syscheck-gitlab/hli-syscheck-service.git
git push -u origin main --tags
```

올린 뒤 Settings → Repository → Protected branches 에서 `main` 을 보호한다.

---

## CI 뼈대

새 프로젝트 루트에 `.gitlab-ci.yml` 로 둔다. push 할 때마다 테스트가 돈다.

```yaml
stages: [test]

variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.pip-cache"
  # 사내 PyPI 미러가 있으면 Settings → CI/CD → Variables 에 PIP_INDEX_URL 을 넣는다.

cache:
  key: pip-$CI_COMMIT_REF_SLUG
  paths: [.pip-cache]

테스트:
  stage: test
  image: python:3.11
  # 같은 branch 에 연달아 push 하면 앞선 실행은 의미가 없다. 취소해 대기열을 비운다.
  interruptible: true
  before_script:
    - python -m pip install --upgrade pip
    - pip install -r requirements-dev.txt
  script:
    - python -m pytest -q
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH
```

Runner 가 docker executor 가 아니거나(폐쇄망에서 흔하다) `python:3.11` 이미지를 못
받으면, runner 서버에 파이썬을 미리 깔고 shell executor 로 바꾼다.

```yaml
테스트:
  stage: test
  tags: [syscheck-shell]        # shell executor runner 에 붙인 tag
  before_script:
    - python -m venv .venv
    - .venv/bin/pip install -r requirements-dev.txt
  script:
    - .venv/bin/python -m pytest -q
```

Runner 를 아예 못 붙이면 CI 없이 가되, MR 올리기 전에 사람이 `python -m pytest -q`
를 돌린다.

### 배포까지 GitLab 에서

배포 서버(OpenShift 든 WAS 든)에 shell executor runner 를 붙이고 stage 를 하나 더 둔다.
**자동 배포는 켜지 않는다.** 일일점검은 정해진 시각에 도는 배치라, 아무 때나 재기동하면
점검이 통째로 빈다.

```yaml
배포:
  stage: deploy
  tags: [syscheck-deploy]
  when: manual                  # 버튼을 눌러야 나간다
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  script:
    - ./deploy.sh
```

---

## 사내 LLM 게이트웨이로 호출할 때

LLM·RAG 는 사내 게이트웨이를 거친다. 코드에서 지킬 것은 세 가지다.

**1. 키는 코드에 넣지 않는다.** 환경변수로 읽고, 값은 GitLab
Settings → CI/CD → Variables 에 **Masked + Protected** 로 넣는다. 운영 서버에서는
서버 환경변수나 OpenShift Secret 으로 준다. `.env` 는 반드시 `.gitignore` 에 넣는다.
한 번 commit 되면 이력에 남으므로, 실수로 올렸으면 지우기 전에 **키부터 폐기**한다.

**2. 게이트웨이 주소는 설정으로 뺀다.** 게이트웨이 URL 이 바뀌거나, 나중에
Anthropic API 를 직접 부르게 되어도 코드를 안 고치도록 한 군데서 읽는다.

```python
# llm_client.py — 호출부는 전부 여기를 거친다
import os
import anthropic

def build_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        base_url=os.environ["LLM_GATEWAY_URL"],   # 사내 게이트웨이
        api_key=os.environ["LLM_API_KEY"],
    )

MODEL = os.environ.get("LLM_MODEL", "claude-opus-5")
```

**3. 게이트웨이에 먼저 확인할 것** — 이 답에 따라 클라이언트가 달라진다.

| 확인할 것 | 왜 |
|---|---|
| 어떤 API 규격인가 (Anthropic Messages API 호환인지) | 호환이면 위처럼 `anthropic` SDK 에 `base_url` 만 돌리면 된다. 다른 규격이면 클라이언트를 따로 써야 한다 |
| 쓸 수 있는 모델 목록 | 게이트웨이가 열어 준 모델만 쓸 수 있다 |
| **임베딩을 주는가** | RAG 의 절반은 임베딩이다. 게이트웨이가 안 주면 벡터를 어디서 만들지 따로 정해야 한다 |
| 요청 크기·rate limit | 점검 로그를 통째로 넣으면 걸린다 |
| 로그 보관 정책 | 점검 데이터가 나가는 것이라 사내 승인이 필요할 수 있다 |

---

## 처음 한 번 체크리스트

- [ ] 위 GitLab 주소가 사내망 전용인지 인터넷에서도 열리는지 확인 → 경로 A / B 결정
- [ ] 사내망 PC 에서 Claude 를 쓸 거면 `api.anthropic.com` 방화벽 허용 요청
- [ ] 프로젝트 Access Token 또는 SSH 키 등록
- [ ] 빈 프로젝트에 뼈대 push, `main` Protected branch 설정
- [ ] `.gitlab-ci.yml` 넣고 파이프라인 한 번 통과시키기 (Runner 유무 먼저 확인)
- [ ] LLM 게이트웨이 규격·모델·임베딩 제공 여부 확인
- [ ] `LLM_API_KEY` 를 CI/CD Variables 에 Masked + Protected 로 등록
