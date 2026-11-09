param(
    [Parameter(Mandatory = $true)][string]$BiblesDirectory,
    [string]$ListPath = "",
    [switch]$CheckOnly
)

# Remove Bibles that may not be redistributed from a release payload, or (with
# -CheckOnly) fail when any are present so a package is never built with them.

$ErrorActionPreference = "Stop"
if (-not $ListPath) { $ListPath = Join-Path $PSScriptRoot "Bibles\licenses\NOT_FOR_REDISTRIBUTION.txt" }
if (-not (Test-Path -LiteralPath $ListPath -PathType Leaf)) { Write-Host "[ERROR] Bible redistribution list not found: $ListPath"; exit 1 }
if (-not (Test-Path -LiteralPath $BiblesDirectory -PathType Container)) { exit 0 }

$stems = @(Get-Content -LiteralPath $ListPath -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith("#") })
$found = New-Object System.Collections.Generic.List[string]
foreach ($stem in $stems) {
    $cachePrefix = ([regex]::Replace($stem, '[^A-Za-z0-9]+', '_')).Trim('_') + '_'
    $candidates = @()
    $candidates += @(Get-ChildItem -LiteralPath $BiblesDirectory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name.StartsWith($stem, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($folder in @("licenses", ".read_bible_cache")) {
        $root = Join-Path $BiblesDirectory $folder
        if (Test-Path -LiteralPath $root -PathType Container) {
            $candidates += @(Get-ChildItem -LiteralPath $root -Force | Where-Object {
                ($_.Name.StartsWith($stem, [StringComparison]::OrdinalIgnoreCase) -and $_.Name -ne "NOT_FOR_REDISTRIBUTION.txt") -or
                ($folder -eq ".read_bible_cache" -and $_.Name.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase))
            })
        }
    }
    foreach ($item in $candidates) {
        $found.Add($item.FullName)
        if (-not $CheckOnly) { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
    }
}
if ($CheckOnly -and $found.Count -gt 0) {
    Write-Host "[ERROR] These Bibles may not be redistributed but are in the release payload:"
    $found | ForEach-Object { Write-Host "  $_" }
    exit 1
}
if (-not $CheckOnly -and $found.Count -gt 0) { Write-Host "Removed $($found.Count) restricted Bible item(s) from the release payload." }
exit 0
