#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
source_root="$repo_root/navi_home"
target_root="${1:-${NAVI_HOME:-$HOME/.navi}}"

if [[ ! -d "$source_root" ]]; then
  echo "Source navi_home directory not found: $source_root" >&2
  exit 1
fi

mkdir -p "$target_root"

files=(
  "system.md"
  "compact-prompt.md"
  "memory-review-prompt.md"
  "skill-review-prompt.md"
  "SOUL.md"
  "SOUL-Chinese.md"
)

for file in "${files[@]}"; do
  source_file="$source_root/$file"
  if [[ -f "$source_file" ]]; then
    cp -f "$source_file" "$target_root/$file"
  fi
done

echo "Synced navi_home to $target_root"
echo "Skipped local-only files: config.json, .env, sessions, memories, skills, chat_history.txt, debug_system_prompt.txt"
