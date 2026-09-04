@echo off
REM One-time Google Drive setup for the daily cloud push.
REM Double-click this file, or run it from any terminal.
cd /d "%~dp0"

set "RCLONE="
for /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" %%f in (rclone.exe) do set "RCLONE=%%f"
if not defined RCLONE (
  where rclone >nul 2>&1 && set "RCLONE=rclone"
)
if not defined RCLONE (
  echo rclone not found. Install it with:  winget install Rclone.Rclone
  pause
  exit /b 1
)

echo Using rclone at: %RCLONE%
echo.
echo === Step 1: authorise Google Drive =========================================
echo When rclone asks:
echo    n  (new remote)            name^> gdrive          Storage^> drive
echo    client_id / client_secret / service_account_file  ^> leave blank
echo    scope^> 3      Edit advanced config^> n     Use auto config^> y
echo    (a browser opens - sign in with your PERSONAL Google, click Allow)
echo    Configure as Shared Drive^> n      Keep this remote^> y      then  q
echo ===========================================================================
echo.
pause
"%RCLONE%" config

echo.
echo === Step 2: test the push =================================================
"C:\Users\yager\AppData\Local\Python\pythoncore-3.14-64\python.exe" push_to_cloud.py
echo.
echo If that said "4 file(s) -^> gdrive:SportsVolume" you're done - the daily
echo job will push automatically from now on.
pause
