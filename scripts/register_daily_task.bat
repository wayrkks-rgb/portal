@echo off
rem 매일 정해진 시각에 수집 배치가 돌도록 Windows 작업 스케줄러에 등록한다.
rem
rem   관리자 권한으로 실행해야 한다. (우클릭 - 관리자 권한으로 실행)
rem   시각과 작업 이름은 설정(scheduler.daily_time / task_name)을 따른다.
rem
rem   scripts\register_daily_task.bat            등록 또는 갱신
rem   scripts\register_daily_task.bat /status    현재 등록 상태만 확인
rem   scripts\register_daily_task.bat /delete    등록 해제
rem
rem 배치를 잠시 쉬게 하려면 이 작업을 지울 필요가 없다. 관리 - 연계 설정에서
rem 일일 배치를 끄면 배치가 스스로 빠진다.
setlocal EnableExtensions
cd /d "%~dp0.."
call scripts\check_python_313.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" scripts\manage_daily_task.py %*
exit /b %errorlevel%
