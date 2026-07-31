@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

call scripts\resolve_python_313.bat
if errorlevel 1 exit /b 1

echo [INFO] Python executable: %PYTHON313_EXE%
"%PYTHON313_EXE%" --version
if errorlevel 1 exit /b 1

if not exist "wheels" mkdir "wheels"

"%PYTHON313_EXE%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

"%PYTHON313_EXE%" -m pip download --only-binary=:all: --dest "wheels" -r requirements-oracle.txt
if errorlevel 1 exit /b 1

"%PYTHON313_EXE%" -m pip download --only-binary=:all: --dest "wheels" -r requirements-dev.txt
if errorlevel 1 exit /b 1

REM 통합 웹이 대메뉴 WAS 를 호출할 때 필요하다. 없으면 원격 대메뉴를 쓸 수 없다.
"%PYTHON313_EXE%" -m pip download --only-binary=:all: --dest "wheels" -r requirements-bff.txt
if errorlevel 1 exit /b 1

REM 여러 WAS 가 하나의 DB 를 공유할 때 필요하다. SQLite 로만 쓰면 설치되어 있어도 무해하다.
"%PYTHON313_EXE%" -m pip download --only-binary=:all: --dest "wheels" -r requirements-mysql.txt
if errorlevel 1 exit /b 1

echo [OK] Offline wheels are ready: %CD%\wheels
exit /b 0
