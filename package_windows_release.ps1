param(
    [string]$AppDirectory = "",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $AppDirectory) { $AppDirectory = Join-Path $scriptDirectory "build\release-package\PewVista" }
if (-not $OutputRoot) { $OutputRoot = Join-Path $scriptDirectory "dist\releases" }
$temporaryDirectory = $null
$setupWorkingDirectory = $null
try {
    foreach ($required in @("apply_windows_update.ps1", "install_windows_app.ps1", "install_windows_app.bat", "install_obs_runtime.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat")) {
        if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory $required) -PathType Leaf)) { throw "Release payload is missing $required. Run build_desktop_app.bat first." }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory "PewVista.exe") -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $AppDirectory "The Light Ministries.exe") -PathType Leaf)) { throw "Release payload is missing PewVista.exe." }
    if (-not (Test-Path -LiteralPath (Join-Path $scriptDirectory "setup_windows_self_extract.ps1") -PathType Leaf)) { throw "The self-extracting setup bootstrapper is missing." }
    $version = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    [System.IO.Directory]::CreateDirectory($OutputRoot) | Out-Null
    $temporaryDirectory = Join-Path $OutputRoot (".preparing-" + [guid]::NewGuid().ToString("N"))
    [System.IO.Directory]::CreateDirectory($temporaryDirectory) | Out-Null
    $previousReleaseDirectory = Join-Path $OutputRoot "latest"
    $previousInstallArchive = Join-Path $previousReleaseDirectory "PewVista-Install-Windows-x64.zip"
    if (-not (Test-Path -LiteralPath $previousInstallArchive -PathType Leaf)) {
        $legacyArchive = Join-Path $previousReleaseDirectory "The Light Ministries-Install-Windows-x64.zip"
        if (Test-Path -LiteralPath $legacyArchive -PathType Leaf) { $previousInstallArchive = $legacyArchive }
    }
    foreach ($kind in @("Installation", "Update")) {
        $label = if ($kind -eq "Installation") { "Install" } else { "Update" }
        $archive = Join-Path $temporaryDirectory "PewVista-$label-Windows-x64.zip"
        if ($kind -eq "Update" -and (Test-Path -LiteralPath $previousInstallArchive -PathType Leaf)) {
            powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $scriptDirectory "create_windows_zip.ps1") -AppDirectory $AppDirectory -ArchivePath $archive -PackageType $kind -PreviousInstallArchive $previousInstallArchive -ReleaseVersion $version
        }
        else {
            powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $scriptDirectory "create_windows_zip.ps1") -AppDirectory $AppDirectory -ArchivePath $archive -PackageType $kind -ReleaseVersion $version
        }
        if ($LASTEXITCODE -ne 0) { throw "$kind package creation failed. No finished release has been published." }
    }
    $setupExecutable = Join-Path $temporaryDirectory "PewVista Setup.exe"
    Write-Host "[SETUP] Building and verifying the single-file installer..."
    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $scriptDirectory "build_windows_setup.ps1") -InstallationArchive (Join-Path $temporaryDirectory "PewVista-Install-Windows-x64.zip") -SetupExecutable $setupExecutable
    if ($LASTEXITCODE -ne 0) { throw "Setup.exe build failed. No finished release has been published." }
    $pythonExe = Join-Path $scriptDirectory ".venv\Scripts\python.exe"
    $autoUpdateScript = Join-Path $scriptDirectory "auto_update.py"
    if ((Test-Path -LiteralPath $pythonExe -PathType Leaf) -and (Test-Path -LiteralPath $autoUpdateScript -PathType Leaf)) {
        & $pythonExe $autoUpdateScript feed $temporaryDirectory "https://github.com/antonvalerie89/PewVista/releases/latest/download" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "latest.json feed creation failed. No finished release has been published." }
    }
    function Move-DirectoryWithRetry([string]$Source, [string]$Destination) {
        <#
         What: Rename a folder, waiting while another program briefly holds files open in it.
         Why included: Antivirus scans the freshly built Setup.exe right after its checks run,
           which makes Windows refuse the rename for a short time.
         Where used: Publishing the finished release into dist\releases.
         Returns: Nothing; throws the last error if the folder stays locked for about a minute.
        #>
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            try { [System.IO.Directory]::Move($Source, $Destination); return }
            catch [System.IO.IOException], [System.UnauthorizedAccessException] {
                if ($attempt -eq 30) { throw }
                if ($attempt -eq 1) { Write-Host "Waiting for Windows to release the new release files (antivirus scan)..." }
                Start-Sleep -Seconds 2
            }
        }
    }
    $releaseDirectory = Join-Path $OutputRoot "latest"
    $previousDirectory = Join-Path $OutputRoot "previous"
    if ([System.IO.Directory]::Exists($releaseDirectory)) {
        if ([System.IO.Directory]::Exists($previousDirectory)) {
            $expectedNames = @("PewVista-Install-Windows-x64.zip", "PewVista-Update-Windows-x64.zip", "PewVista Setup.exe", "latest.json", "The Light Ministries-Install-Windows-x64.zip", "The Light Ministries-Update-Windows-x64.zip", "The Light Ministries Setup.exe")
            if (@(Get-ChildItem -LiteralPath $previousDirectory -Force | Where-Object { $_.PSIsContainer -or $_.Name -notin $expectedNames }).Count -gt 0) {
                throw "The previous release folder contains other files. It was preserved; move those files before publishing."
            }
            [System.IO.Directory]::Delete($previousDirectory, $true)
        }
        Move-DirectoryWithRetry $releaseDirectory $previousDirectory
    }
    try {
        Move-DirectoryWithRetry $temporaryDirectory $releaseDirectory
    }
    catch {
        if (-not [System.IO.Directory]::Exists($releaseDirectory) -and [System.IO.Directory]::Exists($previousDirectory)) {
            Move-DirectoryWithRetry $previousDirectory $releaseDirectory
        }
        throw
    }
    $temporaryDirectory = $null
    Write-Host "Release ready: $releaseDirectory"
    Write-Host "New customer: PewVista Setup.exe (no manual extraction required)"
    Write-Host "Alternative: PewVista-Install-Windows-x64.zip"
    Write-Host "Existing customer: PewVista-Update-Windows-x64.zip"
    Write-Host "Your working program and shortcuts were not replaced."
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)"
    exit 1
}
finally {
    if ($setupWorkingDirectory -and [System.IO.Directory]::Exists($setupWorkingDirectory)) {
        Remove-Item -LiteralPath $setupWorkingDirectory -Recurse -Force
    }
    if ($temporaryDirectory -and [System.IO.Directory]::Exists($temporaryDirectory)) {
        [System.IO.Directory]::Delete($temporaryDirectory, $true)
    }
}