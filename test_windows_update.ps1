param(
    [string]$SourceDirectory = "",
    [string]$TestRoot = "",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $SourceDirectory) {
    $SourceDirectory = Join-Path $scriptDirectory "dist\PewVista"
    if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) { $SourceDirectory = Join-Path $scriptDirectory "dist\The Light Ministries" }
}
if (-not $TestRoot) { $TestRoot = Join-Path $scriptDirectory "dist\update-test" }
try {
    $SourceDirectory = [System.IO.Path]::GetFullPath($SourceDirectory).TrimEnd('\')
    $target = [System.IO.Path]::GetFullPath((Join-Path $TestRoot "PewVista")).TrimEnd('\')
    if ($target.Equals($SourceDirectory, [StringComparison]::OrdinalIgnoreCase) -or
        $target.StartsWith($SourceDirectory + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $SourceDirectory.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Test and working installations must be separate folders." }
    if (-not (Test-Path -LiteralPath (Join-Path $target ".test-installation"))) {
        if (Test-Path -LiteralPath $target) { throw "Test destination exists without a TEST marker. It will not be overwritten." }
        $sourceExe = Join-Path $SourceDirectory "PewVista.exe"
        if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) { $sourceExe = Join-Path $SourceDirectory "The Light Ministries.exe" }
        if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) { throw "The old app was not found. Set SourceDirectory to an existing installed version." }
        if (@(Get-Process -Name "PewVista", "The Light Ministries" -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $sourceExe }).Count -gt 0) { throw "Close the working app once to create a consistent test copy. Its files will not be changed." }
        $sourceConfigPath = Join-Path $SourceDirectory "installation.json"
        $sourceData = Join-Path $SourceDirectory "Data"
        $settings = @{ church_name = "The Light Ministries"; data_directory = $sourceData; default_background_path = "" }
        if (Test-Path -LiteralPath $sourceConfigPath -PathType Leaf) {
            $settings = Get-Content -LiteralPath $sourceConfigPath -Raw | ConvertFrom-Json
            if (-not [System.IO.Path]::IsPathRooted([string]$settings.data_directory)) { throw "The source installation has an invalid shared-data path." }
            $sourceData = [System.IO.Path]::GetFullPath([string]$settings.data_directory).TrimEnd('\')
        }
        if (-not (Test-Path -LiteralPath $sourceData -PathType Container)) { throw "Source shared data was not found: $sourceData" }
        $testData = [System.IO.Path]::GetFullPath((Join-Path $TestRoot "SharedData")).TrimEnd('\')
        if ($testData.StartsWith($sourceData + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $sourceData.StartsWith($testData + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Test data and source data must be separate folders." }
        Write-Host "Creating an isolated copy of the old version and its data..."
        & robocopy.exe $SourceDirectory $target /E /XD .update-backups /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1
        if ($LASTEXITCODE -ge 8) { throw "Test copy failed. The working app was not changed." }
        & robocopy.exe $sourceData $testData /E /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1
        if ($LASTEXITCODE -ge 8) { throw "Shared-data test copy failed. The working app was not changed." }
        $settings.data_directory = $testData
        $background = [string]$settings.default_background_path
        if ($background -and $background.StartsWith($sourceData + '\', [StringComparison]::OrdinalIgnoreCase)) {
            $settings.default_background_path = Join-Path $testData $background.Substring($sourceData.Length + 1)
        }
        [System.IO.File]::WriteAllText((Join-Path $target "installation.json"), ($settings | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText((Join-Path $target ".test-installation"), "Isolated update test. Original: $SourceDirectory")
    }
    Write-Host "TEST COPY: $target"
    Write-Host "In this copy only, choose Update > Install Update and select the new Update ZIP."
    Write-Host "If the old copy has no Update menu, bootstrap this TEST folder once with a menu-enabled update; leave the working folder alone."
    Write-Host "Your production program, data, and shortcuts were not replaced."
    if (-not $NoLaunch) {
        $targetExe = Join-Path $target "PewVista.exe"
        if (-not (Test-Path -LiteralPath $targetExe -PathType Leaf)) { $targetExe = Join-Path $target "The Light Ministries.exe" }
        Start-Process -FilePath $targetExe -WorkingDirectory $target
    }
}
catch { Write-Host "[ERROR] $($_.Exception.Message)"; exit 1 }