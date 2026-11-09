@echo off
setlocal
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0install_windows_app.ps1" -Gui
exit /b %ERRORLEVEL%