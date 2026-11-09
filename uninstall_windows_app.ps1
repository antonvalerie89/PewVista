param(
    [string]$AppDirectory = "",
    [ValidateSet("Ask", "Keep", "Remove")][string]$DataAction = "Ask",
    [switch]$ConfirmUninstall,
    [switch]$Gui,
    [switch]$NoAdmin,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Gui = $Gui -or (-not $NoAdmin -and -not $ConfirmUninstall)
if (-not $AppDirectory) { $AppDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition }

function Show-UninstallWizard([string]$DisplayName, [string]$ProgramDirectory, [string]$SharedDataDirectory) {
    <#
     What: Ask whether to keep or remove the church data when uninstalling.
     Why included: Removing data is permanent, so the operator must choose deliberately.
     Where used: Graphical uninstall.
     Returns: The chosen data action, or nothing when uninstall is cancelled.
    #>
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "$DisplayName Uninstall"
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
    $form.ClientSize = New-Object System.Drawing.Size(640, 660)
    $form.MinimumSize = New-Object System.Drawing.Size(600, 540)
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.MinimizeBox = $false
    $layout = New-Object System.Windows.Forms.TableLayoutPanel
    $layout.Dock = [System.Windows.Forms.DockStyle]::Fill
    $layout.Padding = New-Object System.Windows.Forms.Padding(20)
    $layout.ColumnCount = 1
    $layout.AutoScroll = $true
    [void]$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    [void]$form.Controls.Add($layout)

    $heading = New-Object System.Windows.Forms.Label
    $heading.Text = "Uninstall $DisplayName"
    $heading.Font = New-Object System.Drawing.Font("Segoe UI", 16)
    $heading.AutoSize = $true
    $heading.MaximumSize = New-Object System.Drawing.Size(560, 0)
    $heading.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 18)
    [void]$layout.Controls.Add($heading)
    foreach ($entry in @(@("Program folder", $ProgramDirectory), @("Shared data folder", $SharedDataDirectory))) {
        $caption = New-Object System.Windows.Forms.Label
        $caption.Text = $entry[0]
        $caption.AutoSize = $true
        [void]$layout.Controls.Add($caption)
        $pathBox = New-Object System.Windows.Forms.TextBox
        $pathBox.Text = $entry[1]
        $pathBox.ReadOnly = $true
        $pathBox.Multiline = $true
        $pathBox.Height = 48
        $pathBox.Dock = [System.Windows.Forms.DockStyle]::Top
        $pathBox.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 16)
        [void]$layout.Controls.Add($pathBox)
    }
    $keep = New-Object System.Windows.Forms.RadioButton
    $keep.Text = "Keep saved data (recommended)"
    $keep.AutoSize = $true
    $keep.Checked = $true
    [void]$layout.Controls.Add($keep)
    $keepDescription = New-Object System.Windows.Forms.Label
    $keepDescription.Text = "Remove the program and its shortcuts. Keep saved records, settings, imported files, and local backups."
    $keepDescription.AutoSize = $true
    $keepDescription.MaximumSize = New-Object System.Drawing.Size(550, 0)
    $keepDescription.Margin = New-Object System.Windows.Forms.Padding(22, 0, 0, 16)
    [void]$layout.Controls.Add($keepDescription)
    $remove = New-Object System.Windows.Forms.RadioButton
    $remove.Text = "Remove the program and permanently delete saved data"
    $remove.AutoSize = $true
    $remove.MaximumSize = New-Object System.Drawing.Size(550, 0)
    [void]$layout.Controls.Add($remove)
    $deleteLabel = New-Object System.Windows.Forms.Label
    $deleteLabel.Text = "Type DELETE to confirm permanent deletion."
    $deleteLabel.AutoSize = $true
    $deleteLabel.Margin = New-Object System.Windows.Forms.Padding(22, 12, 0, 4)
    [void]$layout.Controls.Add($deleteLabel)
    $deleteBox = New-Object System.Windows.Forms.TextBox
    $deleteBox.Enabled = $false
    $deleteBox.Dock = [System.Windows.Forms.DockStyle]::Top
    $deleteBox.Margin = New-Object System.Windows.Forms.Padding(22, 0, 0, 20)
    [void]$layout.Controls.Add($deleteBox)
    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.FlowDirection = [System.Windows.Forms.FlowDirection]::RightToLeft
    $buttons.AutoSize = $true
    $buttons.Dock = [System.Windows.Forms.DockStyle]::Top
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = "Cancel"
    $cancel.Size = New-Object System.Drawing.Size(100, 34)
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $uninstall = New-Object System.Windows.Forms.Button
    $uninstall.Text = "Uninstall"
    $uninstall.Size = New-Object System.Drawing.Size(110, 34)
    [void]$buttons.Controls.Add($cancel)
    [void]$buttons.Controls.Add($uninstall)
    [void]$layout.Controls.Add($buttons)
    $form.CancelButton = $cancel
    $remove.Add_CheckedChanged({
        $deleteBox.Enabled = $remove.Checked
        $deleteLabel.Enabled = $remove.Checked
        $uninstall.Enabled = -not $remove.Checked -or $deleteBox.Text -ceq "DELETE"
    }.GetNewClosure())
    $deleteBox.Add_TextChanged({ $uninstall.Enabled = -not $remove.Checked -or $deleteBox.Text -ceq "DELETE" }.GetNewClosure())
    $uninstall.Add_Click({
        if ($remove.Checked -and $deleteBox.Text -cne "DELETE") { return }
        $form.Tag = if ($remove.Checked) { "Remove" } else { "Keep" }
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    }.GetNewClosure())
    $deleteLabel.Enabled = $false
    $result = $form.ShowDialog()
    $selection = $form.Tag
    $form.Dispose()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) { return $selection }
    return $null
}

