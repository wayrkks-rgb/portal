@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

REM 폐쇄망 PC 에서 돌린다. 사내 GitLab 을 clone 한 폴더에서 실행해야 한다
REM (origin = 사내 GitLab). 반입한 bundle 을 읽어 GitLab 에 올린다.
REM
REM   scripts\import_git_bundle.bat D:\반입\portal-master-3cda991.bundle master

set "BUNDLE=%~1"
if "%BUNDLE%"=="" (
  echo [ERROR] 사용법: scripts\import_git_bundle.bat ^<bundle 파일^> [branch]
  exit /b 1
)
if not exist "%BUNDLE%" (
  echo [ERROR] 파일이 없다: %BUNDLE%
  exit /b 1
)

set "BRANCH=%~2"
if "%BRANCH%"=="" set "BRANCH=master"

echo [INFO] 반입 파일을 확인한다
git bundle verify "%BUNDLE%"
if errorlevel 1 exit /b 1

echo [INFO] %BRANCH% 로 옮긴다
git checkout %BRANCH%
if errorlevel 1 exit /b 1

REM merge 로 받는다. 사내 GitLab 에서 따로 한 작업이 있으면 여기서 충돌이
REM 드러난다. 덮어쓰지 않으므로 사람이 보고 판단할 수 있다.
git pull "%BUNDLE%" %BRANCH%
if errorlevel 1 (
  echo [ERROR] 충돌이 났다. 해결한 뒤 git commit 하고 다시 push 한다.
  exit /b 1
)

git fetch "%BUNDLE%" "refs/tags/*:refs/tags/*"
if errorlevel 1 exit /b 1

echo [INFO] 사내 GitLab 으로 보낸다
git push origin %BRANCH% --tags
if errorlevel 1 exit /b 1

echo [OK] 사내 GitLab %BRANCH% 반영 완료
exit /b 0
