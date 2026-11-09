param(
    [string]$AppDirectory = "",
    [string]$BackupRoot = ""
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $AppDirectory) {
    $AppDirectory = Join-Path $scriptDirectory "dist\PewVista"
    if (-not (Test-Path -LiteralPath $AppDirectory -PathType Container)) { $AppDirectory = Join-Path $scriptDirectory "dist\The Light Ministries" }
}
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $scriptDirectory "backups\desktop-data"
}

try {
    if (Get-Process -Name "PewVista", "The Light Ministries" -ErrorAction SilentlyContinue) {
        throw "Close PewVista before backing up or updating its data."
    }
    $source = Join-Path $AppDirectory "Data"
    $installationConfig = Join-Path $AppDirectory "installation.json"
    if (Test-Path -LiteralPath $installationConfig -PathType Leaf) {
        $settings = Get-Content -LiteralPath $installationConfig -Raw | ConvertFrom-Json
        $configuredData = [string]$settings.data_directory
        $programDataRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)).TrimEnd('\')
        if (-not [System.IO.Path]::IsPathRooted($configuredData)) { throw "Installed shared-data path is invalid." }
        $configuredData = [System.IO.Path]::GetFullPath($configuredData).TrimEnd('\')
        if (-not $configuredData.StartsWith($programDataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Installed shared-data path is outside ProgramData." }
        $source = $configuredData
    }
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Write-Host "No installed Data folder to back up; this is a new installation."
        exit 0
    }
    $name = (Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-" + [guid]::NewGuid().ToString("N")
    $destination = Join-Path (Join-Path $BackupRoot $name) "Data"
    [System.IO.Directory]::CreateDirectory($destination) | Out-Null
    & robocopy.exe $source $destination /E /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1
    if ($LASTEXITCODE -ge 8) {
        throw "Data backup failed. The update must not continue."
    }
    Write-Host "Installed data backed up to: $destination"
    exit 0
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)"
    exit 1
}