param(
    [Parameter(Mandatory = $true)][string]$PayloadDirectory,
    [Parameter(Mandatory = $true)][string]$InstallDirectory,
    [int]$WaitForProcessId = 0,
    [string]$CleanupDirectory = "",
    [switch]$NoAdmin,
    [switch]$NoLaunch,
    [switch]$NoPause,
    [switch]$Gui,
    [string]$ProgressFile = "",
    [string]$UpdateArchive = "",
    [string]$SecureStagingDirectory = ""
)

$ErrorActionPreference = "Stop"
$changedFiles = New-Object System.Collections.Generic.List[object]
$backupDirectory = $null
$logPath = Join-Path $InstallDirectory "update-install.log"
$secureStagingPattern = '^PewVista-Update-[0-9a-f]{32}$'
function Remove-SecureStaging([string]$Directory) {
    <#
     What: Delete an administrator-only update staging folder that this script created.
     Why included: Staging holds a verified copy of the update and must not be left behind.
     Where used: Success and failure paths of the elevated secure update check.
     Returns: Nothing; folders not matching the staging name pattern are never touched.
    #>
    if (-not $Directory) { return }
    $resolved = [System.IO.Path]::GetFullPath($Directory).TrimEnd('\')
    $programData = [System.IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\')
    # Only delete folders this script created, so the parameter cannot target other paths.
    if ([System.IO.Path]::GetDirectoryName($resolved) -eq $programData -and
        [System.IO.Path]::GetFileName($resolved) -match $secureStagingPattern -and
        [System.IO.Directory]::Exists($resolved)) {
        try { [System.IO.Directory]::Delete($resolved, $true) } catch { Write-Host "Secure update staging could not be removed: $resolved" }
    }
}
if ($Gui) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class UpdateConsoleWindow {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
}
'@
    $consoleWindow = [UpdateConsoleWindow]::GetConsoleWindow()
    if ($consoleWindow -ne [IntPtr]::Zero) { [void][UpdateConsoleWindow]::ShowWindow($consoleWindow, 0) }
}
function Set-UpdateStatus([string]$Message, [int]$Percent = -1, [bool]$Succeeded = $false) {
    <#
     What: Write the current update message and percentage to the progress file.
     Why included: The visible update window reads this file to show progress from the hidden worker.
     Where used: Every stage of the update worker.
     Returns: Nothing; write failures are ignored so progress never stops an update.
    #>
    if (-not $ProgressFile) { return }
    try {
        $status = @{ message = $Message; percent = $Percent; succeeded = $Succeeded } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($ProgressFile, $status, [System.Text.Encoding]::UTF8)
    }
    catch { }
}
function Close-ProcessesUsingFolder([string[]]$Folders, [string[]]$PathPatterns = @()) {
    <#
     What: Close every process that runs from, or has a program file loaded from, the given folders.
     Why included: Any process holding a program file open (OBS, the grammar runtime, another
     account's PewVista) makes Windows refuse to replace it, and the update then rolls back.
     Where used: Before program files are copied, and again when a copy finds a locked file.
     Returns: Names of the processes that were closed; throws when one cannot be closed.
    #>
    $roots = @($Folders | Where-Object { $_ } | ForEach-Object { [System.IO.Path]::GetFullPath($_).TrimEnd('\') + '\' })
    $closed = New-Object System.Collections.Generic.List[string]
    foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
        if ($process.Id -in @(0, 4, $PID)) { continue }
        $uses = $false
        $processPath = $null
        try { $processPath = $process.Path } catch { }
        if ($processPath) {
            foreach ($root in $roots) { if ($processPath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $uses = $true } }
            foreach ($pattern in $PathPatterns) { if ($processPath -match $pattern) { $uses = $true } }
        }
        if (-not $uses) {
            try {
                foreach ($module in $process.Modules) {
                    foreach ($root in $roots) { if ($module.FileName -and $module.FileName.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { $uses = $true } }
                    if ($uses) { break }
                }
            }
            catch { }
        }
        if (-not $uses -or $process.HasExited) { continue }
        Write-Host "Closing $($process.Name) (process $($process.Id)), which is using program files..."
        try {
            if ($process.CloseMainWindow()) { [void]$process.WaitForExit(15000) }
            if (-not $process.HasExited) {
                & taskkill.exe /PID $process.Id /T /F | Out-Null
                [void]$process.WaitForExit(5000)
            }
        }
        catch { }
        if (-not $process.HasExited) { throw "Unable to close $($process.Name) (process $($process.Id)), which is using program files. No program files have been changed." }
        $closed.Add($process.Name)
    }
    return $closed
}
function Test-FileLockError($ErrorRecord) {
    <#
     What: Recognize Windows sharing and lock violations.
     Why included: Only locked files are worth retrying after closing the process holding them.
     Where used: The program-file copy loop.
     Returns: True for "being used by another process" errors.
    #>
    $exception = $ErrorRecord.Exception
    while ($exception) {
        if ($exception.HResult -in @(-2147024864, -2147024863)) { return $true }
        $exception = $exception.InnerException
    }
    return $false
}
try {
    $PayloadDirectory = [System.IO.Path]::GetFullPath($PayloadDirectory).TrimEnd('\')
    $InstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
    if (-not $NoAdmin) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                "-PayloadDirectory", "`"$PayloadDirectory`"", "-InstallDirectory", "`"$InstallDirectory`"")
            if ($WaitForProcessId -gt 0) { $arguments += @("-WaitForProcessId", [string]$WaitForProcessId) }
            if ($CleanupDirectory) { $arguments += @("-CleanupDirectory", "`"$CleanupDirectory`"") }
            if ($UpdateArchive) { $arguments += @("-UpdateArchive", "`"$UpdateArchive`"") }
            if ($NoLaunch) { $arguments += "-NoLaunch" }
            if ($NoPause) { $arguments += "-NoPause" }
            if ($Gui) { $arguments += "-Gui" }
            try {
                $elevated = Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -Verb RunAs -ArgumentList $arguments -Wait -PassThru
            }
            catch {
                if ($Gui) {
                    Add-Type -AssemblyName System.Windows.Forms
                    [System.Windows.Forms.MessageBox]::Show("The update could not obtain administrator permission. No files were changed.`r`n`r`n$($_.Exception.Message)", "Update Not Started", 'OK', 'Error') | Out-Null
                    exit 1
                }
                throw
            }
            exit $elevated.ExitCode
        }
    }
    if ($UpdateArchive -and -not $SecureStagingDirectory) {
        # The payload in the user's temp folder could be swapped by that user after the app
        # checked it. Re-check the original ZIP here, elevated, inside an administrator-only
        # folder, using the installed (administrator-protected) PewVista.exe.
        try {
            $programDataRoot = [System.IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\')
            Get-ChildItem -LiteralPath $programDataRoot -Directory -Filter "PewVista-Update-*" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match $secureStagingPattern -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
                ForEach-Object { Remove-SecureStaging $_.FullName }
            $SecureStagingDirectory = Join-Path $programDataRoot ("PewVista-Update-" + [guid]::NewGuid().ToString("N"))
            [System.IO.Directory]::CreateDirectory($SecureStagingDirectory) | Out-Null
            & icacls.exe $SecureStagingDirectory /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "The secure update folder could not be protected." }
            $stagedArchive = Join-Path $SecureStagingDirectory "update.zip"
            [System.IO.File]::Copy($UpdateArchive, $stagedArchive, $true)
            $verifier = Join-Path $InstallDirectory "PewVista.exe"
            if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) { throw "The installed PewVista.exe needed to check the update was not found." }
            $preparedDirectory = Join-Path $SecureStagingDirectory "prepared"
            $check = Start-Process -FilePath $verifier -ArgumentList @("--prepare-update", "`"$stagedArchive`"", "`"$preparedDirectory`"") -Wait -PassThru
            if ($check.ExitCode -ne 0) {
                $reasonPath = $preparedDirectory + ".error.txt"
                $reason = if (Test-Path -LiteralPath $reasonPath) { (Get-Content -LiteralPath $reasonPath -Raw).Trim() } else { "The update could not be verified." }
                throw $reason
            }
            if ($CleanupDirectory) {
                $userCleanup = [System.IO.Path]::GetFullPath($CleanupDirectory).TrimEnd('\')
                if ([System.IO.Path]::GetDirectoryName($PayloadDirectory) -eq $userCleanup -and
                    [System.IO.Path]::GetFileName($userCleanup).StartsWith("TheLightUpdate-")) {
                    try { [System.IO.Directory]::Delete($userCleanup, $true) } catch { }
                }
                $CleanupDirectory = ""
            }
            $PayloadDirectory = Join-Path $preparedDirectory "payload"
        }
        catch {
            Remove-SecureStaging $SecureStagingDirectory
            if ($Gui) {
                Add-Type -AssemblyName System.Windows.Forms
                [System.Windows.Forms.MessageBox]::Show("The update was not installed because it failed its security check. No files were changed.`r`n`r`n$($_.Exception.Message)", "Update Not Installed", 'OK', 'Error') | Out-Null
            }
            else { Write-Host "[ERROR] Update security check failed: $($_.Exception.Message)" }
            exit 1
        }
    }
    if ($Gui) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        [System.Windows.Forms.Application]::EnableVisualStyles()
        $statusDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("LightUpdateStatus-" + [guid]::NewGuid().ToString("N"))
        [System.IO.Directory]::CreateDirectory($statusDirectory) | Out-Null
        $statusPath = Join-Path $statusDirectory "status.json"
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "PewVista | Update"
        $form.ClientSize = New-Object System.Drawing.Size(620, 280)
        $form.MinimumSize = New-Object System.Drawing.Size(560, 300)
        $form.StartPosition = "CenterScreen"
        $form.BackColor = [System.Drawing.Color]::FromArgb(245, 247, 250)
        $form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
        $header = New-Object System.Windows.Forms.Panel
        $header.Dock = "Top"
        $header.Height = 88
        $header.BackColor = [System.Drawing.Color]::FromArgb(29, 65, 78)
        $title = New-Object System.Windows.Forms.Label
        $title.Text = "PewVista"
        $title.ForeColor = [System.Drawing.Color]::White
        $title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
        $title.AutoSize = $true
        $title.Location = New-Object System.Drawing.Point(24, 14)
        $subtitle = New-Object System.Windows.Forms.Label
        $subtitle.Text = "Installing update"
        $subtitle.ForeColor = [System.Drawing.Color]::FromArgb(210, 230, 229)
        $subtitle.AutoSize = $true
        $subtitle.Location = New-Object System.Drawing.Point(26, 52)
        $header.Controls.AddRange(@($title, $subtitle))
        $statusLabel = New-Object System.Windows.Forms.Label
        $statusLabel.Text = "Preparing update..."
        $statusLabel.Location = New-Object System.Drawing.Point(26, 110)
        $statusLabel.Size = New-Object System.Drawing.Size(565, 50)
        $statusLabel.Anchor = 'Top, Left, Right'
        $progressBar = New-Object System.Windows.Forms.ProgressBar
        $progressBar.Location = New-Object System.Drawing.Point(26, 165)
        $progressBar.Size = New-Object System.Drawing.Size(565, 18)
        $progressBar.Anchor = 'Top, Left, Right'
        $progressBar.Style = 'Marquee'
        $detailLabel = New-Object System.Windows.Forms.Label
        $detailLabel.Text = "Your existing data is backed up before program files are updated."
        $detailLabel.ForeColor = [System.Drawing.Color]::FromArgb(78, 93, 99)
        $detailLabel.Location = New-Object System.Drawing.Point(26, 195)
        $detailLabel.Size = New-Object System.Drawing.Size(565, 40)
        $detailLabel.Anchor = 'Top, Left, Right'
        $closeButton = New-Object System.Windows.Forms.Button
        $closeButton.Text = "Close"
        $closeButton.Size = New-Object System.Drawing.Size(100, 34)
        $closeButton.Location = New-Object System.Drawing.Point(491, 237)
        $closeButton.Anchor = 'Bottom, Right'
        $closeButton.Enabled = $false
        $closeButton.Add_Click({ $form.Close() })
        $form.Controls.AddRange(@($header, $statusLabel, $progressBar, $detailLabel, $closeButton))
        $workerArguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-PayloadDirectory', "`"$PayloadDirectory`"", '-InstallDirectory', "`"$InstallDirectory`"", '-NoAdmin', '-NoPause', '-ProgressFile', "`"$statusPath`"")
        if ($WaitForProcessId -gt 0) { $workerArguments += @('-WaitForProcessId', [string]$WaitForProcessId) }
        if ($CleanupDirectory) { $workerArguments += @('-CleanupDirectory', "`"$CleanupDirectory`"") }
        if ($SecureStagingDirectory) { $workerArguments += @('-SecureStagingDirectory', "`"$SecureStagingDirectory`"") }
        if ($NoLaunch) { $workerArguments += '-NoLaunch' }
        $worker = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $workerArguments -RedirectStandardOutput (Join-Path $statusDirectory 'output.log') -RedirectStandardError (Join-Path $statusDirectory 'error.log') -WindowStyle Hidden -PassThru
        $timer = New-Object System.Windows.Forms.Timer
        $workerResult = @{ ExitCode = $null }
        $timer.Interval = 200
        $timer.Add_Tick({
            try {
                if ([System.IO.File]::Exists($statusPath)) {
                    $current = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $statusLabel.Text = [string]$current.message
                    if ([int]$current.percent -ge 0) {
                        $progressBar.Style = 'Continuous'
                        $progressBar.Value = [Math]::Max(0, [Math]::Min(100, [int]$current.percent))
                    }
                }
            } catch { }
            if ($worker.HasExited) {
                $timer.Stop()
                $worker.WaitForExit()
                $worker.Refresh()
                $workerResult["ExitCode"] = [int]$worker.ExitCode
                try {
                    if ([System.IO.File]::Exists($statusPath)) {
                        $finalStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                        if (-not [bool]$finalStatus.succeeded) { $workerResult["ExitCode"] = 1 }
                    } else { $workerResult["ExitCode"] = 1 }
                } catch { $workerResult["ExitCode"] = 1 }
                if ($workerResult.ExitCode -eq 0) {
                    $statusLabel.Text = 'Update complete.'
                    $detailLabel.Text = 'Your existing data was preserved. The application can now be used.'
                    $progressBar.Style = 'Continuous'
                    $progressBar.Value = 100
                    $closeButton.Enabled = $true
                    $form.Close()
                } else {
                    $statusLabel.Text = 'The update could not be completed.'
                    $detailLabel.Text = "Program changes were rolled back where possible. Details: $logPath"
                    $closeButton.Enabled = $true
                    if ($NoPause) { $form.Close() }
                }
            }
        }.GetNewClosure())
        $form.Add_FormClosing({ if (-not $worker.HasExited) { $_.Cancel = $true } }.GetNewClosure())
        $timer.Start()
        [void]$form.ShowDialog()
        $timer.Dispose()
        $exitCode = $workerResult.ExitCode
        if ($NoPause) { Write-Output "Graphical update worker exit code: $exitCode" }
        $form.Dispose()
        try { [System.IO.Directory]::Delete($statusDirectory, $true) } catch { }
        exit $exitCode
    }
    Set-UpdateStatus 'Checking the installation and waiting for the app to close...'
    if ($PayloadDirectory.Equals($InstallDirectory, [StringComparison]::OrdinalIgnoreCase) -or
        $PayloadDirectory.StartsWith($InstallDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "The update payload must be staged outside the installation."
    }
    $payloadExecutableName = if (Test-Path -LiteralPath (Join-Path $PayloadDirectory "PewVista.exe") -PathType Leaf) { "PewVista.exe" } else { "The Light Ministries.exe" }
    $payloadExecutable = Join-Path $PayloadDirectory $payloadExecutableName
    $legacyExecutable = Join-Path $InstallDirectory "The Light Ministries.exe"
    $currentExecutable = Join-Path $InstallDirectory "PewVista.exe"
    $executable = if (Test-Path -LiteralPath $currentExecutable -PathType Leaf) { $currentExecutable } else { $legacyExecutable }
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or
        -not (Test-Path -LiteralPath $payloadExecutable -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $PayloadDirectory "_internal") -PathType Container)) {
        throw "The installed app or prepared update is incomplete."
    }
    if ($WaitForProcessId -gt 0) {
        $parent = Get-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue
        if ($parent -and -not $parent.WaitForExit(120000)) {
            throw "The app did not close. No program files have been changed."
        }
    }
    $data = Join-Path $InstallDirectory "Data"
    $installationConfig = Join-Path $InstallDirectory "installation.json"
    if (Test-Path -LiteralPath $installationConfig -PathType Leaf) {
        $settings = Get-Content -LiteralPath $installationConfig -Raw | ConvertFrom-Json
        $configuredData = [string]$settings.data_directory
        $programDataRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)).TrimEnd('\')
        if (-not [System.IO.Path]::IsPathRooted($configuredData)) { throw "Installed shared-data path is invalid." }
        $configuredData = [System.IO.Path]::GetFullPath($configuredData).TrimEnd('\')
        if (-not $NoAdmin -and -not $configuredData.StartsWith($programDataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Installed shared-data path is outside ProgramData; no update was applied." }
        $data = $configuredData
    }
    Set-UpdateStatus "Closing programs that are using PewVista's files..."
    $obsRoots = @((Join-Path $InstallDirectory "Resourses\obs_portable"), (Join-Path $data "obs_portable"))
    # Each Windows account runs its own copy under %LOCALAPPDATA%\PewVista\<data folder name>\obs_portable.
    $privateObsPattern = '\\AppData\\Local\\PewVista\\' + [regex]::Escape((Split-Path -Leaf $data)) + '\\obs_portable\\'
    $closedPrograms = Close-ProcessesUsingFolder -Folders (@($InstallDirectory) + $obsRoots) -PathPatterns @($privateObsPattern)
    if ($closedPrograms.Count -gt 0) { Write-Host ("Closed: " + (($closedPrograms | Select-Object -Unique) -join ", ")) }
    $privateObsSentinel = Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA "PewVista") (Split-Path -Leaf $data)) "obs_portable\config\obs-studio\.sentinel"
    foreach ($sentinelDir in @((Join-Path $InstallDirectory "Resourses\obs_portable\config\obs-studio\.sentinel"), (Join-Path $data "obs_portable\config\obs-studio\.sentinel"), $privateObsSentinel)) {
        if ($sentinelDir -and (Test-Path -LiteralPath $sentinelDir)) {
            Remove-Item -LiteralPath $sentinelDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $backupName = (Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-" + [guid]::NewGuid().ToString("N")
    $backupDirectory = Join-Path (Join-Path $InstallDirectory ".update-backups") $backupName
    [System.IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
    if (Test-Path -LiteralPath $data -PathType Container) {
        Set-UpdateStatus 'Backing up your data and settings...'
        Write-Host "Backing up current user data..."
        $sharedObsRuntime = Join-Path $data "obs_portable"
        Write-Host "The shared OBS runtime and profile remain untouched and are not included in this backup."
        & robocopy.exe $data (Join-Path $backupDirectory "Data") /E /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1 /XD $sharedObsRuntime
        if ($LASTEXITCODE -ge 8) { throw "User-data backup failed; installation stopped before replacing any files." }
    }
    $files = @(Get-ChildItem -LiteralPath $PayloadDirectory -File -Recurse -Force)
    Set-UpdateStatus 'Installing program files...' 0
    $completed = 0
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($PayloadDirectory.Length + 1)
        if ($relative -match '^(Data|config|\.update-backups|\.test-installation|installation\.json)(\\|$)|^Resourses\\obs_portable\\config(\\|$)' -or
            $file.Name -match '\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$') {
            continue
        }
        $target = if ($relative -match '^Backgrounds\\') { Join-Path $data $relative } else { Join-Path $InstallDirectory $relative }
        if ($relative -match '^Backgrounds\\' -and [System.IO.File]::Exists($target)) {
            $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $sourceHash) { continue }
            $target = Join-Path ([System.IO.Path]::GetDirectoryName($target)) ([System.IO.Path]::GetFileNameWithoutExtension($target) + '_' + $sourceHash.Substring(0, 12).ToLowerInvariant() + [System.IO.Path]::GetExtension($target))
            if ([System.IO.File]::Exists($target)) {
                if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $sourceHash) { continue }
                throw "Selected background conflicts with an existing file: $target"
            }
        }
        if ($relative -match '^(Bibles|Backgrounds)\\' -and [System.IO.File]::Exists($target)) {
            continue
        }
        $backup = Join-Path (Join-Path $backupDirectory "program") $relative
        $existed = [System.IO.File]::Exists($target)
        if ($existed) {
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($backup)) | Out-Null
            [System.IO.File]::Copy($target, $backup)
        }
        $changedFiles.Add([pscustomobject]@{ Target = $target; Backup = $backup; Existed = $existed })
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
        $copyAttempt = 0
        while ($true) {
            try {
                [System.IO.File]::Copy($file.FullName, $target, $true)
                break
            }
            catch {
                $copyAttempt++
                if ($copyAttempt -ge 4 -or -not (Test-FileLockError $_)) { throw }
                Write-Host "$relative is in use; closing the program holding it and retrying..."
                [void](Close-ProcessesUsingFolder -Folders (@($InstallDirectory) + $obsRoots) -PathPatterns @($privateObsPattern))
                Start-Sleep -Seconds 2
            }
        }
        $completed++
        $percent = [int][Math]::Floor(100.0 * $completed / [Math]::Max(1.0, [double]$files.Count))
        if ($completed -eq 1 -or $completed % 10 -eq 0 -or $completed -eq $files.Count) { Set-UpdateStatus "Installing program files ($completed of $($files.Count))..." $percent }
        Write-Progress -Activity "Installing PewVista update" -Status $relative -PercentComplete $percent
        if ($completed -eq 1 -or $completed % 100 -eq 0) { Write-Host "Installed $completed/$($files.Count) files..." }
    }
    $executable = Join-Path $InstallDirectory $payloadExecutableName
    if ($payloadExecutableName -eq "PewVista.exe" -and [System.IO.File]::Exists($legacyExecutable)) {
        $legacyBackup = Join-Path (Join-Path $backupDirectory "program") "The Light Ministries.exe"
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($legacyBackup)) | Out-Null
        [System.IO.File]::Copy($legacyExecutable, $legacyBackup, $true)
        $changedFiles.Add([pscustomobject]@{ Target = $legacyExecutable; Backup = $legacyBackup; Existed = $true })
        [System.IO.File]::Delete($legacyExecutable)
        try {
            $iconSource = Join-Path $InstallDirectory "Resourses\PewVista.ico"
            $installationConfig = Join-Path $InstallDirectory "installation.json"
            if (Test-Path -LiteralPath $installationConfig -PathType Leaf) {
                try {
                    $installationSettings = Get-Content -LiteralPath $installationConfig -Raw | ConvertFrom-Json
                    if ($installationSettings.custom_icon_path -and (Test-Path -LiteralPath ([string]$installationSettings.custom_icon_path) -PathType Leaf)) {
                        $iconSource = [string]$installationSettings.custom_icon_path
                    }
                }
                catch { }
            }
            $shell = New-Object -ComObject WScript.Shell
            $shortcutFolders = @([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory),
                [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonPrograms),
                [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop),
                [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)) | Select-Object -Unique
            foreach ($folder in $shortcutFolders) {
                if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
                foreach ($link in @(Get-ChildItem -LiteralPath $folder -Filter "*.lnk" -File -ErrorAction SilentlyContinue)) {
                    try {
                        $shortcut = $shell.CreateShortcut($link.FullName)
                        if (-not $shortcut.TargetPath) { continue }
                        if (-not [System.IO.Path]::GetFullPath($shortcut.TargetPath).Equals($legacyExecutable, [StringComparison]::OrdinalIgnoreCase)) { continue }
                        $shortcut.TargetPath = $executable
                        $shortcut.WorkingDirectory = $InstallDirectory
                        if (Test-Path -LiteralPath $iconSource -PathType Leaf) { $shortcut.IconLocation = "$iconSource,0" }
                        $shortcut.Save()
                        Write-Host "Updated shortcut to the renamed app: $($link.FullName)"
                    }
                    catch { Write-Host "Could not update shortcut: $($link.FullName)" }
                }
            }
        }
        catch { Write-Host "Shortcuts pointing to the old executable could not be updated automatically." }
    }
    Write-Progress -Activity "Installing PewVista update" -Completed
    [System.IO.File]::WriteAllText($logPath, "Update completed: $(Get-Date -Format o)`r`nBackup: $backupDirectory")
    try {
        # Keep only the two most recent update backups (this update's and the previous one).
        $backupRoot = Join-Path $InstallDirectory ".update-backups"
        @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^\d{8}-\d{6}-\d{3}-[0-9a-f]{32}$' } |
            Sort-Object Name -Descending | Select-Object -Skip 2) |
            ForEach-Object {
                try { [System.IO.Directory]::Delete($_.FullName, $true) }
                catch { Write-Host "An old update backup could not be removed: $($_.Exception.Message)" }
            }
    }
    catch { Write-Host "Old update backups could not be pruned: $($_.Exception.Message)" }
    Set-UpdateStatus 'Update completed.' 100 $true
    Write-Host "Update completed. User databases and settings were preserved."
    Write-Host "Backup: $backupDirectory"
    if ($CleanupDirectory) {
        $cleanup = [System.IO.Path]::GetFullPath($CleanupDirectory).TrimEnd('\')
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
        if ([System.IO.Path]::GetDirectoryName($PayloadDirectory) -eq $cleanup -and
            [System.IO.Path]::GetDirectoryName($cleanup) -eq $tempRoot -and
            [System.IO.Path]::GetFileName($cleanup).StartsWith("TheLightUpdate-")) {
            try { [System.IO.Directory]::Delete($cleanup, $true) }
            catch { Write-Host "Temporary update files could not be removed: $cleanup" }
        }
    }
    Remove-SecureStaging $SecureStagingDirectory
    if (-not $NoLaunch) { Start-Process -FilePath $executable -WorkingDirectory $InstallDirectory }
    exit 0
}
catch {
    $failure = $_.Exception.ToString()
    Set-UpdateStatus "Update failed: $($_.Exception.Message)"
    Write-Host "[ERROR] Update failed: $($_.Exception.Message)"
    for ($index = $changedFiles.Count - 1; $index -ge 0; $index--) {
        $change = $changedFiles[$index]
        try {
            if ($change.Existed) { [System.IO.File]::Copy($change.Backup, $change.Target, $true) }
            elseif ([System.IO.File]::Exists($change.Target)) { [System.IO.File]::Delete($change.Target) }
        }
        catch { $failure += "`r`nRollback issue for $($change.Target): $($_.Exception.Message)" }
    }
    try {
        [System.IO.File]::WriteAllText($logPath, "Backup: $backupDirectory`r`n$failure")
        Write-Host "Details: $logPath"
    }
    catch { Write-Host "Could not write the update log: $($_.Exception.Message)" }
    Write-Host "User Data was not replaced by the update."
    Remove-SecureStaging $SecureStagingDirectory
    if (-not $NoPause) {
        Write-Host "Press Enter to close this window."
        [Console]::ReadLine() | Out-Null
    }
    exit 1
}