function Invoke-UninstallRemoval([string[]]$Paths, [string]$ProgramDirectory, [bool]$ShowGui) {
    <#
     What: Delete the listed program and data paths while showing progress.
     Why included: Uninstall removes many files and must report progress and failures clearly.
     Where used: Uninstall after the operator confirms.
     Returns: Nothing; throws when a path cannot be removed.
    #>
    $state = [hashtable]::Synchronized(@{ Completed = 0; Current = "Preparing removal..." })
    $removal = {
        param($targets, $programFolder, $progressState)
        $ErrorActionPreference = "Stop"
        foreach ($target in $targets) {
            $progressState.Current = "Removing " + [System.IO.Path]::GetFileName($target)
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            $progressState.Completed++
        }
        if ((Test-Path -LiteralPath $programFolder) -and @(Get-ChildItem -LiteralPath $programFolder -Force).Count -eq 0) {
            Remove-Item -LiteralPath $programFolder -Force
        }
    }
    if (-not $ShowGui) { & $removal $Paths $ProgramDirectory $state; return }
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Uninstalling"
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
    $form.ClientSize = New-Object System.Drawing.Size(560, 230)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.ControlBox = $false
    $label = New-Object System.Windows.Forms.Label
    $label.Location = New-Object System.Drawing.Point(20, 20)
    $label.Size = New-Object System.Drawing.Size(520, 110)
    $label.Text = "Preparing removal..."
    $bar = New-Object System.Windows.Forms.ProgressBar
    $bar.Location = New-Object System.Drawing.Point(20, 174)
    $bar.Size = New-Object System.Drawing.Size(520, 24)
    $bar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
    [void]$form.Controls.Add($label)
    [void]$form.Controls.Add($bar)
    $worker = [System.Management.Automation.PowerShell]::Create()
    [void]$worker.AddScript($removal.ToString()).AddArgument($Paths).AddArgument($ProgramDirectory).AddArgument($state)
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 150
    try {
        $operation = $worker.BeginInvoke()
        $timer.Add_Tick({
            $label.Text = $state.Current + "`r`nCompleted steps: " + $state.Completed + "/" + $Paths.Count
            if ($operation.IsCompleted) { $timer.Stop(); $form.Close() }
        }.GetNewClosure())
        $timer.Start()
        [void]$form.ShowDialog()
        [void]$worker.EndInvoke($operation)
        if ($worker.HadErrors) { throw $worker.Streams.Error[0].ToString() }
    }
    finally { $timer.Dispose(); $worker.Dispose(); $form.Dispose() }
}

