@echo off
REM Daily sports-volume pull. Registered with Windows Task Scheduler by
REM install_schedule.ps1. Logs (overwritten each run) go to run_all.log.
cd /d "%~dp0"
"C:\Users\yager\AppData\Local\Python\pythoncore-3.14-64\python.exe" -u run_all.py > run_all.log 2>&1
