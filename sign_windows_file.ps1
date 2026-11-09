param(
    [Parameter(Mandatory = $true)][string]$FilePath
)

# Authenticode-sign a PewVista program so Windows shows a verified publisher instead
# of the SmartScreen "unknown publisher" warning. Configure ONE of these on the build PC:
#
#   Azure Trusted Signing (recommended):
#     PEWVISTA_SIGN_DLIB      = path to Azure.CodeSigning.Dlib.dll
#     PEWVISTA_SIGN_METADATA  = path to your metadata.json (endpoint, account, profile)
#     PEWVISTA_SIGNTOOL       = optional path to signtool.exe (Windows SDK); found automatically otherwise
#
#   Certificate installed in Windows (including USB-token certificates):
#     PEWVISTA_SIGN_THUMBPRINT = the certificate's thumbprint
#
# Without either, signing is skipped with a warning. Set PEWVISTA_REQUIRE_SIGNING=1 to
# make an unsigned build fail instead (recommended for public releases).

$ErrorActionPreference = "Stop"
$timestampServer = "http://timestamp.acs.microsoft.com"
$file = [System.IO.Path]::GetFullPath($FilePath)
if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { Write-Host "[SIGN][ERROR] File not found: $file"; exit 1 }

function Find-SignTool {
    <#
     What: Locate signtool.exe from PEWVISTA_SIGNTOOL or the Windows SDK.
     Why included: Azure Trusted Signing needs signtool, which may be installed in different places.
     Where used: Azure Trusted Signing route of this script.
     Returns: The signtool.exe path, or nothing when it is not installed.
    #>
    if ($env:PEWVISTA_SIGNTOOL -and (Test-Path -LiteralPath $env:PEWVISTA_SIGNTOOL -PathType Leaf)) { return $env:PEWVISTA_SIGNTOOL }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $found = Get-ChildItem -LiteralPath $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName | Select-Object -Last 1
    if ($found) { return $found.FullName }
    return $null
}

try {
    if ($env:PEWVISTA_SIGN_DLIB -and $env:PEWVISTA_SIGN_METADATA) {
        $signTool = Find-SignTool
        if (-not $signTool) { throw "signtool.exe was not found. Install the Windows SDK signing tools or set PEWVISTA_SIGNTOOL." }
        & $signTool sign /v /fd SHA256 /tr $timestampServer /td SHA256 /dlib $env:PEWVISTA_SIGN_DLIB /dmdf $env:PEWVISTA_SIGN_METADATA $file
        if ($LASTEXITCODE -ne 0) { throw "Azure Trusted Signing failed for $file." }
    }
    elseif ($env:PEWVISTA_SIGN_THUMBPRINT) {
        $thumbprint = $env:PEWVISTA_SIGN_THUMBPRINT.Replace(" ", "").ToUpperInvariant()
        $certificate = @(Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert | Where-Object { $_.Thumbprint -eq $thumbprint }) | Select-Object -First 1
        if (-not $certificate) { throw "No code-signing certificate with thumbprint $thumbprint is installed on this PC." }
        $timestamp = if ($env:PEWVISTA_SIGN_TIMESTAMP) { $env:PEWVISTA_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
        $result = Set-AuthenticodeSignature -FilePath $file -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer $timestamp
        if ($result.Status -ne "Valid") { throw "Signing failed for ${file}: $($result.StatusMessage)" }
    }
    else {
        if ($env:PEWVISTA_REQUIRE_SIGNING -eq "1") { throw "Code signing is required but not configured. See sign_windows_file.ps1." }
        Write-Host "[SIGN][WARNING] Code signing is not configured; $([System.IO.Path]::GetFileName($file)) is unsigned and Windows will warn users."
        exit 0
    }
    $signature = Get-AuthenticodeSignature -FilePath $file
    if ($signature.Status -ne "Valid") { throw "The signature on $file is not valid: $($signature.StatusMessage)" }
    Write-Host "[SIGN] Signed $([System.IO.Path]::GetFileName($file)) as $($signature.SignerCertificate.Subject)"
    exit 0
}
catch {
    Write-Host "[SIGN][ERROR] $($_.Exception.Message)"
    exit 1
}
