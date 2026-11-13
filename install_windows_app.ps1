param(
    [string]$SourceDirectory = "",
    [string]$DestinationDirectory = "",
    [string]$DataDirectory = "",
    [string]$ChurchName = "",
    [string]$DefaultBackgroundFile = "",
    [string]$CustomIconFile = "",
    [string]$LegacyDataDirectory = "",
    [switch]$Gui,
    [switch]$AcceptTerms,
    [switch]$NoAdmin,
    [switch]$NoShortcuts,
    [switch]$NoLaunch,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$createdProgram = $false
$createdData = $false
$Gui = $Gui -or (-not $NoAdmin -and -not ($ChurchName -or $DestinationDirectory -or $DataDirectory -or $DefaultBackgroundFile -or $CustomIconFile -or $LegacyDataDirectory))
$showWizard = [bool]$Gui
$progressWindow = $null
if (-not $SourceDirectory) { $SourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition }

function ConvertTo-SafeFolderName([string]$Value) {
    <#
     What: Turn the church name into a valid Windows folder name.
     Why included: The program and shared-data folders are named after the church.
     Where used: Setup wizard and command-line installation.
     Returns: The safe folder name; throws when nothing usable remains.
    #>
    $safe = [regex]::Replace($Value.Trim(), '[<>:"/\\|?*\x00-\x1F]', '_').TrimEnd('.', ' ')
    if (-not $safe -or $safe -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])($|\.)') { throw "The church name cannot be used as a Windows folder name." }
    if ($safe.Length -gt 80) { $safe = $safe.Substring(0, 80).TrimEnd('.', ' ') }
    return $safe
}

function Test-SharedAppDataFolder([string]$Directory) {
    <#
     What: Check whether a folder already holds PewVista databases or settings.
     Why included: Reinstalls must recognize and keep existing church data.
     Where used: Get-UninstalledInstallationState().
     Returns: True when PewVista data files are present.
    #>
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return $false }
    return [bool]@(Get-ChildItem -LiteralPath $Directory -File -Force | Where-Object {
        $_.Name -match '\.(db|sqlite|sqlite3)$' -or $_.Name -in @("ui_settings.json", "obs_secondary_settings.json")
    }).Count
}

