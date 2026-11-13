# Prepare PewVista's private OBS Studio copy (the secondary-screen player).
#
# Setup calls Install-PewVistaObsRuntime after copying the program files. It reuses the
# program files of an OBS Studio already installed on this PC (version 28 or newer), or
# otherwise downloads the official OBS portable ZIP, checks its SHA-256 and extracts it.
# The church's own OBS settings are never read or changed; PewVista runs its private copy
# with its own profile. Keep the URL and hash in step with obs_secondary_display.py.
#
# Standalone use:  powershell -File install_obs_runtime.ps1 -TargetDirectory "<folder>"

param(
    [string]$TargetDirectory = ""
)

$script:ObsDownloadUrl = "https://github.com/obsproject/obs-studio/releases/download/32.1.2/OBS-Studio-32.1.2-Windows-x64.zip"
$script:ObsDownloadSha256 = "8D97E4563BD8D22D03E63042AA7DCCEDE1D555C9BD35CE8A9E5019B0D0201BF6"
$script:ObsRuntimeFolders = @("bin", "data", "obs-plugins")
$script:ObsMinimumMajorVersion = 28
$script:VcRedistDownloadUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$script:VcRedistRequiredDlls = @("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll")

function Test-VcRedistInstalled {
    <#
        What: Check whether the 64-bit Microsoft Visual C++ 2015-2022 runtime DLLs are installed.
        Why included: Portable OBS Studio (obs64.exe and Qt6) cannot start without MSVCP140/VCRUNTIME140.
        Where used: Install-VcRedistRuntime before downloading or running vc_redist.x64.exe.
        Returns: True when every required DLL is present in System32; otherwise False.
    #>
    $system32 = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    if (-not $system32) { return $false }
    foreach ($dll in $script:VcRedistRequiredDlls) {
        if (-not (Test-Path -LiteralPath (Join-Path $system32 $dll) -PathType Leaf)) { return $false }
    }
    return $true
}

function Install-VcRedistRuntime([scriptblock]$Progress = $null) {
    <#
        What: Ensure the 64-bit Microsoft Visual C++ Redistributable is installed when running as administrator.
        Why included: Fresh Windows installations lack msvcp140.dll and vcruntime140(_1).dll needed by obs64.exe.
        Where used: Install-PewVistaObsRuntime before or alongside preparing OBS Studio.
        Returns: "present", "installed", or "skipped"; throws with a readable reason on failure.
    #>
    if (Test-VcRedistInstalled) { return "present" }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { return "skipped" }
    $installerPath = Join-Path ([System.IO.Path]::GetTempPath()) ("PewVista-vc_redist.x64-" + [guid]::NewGuid().ToString("N") + ".exe")
    try {
        if ($Progress) { & $Progress "Downloading Microsoft Visual C++ Runtime..." 0 }
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $request = [System.Net.HttpWebRequest]::Create($script:VcRedistDownloadUrl)
        $request.UserAgent = "PewVista-Setup"
        $request.Timeout = 60000
        $request.ReadWriteTimeout = 60000
        $response = $request.GetResponse()
        try {
            $total = [double]$response.ContentLength
            $download = $response.GetResponseStream()
            $output = [System.IO.File]::Create($installerPath)
            try {
                $buffer = New-Object byte[] (1024 * 1024)
                $received = 0.0
                while (($read = $download.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $output.Write($buffer, 0, $read)
                    $received += $read
                    if ($Progress -and $total -gt 0) { & $Progress ("Downloading Microsoft Visual C++ Runtime... {0:N0} of {1:N0} MB" -f ($received / 1MB), ($total / 1MB)) ([int](100 * $received / $total)) }
                }
            }
            finally {
                $output.Dispose()
                $download.Dispose()
            }
        }
        finally {
            $response.Close()
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notlike "*Microsoft Corporation*") {
            throw "The downloaded Microsoft Visual C++ Runtime failed its digital signature check."
        }
        if ($Progress) { & $Progress "Installing Microsoft Visual C++ Runtime..." 100 }
        $process = Start-Process -FilePath $installerPath -ArgumentList @("/install", "/quiet", "/norestart") -Wait -PassThru
        if ($process.ExitCode -notin @(0, 1638, 3010) -and -not (Test-VcRedistInstalled)) {
            throw "The Microsoft Visual C++ Runtime installer exited with code $($process.ExitCode)."
        }
        if (-not (Test-VcRedistInstalled)) {
            throw "The Microsoft Visual C++ Runtime finished, but required runtime DLLs are still missing."
        }
        return "installed"
    }
    finally {
        if (Test-Path -LiteralPath $installerPath) { Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue }
    }
}

function Find-InstalledObsRoot {
    <#
        What: Locate an OBS Studio installation that can serve as PewVista's private copy.
        Why included: Copying existing program files avoids a 185 MB download.
        Where used: Install-PewVistaObsRuntime.
        Returns: The OBS root folder, or $null when none is usable.
    #>
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($key in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio",
                       "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio",
                       "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio")) {
        $location = (Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).InstallLocation
        if ($location) { $candidates.Add([string]$location) }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path ([string]$env:LOCALAPPDATA) "Programs"))) {
        if ($base) { $candidates.Add((Join-Path $base "obs-studio")) }
    }
    foreach ($root in $candidates) {
        $executable = Join-Path $root "bin\64bit\obs64.exe"
        if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { continue }
        if ((Get-Item -LiteralPath $executable).VersionInfo.FileMajorPart -lt $script:ObsMinimumMajorVersion) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $root "obs-plugins\64bit\obs-websocket.dll") -PathType Leaf)) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $root "obs-plugins\64bit\obs-browser.dll") -PathType Leaf)) { continue }
        return [System.IO.Path]::GetFullPath($root).TrimEnd('\')
    }
    return $null
}

