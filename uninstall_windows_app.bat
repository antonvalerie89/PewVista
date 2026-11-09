@echo off
setlocal
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0uninstall_windows_app.ps1" -AppDirectory "%~dp0." -Gui
exit /b %ERRORLEVEL%