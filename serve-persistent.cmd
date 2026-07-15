@echo off
rem Runs `python server.py` in a loop, restarting it if it ever exits (crash or
rem otherwise). Logs are appended (not overwritten) to cascade-serve.log with
rem a timestamp marker each time the server starts or stops, so a real crash
rem leaves a traceback you can actually find. Ctrl+C to stop for good.
rem
rem This is the foreground/manual option. For persistence across reboots and
rem logins with no window open at all, register the Task Scheduler task
rem instead (see schtasks-cascade.xml in this directory).

setlocal
cd /d "%~dp0"
set LOGFILE=cascade-serve.log

:loop
echo [%date% %time%] starting cascade server >> "%LOGFILE%"
python server.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] cascade server exited (code %errorlevel%) - restarting in 3s >> "%LOGFILE%"
timeout /t 3 /nobreak >nul
goto loop
