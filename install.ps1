$ErrorActionPreference = "Stop"

$NaviSpec = if ($env:NAVI_SPEC) {
    $env:NAVI_SPEC
} else {
    "navi_agent @ https://github.com/abxxvrv/navi_agent/archive/refs/heads/gpt.zip"
}
$NaviPython = if ($env:NAVI_PYTHON) { $env:NAVI_PYTHON } else { ">=3.11" }

Write-Host "Installing Navi from: $NaviSpec"

# Ensure uv is available. The official installer drops a standalone binary and
# never touches a system Python, so no Python needs to be installed first.
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    Write-Host "uv not found. Installing uv..."
    irm https://astral.sh/uv/install.ps1 | iex
}

# uv installs into %USERPROFILE%\.local\bin; make it visible to this shell.
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $UvBin = "uv"
} else {
    $UvBin = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
}

# Install Navi. uv provisions a matching interpreter, downloading a managed
# CPython when nothing on the system satisfies $NaviPython.
& $UvBin tool install --force --python $NaviPython $NaviSpec
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Persist the tool bin directory on PATH for future shells.
& $UvBin tool update-shell

$ToolBin = if ($env:UV_TOOL_BIN_DIR) {
    $env:UV_TOOL_BIN_DIR
} else {
    Join-Path $env:USERPROFILE ".local\bin"
}
$env:Path = "$ToolBin;$env:Path"

$Navi = Get-Command navi -ErrorAction SilentlyContinue
if ($Navi) {
    navi init
} else {
    & (Join-Path $ToolBin "navi.exe") init
}

Write-Host ""
Write-Host "Navi installed. Run: navi doctor"
