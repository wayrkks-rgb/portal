@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\check_auto_configuration.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" jobs\daily_batch.py >> "logs\daily_batch_console.log" 2>&1
exit /b %errorlevel%
