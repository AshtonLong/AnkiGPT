<#
.SYNOPSIS
  Freeze the AnkiGPT Desktop backend: clean venv -> pip install -> PyInstaller -> self-check.

.DESCRIPTION
  Builds desktop-app\backend-dist\ankigpt-backend\ankigpt-backend.exe, the folder the
  installer ships as resources\backend. See desktop-app\SPEC.md section 8.1.

  The virtual environment is made from scratch on every run and holds only the root
  requirements.txt plus desktop-app\backend\requirements.txt, so nothing from a
  development environment (Playwright, test tools) can end up in the bundle. Exact
  versions come from desktop-app\backend\constraints.txt when that file exists.

.PARAMETER Python
  The Python 3.12 to build with. Defaults to the py launcher's 3.12, then `python`.

.PARAMETER UpdateLock
  Ignore constraints.txt, install the newest versions the requirements allow, and
  rewrite constraints.txt from what was installed. Do this to move to newer packages.

.PARAMETER BuildRoot
  Where the scratch venv and PyInstaller's work folder go. Defaults to backend-dist.
  Point it outside the checkout if the checkout lives in a synced folder (OneDrive).

.PARAMETER SkipSmoke
  Skip the last step, which starts the frozen backend for real (needs Node.js).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File desktop-app\scripts\build-backend.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$UpdateLock,
    [string]$BuildRoot = "",
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$desktop = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $desktop
$dist = Join-Path $desktop "backend-dist"
if (-not $BuildRoot) { $BuildRoot = $dist }
$venv = Join-Path $BuildRoot "venv"
$work = Join-Path $BuildRoot "work"
$constraints = Join-Path $desktop "backend\constraints.txt"
$spec = Join-Path $desktop "backend\ankigpt-backend.spec"
$bundle = Join-Path $dist "ankigpt-backend"
$exe = Join-Path $bundle "ankigpt-backend.exe"

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

# Native commands signal failure through their exit code, not a PowerShell error.
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." } }

# ---------------------------------------------------------------- find Python 3.12
if ($Python) {
    $base = @($Python)
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $base = @("py", "-3.12")
} else {
    $base = @("python")
}
$baseExe = $base[0]
$baseArgs = @($base | Select-Object -Skip 1)
$found = & $baseExe @baseArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
Check "Finding Python"
if ($found -ne "3.12") {
    throw "The backend is built with Python 3.12, the version the app is developed and tested on. Found $found. Pass -Python <path to python 3.12>."
}

# ---------------------------------------------------------------- clean environment
Step "Creating a clean virtual environment in $venv"
foreach ($stale in @($venv, $work, $bundle)) {
    if (Test-Path $stale) { Remove-Item -Recurse -Force $stale }
}
New-Item -ItemType Directory -Force $BuildRoot | Out-Null
& $baseExe @baseArgs -m venv $venv
Check "Creating the virtual environment"
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --disable-pip-version-check --quiet --upgrade pip
Check "Upgrading pip"

Step "Installing requirements"
$install = @(
    "-m", "pip", "install", "--disable-pip-version-check",
    "-r", (Join-Path $root "requirements.txt"),
    "-r", (Join-Path $desktop "backend\requirements.txt")
)
if ((Test-Path $constraints) -and -not $UpdateLock) {
    Write-Host "Using pinned versions from backend\constraints.txt"
    $install += @("-c", $constraints)
} else {
    Write-Host "No pinned versions in use: installing the newest the requirements allow"
}
& $py @install
Check "Installing requirements"
if ($UpdateLock -or -not (Test-Path $constraints)) {
    $header = @(
        "# Exact versions the desktop backend is built with. Written by",
        "# desktop-app/scripts/build-backend.ps1 -UpdateLock; do not edit by hand."
    )
    $frozen = & $py -m pip freeze --disable-pip-version-check
    Check "Recording installed versions"
    # UTF-8 without a byte-order mark, which pip would otherwise read as part of the first line.
    [System.IO.File]::WriteAllLines($constraints, [string[]]($header + $frozen), (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Wrote backend\constraints.txt"
}

# ---------------------------------------------------------------- freeze
Step "Freezing the backend with PyInstaller"
& $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $dist --workpath $work $spec
Check "PyInstaller"
if (-not (Test-Path $exe)) { throw "PyInstaller finished but $exe is missing." }
$size = (Get-ChildItem -Recurse -File $bundle | Measure-Object -Property Length -Sum).Sum
Write-Host ("Bundle: {0:N0} MB in {1}" -f ($size / 1MB), $bundle)

# ---------------------------------------------------------------- prove it works
Step "Running the self-check inside the frozen program"
& $exe --self-check
Check "The self-check"

if ($SkipSmoke) {
    Write-Host "`nSkipped the smoke test."
} else {
    Step "Starting the frozen backend for real"
    & node (Join-Path $PSScriptRoot "smoke-backend.js") $exe
    Check "The smoke test"
}

Write-Host "`nBackend ready: $exe" -ForegroundColor Green
