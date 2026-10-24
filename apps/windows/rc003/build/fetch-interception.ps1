#requires -Version 5.1
<#
.SYNOPSIS
    Fetches the official Interception release archive, verifies its SHA-256,
    and extracts the x64 interception.dll used by the optional Interception
    injection backend plus the official command-line installer used to install
    or uninstall the kernel driver on demand.

    This is the only fetch path for both Interception binaries. Neither is
    bundled in source control. Source-only execution may still omit the
    assets and degrade explicitly to SendInput; a frozen candidate that ran
    this step bundles the verified DLL and installer under sys._MEIPASS. This
    script itself never installs the kernel driver and never runs when the
    application starts: the driver is a separate one-time administrator
    install with a reboot, only ever launched from an explicit user click in
    the settings window (see src/ovb_rc003/interception_driver_install.py)
    through Windows' own UAC elevation verb.

.PARAMETER Destination
    Where to place the verified x64 interception.dll. Defaults to the ignored
    third-party directory under build/ (not committed - see .gitignore).

.PARAMETER InstallerDestination
    Where to place the verified official install-interception.exe. Defaults to
    the same ignored third-party directory under build/.
#>

param(
    [string]$Destination = (Join-Path $PSScriptRoot "third_party\interception\interception.dll"),
    [string]$InstallerDestination = (Join-Path $PSScriptRoot "third_party\interception\install-interception.exe")
)

$ErrorActionPreference = "Stop"

$AssetName = "Interception 1.0.1 (Windows x64 client library)"
$ArchiveUrl = "https://github.com/oblitum/Interception/releases/download/v1.0.1/Interception.zip"
$ArchiveSha256 = "AD038963D6413055765128B0B931F6E765147C9916DBA79E65D872B261F9AF10"
$MemberRelativePath = "Interception\library\x64\interception.dll"
$DllSha256 = "AB88164C11B1B48488772D4C3BFAA4509D5B0AE9DBC5A691DC4F96F0260443C8"
# The official command-line installer from the same verified archive. Bundling
# the UNMODIFIED official binary (never a repacked/custom one) is what lets the
# application hash-pin and launch it later through UAC.
$InstallerMemberRelativePath = "Interception\command line installer\install-interception.exe"
$InstallerSha256 = "E137863A79DA797F08E7A137280FF2A123809044A888FD75CE9C973198915ABE"

function Get-Sha256Upper([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

$destinationDir = Split-Path -Parent $Destination
if (-not (Test-Path $destinationDir)) {
    New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
}
$installerDestinationDir = Split-Path -Parent $InstallerDestination
if (-not (Test-Path $installerDestinationDir)) {
    New-Item -ItemType Directory -Path $installerDestinationDir -Force | Out-Null
}

if ((Test-Path $Destination) -and (Test-Path $InstallerDestination)) {
    $existingHash = Get-Sha256Upper $Destination
    $existingInstallerHash = Get-Sha256Upper $InstallerDestination
    if (($existingHash -eq $DllSha256) -and ($existingInstallerHash -eq $InstallerSha256)) {
        Write-Host "[fetch-interception] $AssetName already verified at $destinationDir"
        return
    }
    Write-Warning "[fetch-interception] existing file under $destinationDir has an unexpected hash; re-downloading"
}

$workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("rc003-interception-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $workDir -Force | Out-Null
$archivePath = Join-Path $workDir "Interception.zip"
$extractDir = Join-Path $workDir "extract"

try {
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        $curlArgs = @(
            "--fail", "--location", "--silent", "--show-error",
            "--retry", "5", "--retry-delay", "2",
            "--connect-timeout", "30", "--max-time", "600",
            "--output", $archivePath, $ArchiveUrl
        )
        & curl.exe @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe exited with code $LASTEXITCODE while fetching $AssetName"
        }
    } else {
        Invoke-WebRequest -Uri $ArchiveUrl -OutFile $archivePath -UseBasicParsing -TimeoutSec 600
    }

    $archiveHash = Get-Sha256Upper $archivePath
    if ($archiveHash -ne $ArchiveSha256) {
        throw "SHA-256 mismatch for the Interception archive: expected $ArchiveSha256, got $archiveHash. Refusing to use this file."
    }

    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractDir -Force
    $memberPath = Join-Path $extractDir $MemberRelativePath
    if (-not (Test-Path $memberPath)) {
        throw "expected member $MemberRelativePath is missing from the Interception archive"
    }

    $dllHash = Get-Sha256Upper $memberPath
    if ($dllHash -ne $DllSha256) {
        throw "SHA-256 mismatch for interception.dll: expected $DllSha256, got $dllHash. Refusing to use this file."
    }

    $installerMemberPath = Join-Path $extractDir $InstallerMemberRelativePath
    if (-not (Test-Path $installerMemberPath)) {
        throw "expected member $InstallerMemberRelativePath is missing from the Interception archive"
    }

    $installerHash = Get-Sha256Upper $installerMemberPath
    if ($installerHash -ne $InstallerSha256) {
        throw "SHA-256 mismatch for install-interception.exe: expected $InstallerSha256, got $installerHash. Refusing to use this file."
    }

    Copy-Item -Force -LiteralPath $memberPath -Destination $Destination
    Copy-Item -Force -LiteralPath $installerMemberPath -Destination $InstallerDestination
    Write-Host "[fetch-interception] verified and saved $AssetName to $destinationDir"
} finally {
    if (Test-Path $workDir) {
        Remove-Item -Recurse -Force -LiteralPath $workDir -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Interception license: LGPL-3.0 for non-commercial use; see the licenses folder"
Write-Host "shipped inside the official Interception.zip for commercial terms."
Write-Host "The kernel driver (keyboard.sys/mouse.sys) is a separate one-time"
Write-Host "administrator install with a reboot. The application never installs it on"
Write-Host "startup; it only launches the bundled official install-interception.exe"
Write-Host "through UAC when the user explicitly clicks install/uninstall in settings."
