param(
    [string]$PackageDirectory = "",
    [string]$ExtractedDirectory = ""
)

$ErrorActionPreference = "Stop"
if (-not $PackageDirectory) { $PackageDirectory = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("TheLightSetup-" + [guid]::NewGuid().ToString("N"))

try {
    if ($ExtractedDirectory) {
        $sourceDirectory = Join-Path ([System.IO.Path]::GetFullPath($ExtractedDirectory)) "PewVista"
        if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) { $sourceDirectory = Join-Path ([System.IO.Path]::GetFullPath($ExtractedDirectory)) "The Light Ministries" }
    }
    else {
        $archivePath = Join-Path $PackageDirectory "PewVista-Install-Windows-x64.zip"
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            $archivePath = Join-Path $PackageDirectory "The Light Ministries-Install-Windows-x64.zip"
        }
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) { throw "The installation files could not be found beside the setup program." }
        [System.IO.Directory]::CreateDirectory($temporaryDirectory) | Out-Null
        Expand-Archive -LiteralPath $archivePath -DestinationPath $temporaryDirectory -Force
        $sourceDirectory = Join-Path $temporaryDirectory "PewVista"
        if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) { $sourceDirectory = Join-Path $temporaryDirectory "The Light Ministries" }
    }
    $manifestPath = Join-Path $sourceDirectory "release.json"
    $installerPath = Join-Path $sourceDirectory "install_windows_app.ps1"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "The installation archive is incomplete or invalid."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.application -ne "The Light Ministries" -or $manifest.package_type -ne "installation") {
        throw "This setup file does not contain a valid Installation package."
    }

    $arguments = @("-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $installerPath), "-SourceDirectory", ('"{0}"' -f $sourceDirectory), "-Gui")
    $installer = Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -ArgumentList $arguments -Wait -PassThru
    exit $installer.ExitCode
}
catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "PewVista Setup", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    exit 1
}
finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        try { Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force }
        catch { }
    }
}