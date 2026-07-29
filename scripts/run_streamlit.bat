@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\streamlit.exe" (
  echo [ERROR] Streamlit is not installed in .venv.
  echo [ACTION] Prepare and install requirements-streamlit.txt wheels.
  exit /b 1
)
".venv\Scripts\streamlit.exe" run dashboard\streamlit_app.py --server.address 0.0.0.0 --server.port 8501
exit /b %errorlevel%
