@echo off
setlocal enabledelayedexpansion

rem Build an isolated release payload. Never deploy over the working app or change shortcuts.

echo =======================================================
echo   PewVista - Windows Desktop App Builder
echo   Compatible with Windows 10 and Windows 11 (64-bit)
echo =======================================================
echo.

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
set "PYINSTALLER_EXE=%ROOT_DIR%.venv\Scripts\pyinstaller.exe"
set "DIST_APP_DIR=%ROOT_DIR%build\release-package\PewVista"
set "BUILD_DIST_DIR=%ROOT_DIR%build\desktop-staging"
set "STAGED_APP_DIR=%BUILD_DIST_DIR%\PewVista"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment Python not found at: "%PYTHON_EXE%"
    exit /b 1
)

echo [Step 1/5] Ensuring build, spelling, and grammar dependencies are available...
"%PYTHON_EXE%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PYTHON_EXE%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import spylls.hunspell" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install spylls==0.1.7
    if errorlevel 1 (
        echo [ERROR] Failed to install the spell-check dictionary reader.
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import cryptography.hazmat.primitives.asymmetric.ed25519" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install cryptography
    if errorlevel 1 (
        echo [ERROR] Failed to install the update-signature library.
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import sqlcipher3" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install sqlcipher3-wheels
    if errorlevel 1 (
        echo [ERROR] Failed to install the member-data encryption library.
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import langid.langid" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install langid==1.1.6
    if errorlevel 1 (
        echo [ERROR] Failed to install the offline language detector.
        exit /b 1
    )
)

if not exist "%ROOT_DIR%Resourses\grammar\java\bin\java.exe" (
    echo [ERROR] Portable Java is missing from Resourses\grammar\java.
    exit /b 1
)
if not exist "%ROOT_DIR%Resourses\grammar\LanguageTool\languagetool-server.jar" (
    echo [ERROR] Local LanguageTool is missing from Resourses\grammar\LanguageTool.
    exit /b 1
)

echo [Step 2/5] Running pre-build reliability checks...
if /I "%PEWVISTA_SKIP_CHECKS%"=="1" (
    echo [Gate] PEWVISTA_SKIP_CHECKS=1 set; skipping reliability checks.
) else (
    call "%ROOT_DIR%run_reliability_checks.bat"
    if errorlevel 1 (
        echo [ERROR] Pre-build reliability checks failed. Aborting build.
        echo         Fix the failures, or set PEWVISTA_SKIP_CHECKS=1 to bypass once.
        exit /b 1
    )
)

echo.
echo [Step 3/5] Building Windows executable with PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --distpath "%BUILD_DIST_DIR%" "%ROOT_DIR%PewVista.spec"
if errorlevel 1 (
    echo [ERROR] PyInstaller compilation failed.
    exit /b 1
)

echo.
echo [Step 4/5] Syncing runtime data and resources into app package...

if not exist "%STAGED_APP_DIR%\PewVista.exe" (
    echo [ERROR] Staged executable not found: "%STAGED_APP_DIR%\PewVista.exe"
    exit /b 1
)

if not exist "%DIST_APP_DIR%" mkdir "%DIST_APP_DIR%"
echo Preparing isolated release program files...
robocopy "%STAGED_APP_DIR%\_internal" "%DIST_APP_DIR%\_internal" /MIR /NP /NFL /NDL /R:1 /W:1 >nul 2>&1
if errorlevel 8 (
    echo [ERROR] Could not install updated runtime files. Close the app and try again.
    exit /b 1
)
copy /Y "%STAGED_APP_DIR%\PewVista.exe" "%DIST_APP_DIR%\PewVista.exe" >nul
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT_DIR%sign_windows_file.ps1" -FilePath "%DIST_APP_DIR%\PewVista.exe"
if errorlevel 1 (
    echo [ERROR] PewVista.exe could not be code-signed.
    exit /b 1
)
if exist "%DIST_APP_DIR%\The Light Ministries.exe" del /Q "%DIST_APP_DIR%\The Light Ministries.exe"
copy /Y "%ROOT_DIR%apply_windows_update.ps1" "%DIST_APP_DIR%\apply_windows_update.ps1" >nul
if errorlevel 1 exit /b 1

echo Preparing Backgrounds folder...
if not exist "%DIST_APP_DIR%\Backgrounds" mkdir "%DIST_APP_DIR%\Backgrounds"

echo Syncing Bibles...
robocopy "%ROOT_DIR%Bibles" "%DIST_APP_DIR%\Bibles" /E /XO /NP /NFL /NDL /R:1 /W:1 >nul 2>&1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%ROOT_DIR%prune_restricted_bibles.ps1" -BiblesDirectory "%DIST_APP_DIR%\Bibles"
if errorlevel 1 (
    echo [ERROR] Restricted Bibles could not be removed from the release payload.
    exit /b 1
)

echo Syncing Data...
rem Seed only missing files so operational app data is never overwritten by source copies.
robocopy "%ROOT_DIR%Data" "%DIST_APP_DIR%\Data" /E /XC /XN /XO /NP /NFL /NDL /R:1 /W:1 /XD "%ROOT_DIR%Data\Backgrounds" "%ROOT_DIR%Data\.backups" "%ROOT_DIR%Data\.import-backups" "%ROOT_DIR%Data\.background-migration-backups" /XF *.db *.db-wal *.db-shm *.db-journal members.key *.log display_queue_bridge.json bible_settings.json music_licenses.json obs_secondary_settings.json branding.json license.json license.json.tmp .background-migration.json >nul 2>&1

echo Syncing Resourses...
rem OBS is not shipped: Setup reuses an installed OBS or downloads the official build.
robocopy "%ROOT_DIR%Resourses" "%DIST_APP_DIR%\Resourses" /E /XO /NP /NFL /NDL /R:1 /W:1 /XD "%ROOT_DIR%Resourses\obs_portable" "%ROOT_DIR%Resourses\obs_bootstrap" >nul 2>&1
if exist "%DIST_APP_DIR%\Resourses\obs_portable" rmdir /S /Q "%DIST_APP_DIR%\Resourses\obs_portable"
if exist "%DIST_APP_DIR%\Resourses\obs_bootstrap" rmdir /S /Q "%DIST_APP_DIR%\Resourses\obs_bootstrap"
if exist "%DIST_APP_DIR%\Resourses\obs_portable" (
    echo [ERROR] Could not remove the old bundled OBS from the app package. Close OBS and try again.
    exit /b 1
)

if exist "%ROOT_DIR%config" (
    echo Syncing config...
    robocopy "%ROOT_DIR%config" "%DIST_APP_DIR%\config" /E /XO /NP /NFL /NDL /R:1 /W:1 >nul 2>&1
)
copy /Y "%ROOT_DIR%WINDOWS_REQUIREMENTS.txt" "%DIST_APP_DIR%\WINDOWS_REQUIREMENTS.txt" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy Windows requirements into the app package.
    exit /b 1
)
copy /Y "%ROOT_DIR%TERMS_AND_CONDITIONS.txt" "%DIST_APP_DIR%\TERMS_AND_CONDITIONS.txt" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy the Terms and Conditions into the app package.
    exit /b 1
)
copy /Y "%ROOT_DIR%OPERATING_MANUAL.txt" "%DIST_APP_DIR%\OPERATING_MANUAL.txt" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy the Operating Manual into the app package.
    exit /b 1
)
copy /Y "%ROOT_DIR%THIRD_PARTY_NOTICES.txt" "%DIST_APP_DIR%\THIRD_PARTY_NOTICES.txt" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy the third-party notices into the app package.
    exit /b 1
)
copy /Y "%ROOT_DIR%SECURITY_AND_PRIVACY.txt" "%DIST_APP_DIR%\SECURITY_AND_PRIVACY.txt" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy the Security and Privacy statement into the app package.
    exit /b 1
)
rem Put the publisher's price from Resourses\pricing.json into the packaged documents.
"%PYTHON_EXE%" "%ROOT_DIR%licensing.py" fill-price "%DIST_APP_DIR%\TERMS_AND_CONDITIONS.txt" "%DIST_APP_DIR%\OPERATING_MANUAL.txt"
if errorlevel 1 (
    echo [ERROR] Could not insert the subscription price into the packaged documents.
    exit /b 1
)

echo.
echo [Step 5/5] Including customer installation helpers...
for %%F in (install_windows_app.ps1 install_windows_app.bat install_obs_runtime.ps1 create_desktop_shortcut.ps1 uninstall_windows_app.ps1 uninstall_windows_app.bat) do (
    copy /Y "%ROOT_DIR%%%F" "%DIST_APP_DIR%\%%F" >nul
    if errorlevel 1 exit /b 1
)

echo.
echo =======================================================
echo   BUILD COMPLETE!
echo.
echo   Application Location:
echo   "%DIST_APP_DIR%\PewVista.exe"
echo.
echo   Your existing app and shortcuts have not been changed.
echo   Run update_windows_app.bat to create customer release ZIPs.
echo =======================================================
exit /b 0
