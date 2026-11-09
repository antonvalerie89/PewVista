@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0test_windows_update.ps1"
set "TEST_RESULT=%ERRORLEVEL%"
if not "%TEST_RESULT%"=="0" pause
exit /b %TEST_RESULT%