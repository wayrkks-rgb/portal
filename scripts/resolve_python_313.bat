@echo off
setlocal EnableExtensions
set "PYTHON_EXE="

for %%C in (python.exe python3.13.exe) do (
  for /f "delims=" %%P in ('where %%C 2^>nul') do (
    if not defined PYTHON_EXE (
      "%%P" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>&1
      if not errorlevel 1 set "PYTHON_EXE=%%P"
    )
  )
)

if not defined PYTHON_EXE (
  where py.exe >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%P in ('py.exe -3.13 -c "import sys; print(sys.executable)" 2^>nul') do (
      if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
    )
  )
)

if not defined PYTHON_EXE (
  echo [ERROR] CPython 3.13 executable was not found.
  echo [ACTION] Run: python --version
  echo [ACTION] Add Python 3.13 to PATH, or install the Python Launcher.
  exit /b 1
)

endlocal & set "PYTHON313_EXE=%PYTHON_EXE%"
exit /b 0
