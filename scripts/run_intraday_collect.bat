@echo off
rem Intraday collection (every 30/60 minutes in business hours). Inventory only.
rem Stores a snapshot only when something changed. Shares the daily batch lock.
rem
rem There is no console when the scheduler runs it, so EVERYTHING goes to
rem logs\intraday_console.log. Run it from cmd, not with python in front.
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "LOG=logs\intraday_console.log"

>> "%LOG%" echo.
>> "%LOG%" echo ===== INTRADAY START %date% %time% / account %USERDOMAIN%\%USERNAME% / dir %CD% =====

call scripts\check_python_313.bat >> "%LOG%" 2>&1
if errorlevel 1 (
  >> "%LOG%" echo [STOP] Python .venv is not usable. See the lines above. Run scripts\install_offline.bat.
  >> "%LOG%" echo ===== INTRADAY END %date% %time% / exit 1 =====
  exit /b 1
)

if exist "scripts\env_local.bat" call "scripts\env_local.bat"

".venv\Scripts\python.exe" jobs\intraday_collect.py >> "%LOG%" 2>&1
set "RC=%errorlevel%"
>> "%LOG%" echo ===== INTRADAY END %date% %time% / exit %RC% =====
exit /b %RC%