function Get-UninstalledInstallationState([string]$Directory, [string]$ChurchName, [string]$ProgramDataRoot, [bool]$AllowExternalData, [string]$DataDirectoryOverride = "") {
    <#
     What: Decide whether an existing folder is safe to reuse after a keep-data uninstall.
     Why included: Setup must never overwrite a live installation or unrelated files.
     Where used: Setup wizard validation and command-line installation.
     Returns: The saved installation state, or nothing when the folder must not be reused.
    #>
    $markerPath = Join-Path $Directory ".light-ministries-uninstalled.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        if ((Test-Path -LiteralPath (Join-Path $Directory "PewVista.exe") -PathType Leaf) -or
            (Test-Path -LiteralPath (Join-Path $Directory "The Light Ministries.exe") -PathType Leaf) -or
            (Test-Path -LiteralPath (Join-Path $Directory "_internal") -PathType Container)) { return $null }
        $knownLeftovers = @("Bibles", "Backgrounds", "Data", "config", ".update-backups")
        $leftovers = @(Get-ChildItem -LiteralPath $Directory -Force -ErrorAction SilentlyContinue)
        if ($leftovers.Count -eq 0 -or @($leftovers | Where-Object { $_.Name -notin $knownLeftovers }).Count -gt 0) { return $null }
        foreach ($item in $leftovers) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "The old program folder contains linked paths and cannot be reused automatically." }
        }
        $candidateData = if ($DataDirectoryOverride) { $DataDirectoryOverride } else { Join-Path $ProgramDataRoot $ChurchName }
        $candidateData = [System.IO.Path]::GetFullPath($candidateData).TrimEnd('\')
        $legacyLocalData = Join-Path $Directory "Data"
        $hasLegacyLocalData = Test-SharedAppDataFolder $legacyLocalData
        if (-not (Test-SharedAppDataFolder $candidateData) -and -not $hasLegacyLocalData) { return $null }
        if (-not $AllowExternalData) {
            $programDataPath = [System.IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
            if (-not $candidateData.StartsWith($programDataPath + '\', [StringComparison]::OrdinalIgnoreCase)) { return $null }
        }
        $legacyDataToImport = if ($hasLegacyLocalData -and
            -not $legacyLocalData.Equals($candidateData, [StringComparison]::OrdinalIgnoreCase)) { $legacyLocalData } else { "" }
        return [pscustomobject]@{
            application = "The Light Ministries"
            state = "legacy-uninstalled"
            church_name = $ChurchName
            data_directory = $candidateData
            normalized_data_directory = $candidateData
            default_background_path = ""
            custom_icon_path = ""
            legacy_data_directory = $legacyDataToImport
        }
    }
    $markerItem = Get-Item -LiteralPath $markerPath -Force
    if (($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "The uninstall marker is linked and cannot authorize reinstalling this folder." }
    try { $state = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json }
    catch { throw "The uninstall marker is invalid. Choose an empty program folder or use Update for an installed app." }
    if ($state.application -ne "The Light Ministries" -or $state.state -ne "uninstalled" -or
        -not ([string]$state.church_name).Equals($ChurchName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "This existing folder is not a completed uninstall of '$ChurchName'. It will not be overwritten."
    }
    if ((Test-Path -LiteralPath (Join-Path $Directory "PewVista.exe") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $Directory "The Light Ministries.exe") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $Directory "_internal") -PathType Container)) {
        throw "Program files still exist in this folder. Use Update for an installed app; setup will not overwrite it."
    }
    $savedDataDirectory = [string]$state.data_directory
    if (-not [System.IO.Path]::IsPathRooted($savedDataDirectory)) { throw "The saved shared-data path in the uninstall marker is invalid." }
    $savedDataDirectory = [System.IO.Path]::GetFullPath($savedDataDirectory).TrimEnd('\')
    $programDataPath = [System.IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
    if (-not $AllowExternalData -and -not $savedDataDirectory.StartsWith($programDataPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "The saved shared-data path is outside ProgramData. The existing folder will not be reused."
    }
    $state | Add-Member -NotePropertyName normalized_data_directory -NotePropertyValue $savedDataDirectory -Force
    $state | Add-Member -NotePropertyName legacy_data_directory -NotePropertyValue "" -Force
    return $state
}

function Show-SetupWizard {
    <#
     What: Show the setup window for the church name, folders, background, icon, and terms.
     Why included: Most churches install through this window rather than the command line.
     Where used: Installation when started without command-line choices.
     Returns: The chosen settings, or nothing when setup is cancelled.
    #>
    param([string]$ProgramFilesRoot, [string]$ProgramDataRoot, [string]$SourceRoot)

    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "PewVista Setup"
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $form.ClientSize = New-Object System.Drawing.Size(640, 700)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::Sizable
    $form.MinimumSize = New-Object System.Drawing.Size(600, 480)
    $form.AutoScroll = $true
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.MaximizeBox = $true
    $form.MinimizeBox = $false
    $form.ShowInTaskbar = $true

    $iconPath = Join-Path $SourceRoot "Resourses\PewVista.ico"
    if (Test-Path -LiteralPath $iconPath) { $form.Icon = New-Object System.Drawing.Icon($iconPath) }

    $header = New-Object System.Windows.Forms.Panel
    $header.Location = New-Object System.Drawing.Point(0, 0)
    $header.Size = New-Object System.Drawing.Size(640, 86)
    $header.BackColor = [System.Drawing.Color]::White
    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Set up PewVista"
    $title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Regular)
    $title.Location = New-Object System.Drawing.Point(24, 16)
    $title.Size = New-Object System.Drawing.Size(590, 31)
    $subtitle = New-Object System.Windows.Forms.Label
    $subtitle.Text = "Choose the name and shared settings for this computer."
    $subtitle.Location = New-Object System.Drawing.Point(26, 51)
    $subtitle.Size = New-Object System.Drawing.Size(590, 20)
    [void]$header.Controls.Add($title)
    [void]$header.Controls.Add($subtitle)
    [void]$form.Controls.Add($header)

    $churchLabel = New-Object System.Windows.Forms.Label
    $churchLabel.Text = "Church name"
    $churchLabel.Location = New-Object System.Drawing.Point(26, 103)
    $churchLabel.Size = New-Object System.Drawing.Size(250, 20)
    $churchBox = New-Object System.Windows.Forms.TextBox
    $churchBox.Text = "Your Church"
    $churchBox.Location = New-Object System.Drawing.Point(26, 126)
    $churchBox.Size = New-Object System.Drawing.Size(584, 24)

    $programFilesRoot = [System.IO.Path]::GetFullPath($ProgramFilesRoot).TrimEnd('\')
    $programDataRoot = [System.IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
    $safeDefault = ConvertTo-SafeFolderName $churchBox.Text
    $defaultInstallPath = Join-Path $programFilesRoot $safeDefault
    $state = @{ AutoInstallPath = $defaultInstallPath }

    $installLabel = New-Object System.Windows.Forms.Label
    $installLabel.Text = "Program folder"
    $installLabel.Location = New-Object System.Drawing.Point(26, 166)
    $installLabel.Size = New-Object System.Drawing.Size(250, 20)
    $installBox = New-Object System.Windows.Forms.TextBox
    $installBox.Text = $defaultInstallPath
    $installBox.Location = New-Object System.Drawing.Point(26, 189)
    $installBox.Size = New-Object System.Drawing.Size(462, 24)
    $installBox.ReadOnly = $true
    $installBox.BackColor = [System.Drawing.SystemColors]::Control
    $installBrowse = New-Object System.Windows.Forms.Button
    $installBrowse.Text = "Browse..."
    $installBrowse.Location = New-Object System.Drawing.Point(500, 188)
    $installBrowse.Size = New-Object System.Drawing.Size(110, 27)

    $dataLabel = New-Object System.Windows.Forms.Label
    $dataLabel.Text = "Shared data folder"
    $dataLabel.Location = New-Object System.Drawing.Point(26, 229)
    $dataLabel.Size = New-Object System.Drawing.Size(250, 20)
    $dataBox = New-Object System.Windows.Forms.TextBox
    $dataBox.Text = Join-Path $programDataRoot $safeDefault
    $dataBox.Location = New-Object System.Drawing.Point(26, 252)
    $dataBox.Size = New-Object System.Drawing.Size(584, 24)
    $dataBox.ReadOnly = $true
    $dataBox.BackColor = [System.Drawing.SystemColors]::Control

    $backgroundLabel = New-Object System.Windows.Forms.Label
    $backgroundLabel.Text = "Optional default background (leave blank to skip)"
    $backgroundLabel.Location = New-Object System.Drawing.Point(26, 292)
    $backgroundLabel.Size = New-Object System.Drawing.Size(450, 20)
    $backgroundBox = New-Object System.Windows.Forms.TextBox
    $backgroundBox.Location = New-Object System.Drawing.Point(26, 315)
    $backgroundBox.Size = New-Object System.Drawing.Size(390, 24)
    $backgroundBrowse = New-Object System.Windows.Forms.Button
    $backgroundBrowse.Text = "Browse..."
    $backgroundBrowse.Location = New-Object System.Drawing.Point(428, 314)
    $backgroundBrowse.Size = New-Object System.Drawing.Size(90, 27)
    $backgroundClear = New-Object System.Windows.Forms.Button
    $backgroundClear.Text = "Clear"
    $backgroundClear.Location = New-Object System.Drawing.Point(526, 314)
    $backgroundClear.Size = New-Object System.Drawing.Size(84, 27)

    $iconLabel = New-Object System.Windows.Forms.Label
    $iconLabel.Text = "Optional app icon (.ico, leave blank for the PewVista icon)"
    $iconLabel.Location = New-Object System.Drawing.Point(26, 355)
    $iconLabel.Size = New-Object System.Drawing.Size(450, 20)
    $iconBox = New-Object System.Windows.Forms.TextBox
    $iconBox.Location = New-Object System.Drawing.Point(26, 378)
    $iconBox.Size = New-Object System.Drawing.Size(390, 24)
    $iconBrowse = New-Object System.Windows.Forms.Button
    $iconBrowse.Text = "Browse..."
    $iconBrowse.Location = New-Object System.Drawing.Point(428, 377)
    $iconBrowse.Size = New-Object System.Drawing.Size(90, 27)
    $iconClear = New-Object System.Windows.Forms.Button
    $iconClear.Text = "Clear"
    $iconClear.Location = New-Object System.Drawing.Point(526, 377)
    $iconClear.Size = New-Object System.Drawing.Size(84, 27)

    $legacyLabel = New-Object System.Windows.Forms.Label
    $legacyLabel.Text = "Optional previous Data folder to import"
    $legacyLabel.Location = New-Object System.Drawing.Point(26, 418)
    $legacyLabel.Size = New-Object System.Drawing.Size(360, 20)
    $legacyBox = New-Object System.Windows.Forms.TextBox
    $legacyBox.Location = New-Object System.Drawing.Point(26, 441)
    $legacyBox.Size = New-Object System.Drawing.Size(462, 24)
    $legacyBrowse = New-Object System.Windows.Forms.Button
    $legacyBrowse.Text = "Browse..."
    $legacyBrowse.Location = New-Object System.Drawing.Point(500, 440)
    $legacyBrowse.Size = New-Object System.Drawing.Size(110, 27)

    $startCheck = New-Object System.Windows.Forms.CheckBox
    $startCheck.Text = "Start the app when setup finishes"
    $startCheck.Checked = $true
    $startCheck.Location = New-Object System.Drawing.Point(26, 488)
    $startCheck.Size = New-Object System.Drawing.Size(320, 24)
    $shortcutCheck = New-Object System.Windows.Forms.CheckBox
    $shortcutCheck.Text = "Create shortcuts for all users"
    $shortcutCheck.Checked = $true
    $shortcutCheck.Location = New-Object System.Drawing.Point(26, 515)
    $shortcutCheck.Size = New-Object System.Drawing.Size(320, 24)

    $termsCheck = New-Object System.Windows.Forms.CheckBox
    $termsCheck.Text = "I have read and accept the Terms and Conditions"
    $termsCheck.Checked = $false
    $termsCheck.Location = New-Object System.Drawing.Point(26, 542)
    $termsCheck.Size = New-Object System.Drawing.Size(380, 24)
    $termsView = New-Object System.Windows.Forms.Button
    $termsView.Text = "View terms..."
    $termsView.Location = New-Object System.Drawing.Point(500, 540)
    $termsView.Size = New-Object System.Drawing.Size(110, 27)

    $churchBox.Add_TextChanged({
        $name = $churchBox.Text.Trim()
        if (-not $name) { return }
        try { $safe = ConvertTo-SafeFolderName $name } catch { return }
        $installParent = [System.IO.Path]::GetDirectoryName($state.AutoInstallPath)
        $newInstallPath = Join-Path $installParent $safe
        if ($installBox.Text -eq $state.AutoInstallPath) { $installBox.Text = $newInstallPath }
        $state.AutoInstallPath = $newInstallPath
        $dataBox.Text = Join-Path $programDataRoot $safe
    }.GetNewClosure())

    $installBrowse.Add_Click({
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = "Choose where the program folder should be created."
        $dialog.SelectedPath = $programFilesRoot
        if ($dialog.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) {
            $installBox.Text = Join-Path $dialog.SelectedPath (ConvertTo-SafeFolderName $churchBox.Text)
            $state.AutoInstallPath = $installBox.Text
        }
        $dialog.Dispose()
    }.GetNewClosure())

    $backgroundBrowse.Add_Click({
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Choose the default background image"
        $dialog.Filter = "Image backgrounds (*.png;*.jpg;*.jpeg;*.webp;*.bmp)|*.png;*.jpg;*.jpeg;*.webp;*.bmp|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        if ($dialog.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) { $backgroundBox.Text = $dialog.FileName }
        $dialog.Dispose()
    }.GetNewClosure())
    $backgroundClear.Add_Click({ $backgroundBox.Clear() }.GetNewClosure())

    $iconBrowse.Add_Click({
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Choose the app icon"
        $dialog.Filter = "Icon files (*.ico)|*.ico|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        if ($dialog.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) { $iconBox.Text = $dialog.FileName }
        $dialog.Dispose()
    }.GetNewClosure())
    $iconClear.Add_Click({ $iconBox.Clear() }.GetNewClosure())

    $termsView.Add_Click({
        $termsPath = Join-Path $SourceRoot "TERMS_AND_CONDITIONS.txt"
        if (Test-Path -LiteralPath $termsPath -PathType Leaf) { Start-Process notepad.exe -ArgumentList ('"{0}"' -f $termsPath) }
        else { [void][System.Windows.Forms.MessageBox]::Show($form, "TERMS_AND_CONDITIONS.txt was not found in the setup files.", "Terms and Conditions", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning) }
    }.GetNewClosure())

    $legacyBrowse.Add_Click({
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = "Choose the old app's Data folder. Its files will be copied, not removed."
        $dialog.ShowNewFolderButton = $false
        if ($dialog.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK) { $legacyBox.Text = $dialog.SelectedPath }
        $dialog.Dispose()
    }.GetNewClosure())

    $separator = New-Object System.Windows.Forms.Label
    $separator.BorderStyle = [System.Windows.Forms.BorderStyle]::Fixed3D
    $separator.Location = New-Object System.Drawing.Point(0, 586)
    $separator.Size = New-Object System.Drawing.Size(640, 2)
    $installButton = New-Object System.Windows.Forms.Button
    $installButton.Text = "Install"
    $installButton.Location = New-Object System.Drawing.Point(422, 612)
    $installButton.Size = New-Object System.Drawing.Size(90, 30)
    $installButton.UseVisualStyleBackColor = $true
    $cancelButton = New-Object System.Windows.Forms.Button
    $cancelButton.Text = "Cancel"
    $cancelButton.Location = New-Object System.Drawing.Point(520, 612)
    $cancelButton.Size = New-Object System.Drawing.Size(90, 30)
    $cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

    foreach ($control in @($churchLabel, $churchBox, $installLabel, $installBox, $installBrowse,
            $dataLabel, $dataBox, $backgroundLabel, $backgroundBox, $backgroundBrowse, $backgroundClear,
            $iconLabel, $iconBox, $iconBrowse, $iconClear,
            $legacyLabel, $legacyBox, $legacyBrowse, $startCheck, $shortcutCheck, $termsCheck, $termsView, $separator, $installButton, $cancelButton)) {
        [void]$form.Controls.Add($control)
    }
    $form.AcceptButton = $installButton
    $form.CancelButton = $cancelButton
    $installButton.Add_Click({
        try {
            if (-not $termsCheck.Checked) { throw "Please read and accept the Terms and Conditions to continue." }
            $safe = ConvertTo-SafeFolderName $churchBox.Text
            $destination = [System.IO.Path]::GetFullPath($installBox.Text.Trim()).TrimEnd('\')
            if (-not [System.IO.Path]::GetFileName($destination).Equals($safe, [StringComparison]::OrdinalIgnoreCase)) { throw "The program folder must be named '$safe'. Use Browse to choose a different parent folder." }
            if (-not $destination.StartsWith($programFilesRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Choose a folder under Program Files." }
            $existingState = Get-UninstalledInstallationState $destination $safe $programDataRoot $false $dataBox.Text
            if ((Test-Path -LiteralPath $destination) -and -not $existingState) { throw "That program folder already exists. Setup only reuses a folder marked by this app's Keep-data uninstall." }
            if ($destination.StartsWith($SourceRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "The program folder must be outside the extracted setup files." }
            if ($backgroundBox.Text.Trim() -and -not (Test-Path -LiteralPath $backgroundBox.Text.Trim() -PathType Leaf)) { throw "The selected background file could not be found." }
            if ($iconBox.Text.Trim()) {
                if (-not (Test-Path -LiteralPath $iconBox.Text.Trim() -PathType Leaf)) { throw "The selected icon file could not be found." }
                if ([System.IO.Path]::GetExtension($iconBox.Text.Trim()).ToLowerInvariant() -ne ".ico") { throw "The app icon must be a .ico file." }
            }
            if ($legacyBox.Text.Trim() -and -not (Test-Path -LiteralPath $legacyBox.Text.Trim() -PathType Container)) { throw "The selected previous Data folder could not be found." }
            $form.Tag = [pscustomobject]@{
                ChurchName = $safe
                DestinationDirectory = $destination
                DataDirectory = if ($existingState) { $existingState.normalized_data_directory } else { Join-Path $programDataRoot $safe }
                DefaultBackgroundFile = $backgroundBox.Text.Trim()
                CustomIconFile = $iconBox.Text.Trim()
                LegacyDataDirectory = if ($legacyBox.Text.Trim()) { $legacyBox.Text.Trim() } elseif ($existingState) { [string]$existingState.legacy_data_directory } else { "" }
                CreateShortcuts = $shortcutCheck.Checked
                LaunchAfterInstall = $startCheck.Checked
            }
            $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
            $form.Close()
        }
        catch {
            [void][System.Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, "Check setup choices", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
        }
    }.GetNewClosure())

    $result = $form.ShowDialog()
    $selection = $form.Tag
    $form.Dispose()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
    return $selection
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
function New-InstallProgressWindow([string]$ChurchName) {
    <#
     What: Open a small window that shows installation progress.
     Why included: Copying a large installation takes time and must show that it is working.
     Where used: Graphical installation after the setup wizard.
     Returns: An object holding the window, its label, and its progress bar.
    #>
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "$ChurchName Setup"
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $form.ClientSize = New-Object System.Drawing.Size(560, 230)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.ControlBox = $false
    $form.ShowInTaskbar = $true
    $label = New-Object System.Windows.Forms.Label
    $label.Text = "Preparing files..."
    $label.Location = New-Object System.Drawing.Point(18, 20)
    $label.Size = New-Object System.Drawing.Size(524, 130)
    $bar = New-Object System.Windows.Forms.ProgressBar
    $bar.Location = New-Object System.Drawing.Point(18, 174)
    $bar.Size = New-Object System.Drawing.Size(524, 24)
    $bar.Minimum = 0
    $bar.Maximum = 100
    [void]$form.Controls.Add($label)
    [void]$form.Controls.Add($bar)
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    return [pscustomobject]@{ Form = $form; Label = $label; Bar = $bar }
}

try {
    if ($Gui) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        if (-not ("SetupConsoleWindow" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SetupConsoleWindow {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
}
"@
        }
        $consoleWindow = [SetupConsoleWindow]::GetConsoleWindow()
        if ($consoleWindow -ne [IntPtr]::Zero) { [void][SetupConsoleWindow]::ShowWindow($consoleWindow, 0) }
    }
    if (-not [Environment]::Is64BitOperatingSystem) { throw "64-bit Windows is required." }
    if (-not $NoAdmin) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            $arguments = @("-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $PSCommandPath))
            foreach ($pair in @(@("SourceDirectory", $SourceDirectory), @("DestinationDirectory", $DestinationDirectory),
                    @("DataDirectory", $DataDirectory), @("ChurchName", $ChurchName),
                    @("DefaultBackgroundFile", $DefaultBackgroundFile), @("CustomIconFile", $CustomIconFile), @("LegacyDataDirectory", $LegacyDataDirectory))) {
                if ($pair[1]) { $arguments += @("-" + $pair[0], ('"{0}"' -f $pair[1])) }
            }
            foreach ($switchName in @("Gui", "AcceptTerms", "NoShortcuts", "NoLaunch", "NoPause")) {
                if ((Get-Variable -Name $switchName -ValueOnly).IsPresent) { $arguments += "-$switchName" }
            }
            $elevated = Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -Verb RunAs -WindowStyle Normal -ArgumentList $arguments -Wait -PassThru
            exit $elevated.ExitCode
        }
    }

    $SourceDirectory = [System.IO.Path]::GetFullPath($SourceDirectory).TrimEnd('\')
    $manifest = Get-Content -LiteralPath (Join-Path $SourceDirectory "release.json") -Raw | ConvertFrom-Json
    if ($manifest.application -ne "The Light Ministries" -or $manifest.package_type -ne "installation") {
        throw "Extract the Installation ZIP before running setup. Update ZIPs are for the app's Update menu."
    }
    foreach ($required in @("apply_windows_update.ps1", "create_desktop_shortcut.ps1", "uninstall_windows_app.ps1", "uninstall_windows_app.bat")) {
        if (-not (Test-Path -LiteralPath (Join-Path $SourceDirectory $required) -PathType Leaf)) { throw "Installation is missing $required." }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDirectory "PewVista.exe") -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $SourceDirectory "The Light Ministries.exe") -PathType Leaf)) { throw "Installation is missing PewVista.exe." }
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDirectory "_internal") -PathType Container)) { throw "The installation runtime is missing." }

    $hasSetupOverrides = $ChurchName -or $DestinationDirectory -or $DataDirectory -or $DefaultBackgroundFile -or $CustomIconFile -or $LegacyDataDirectory
    if ($Gui -or (-not $NoAdmin -and -not $hasSetupOverrides)) {
        $showWizard = $true
        $programFilesRoot = [Environment]::GetFolderPath("ProgramFiles")
        $programDataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
        $selection = Show-SetupWizard -ProgramFilesRoot $programFilesRoot -ProgramDataRoot $programDataRoot -SourceRoot $SourceDirectory
        if (-not $selection) { exit 0 }
        $ChurchName = $selection.ChurchName
        $DestinationDirectory = $selection.DestinationDirectory
        $DataDirectory = $selection.DataDirectory
        $DefaultBackgroundFile = $selection.DefaultBackgroundFile
        $CustomIconFile = $selection.CustomIconFile
        $LegacyDataDirectory = $selection.LegacyDataDirectory
        $NoShortcuts = -not $selection.CreateShortcuts
        $NoLaunch = -not $selection.LaunchAfterInstall
    }

    if (-not $showWizard -and -not $AcceptTerms) {
        $termsPath = Join-Path $SourceDirectory "TERMS_AND_CONDITIONS.txt"
        if (Test-Path -LiteralPath $termsPath -PathType Leaf) {
            Write-Host "---- Terms and Conditions ----"
            Get-Content -LiteralPath $termsPath -Encoding UTF8 | Write-Host
            Write-Host "------------------------------"
        }
        try { $answer = Read-Host "Do you accept the Terms and Conditions? (yes/no)" }
        catch { throw "Setup requires the Terms and Conditions to be accepted. Run again with -AcceptTerms to accept them in scripted installs." }
        if ($answer.Trim() -notmatch '^(?i)(y|yes)$') { throw "Setup cannot continue without accepting the Terms and Conditions." }
    }

    if (-not $ChurchName) {
        Write-Host "Enter the church/program name shown in the app title and shortcuts."
        $inputName = Read-Host "Church name [Your Church]"
        $ChurchName = if ($inputName.Trim()) { $inputName.Trim() } else { "Your Church" }
    }
    $safeName = ConvertTo-SafeFolderName $ChurchName
    $ChurchName = $safeName
    if (-not $DestinationDirectory) {
        $DestinationDirectory = Join-Path ([Environment]::GetFolderPath("ProgramFiles")) $safeName
        $choice = Read-Host "All-users program folder [${DestinationDirectory}]"
        if ($choice.Trim()) { $DestinationDirectory = $choice.Trim().Trim('"') }
    }
    $DestinationDirectory = [System.IO.Path]::GetFullPath($DestinationDirectory).TrimEnd('\')
    if (-not [System.IO.Path]::GetFileName($DestinationDirectory).Equals($safeName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The program folder must be named '$safeName' to match the church name entered in setup."
    }
    $programFilesRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath("ProgramFiles")).TrimEnd('\')
    if (-not $NoAdmin -and -not $DestinationDirectory.StartsWith($programFilesRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Install the all-users app under Program Files. Received folder: $DestinationDirectory"
    }
    $programDataRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    $existingState = Get-UninstalledInstallationState $DestinationDirectory $safeName $programDataRoot ([bool]$NoAdmin) $DataDirectory
    $isReinstall = [bool]$existingState
    if ((Test-Path -LiteralPath $DestinationDirectory) -and -not $existingState) {
        throw "Destination already exists and is not marked as uninstalled. Use Update > Install Update for an installed app, or choose an empty folder."
    }
    if ($DestinationDirectory.StartsWith($SourceDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Choose a program folder outside the extracted package." }

    if ($existingState) {
        if ($DataDirectory -and -not [System.IO.Path]::GetFullPath($DataDirectory).TrimEnd('\').Equals($existingState.normalized_data_directory, [StringComparison]::OrdinalIgnoreCase)) {
            throw "This folder belongs to shared data at $($existingState.normalized_data_directory). Setup will keep that database location."
        }
        $DataDirectory = $existingState.normalized_data_directory
        if (-not $LegacyDataDirectory -and $existingState.legacy_data_directory) {
            $LegacyDataDirectory = [string]$existingState.legacy_data_directory
        }
    }
    if (-not $DataDirectory) { $DataDirectory = Join-Path $programDataRoot $safeName }
    $DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory).TrimEnd('\')
    if (-not $NoAdmin -and -not $DataDirectory.StartsWith([System.IO.Path]::GetFullPath($programDataRoot).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Shared all-users data must be stored under ProgramData. Received folder: $DataDirectory"
    }
    if ($DataDirectory.StartsWith($DestinationDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Data must be outside the read-only Program Files folder." }

    if (-not $DefaultBackgroundFile -and -not $showWizard) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Choose an optional default background"
        $dialog.Filter = "Image backgrounds (*.png;*.jpg;*.jpeg;*.webp;*.bmp)|*.png;*.jpg;*.jpeg;*.webp;*.bmp|All files (*.*)|*.*"
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $DefaultBackgroundFile = $dialog.FileName }
        $dialog.Dispose()
    }
    if ($DefaultBackgroundFile -and -not (Test-Path -LiteralPath $DefaultBackgroundFile -PathType Leaf)) { throw "Selected default background does not exist: $DefaultBackgroundFile" }

    if ($CustomIconFile) {
        if (-not (Test-Path -LiteralPath $CustomIconFile -PathType Leaf)) { throw "Selected app icon does not exist: $CustomIconFile" }
        if ([System.IO.Path]::GetExtension($CustomIconFile).ToLowerInvariant() -ne ".ico") { throw "The app icon must be a .ico file: $CustomIconFile" }
    }

    if (-not $LegacyDataDirectory -and -not $showWizard) {
        Write-Host "Shared data will be created at: $DataDirectory"
        $oldFolder = Read-Host "Optional previous Data folder to import (press Enter to skip)"
        if ($oldFolder.Trim()) { $LegacyDataDirectory = $oldFolder.Trim().Trim('"') }
    }
    if ($LegacyDataDirectory) {
        $LegacyDataDirectory = [System.IO.Path]::GetFullPath($LegacyDataDirectory).TrimEnd('\')
        if (-not (Test-Path -LiteralPath $LegacyDataDirectory -PathType Container)) { throw "Previous Data folder was not found: $LegacyDataDirectory" }
        $isLegacyLocalData = $existingState -and $LegacyDataDirectory.Equals((Join-Path $DestinationDirectory "Data"), [StringComparison]::OrdinalIgnoreCase)
        $overlapsProgram = -not $isLegacyLocalData -and ($LegacyDataDirectory.Equals($DestinationDirectory, [StringComparison]::OrdinalIgnoreCase) -or
            $LegacyDataDirectory.StartsWith($DestinationDirectory + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $DestinationDirectory.StartsWith($LegacyDataDirectory + '\', [StringComparison]::OrdinalIgnoreCase))
        $overlapsSharedData = $LegacyDataDirectory.Equals($DataDirectory, [StringComparison]::OrdinalIgnoreCase) -or
            $LegacyDataDirectory.StartsWith($DataDirectory + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $DataDirectory.StartsWith($LegacyDataDirectory + '\', [StringComparison]::OrdinalIgnoreCase)
        if ($overlapsProgram -or $overlapsSharedData) { throw "The previous Data folder must be separate from the new program and shared-data folders." }
    }

    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($DestinationDirectory)) | Out-Null
    [System.IO.Directory]::CreateDirectory($DestinationDirectory) | Out-Null
    $createdProgram = $true
    [System.IO.Directory]::CreateDirectory($DataDirectory) | Out-Null
    $createdData = $true
    if (-not $NoAdmin) {
        $restrictedAccess = $false
        try {
            $groupName = "PewVista Users"
            if (-not (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue)) {
                New-LocalGroup -Name $groupName -Description "Windows accounts allowed to use PewVista and its member data." | Out-Null
            }
            $installingAccount = [Security.Principal.WindowsIdentity]::GetCurrent()
            if (-not (Get-LocalGroupMember -Group $groupName -Member $installingAccount.Name -ErrorAction SilentlyContinue)) {
                Add-LocalGroupMember -Group $groupName -Member $installingAccount.Name
            }
            $groupSid = (Get-LocalGroup -Name $groupName).SID.Value
            # Replace the inherited all-users ProgramData access with SYSTEM, Administrators, the
            # PewVista Users group, and the installing account (usable before it signs out and in).
            & icacls.exe $DataDirectory /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*${groupSid}:(OI)(CI)M" "*$($installingAccount.User.Value):(OI)(CI)M" /T /C | Out-Null
            if ($LASTEXITCODE -eq 0) {
                & icacls.exe $DataDirectory /remove:g "*S-1-5-32-545" /T /C | Out-Null
                $restrictedAccess = $LASTEXITCODE -eq 0
            }
        }
        catch { Write-Host "The PewVista Users group could not be configured: $($_.Exception.Message)" }
        if (-not $restrictedAccess) {
            Write-Host "Falling back to shared data access for all Windows accounts on this computer."
            & icacls.exe $DataDirectory /grant "*S-1-5-32-545:(OI)(CI)M" /T /C
            if ($LASTEXITCODE -ne 0) { throw "Could not grant standard users shared access to ProgramData: $DataDirectory" }
        }
    }

    $files = @(Get-ChildItem -LiteralPath $SourceDirectory -File -Recurse -Force)
    if ($showWizard) { $progressWindow = New-InstallProgressWindow $ChurchName }
    # Reinstalling over an existing copy: close anything holding its program files first,
    # and keep a backup of the previous installation (retaining only the 2 most recent backups).
    $privateObsPattern = '\\AppData\\Local\\PewVista\\' + [regex]::Escape((Split-Path -Leaf $DataDirectory)) + '\\obs_portable\\'
    $lockFolders = @($DestinationDirectory, (Join-Path $DataDirectory "obs_portable"))
    if (Test-Path -LiteralPath $DestinationDirectory -PathType Container) {
        if ($showWizard) { $progressWindow.Label.Text = "Closing programs that are using PewVista's files..."; [System.Windows.Forms.Application]::DoEvents() }
        $closedPrograms = Close-ProcessesUsingFolder -Folders $lockFolders -PathPatterns @($privateObsPattern)
        if ($closedPrograms.Count -gt 0) { Write-Host ("Closed: " + (($closedPrograms | Select-Object -Unique) -join ", ")) }
        if ($isReinstall) {
            try {
                $reinstallBackupName = (Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-" + [guid]::NewGuid().ToString("N")
                $reinstallBackupDir = Join-Path (Join-Path $DestinationDirectory ".update-backups") $reinstallBackupName
                [System.IO.Directory]::CreateDirectory($reinstallBackupDir) | Out-Null
                if (Test-Path -LiteralPath $DataDirectory -PathType Container) {
                    $sharedObsRuntime = Join-Path $DataDirectory "obs_portable"
                    & robocopy.exe $DataDirectory (Join-Path $reinstallBackupDir "Data") /E /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1 /XD $sharedObsRuntime | Out-Null
                }
                $backupRoot = Join-Path $DestinationDirectory ".update-backups"
                @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^\d{8}-\d{6}-\d{3}-[0-9a-f]{32}$' } |
                    Sort-Object Name -Descending | Select-Object -Skip 2) |
                    ForEach-Object {
                        try { [System.IO.Directory]::Delete($_.FullName, $true) }
                        catch { Write-Host "An old installation backup could not be removed: $($_.Exception.Message)" }
                    }
            }
            catch { Write-Host "Installation backup could not be completed: $($_.Exception.Message)" }
        }
    }
    $count = 0
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($SourceDirectory.Length + 1)
        $referenceData = $relative -match '^Data\\(biblical_lexicon|translation_lexicon|translation_word_cache|song_packs)\\'
        $appearanceSettings = $relative -ieq 'Data\ui_settings.json'
        if ($appearanceSettings -or
            ($relative -match '^(Data|config|\.update-backups|\.test-installation)(\\|$)' -and -not $referenceData) -or
            $relative -match '^Resourses\\obs_portable\\config(\\|$)' -or
            $file.Name -match '\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$') { continue }
        if ($referenceData) {
            $dataRelative = $relative.Substring(5)
            $target = Join-Path $DataDirectory $dataRelative
        }
        elseif ($relative -match '^Backgrounds\\') {
            $target = Join-Path $DataDirectory $relative
            if ([System.IO.File]::Exists($target)) {
                $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
                if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $sourceHash) { continue }
                $target = Join-Path ([System.IO.Path]::GetDirectoryName($target)) ([System.IO.Path]::GetFileNameWithoutExtension($target) + '_' + $sourceHash.Substring(0, 12).ToLowerInvariant() + [System.IO.Path]::GetExtension($target))
                if ([System.IO.File]::Exists($target)) {
                    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $sourceHash) { continue }
                    throw "Selected background conflicts with an existing file: $target"
                }
            }
        }
        else {
            $target = Join-Path $DestinationDirectory $relative
        }
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
        if ($isReinstall -and $relative -match '^(Bibles|Backgrounds)\\' -and
            (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
        $copyAttempt = 0
        while ($true) {
            try {
                [System.IO.File]::Copy($file.FullName, $target, $isReinstall)
                break
            }
            catch {
                $copyAttempt++
                if ($copyAttempt -ge 4 -or -not (Test-FileLockError $_)) { throw }
                Write-Host "$relative is in use; closing the program holding it and retrying..."
                [void](Close-ProcessesUsingFolder -Folders $lockFolders -PathPatterns @($privateObsPattern))
                Start-Sleep -Seconds 2
            }
        }
        $count++
        $percent = [int](100.0 * $count / [Math]::Max(1.0, [double]$files.Count))
        if ($showWizard) {
            $progressWindow.Label.Text = "Installing ${ChurchName}: $relative"
            $progressWindow.Bar.Value = [Math]::Min(100, $percent)
            [System.Windows.Forms.Application]::DoEvents()
        }
        else { Write-Progress -Activity "Installing $ChurchName" -Status $relative -PercentComplete $percent }
    }

    if ($LegacyDataDirectory) {
        Write-Host "Importing previous user data (newer files in ProgramData will be preserved)..."
        & robocopy.exe $LegacyDataDirectory $DataDirectory /E /XC /XN /XO /COPY:DAT /DCOPY:DAT /NP /NFL /NDL /R:1 /W:1
        if ($LASTEXITCODE -ge 8) { throw "Previous data migration failed. The original data folder was not deleted." }
    }
    $sourceAppearanceSettings = Join-Path $SourceDirectory "Data\ui_settings.json"
    $targetAppearanceSettings = Join-Path $DataDirectory "ui_settings.json"
    if ((Test-Path -LiteralPath $sourceAppearanceSettings -PathType Leaf) -and
        -not (Test-Path -LiteralPath $targetAppearanceSettings -PathType Leaf)) {
        [System.IO.File]::Copy($sourceAppearanceSettings, $targetAppearanceSettings)
    }
    # OBS is no longer shipped; reuse an installed OBS or download it. Failure is not fatal
    # because PewVista downloads OBS itself the first time the secondary screen starts.
    $obsWarning = ""
    $obsHelper = Join-Path $SourceDirectory "install_obs_runtime.ps1"
    if (Test-Path -LiteralPath $obsHelper -PathType Leaf) {
        try {
            . $obsHelper
            $obsProgress = {
                param($text, $percent)
                if ($showWizard) {
                    $progressWindow.Label.Text = $text
                    $progressWindow.Bar.Value = [Math]::Max(0, [Math]::Min(100, [int]$percent))
                    [System.Windows.Forms.Application]::DoEvents()
                }
                else { Write-Progress -Activity "Preparing OBS Studio for PewVista" -Status $text -PercentComplete ([Math]::Max(0, [Math]::Min(100, [int]$percent))) }
            }
            $obsOutcome = Install-PewVistaObsRuntime -Destination (Join-Path $DestinationDirectory "Resourses\obs_portable") -Progress $obsProgress
            Write-Host "OBS Studio for PewVista: $obsOutcome"
        }
        catch {
            $obsWarning = "OBS Studio (the secondary-screen player) could not be prepared: $($_.Exception.Message) PewVista will download it the first time the secondary screen is started; an internet connection is needed then."
            Write-Warning $obsWarning
        }
    }
    $defaultBackgroundPath = ""
    if ($existingState -and $existingState.default_background_path) {
        $savedBackgroundPath = [string]$existingState.default_background_path
        if ([System.IO.Path]::IsPathRooted($savedBackgroundPath) -and (Test-Path -LiteralPath $savedBackgroundPath -PathType Leaf)) {
            $defaultBackgroundPath = $savedBackgroundPath
        }
    }
    if ($DefaultBackgroundFile) {
        $backgroundDirectory = Join-Path $DataDirectory "default_background"
        [System.IO.Directory]::CreateDirectory($backgroundDirectory) | Out-Null
        $backgroundTarget = Join-Path $backgroundDirectory ("church-default" + [System.IO.Path]::GetExtension($DefaultBackgroundFile).ToLowerInvariant())
        [System.IO.File]::Copy($DefaultBackgroundFile, $backgroundTarget, $true)
        $defaultBackgroundPath = $backgroundTarget
    }
    $customIconPath = ""
    if ($existingState -and $existingState.custom_icon_path) {
        $savedIconPath = [string]$existingState.custom_icon_path
        if ([System.IO.Path]::IsPathRooted($savedIconPath) -and (Test-Path -LiteralPath $savedIconPath -PathType Leaf)) {
            $customIconPath = $savedIconPath
        }
    }
    if ($CustomIconFile) {
        $iconDirectory = Join-Path $DataDirectory "custom_icon"
        [System.IO.Directory]::CreateDirectory($iconDirectory) | Out-Null
        $iconTarget = Join-Path $iconDirectory "church-icon.ico"
        [System.IO.File]::Copy($CustomIconFile, $iconTarget, $true)
        $customIconPath = $iconTarget
    }
    $installation = @{
        application = "The Light Ministries"
        product_name = "PewVista"
        church_name = $ChurchName
        data_directory = $DataDirectory
        default_background_path = $defaultBackgroundPath
        custom_icon_path = $customIconPath
    }
    [System.IO.File]::WriteAllText((Join-Path $DestinationDirectory "installation.json"), ($installation | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
    $uninstallMarker = Join-Path $DestinationDirectory ".light-ministries-uninstalled.json"
    if ($isReinstall -and (Test-Path -LiteralPath $uninstallMarker -PathType Leaf)) {
        Remove-Item -LiteralPath $uninstallMarker -Force
    }

    $executable = if (Test-Path -LiteralPath (Join-Path $DestinationDirectory "PewVista.exe") -PathType Leaf) { Join-Path $DestinationDirectory "PewVista.exe" } else { Join-Path $DestinationDirectory "The Light Ministries.exe" }
    if (-not $NoShortcuts) {
        powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $DestinationDirectory "create_desktop_shortcut.ps1") -TargetExe $executable -AllUsers
        if ($LASTEXITCODE -ne 0) { throw "Shortcut creation failed." }
    }
    if ($showWizard) {
        $progressWindow.Label.Text = "Setup is complete."
        $progressWindow.Bar.Value = 100
        [System.Windows.Forms.Application]::DoEvents()
        $progressWindow.Form.Close()
        $progressWindow.Form.Dispose()
        $progressWindow = $null
        [void][System.Windows.Forms.MessageBox]::Show("PewVista is installed for $ChurchName.`r`n`r`nShared data: $DataDirectory`r`n`r`nOther Windows accounts that run services must be added to the 'PewVista Users' group by an administrator (Computer Management > Local Users and Groups), then sign out and in." + $(if ($obsWarning) { "`r`n`r`n" + $obsWarning } else { "" }), "Setup complete", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
    }
    else { Write-Progress -Activity "Installing PewVista for $ChurchName" -Completed }
    Write-Host "PewVista all-users installation complete: $DestinationDirectory"
    Write-Host "Shared databases/settings: $DataDirectory"
    if (-not $NoLaunch) { Start-Process -FilePath $executable -WorkingDirectory $DestinationDirectory }
    exit 0
}
catch {
    if ($progressWindow) {
        try { $progressWindow.Form.Close(); $progressWindow.Form.Dispose() } catch { }
        $progressWindow = $null
    }
    if ($showWizard) {
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Setup could not finish", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    }
    else { Write-Host "[ERROR] $($_.Exception.Message)" }
    if ($createdProgram) { Write-Host "Incomplete program folder retained for inspection: $DestinationDirectory" }
    if ($createdData) { Write-Host "Shared data folder retained; original imported data was not deleted: $DataDirectory" }
    if (-not $NoPause -and -not $showWizard) { Write-Host "Press Enter to close."; [Console]::ReadLine() | Out-Null }
    exit 1
}
