param(
    [string]$NaviHome = "$env:USERPROFILE\.navi"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$SourceRoot = Join-Path $RepoRoot "navi_home"
$TargetRoot = $NaviHome

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source navi_home directory not found: $SourceRoot"
}

New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null

$Files = @(
    "system.md",
    "compact-prompt.md",
    "memory-review-prompt.md",
    "skill-review-prompt.md",
    "SOUL.md",
    "SOUL-Chinese.md"
)

foreach ($File in $Files) {
    $Source = Join-Path $SourceRoot $File
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        Copy-Item -LiteralPath $Source -Destination (Join-Path $TargetRoot $File) -Force
    }
}

Write-Host "Synced navi_home to $TargetRoot"
Write-Host "Skipped local-only files: config.json, .env, sessions, memories, skills, chat_history.txt, debug_system_prompt.txt"
