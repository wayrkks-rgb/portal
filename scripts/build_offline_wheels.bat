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

echo [OK] Offline wheels are ready: %CD%\wheels
exit /b 0
