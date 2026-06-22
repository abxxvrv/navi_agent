---
name: skill-installer
description: Install Navi skills into $NAVI_HOME/skills from a curated list or a GitHub repo path. Use when a user asks to list installable skills, install a curated skill, or install a skill from another repo (including private repos).
metadata:
  short-description: Install curated skills from openai/skills or other repos
---

# Skill Installer

Helps install skills. By default these are from https://github.com/openai/skills/tree/main/skills/.curated, but users can also provide other locations. Experimental skills live in https://github.com/openai/skills/tree/main/skills/.experimental and can be installed the same way.

Use the helper scripts based on the task:
- List skills when the user asks what is available, or if they use this skill without specifying what to do. Default listing is `.curated`; pass `--path skills/.experimental` for experimental skills.
- Install from the curated list when the user provides a skill name.
- Install from another repo when the user provides a GitHub repo/path (including private repos).

Install skills with the helper scripts.

## Communication

When listing skills, output approximately as follows, depending on the context of the user's request. If they ask about experimental skills, list from `.experimental` instead of `.curated` and label the source accordingly:
"""
Skills from {repo}:
1. skill-1
2. skill-2 (already installed)
3. ...
Which ones would you like installed?
"""

After installing a skill, tell the user it is installed under `$NAVI_HOME/skills` and can be checked with `/skills`.

## Scripts

All of these scripts use network, so when running in the sandbox, request escalation when running them.

- `scripts/list-skills.py` (prints skills list with installed annotations)
- `scripts/list-skills.py --format json`
- Example (experimental list): `scripts/list-skills.py --path skills/.experimental`
- `scripts/install-skill-from-github.py --repo <owner>/<repo> --path <path/to/skill> [<path/to/skill> ...]`
- `scripts/install-skill-from-github.py --url https://github.com/<owner>/<repo>/tree/<ref>/<path>`
- `scripts/install-skill-from-github.py --url https://github.com/<owner>/<repo>` when the repo root itself contains `SKILL.md`
- Example (experimental skill): `scripts/install-skill-from-github.py --repo openai/skills --path skills/.experimental/<skill-name>`

## Behavior and Options

- Defaults to direct download for public GitHub repos.
- If download fails with auth/permission errors, falls back to git sparse checkout.
- Aborts if the destination skill directory already exists.
- Installs into `$NAVI_HOME/skills/<skill-name>` (defaults to `~/.navi/skills`).
- Multiple `--path` values install multiple skills in one run. Each installed directory is named from the `name` field in that skill's `SKILL.md`.
- If `--name` is supplied, it must exactly match the `name` field in `SKILL.md`.
- Options: `--ref <ref>` (default `main`), `--dest <path>`, `--method auto|download|git`.

## External Skills via `npx skills` (OpenAI Skills CLI)

A separate ecosystem from the internal `install-skill-from-github.py` script. Uses `npm install -g skills`.

### Installation Pattern

```bash
# 1. Install the skills CLI (if not already installed)
npm install -g skills@latest

# 2. Install the skill (non-interactive)
skills add https://github.com/<owner>/<repo> --skill <skill-name> --agents "Claude Code" -y

# 3. Or install to all agents
skills add <owner>/<repo> --skill <skill-name> -g -y
```

### Key Flags

| Flag | Purpose |
|------|---------|
| `--skill <name>` | Select specific skill from repo (when repo contains multiple) |
| `--agents "Agent1,Agent2"` | Target specific agents for installation |
| `-g` | Install globally to all supported agents |
| `-y` | Skip confirmation prompts |

### Where Skills Are Installed

The Skills CLI uses its **own** directory structure, completely separate from `$NAVI_HOME/skills/`:

| Scope | Directory | Lock File |
|-------|-----------|-----------|
| Project-level | `<project>/.agents/skills/` | `<project>/skills-lock.json` |
| Global (`-g`) | `~/.agents/skills/` | `~/.agents/.skill-lock.json` |

### Lock File (`skills-lock.json`)

The Skills CLI generates a lock file to track installed skills:

```json
{
  "version": 1,
  "skills": {
    "skill-name": {
      "source": "owner/repo",
      "sourceType": "github",
      "skillPath": "skills/skill-name/SKILL.md",
      "computedHash": "<sha256>"
    }
  }
}
```

- `computedHash`: SHA-256 of SKILL.md content; used for integrity checking and detecting local modifications.
- **Navi does NOT read this file.** Navi only scans `$NAVI_HOME/skills/` for directories containing `SKILL.md`. The lock file is purely for the Skills CLI's own tracking.
- Safe to delete if not using the Skills CLI's `experimental_install` / `update` commands.

### Relationship to Navi

- Skills CLI installs to `.agents/skills/` — Navi does **not** load from here by default.
- To make Skills CLI-installed skills visible to Navi: copy or symlink from `.agents/skills/<name>/` into `$NAVI_HOME/skills/`.
- Or use the internal `install-skill-from-github.py` script instead, which installs directly into `$NAVI_HOME/skills/`.

### Common Issues

1. **Interactive prompts timeout**: Always use `-y` flag and specify `--agents` to avoid interactive selection
2. **npm package not found**: Run `npm install -g skills@latest` first
3. **Skill not visible to Navi**: Installed to `.agents/skills/`, not `$NAVI_HOME/skills/` — copy or symlink

### Post-Install Verification

Skills often depend on external npm/pip packages. After installing:

```bash
# 1. Check SKILL.md for dependencies
cat .agents/skills/<skill-name>/SKILL.md

# 2. Install required packages
npm install -g <package>@latest
# or
pip install <package>

# 3. Verify the tool works
<tool-command> --version
```

### Environment Variables

Some skills require API keys. Add them to `~/.navi/.env`:

```bash
echo 'ENV_VAR=value' >> ~/.navi/.env
```

## Post-Install Verification (General)

Skills often depend on external tools (npm packages, Python packages, CLI binaries). After installing a skill:

1. **Read the skill's SKILL.md** to identify required dependencies
2. **Verify dependencies are installed** by running version checks or test commands
3. **Install missing dependencies** before reporting success to the user

Example workflow for a skill requiring npm:
```bash
# 1. Install skill
python scripts/install-skill-from-github.py --repo owner/repo --path skills/skill-name

# 2. Read skill to find dependencies
cat ~/.navi/skills/skill-name/SKILL.md

# 3. Verify/install dependencies
npm install -g required-package@latest
playwright-cli --version  # verify it works
```

This ensures the skill is functional immediately after installation, not just present on disk.

## Notes

- Curated listing is fetched from `https://github.com/openai/skills/tree/main/skills/.curated` via the GitHub API. If it is unavailable, explain the error and exit.
- Private GitHub repos can be accessed via existing git credentials or optional `GITHUB_TOKEN`/`GH_TOKEN` for download.
- Git fallback tries HTTPS first, then SSH.
- The skills at https://github.com/openai/skills/tree/main/skills/.system are preinstalled, so no need to help users install those. If they ask, just explain this. If they insist, you can download and overwrite.
- Installed annotations come from `$NAVI_HOME/skills`.
