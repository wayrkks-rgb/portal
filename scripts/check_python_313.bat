@echo off
setlocal EnableExtensions

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv was not found.
  echo [ACTION] Run scripts\install_offline.bat first.
  exit /b 1
)

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
if errorlevel 1 (
  echo [ERROR] Current .venv is not Python 3.13.
  ".venv\Scripts\python.exe" --version
  echo [ACTION] Delete .venv and run scripts\install_offline.bat again.
  exit /b 1
)
exit /b 0
