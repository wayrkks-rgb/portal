@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

REM GitHub 에서 개발하고 사내 GitLab 에는 사본을 두는 경우에 쓴다.
REM 두 저장소에 모두 닿는 PC 에서만 동작한다. 망이 분리되어 있으면
REM export_git_bundle.bat / import_git_bundle.bat 을 쓴다.
REM 자세한 절차는 GITLAB_SETUP_GUIDE.md 를 본다.

git remote get-url gitlab >nul 2>&1
if errorlevel 1 (
  echo [ERROR] gitlab remote 가 없다. 처음 한 번만 등록한다:
  echo         git remote add gitlab https://gitlab.example.local/그룹/portal.git
  exit /b 1
)

set "BRANCH=%~1"
if "%BRANCH%"=="" set "BRANCH=master"

echo [INFO] origin/%BRANCH% 를 받는다
git fetch origin %BRANCH%
if errorlevel 1 exit /b 1

echo [INFO] gitlab/%BRANCH% 로 보낸다
REM 강제 push 는 하지 않는다. GitLab 쪽에만 있는 commit 이 있으면 여기서
REM 거부되는 것이 맞다. 덮어쓰면 사내에서 한 작업이 조용히 사라진다.
git push gitlab FETCH_HEAD:refs/heads/%BRANCH%
if errorlevel 1 (
  echo [ERROR] 거부되었다. GitLab 에만 있는 commit 이 있는지 먼저 확인한다:
  echo         git fetch gitlab %BRANCH% ^&^& git log --oneline origin/%BRANCH%..FETCH_HEAD
  exit /b 1
)

echo [INFO] tag 를 보낸다
git push gitlab --tags
if errorlevel 1 exit /b 1

echo [OK] gitlab/%BRANCH% 동기화 완료
exit /b 0