try {
    if ($Gui) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        [System.Windows.Forms.Application]::EnableVisualStyles()
        if (-not ("UninstallConsoleWindow" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class UninstallConsoleWindow {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
}
"@
        }
        $consoleWindow = [UninstallConsoleWindow]::GetConsoleWindow()
        if ($consoleWindow -ne [IntPtr]::Zero) { [void][UninstallConsoleWindow]::ShowWindow($consoleWindow, 0) }
    }
    $AppDirectory = [System.IO.Path]::GetFullPath($AppDirectory).TrimEnd('\')
    if (-not $NoAdmin) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            $arguments = @("-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                "-AppDirectory", "`"$AppDirectory`"", "-DataAction", $DataAction)
            if ($ConfirmUninstall) { $arguments += "-ConfirmUninstall" }
            if ($NoPause) { $arguments += "-NoPause" }
            if ($Gui) { $arguments += "-Gui" }
            $elevated = Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -Verb RunAs -WindowStyle Normal -ArgumentList $arguments -Wait -PassThru
            exit $elevated.ExitCode
        }
    }
    if ($AppDirectory -eq [System.IO.Path]::GetPathRoot($AppDirectory).TrimEnd('\')) { throw "A drive root cannot be uninstalled." }
    foreach ($sourceMarker in @("main.py", "PewVista.spec", "The_Light_Ministries.spec", ".venv")) {
        if (Test-Path -LiteralPath (Join-Path $AppDirectory $sourceMarker)) { throw "This is a development workspace. Uninstall is only for a separate desktop installation." }
    }
    $executable = if (Test-Path -LiteralPath (Join-Path $AppDirectory "PewVista.exe") -PathType Leaf) { Join-Path $AppDirectory "PewVista.exe" } else { Join-Path $AppDirectory "The Light Ministries.exe" }
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $AppDirectory "_internal") -PathType Container)) {
        throw "The selected folder is not a recognised PewVista installation."
    }
    $churchName = "Your Church"
    $dataDirectory = Join-Path $AppDirectory "Data"
    $defaultBackgroundPath = ""
    $customIconPath = ""
    $installationConfig = Join-Path $AppDirectory "installation.json"
    if (Test-Path -LiteralPath $installationConfig -PathType Leaf) {
        $settings = Get-Content -LiteralPath $installationConfig -Raw | ConvertFrom-Json
        if ($settings.church_name) { $churchName = [string]$settings.church_name }
        if ($settings.default_background_path) { $defaultBackgroundPath = [string]$settings.default_background_path }
        if ($settings.custom_icon_path) { $customIconPath = [string]$settings.custom_icon_path }
        $configuredData = [string]$settings.data_directory
        $programDataRoot = [System.IO.Path]::GetFullPath([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)).TrimEnd('\')
        if (-not [System.IO.Path]::IsPathRooted($configuredData)) { throw "Installed shared-data path is invalid; no files were removed." }
        $dataDirectory = [System.IO.Path]::GetFullPath($configuredData).TrimEnd('\')
        if (-not $NoAdmin -and -not $dataDirectory.StartsWith($programDataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Installed shared-data path is outside ProgramData; no files were removed." }
    }
    $displayName = "PewVista - " + $churchName
    $active = @(Get-Process -Name "PewVista", "The Light Ministries", "obs64", "java", "javaw" -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($AppDirectory + '\', [StringComparison]::OrdinalIgnoreCase)
    })
    if ($active.Count -gt 0) { throw "Close this installation's app, bundled OBS, and grammar runtime before uninstalling. Nothing has been removed." }

    Write-Host "Uninstall $displayName from: $AppDirectory"
    if ($Gui) {
        $selection = Show-UninstallWizard $displayName $AppDirectory $dataDirectory
        if (-not $selection) { exit 0 }
        $DataAction = $selection
        $ConfirmUninstall = $true
    }
    if (-not $ConfirmUninstall) {
        if ((Read-Host "Remove this program? Type YES to continue").Trim() -cne "YES") { Write-Host "Cancelled. Nothing was removed."; exit 0 }
    }
    if ($DataAction -eq "Ask") {
        Write-Host "1 - Keep personal data (default): databases, settings, imported Bibles/backgrounds, and update backups."
        Write-Host "2 - Permanently remove personal data as well as the program."
        $choice = (Read-Host "Choose 1 or 2, or press Enter to keep data").Trim()
        if ($choice -eq "2") { $DataAction = "Remove" }
        elseif ($choice -eq "1" -or $choice -eq "") { $DataAction = "Keep" }
        else { throw "Invalid choice. Nothing has been removed." }
    }
    if ($DataAction -eq "Remove" -and -not $ConfirmUninstall) {
        if ((Read-Host "This permanently deletes saved songs, members, services, settings, imports, and local backups. Type DELETE to confirm").Trim() -cne "DELETE") {
            Write-Host "Cancelled. Nothing was removed."
            exit 0
        }
    }

    $directories = @("_internal", "Resourses", "background_cache", "__pycache__")
    if ($DataAction -eq "Remove") { $directories += @("Data", "Bibles", "Backgrounds", "config", ".update-backups") }
    if ($DataAction -eq "Remove" -and -not $dataDirectory.Equals((Join-Path $AppDirectory "Data"), [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $dataDirectory -PathType Container)) {
        $item = Get-Item -LiteralPath $dataDirectory -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            @(Get-ChildItem -LiteralPath $dataDirectory -Recurse -Force | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) {
            throw "Linked paths were found in shared data. Nothing has been removed; review those links manually first."
        }
    }
    foreach ($name in $directories) {
        $path = Join-Path $AppDirectory $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            @(Get-ChildItem -LiteralPath $path -Recurse -Force | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) {
            throw "Linked paths were found in $name. Nothing has been removed; review those links manually first."
        }
    }
    $shortcuts = @()
    $shell = New-Object -ComObject WScript.Shell
    $shortcutFolders = @([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonPrograms),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)) | Select-Object -Unique
    foreach ($folder in $shortcutFolders) {
        foreach ($shortcutName in @($displayName + ".lnk", "PewVista.lnk", $churchName + ".lnk", "The Light Ministries.lnk") | Select-Object -Unique) {
            $path = Join-Path $folder $shortcutName
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $target = $shell.CreateShortcut($path).TargetPath
                if ($target -and [System.IO.Path]::GetFullPath($target).Equals($executable, [StringComparison]::OrdinalIgnoreCase)) { $shortcuts += $path }
            }
        }
    }
    $removalPaths = @($directories | ForEach-Object { Join-Path $AppDirectory $_ }) + $shortcuts
    if ($DataAction -eq "Remove" -and -not $dataDirectory.Equals((Join-Path $AppDirectory "Data"), [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $dataDirectory -PathType Container)) {
        $removalPaths += $dataDirectory
    }
    $programFiles = @("PewVista.exe", "The Light Ministries.exe", "apply_windows_update.ps1", "install_windows_app.ps1",
        "install_windows_app.bat", "create_desktop_shortcut.ps1", "create_desktop_shortcut.bat",
        "WINDOWS_REQUIREMENTS.txt", "TERMS_AND_CONDITIONS.txt", "OPERATING_MANUAL.txt", "SECURITY_AND_PRIVACY.txt", "THIRD_PARTY_NOTICES.txt", "release.json", "installation.json", "update-install.log", ".installation.json", ".test-installation",
        "uninstall_windows_app.bat", "uninstall_windows_app.ps1")
    $removalPaths += @($programFiles | ForEach-Object { Join-Path $AppDirectory $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    Invoke-UninstallRemoval $removalPaths $AppDirectory ([bool]$Gui)
    if ($DataAction -eq "Keep") {
        [System.IO.Directory]::CreateDirectory($AppDirectory) | Out-Null
        $marker = @{
            application = "The Light Ministries"
            state = "uninstalled"
            church_name = $churchName
            data_directory = $dataDirectory
            default_background_path = $defaultBackgroundPath
            custom_icon_path = $customIconPath
            uninstalled_at = [DateTime]::UtcNow.ToString("o")
        }
        [System.IO.File]::WriteAllText((Join-Path $AppDirectory ".light-ministries-uninstalled.json"),
            ($marker | ConvertTo-Json -Depth 3), [System.Text.UTF8Encoding]::new($false))
    }
    Write-Host "Program removed. Shortcuts belonging to other installations were not changed."
    if ($DataAction -eq "Keep") { Write-Host "Your personal data remains in: $dataDirectory" }
    else { Write-Host "Personal data in this installation was removed. External backups and unrelated files were not removed." }
    if ($Gui) {
        $completion = if ($DataAction -eq "Keep") { "The program was removed.`r`n`r`nYour saved data remains in:`r`n$dataDirectory" } else { "The program and its selected saved data were removed. External backups were not changed." }
        [void][System.Windows.Forms.MessageBox]::Show($completion, "Uninstall complete", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
    }
    elseif (-not $NoPause) { Write-Host "Press Enter to close."; [Console]::ReadLine() | Out-Null }
    exit 0
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)"
    Write-Host "Uninstall stopped. Review the message before trying again."
    if ($Gui) {
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message + "`r`n`r`nUninstall stopped. Some program files may already have been removed; review the installation before retrying.", "Uninstall could not finish", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    }
    elseif (-not $NoPause) { Write-Host "Press Enter to close."; [Console]::ReadLine() | Out-Null }
    exit 1
}
