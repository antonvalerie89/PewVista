# Create Windows Desktop/Start Menu shortcuts for the portable app bundle.
# x64 is required because this release's secondary display launches obs64.exe.
param(
    [string]$TargetExe = "",
    [switch]$AllUsers
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Host "This release requires 64-bit Windows. The secondary display uses bundled 64-bit OBS." -ForegroundColor Red
    Write-Host "A 32-bit build is not available for this release." -ForegroundColor Yellow
    exit 1
}

if (-not $TargetExe) {
    $TargetExe = Join-Path $scriptDir "dist\PewVista\PewVista.exe"
    if (-not (Test-Path -LiteralPath $TargetExe -PathType Leaf)) { $TargetExe = Join-Path $scriptDir "dist\The Light Ministries\The Light Ministries.exe" }
}

if (-not (Test-Path $TargetExe)) {
    Write-Host "Target executable not found at: $TargetExe" -ForegroundColor Red
    Write-Host "Please build the desktop application first by running build_desktop_app.bat." -ForegroundColor Yellow
    exit 1
}

$appDir = Split-Path -Parent $TargetExe
$displayName = "PewVista"
$customIconPath = ""
$churchName = ""
$dataDirectory = Join-Path $appDir "Data"
$installationConfig = Join-Path $appDir "installation.json"
if (Test-Path -LiteralPath $installationConfig) {
    try {
        $settings = Get-Content -LiteralPath $installationConfig -Raw | ConvertFrom-Json
        if ($settings.church_name) { $churchName = [string]$settings.church_name }
        if ($settings.custom_icon_path) { $customIconPath = [string]$settings.custom_icon_path }
        if ($settings.data_directory) { $dataDirectory = [string]$settings.data_directory }
    }
    catch { Write-Host "Could not read church name from installation.json; using default shortcut name." -ForegroundColor Yellow }
}
$brandingConfig = Join-Path $dataDirectory "branding.json"
if (Test-Path -LiteralPath $brandingConfig) {
    try {
        $branding = Get-Content -LiteralPath $brandingConfig -Raw | ConvertFrom-Json
        if ($branding.PSObject.Properties.Name -contains "church_name") { $churchName = [string]$branding.church_name }
        if ($branding.PSObject.Properties.Name -contains "custom_icon_path") { $customIconPath = [string]$branding.custom_icon_path }
    }
    catch { Write-Host "Could not read branding.json; using installation settings." -ForegroundColor Yellow }
}
if ($churchName.Trim()) { $displayName = "PewVista - " + $churchName.Trim() }
$iconPath = Join-Path $appDir "Resourses\PewVista.ico"
if (-not (Test-Path $iconPath)) {
    $iconPath = Join-Path $scriptDir "Resourses\PewVista.ico"
}
if ($customIconPath -and (Test-Path -LiteralPath $customIconPath -PathType Leaf)) {
    $iconPath = $customIconPath
}

$wshShell = New-Object -ComObject WScript.Shell

# 1. Create Desktop shortcut
$desktopFolder = if ($AllUsers) { [System.Environment+SpecialFolder]::CommonDesktopDirectory } else { [System.Environment+SpecialFolder]::Desktop }
$desktopDir = [System.Environment]::GetFolderPath($desktopFolder)
$desktopShortcutPath = Join-Path $desktopDir ($displayName + ".lnk")
$desktopShortcut = $wshShell.CreateShortcut($desktopShortcutPath)
$desktopShortcut.TargetPath = $TargetExe
$desktopShortcut.WorkingDirectory = $appDir
if (Test-Path $iconPath) {
    $desktopShortcut.IconLocation = "$iconPath,0"
}
$desktopShortcut.Description = "$displayName - Church Presentation & Display System"
$desktopShortcut.Save()
Write-Host "Created Desktop shortcut: $desktopShortcutPath" -ForegroundColor Green

# 2. Create Start Menu shortcut
$startMenuFolder = if ($AllUsers) { [System.Environment+SpecialFolder]::CommonPrograms } else { [System.Environment+SpecialFolder]::Programs }
$startMenuDir = [System.Environment]::GetFolderPath($startMenuFolder)
$startMenuShortcutPath = Join-Path $startMenuDir ($displayName + ".lnk")
$startMenuShortcut = $wshShell.CreateShortcut($startMenuShortcutPath)
$startMenuShortcut.TargetPath = $TargetExe
$startMenuShortcut.WorkingDirectory = $appDir
if (Test-Path $iconPath) {
    $startMenuShortcut.IconLocation = "$iconPath,0"
}
$startMenuShortcut.Description = "$displayName - Church Presentation & Display System"
$startMenuShortcut.Save()
Write-Host "Created Start Menu shortcut: $startMenuShortcutPath" -ForegroundColor Green

Write-Host "`nDesktop and Start Menu shortcuts are ready!" -ForegroundColor Cyan
