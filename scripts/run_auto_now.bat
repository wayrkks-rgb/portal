@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\check_auto_configuration.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" jobs\daily_batch.py
set "RC=%errorlevel%"
if "%RC%"=="0" (
  echo [OK] Automatic Oracle and PowerCLI vCenter collection completed.
) else (
  echo [ERROR] Automatic collection failed. Check logs\asset_sync.log and logs\daily_batch_console.log.
)
exit /b %RC%
