---
name: gh-init-push
description: Initialize a local directory as a Git repo and push it to a new GitHub repository. Use when the user asks to "push to GitHub", "create a repo and push", "init and publish", or similar. Handles .gitignore, missing git config, and one-command gh repo create.
---

# Git Init + GitHub Push

One-shot workflow: local directory → Git repo → GitHub.

## Steps

```bash
# 1. Enter the target directory
cd <dir>

# 2. Ensure git identity exists (check first, set if missing)
git config user.name || git config user.name "<gh-username>"
git config user.email || git config user.email "<gh-username>@users.noreply.github.com"
# Get gh username: gh api user --jq '.login'

# 3. Create .gitignore if absent (common patterns)
# Python: __pycache__/ *.pyc venv/ .pytest_cache/ *.egg-info/
# Node:  node_modules/ dist/ .env
# General: .DS_Store *.log

# 4. Stage and commit
git add .
git status  # verify no unwanted files
git commit -m "feat: initial commit"

# 5. Create GitHub repo and push in one command
gh repo create <repo-name> --public --source=. --push
# For private: gh repo create <repo-name> --private --source=. --push
```

## Gotchas

- **git config missing**: WSL / fresh installs often lack user.name/email. Always check before commit.
- **Staged junk**: If `.pyc`, `node_modules/`, etc. get staged, `git rm -r --cached <path>` then re-add after fixing `.gitignore`.
- **Symlinks on Windows**: Git may warn about symlink permissions. Usually safe to ignore for cloned repos.
- **Default branch**: `gh repo create` uses the local branch name. Rename to `main` first if desired: `git branch -m main`.
