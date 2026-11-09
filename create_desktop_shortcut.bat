@echo off
setlocal
rem Interactive wrapper for creating Desktop and Start Menu shortcuts.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1" %*
pause
