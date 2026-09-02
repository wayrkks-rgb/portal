@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

REM 인터넷 PC 에서 돌린다. 저장소를 파일 하나로 묶어 폐쇄망 반입용으로 만든다.
REM ZIP 으로 복사하면 commit 이력이 사라져서 사내 GitLab 에서 이어서 작업할 수
REM 없다. bundle 은 이력과 tag 를 그대로 담는다.
REM 반입한 파일은 폐쇄망 PC 에서 import_git_bundle.bat 으로 올린다.

set "BRANCH=%~1"
if "%BRANCH%"=="" set "BRANCH=master"

echo [INFO] origin/%BRANCH% 를 최신으로 맞춘다
git checkout %BRANCH%
if errorlevel 1 exit /b 1
git pull origin %BRANCH%
if errorlevel 1 exit /b 1

for /f %%i in ('git rev-parse --short HEAD') do set "SHA=%%i"

if not exist "data\export" mkdir "data\export"
set "OUT=data\export\portal-%BRANCH%-%SHA%.bundle"

git bundle create "%OUT%" %BRANCH% --tags
if errorlevel 1 exit /b 1

REM 반입 전에 파일이 성한지 여기서 확인한다. 폐쇄망에 들고 들어가서
REM 깨진 것을 알게 되면 다시 나와야 한다.
git bundle verify "%OUT%"
if errorlevel 1 exit /b 1

echo [OK] 반입 파일: %CD%\%OUT%
exit /b 0