function Save-ObsDownload([string]$TargetFile, [scriptblock]$Progress) {
    <#
        What: Download the official OBS ZIP with progress reporting.
        Why included: The installer window must stay responsive during a large download.
        Where used: Install-PewVistaObsRuntime when OBS is not installed.
        Returns: Nothing; throws when the download fails.
    #>
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $request = [System.Net.HttpWebRequest]::Create($script:ObsDownloadUrl)
    $request.UserAgent = "PewVista-Setup"
    $request.Timeout = 60000
    $request.ReadWriteTimeout = 60000
    $response = $request.GetResponse()
    try {
        $total = [double]$response.ContentLength
        $download = $response.GetResponseStream()
        $output = [System.IO.File]::Create($TargetFile)
        try {
            $buffer = New-Object byte[] (1024 * 1024)
            $received = 0.0
            while (($read = $download.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $output.Write($buffer, 0, $read)
                $received += $read
                if ($Progress -and $total -gt 0) { & $Progress ("Downloading OBS Studio... {0:N0} of {1:N0} MB" -f ($received / 1MB), ($total / 1MB)) ([int](100 * $received / $total)) }
            }
        }
        finally {
            $output.Dispose()
            $download.Dispose()
        }
    }
    finally {
        $response.Close()
    }
}

function Install-PewVistaObsRuntime {
    <#
        What: Make sure $Destination holds a working private OBS copy.
        Why included: The installer no longer ships OBS; it is reused or downloaded instead.
        Where used: install_windows_app.ps1 after program files are copied.
        Returns: "present", "copied", or "downloaded"; throws with a readable reason on failure.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Destination,
        [scriptblock]$Progress = $null
    )
    $Destination = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\')
    [void](Install-VcRedistRuntime -Progress $Progress)
    if (Test-Path -LiteralPath (Join-Path $Destination "bin\64bit\obs64.exe") -PathType Leaf) { return "present" }
    $staging = $Destination + ".staging"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    [System.IO.Directory]::CreateDirectory($staging) | Out-Null
    $result = ""
    try {
        $installedRoot = Find-InstalledObsRoot
        if ($installedRoot) {
            if ($Progress) { & $Progress "Copying the installed OBS Studio for PewVista..." 50 }
            foreach ($folder in $script:ObsRuntimeFolders) {
                & robocopy.exe (Join-Path $installedRoot $folder) (Join-Path $staging $folder) /E /NP /NFL /NDL /R:1 /W:1 | Out-Null
                if ($LASTEXITCODE -ge 8) { throw "Could not copy OBS Studio from $installedRoot." }
            }
            $result = "copied"
        }
        else {
            $zipPath = $staging + ".zip"
            try {
                if ($Progress) { & $Progress "Downloading OBS Studio..." 0 }
                Save-ObsDownload $zipPath $Progress
                if ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash -ne $script:ObsDownloadSha256) {
                    throw "The downloaded OBS Studio file failed its security check."
                }
                if ($Progress) { & $Progress "Unpacking OBS Studio..." 100 }
                Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
                $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
                try {
                    $stagingPrefix = $staging + '\'
                    foreach ($entry in $archive.Entries) {
                        $relative = $entry.FullName.Replace('/', '\')
                        if (-not $relative -or $relative.EndsWith('\') -or $relative -match '(^|\\)config(\\|$)') { continue }
                        $target = [System.IO.Path]::GetFullPath((Join-Path $staging $relative))
                        if (-not $target.StartsWith($stagingPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "The OBS Studio download contains an unsafe path." }
                        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
                        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
                    }
                }
                finally {
                    $archive.Dispose()
                }
            }
            finally {
                if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
            }
            $result = "downloaded"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $staging "bin\64bit\obs64.exe") -PathType Leaf)) { throw "OBS Studio was prepared, but obs64.exe is missing." }
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
        Move-Item -LiteralPath $staging -Destination $Destination
        return $result
    }
    catch {
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }
}

if ($TargetDirectory) {
    $ErrorActionPreference = "Stop"
    try {
        $outcome = Install-PewVistaObsRuntime -Destination $TargetDirectory -Progress { param($text, $percent) Write-Host "$text ($percent%)" }
        Write-Host "OBS Studio for PewVista: $outcome ($TargetDirectory)"
        exit 0
    }
    catch {
        Write-Host "OBS Studio could not be prepared: $($_.Exception.Message)"
        exit 1
    }
}
