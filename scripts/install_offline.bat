@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

call scripts\resolve_python_313.bat
if errorlevel 1 exit /b 1

echo [INFO] Python executable: %PYTHON313_EXE%
"%PYTHON313_EXE%" --version
if errorlevel 1 exit /b 1

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
  if errorlevel 1 (
    echo [ERROR] Existing .venv is not Python 3.13.
    echo [ACTION] Delete it with: rmdir /s /q .venv
    exit /b 1
  )
) else (
  "%PYTHON313_EXE%" -m venv .venv
  if errorlevel 1 exit /b 1
)

if not exist "wheels" (
  echo [ERROR] wheels directory was not found.
  echo [ACTION] Run scripts\build_offline_wheels.bat on an Internet-connected PC first.
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --no-index --find-links "wheels" -r requirements-oracle.txt
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pip install --no-index --find-links "wheels" -r requirements-dev.txt
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" scripts\initialize_db.py
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" scripts\verify_installation.py
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" scripts\check_powercli_installation.py
if errorlevel 1 (
  echo [WARN] PowerCLI module is not ready yet. DEMO mode is available.
  echo [ACTION] Install the offline PowerCLI module before vCenter automatic collection.
)

echo [OK] Offline installation completed with Python 3.13.
exit /b 0
