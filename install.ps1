$ErrorActionPreference = "Stop"

$NaviSpec = if ($env:NAVI_SPEC) {
    $env:NAVI_SPEC
} else {
    "https://github.com/abxxvrv/navi_agent/archive/refs/heads/gpt.zip"
}

Write-Host "Installing Navi from: $NaviSpec"

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    throw "python not found. Install Python 3.11+ first."
}

python -c "import sys; raise SystemExit('Python 3.11+ is required.') if sys.version_info < (3, 11) else None"

try {
    python -m pipx --version | Out-Null
} catch {
    Write-Host "pipx not found. Installing pipx..."
    python -m pip install --user pipx
    python -m pipx ensurepath
}

$PipxBin = if ($env:PIPX_BIN_DIR) {
    $env:PIPX_BIN_DIR
} else {
    Join-Path $env:USERPROFILE ".local\bin"
}
$env:Path = "$PipxBin;$env:Path"

python -m pipx install --force $NaviSpec

$Navi = Get-Command navi -ErrorAction SilentlyContinue
if ($Navi) {
    navi init
} else {
    & (Join-Path $PipxBin "navi.exe") init
}

Write-Host ""
Write-Host "Navi installed. Run: navi doctor"
