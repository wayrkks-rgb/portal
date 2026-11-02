@echo off
rem Daily collection batch. Windows Task Scheduler runs this file at 07:00.
rem
rem There is no console when the scheduler runs it, so EVERYTHING must go to
rem logs\daily_batch_console.log - otherwise a failure leaves no trace at all.
rem The pre-check only records problems; it never stops the batch, because the
rem batch records each source's failure on its own (one bad vCenter must not
rem cost the ITSM collection).
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "LOG=logs\daily_batch_console.log"

>> "%LOG%" echo.
>> "%LOG%" echo ===== BATCH START %date% %time% / account %USERDOMAIN%\%USERNAME% / dir %CD% =====

call scripts\check_python_313.bat >> "%LOG%" 2>&1
if errorlevel 1 (
  >> "%LOG%" echo [STOP] Python .venv is not usable. See the lines above. Run scripts\install_offline.bat.
  >> "%LOG%" echo ===== BATCH END %date% %time% / exit 1 =====
  exit /b 1
)

if exist "scripts\env_local.bat" call "scripts\env_local.bat"

".venv\Scripts\python.exe" scripts\check_auto_configuration.py >> "%LOG%" 2>&1
if errorlevel 1 >> "%LOG%" echo [WARN] Pre-check found problems (see "errors" above). The batch runs anyway.

".venv\Scripts\python.exe" jobs\daily_batch.py >> "%LOG%" 2>&1
set "RC=%errorlevel%"
>> "%LOG%" echo ===== BATCH END %date% %time% / exit %RC% =====
exit /b %RC%
