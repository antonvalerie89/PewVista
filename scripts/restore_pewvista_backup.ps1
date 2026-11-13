# Restore a PewVista publisher backup made with "Back Up Now" in the license tool.
# Double-click "Restore PewVista.bat" in the backup folder; this script does the work.
# Existing different keys are never overwritten: they are kept under a dated name.
param([switch]$Quiet, [string]$ProjectDirectory = "C:\PewVista")

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms, System.IO.Compression.FileSystem
$backup = Split-Path -Parent $MyInvocation.MyCommand.Definition
$project = $ProjectDirectory
$report = New-Object System.Collections.Generic.List[string]

function Show-Message([string]$text, [string]$title, [string]$buttons = "OK", [string]$icon = "Information") {
    if ($Quiet) { Write-Host "$title`n$text"; return "Yes" }
    return [string][System.Windows.Forms.MessageBox]::Show($text, $title, $buttons, $icon)
}

function Test-SameFolder([string]$a, [string]$b) {
    $left = @(Get-ChildItem -LiteralPath $a -File | Sort-Object Name | ForEach-Object { $_.Name + ":" + (Get-FileHash -LiteralPath $_.FullName).Hash })
    $right = @(Get-ChildItem -LiteralPath $b -File | Sort-Object Name | ForEach-Object { $_.Name + ":" + (Get-FileHash -LiteralPath $_.FullName).Hash })
    return (($left -join "|") -eq ($right -join "|"))
}

