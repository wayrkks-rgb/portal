@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" jobs\daily_batch.py --demo
exit /b %errorlevel%
