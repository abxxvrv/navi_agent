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

python -c "import sys; sys.exit('Python 3.11+ is required.') if sys.version_info < (3, 11) else sys.exit(0)"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

python -m pipx --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "pipx not found. Installing pipx..."
    python -m pip install --user pipx
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    python -m pipx ensurepath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$PipxBin = if ($env:PIPX_BIN_DIR) {
    $env:PIPX_BIN_DIR
} else {
    Join-Path $env:USERPROFILE ".local\bin"
}
$env:Path = "$PipxBin;$env:Path"

python -m pipx install --force $NaviSpec
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$Navi = Get-Command navi -ErrorAction SilentlyContinue
if ($Navi) {
    navi init
} else {
    & (Join-Path $PipxBin "navi.exe") init
}

Write-Host ""
Write-Host "Navi installed. Run: navi doctor"
