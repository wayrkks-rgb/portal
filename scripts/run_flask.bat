@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
if exist "scripts\env_local.bat" call "scripts\env_local.bat"
set "FLASK_DEBUG=0"
".venv\Scripts\python.exe" app.py
exit /b %errorlevel%
