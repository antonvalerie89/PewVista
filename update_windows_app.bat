@echo off
setlocal
rem Build customer Install/Update ZIPs in isolation. Never replace or launch the working app.

set "ROOT_DIR=%~dp0"
set "APP_DIR=%ROOT_DIR%build\release-package\PewVista"

echo Building customer installation and update packages...
echo Your working PewVista program in dist\PewVista will NOT be replaced.
echo.
call "%ROOT_DIR%build_desktop_app.bat"
if errorlevel 1 (
    echo.
    echo [ERROR] Update failed. The existing desktop app has not been launched.
    exit /b 1
)

if not exist "%APP_DIR%\PewVista.exe" if not exist "%APP_DIR%\The Light Ministries.exe" (
    echo [ERROR] PewVista release executable was not found in: "%APP_DIR%"
    exit /b 1
)

echo Creating the single-file Setup.exe and separate customer Install and Update ZIPs...
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT_DIR%package_windows_release.ps1" -AppDirectory "%APP_DIR%"
if errorlevel 1 (
    echo [ERROR] Release packaging failed. Your working installation was not changed.
    if /i not "%~1"=="--no-pause" pause
    exit /b 1
)
echo Customer packages are in dist\releases\latest: Setup.exe, Install ZIP, and Update ZIP.
echo One previous release is retained for recovery.
echo Run test_windows_update.bat to test the Update ZIP on an isolated copy.
echo.
echo Release build finished. No working program or production shortcut was replaced.
exit /b 0
