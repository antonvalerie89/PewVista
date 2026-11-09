param(
    [string]$AppDirectory = "",
    [string]$ArchivePath = "",
    [switch]$IncludeUserData,
    [ValidateSet("Update", "Installation")][string]$PackageType = "Update",
    [string]$PreviousInstallArchive = "",
    [string]$ReleaseVersion = ""
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $AppDirectory) {
    $AppDirectory = Join-Path $scriptDirectory "dist\PewVista"
}
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $scriptDirectory "dist\PewVista-Windows-x64.zip"
}

$temporaryArchive = $null
$archiveStream = $null
$archive = $null
$currentSource = $AppDirectory
try {
    $AppDirectory = [System.IO.Path]::GetFullPath($AppDirectory)
    $ArchivePath = [System.IO.Path]::GetFullPath($ArchivePath)
    $executablePath = Join-Path $AppDirectory "PewVista.exe"
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) { $executablePath = Join-Path $AppDirectory "The Light Ministries.exe" }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) { throw "PewVista executable was not found in the application folder." }
    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $scriptDirectory "prune_restricted_bibles.ps1") -BiblesDirectory (Join-Path $AppDirectory "Bibles") -CheckOnly
    if ($LASTEXITCODE -ne 0) { throw "The release contains Bibles that may not be redistributed. Run build_desktop_app.bat to remove them." }
    if ($ArchivePath.StartsWith($AppDirectory.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The ZIP must be outside the application directory."
    }

    $archiveDirectory = [System.IO.Path]::GetDirectoryName($ArchivePath)
    [System.IO.Directory]::CreateDirectory($archiveDirectory) | Out-Null
    $temporaryArchive = Join-Path $archiveDirectory (".windows-app-" + [guid]::NewGuid().ToString("N") + ".zip")
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    Write-Host "[ZIP] Scanning application files..."
    $files = @(Get-ChildItem -LiteralPath $AppDirectory -File -Recurse -Force)
    $files = @($files | Where-Object { $_.FullName -ne (Join-Path $AppDirectory "release.json") })
    $files = @($files | Where-Object { $_.FullName -ne (Join-Path $AppDirectory "installation.json") })
    $files = @($files | Where-Object { $_.FullName -ne (Join-Path $AppDirectory "optional-update-data.zip") })
    $directories = @(Get-ChildItem -LiteralPath $AppDirectory -Directory -Recurse -Force)
    $selectedBackgrounds = @()
    $selectedDataArchive = ""
    $backgroundExtensions = @('.png','.jpg','.jpeg','.bmp','.webp','.tif','.tiff','.gif','.mp4','.m4v','.mov','.avi','.mkv','.webm','.wmv','.mpg','.mpeg')
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    if ($env:TLM_RELEASE_SELECTION) {
        if (-not (Test-Path -LiteralPath $env:TLM_RELEASE_SELECTION -PathType Leaf)) { throw "Release content selection file is missing." }
        $selection = Get-Content -LiteralPath $env:TLM_RELEASE_SELECTION -Raw -Encoding UTF8 | ConvertFrom-Json
        $selectedDataArchive = [string]$selection.data_archive
        if ($selectedDataArchive -and -not (Test-Path -LiteralPath $selectedDataArchive -PathType Leaf)) { throw "Selected data archive is missing." }
        foreach ($selectedPath in @($selection.backgrounds)) {
            $selectedFile = Get-Item -LiteralPath ([string]$selectedPath) -ErrorAction Stop
            if ($selectedFile.PSIsContainer -or $selectedFile.Extension -notin $backgroundExtensions) { throw "Invalid selected background: $selectedPath" }
            if (-not $names.Add($selectedFile.Name)) { throw "Selected backgrounds must have distinct filenames." }
            $selectedBackgrounds += $selectedFile
        }
    }
    # Starter backgrounds ship with full installs only; updates never add or replace them.
    $installBackgroundDirectory = Join-Path $scriptDirectory "install_backgrounds"
    if ($PackageType -eq "Installation" -and (Test-Path -LiteralPath $installBackgroundDirectory -PathType Container)) {
        foreach ($bundledFile in @(Get-ChildItem -LiteralPath $installBackgroundDirectory -File | Sort-Object Name)) {
            if ($bundledFile.Extension -notin $backgroundExtensions) { continue }
            if ($names.Add($bundledFile.Name)) { $selectedBackgrounds += $bundledFile }
        }
    }
    $basePrefixLength = $AppDirectory.TrimEnd('\').Length + 1
    $files = @($files | Where-Object { $_.FullName.Substring($basePrefixLength) -notmatch '^(Backgrounds|Data\\Backgrounds)\\' })
    $directories = @($directories | Where-Object { $_.FullName.Substring($basePrefixLength) -notmatch '^(Backgrounds|Data\\Backgrounds)(\\|$)' })
    if ($PackageType -eq "Update") {
        $previousBiblePaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $hasPreviousInstall = [bool]$PreviousInstallArchive
        if ($PreviousInstallArchive) {
            if (-not (Test-Path -LiteralPath $PreviousInstallArchive -PathType Leaf)) { throw "Previous Installation ZIP was not found: $PreviousInstallArchive" }
            $previousArchive = [System.IO.Compression.ZipFile]::OpenRead([System.IO.Path]::GetFullPath($PreviousInstallArchive))
            try {
                $appPrefixes = @("PewVista/", "The Light Ministries/")
                foreach ($entry in $previousArchive.Entries) {
                    $appPrefix = $appPrefixes | Where-Object { $entry.FullName.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1
                    if ($appPrefix) {
                        $relative = $entry.FullName.Substring($appPrefix.Length).Replace('/', '\')
                        if ($relative -match '^Bibles\\' -and -not $relative.EndsWith('\')) { [void]$previousBiblePaths.Add($relative) }
                    }
                }
            }
            finally { $previousArchive.Dispose() }
        }
        $files = @($files | Where-Object {
            $relative = $_.FullName.Substring($basePrefixLength)
            ($relative -notmatch '^Bibles\\' -or ($hasPreviousInstall -and -not $previousBiblePaths.Contains($relative)))
        })
        $directories = @($directories | Where-Object { $_.FullName.Substring($basePrefixLength) -notmatch '^Bibles(\\|$)' })
        $newBibleCount = @($files | Where-Object { $_.FullName.Substring($basePrefixLength) -match '^Bibles\\' }).Count
        Write-Host "[ZIP] Update Bible delta: $newBibleCount new Bible file(s) since the previous Installation ZIP."
    }
    if (-not $IncludeUserData) {
        $protectedPaths = '^(Data|config|\.update-backups|\.test-installation|installation\.json)(\\|$)|^Resourses\\obs_portable\\config(\\|$)'
        $basePrefixLength = $AppDirectory.TrimEnd('\').Length + 1
        $files = @($files | Where-Object {
            $relative = $_.FullName.Substring($basePrefixLength)
            $referenceData = $PackageType -eq "Installation" -and $relative -match '^Data\\(biblical_lexicon|translation_lexicon|translation_word_cache|song_packs)\\'
            $appearanceSettings = $PackageType -eq "Installation" -and $relative -ieq 'Data\ui_settings.json'
            ($relative -notmatch $protectedPaths -or $referenceData -or $appearanceSettings) -and $_.Name -notmatch '\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$'
        })
        $directories = @($directories | Where-Object { $_.FullName.Substring($basePrefixLength) -notmatch $protectedPaths })
        Write-Host "[ZIP] $PackageType package: personal databases/settings are excluded; installation packages include reference dictionaries only."
    }
    else {
        Write-Host "[ZIP] WARNING: full-data package; do not extract over an existing installation's data."
    }
    $totalBytes = [long](($files | Measure-Object -Property Length -Sum).Sum)
    $copiedBytes = [long]0
    $completedFiles = 0
    $baseDirectory = $AppDirectory.TrimEnd('\')
    # Update ZIPs keep the legacy top-level folder so apps installed before the
    # PewVista rename still recognize and accept them. Installation ZIPs use the
    # product name; only the current setup tooling consumes those.
    $baseName = if ($PackageType -eq "Update") { "The Light Ministries" } else { "PewVista" }
    $buffer = New-Object byte[] (1024 * 1024)
    $lastReport = [long]-1000
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $archiveStream = [System.IO.File]::Open($temporaryArchive, [System.IO.FileMode]::CreateNew)
    $archive = New-Object System.IO.Compression.ZipArchive($archiveStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    if (-not $ReleaseVersion) { $ReleaseVersion = Get-Date -Format "yyyyMMdd-HHmmss" }
    $manifest = $archive.CreateEntry("$baseName/release.json")
    $writer = New-Object System.IO.StreamWriter($manifest.Open())
    try {
        $manifestData = @{ application = "The Light Ministries"; product_name = "PewVista"; package_type = $PackageType.ToLowerInvariant(); version = $ReleaseVersion }
        if ($selectedDataArchive) { $manifestData.optional_data_sha256 = (Get-FileHash -LiteralPath $selectedDataArchive -Algorithm SHA256).Hash.ToLowerInvariant() }
        $writer.Write(($manifestData | ConvertTo-Json))
    }
    finally { $writer.Dispose() }
    Write-Host ("[ZIP] 0% - 0/{0} files - {1:N1} MB to compress" -f $files.Count, ($totalBytes / 1MB))

    foreach ($directory in $directories) {
        if ([System.IO.Directory]::GetFileSystemEntries($directory.FullName).Length -eq 0) {
            $relativeDirectory = $directory.FullName.Substring($baseDirectory.Length + 1).Replace('\', '/')
            $archive.CreateEntry("$baseName/$relativeDirectory/") | Out-Null
        }
    }
    foreach ($file in $files) {
        $currentSource = $file.FullName
        $relativePath = $file.FullName.Substring($baseDirectory.Length + 1).Replace('\', '/')
        $entry = $archive.CreateEntry("$baseName/$relativePath", [System.IO.Compression.CompressionLevel]::Optimal)
        if ($file.LastWriteTime.Year -ge 1980 -and $file.LastWriteTime.Year -le 2107) {
            $entry.LastWriteTime = [DateTimeOffset]$file.LastWriteTime
        }
        else {
            $entry.LastWriteTime = [DateTimeOffset][datetime]'1980-01-01'
        }
        $inputStream = $null
        $outputStream = $null
        try {
            $inputStream = [System.IO.File]::OpenRead($file.FullName)
            $outputStream = $entry.Open()
            while (($bytesRead = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outputStream.Write($buffer, 0, $bytesRead)
                $copiedBytes += $bytesRead
                if ($timer.ElapsedMilliseconds - $lastReport -ge 1000) {
                    $percent = [int][Math]::Min(99.0, [Math]::Floor(100.0 * $copiedBytes / [Math]::Max(1.0, [double]$totalBytes)))
                    $status = "{0}% - {1}/{2} files - {3:N1}/{4:N1} MB - {5}" -f $percent, $completedFiles, $files.Count, ($copiedBytes / 1MB), ($totalBytes / 1MB), $relativePath
                    Write-Progress -Activity "Creating updated Windows ZIP" -Status $status -PercentComplete $percent
                    Write-Host "[ZIP] $status"
                    $lastReport = $timer.ElapsedMilliseconds
                }
            }
        }
        finally {
            if ($outputStream) { $outputStream.Dispose() }
            if ($inputStream) { $inputStream.Dispose() }
        }
        $completedFiles++
    }
    if ($PackageType -eq "Update") {
        $newExecutable = Join-Path $AppDirectory "PewVista.exe"
        $legacyExecutableName = "The Light Ministries.exe"
        if ((Test-Path -LiteralPath $newExecutable -PathType Leaf) -and -not (Test-Path -LiteralPath (Join-Path $AppDirectory $legacyExecutableName) -PathType Leaf)) {
            # Apps installed before the PewVista rename refuse updates that lack their
            # original executable name; the updater removes this copy during migration.
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $newExecutable, "$baseName/$legacyExecutableName", [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
    foreach ($background in $selectedBackgrounds) {
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $background.FullName, "$baseName/Backgrounds/$($background.Name)", [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
    if ($selectedBackgrounds.Count -eq 0) { $archive.CreateEntry("$baseName/Backgrounds/") | Out-Null }
    if ($selectedDataArchive) {
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $selectedDataArchive, "$baseName/optional-update-data.zip", [System.IO.Compression.CompressionLevel]::NoCompression) | Out-Null
    }
    Write-Host "[ZIP] Finalizing archive..."
    $currentSource = $temporaryArchive
    $archive.Dispose()
    $archive = $null
    $archiveStream.Dispose()
    $archiveStream = $null

    if ($PackageType -eq "Update") {
        $signingPython = Join-Path $scriptDirectory ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $signingPython -PathType Leaf)) { throw "Update signing needs the workspace Python environment at $signingPython." }
        Write-Host "[ZIP] Signing update package..."
        & $signingPython (Join-Path $scriptDirectory "update_signing.py") sign $temporaryArchive
        if ($LASTEXITCODE -ne 0) { throw "The update package could not be signed. No package was published." }
    }

    if ([System.IO.File]::Exists($ArchivePath)) {
        [System.IO.File]::Replace($temporaryArchive, $ArchivePath, [System.Management.Automation.Language.NullString]::Value)
    }
    else {
        [System.IO.File]::Move($temporaryArchive, $ArchivePath)
    }
    Write-Host ("[ZIP] 100% - {0}/{0} files - completed in {1:N1} seconds" -f $completedFiles, $timer.Elapsed.TotalSeconds)
    Write-Host "Updated Windows package created: $ArchivePath"
}
catch {
    Write-Host "[ERROR] Could not create the updated Windows ZIP: $($_.Exception.Message)"
    Write-Host "[ERROR] Processing: $currentSource"
    $errorLog = $ArchivePath + ".error.log"
    try {
        [System.IO.File]::WriteAllText($errorLog, "Processing: $currentSource`r`n" + $_.Exception.ToString())
        Write-Host "[ERROR] Details saved to: $errorLog"
    }
    catch {
        Write-Host "[ERROR] Could not save the error log: $($_.Exception.Message)"
    }
    exit 1
}
finally {
    Write-Progress -Activity "Creating updated Windows ZIP" -Completed
    if ($archive) { $archive.Dispose() }
    if ($archiveStream) { $archiveStream.Dispose() }
    if ($temporaryArchive -and [System.IO.File]::Exists($temporaryArchive)) {
        [System.IO.File]::Delete($temporaryArchive)
    }
}