try {
    $answer = Show-Message "Restore your PewVista signing keys, project from GitHub (if missing), Python 3.13 environment, grammar checker, license records, and Desktop License Tool icon from this backup onto this PC?" "Restore PewVista" "YesNo" "Question"
    if ($answer -ne "Yes") { exit 0 }

    $keysSource = Join-Path $backup "PewVista Signing"
    $keysTarget = Join-Path $env:USERPROFILE "PewVista Signing"
    if (-not (Test-Path -LiteralPath $keysSource)) { throw "This backup has no 'PewVista Signing' folder." }
    if ((Test-Path -LiteralPath $keysTarget) -and (Test-SameFolder $keysSource $keysTarget)) {
        $report.Add("Signing keys: already in place.")
    }
    else {
        if (Test-Path -LiteralPath $keysTarget) {
            $kept = "$keysTarget (replaced $(Get-Date -Format 'yyyy-MM-dd HHmmss'))"
            Rename-Item -LiteralPath $keysTarget -NewName (Split-Path -Leaf $kept)
            $report.Add("Signing keys: the different keys found were kept as '$(Split-Path -Leaf $kept)'.")
        }
        Copy-Item -LiteralPath $keysSource -Destination $keysTarget -Recurse
        $report.Add("Signing keys: restored to $keysTarget")
    }

    # Ensure Git and GitHub CLI are available for repository management and release publishing
    $gitCmd = (Get-Command git.exe -ErrorAction SilentlyContinue)
    if (-not $gitCmd -and (Test-Path -LiteralPath "C:\Program Files\Git\cmd\git.exe")) {
        $env:Path = "C:\Program Files\Git\cmd;" + $env:Path
        $gitCmd = (Get-Command git.exe -ErrorAction SilentlyContinue)
    }
    if (-not $gitCmd -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        & winget.exe install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent | Out-Null
        if (Test-Path -LiteralPath "C:\Program Files\Git\cmd\git.exe") {
            $env:Path = "C:\Program Files\Git\cmd;" + $env:Path
            $gitCmd = (Get-Command git.exe -ErrorAction SilentlyContinue)
        }
    }
    if (-not (Test-Path -LiteralPath "C:\Program Files\GitHub CLI\gh.exe") -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        & winget.exe install --id GitHub.cli -e --source winget --accept-package-agreements --accept-source-agreements --silent | Out-Null
    }

    # Clone or reconnect the GitHub repository at $project
    if (-not (Test-Path -LiteralPath (Join-Path $project "licensing.py")) -and $gitCmd) {
        & $gitCmd.Source clone "https://github.com/antonvalerie89/PewVista.git" $project | Out-Null
        if (Test-Path -LiteralPath (Join-Path $project "licensing.py")) {
            $report.Add("Project: cloned from GitHub to $project.")
        }
    }
    elseif ((Test-Path -LiteralPath (Join-Path $project "licensing.py")) -and -not (Test-Path -LiteralPath (Join-Path $project ".git")) -and $gitCmd) {
        & $gitCmd.Source clone --bare "https://github.com/antonvalerie89/PewVista.git" (Join-Path $project ".git") | Out-Null
        & $gitCmd.Source -C $project config --bool core.bare false
        & $gitCmd.Source -C $project reset --mixed HEAD | Out-Null
        $report.Add("Git repository: reconnected at $project.")
    }

    if (Test-Path -LiteralPath (Join-Path $project "licensing.py")) {
        $grammarZip = Join-Path $backup "grammar.zip"
        $grammarTarget = Join-Path $project "Resourses\grammar"
        if (-not (Test-Path -LiteralPath $grammarZip)) { $report.Add("Grammar checker: not in this backup.") }
        elseif (Test-Path -LiteralPath $grammarTarget) { $report.Add("Grammar checker: already in place.") }
        else {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($grammarZip, $grammarTarget)
            $report.Add("Grammar checker: restored to $grammarTarget")
        }
        $records = Join-Path $backup "license_records.txt"
        $recordsTarget = Join-Path $project "license_records.txt"
        if (-not (Test-Path -LiteralPath $records)) { $report.Add("License records: not in this backup.") }
        elseif (-not (Test-Path -LiteralPath $recordsTarget)) {
            Copy-Item -LiteralPath $records -Destination $recordsTarget
            $report.Add("License records: restored.")
        }
        elseif ((Get-FileHash -LiteralPath $records).Hash -eq (Get-FileHash -LiteralPath $recordsTarget).Hash) { $report.Add("License records: already in place.") }
        else {
            Copy-Item -LiteralPath $records -Destination (Join-Path $project "license_records (from backup).txt") -Force
            $report.Add("License records: this PC already has a different list; the backup copy was saved as 'license_records (from backup).txt'.")
        }

        # Ensure a working Python 3.13 virtual environment with all PewVista packages
        $venvPython = Join-Path $project ".venv\Scripts\python.exe"
        $venvReady = $false
        if (Test-Path -LiteralPath $venvPython) {
            & $venvPython -c "import sys; assert sys.version_info[:2] == (3, 13); import cryptography, PIL, sqlcipher3, PyInstaller" 2>$null
            $venvReady = ($LASTEXITCODE -eq 0)
        }
        if (-not $venvReady -and (Get-Command uv.exe -ErrorAction SilentlyContinue)) {
            & uv.exe python install 3.13 | Out-Null
            & uv.exe venv (Join-Path $project ".venv") --python 3.13 --clear | Out-Null
            if (Test-Path -LiteralPath $venvPython) {
                & $venvPython -m ensurepip | Out-Null
                & $venvPython -m pip install pillow cryptography imageio imageio-ffmpeg obsws-python pypdf spylls==0.1.7 langid==1.1.6 sqlcipher3-wheels pyinstaller | Out-Null
                $report.Add("Python 3.13 environment: created and packages installed.")
            }
        }

        $licenseTool = Join-Path $project "license_tool.pyw"
        if (Test-Path -LiteralPath $licenseTool) {
            $pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"
            if (-not (Test-Path -LiteralPath $pythonw)) {
                $foundPythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
                if ($foundPythonw) { $pythonw = $foundPythonw.Source }
            }
            $desktopDir = [Environment]::GetFolderPath("Desktop")
            $shortcutPath = Join-Path $desktopDir "PewVista License Tool.lnk"
            $wshShell = New-Object -ComObject WScript.Shell
            $shortcut = $wshShell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $pythonw
            $shortcut.Arguments = "`"$licenseTool`""
            $shortcut.WorkingDirectory = $project
            $iconPath = Join-Path $project "Resourses\PewVista.ico"
            if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
            $shortcut.Description = "PewVista - Create License Code"
            $shortcut.Save()
            $report.Add("Desktop icon: 'PewVista License Tool' placed on your Desktop.")
        }
    }
    else {
        $report.Add("The PewVista project is not at $project yet. Download it from GitHub (github.com/antonvalerie89/PewVista), then run this restore again for the grammar checker and license records.")
    }
    [void](Show-Message ($report -join "`r`n`r`n") "Restore PewVista - finished")
}
catch {
    [void](Show-Message ("The restore stopped: $($_.Exception.Message)`r`n`r`n" + ($report -join "`r`n")) "Restore PewVista" "OK" "Error")
    exit 1
}
