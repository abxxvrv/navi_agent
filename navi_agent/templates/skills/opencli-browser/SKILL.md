---
name: opencli-browser
description: >
  Use OpenCLI to access authenticated web platforms by reusing Chrome browser login state.
  Activate when user wants to: search/read content from platforms like XiaoHongShu, Twitter/X,
  Reddit, Bilibili, LinkedIn, or any website that requires login.
  OpenCLI connects to Chrome via a browser extension and reuses existing login sessions —
  no cookies or API keys needed.
  NOT for: form filling, browser automation (use playwright-form-filler instead),
  platforms with dedicated skills already installed.
---

# OpenCLI Browser Access

Access authenticated web platforms by reusing Chrome's login state via OpenCLI.

## Prerequisites

```bash
npm install -g @jackwener/opencli
```

Install Chrome extension from Chrome Web Store (search "OpenCLI").

Chrome must be open with the user logged into the target platform.

## Core Commands

```bash
opencli <platform> <command> [options] -f yaml   # Structured output
opencli list                                      # List all available platforms/commands
opencli --version                                 # Check version
```

**Always use `-f yaml` or `-f json`** for structured output that's easier to parse.

## Platform Quick Reference

### XiaoHongShu (小红书)
```bash
opencli xiaohongshu search "query" -f yaml        # Search notes
opencli xiaohongshu note "URL" -f yaml             # Read note content
opencli xiaohongshu comments NOTE_ID -f yaml       # Read comments
opencli xiaohongshu feed -f yaml                   # Homepage feed
opencli xiaohongshu user USER_ID -f yaml           # User profile
```

### Twitter/X
```bash
opencli twitter search "query" -f yaml             # Search tweets
opencli twitter tweet "URL" -f yaml                # Read tweet
opencli twitter article "URL" -f yaml              # Read long article
opencli twitter user-posts @username -f yaml       # User timeline
opencli twitter feed -f yaml                       # Home timeline
```

### Reddit
```bash
opencli reddit search "query" -f yaml
opencli reddit read POST_ID -f yaml
opencli reddit subreddit NAME -f yaml
opencli reddit hot -f yaml
```

### Bilibili
```bash
opencli bilibili subtitle BVxxx                    # Get subtitles
```

## Common Patterns

### Search → Read Workflow
```bash
# 1. Search to get URLs/IDs
opencli xiaohongshu search "AI工具" -f yaml

# 2. Read specific content using URL from results
opencli xiaohongshu note "https://www.xiaohongshu.com/..." -f yaml
```

### Error: AUTH_REQUIRED
User is not logged into the platform in Chrome. Ask user to:
1. Open Chrome
2. Navigate to the platform
3. Log in
Then retry the command.

## Limitations

- **Read-only**: Cannot post, comment, like, or perform write operations
- **Chrome must be open**: Extension only works when Chrome is running
- **Single session**: Uses whatever account is currently logged in
- **Rate limits**: High-frequency requests may trigger platform anti-bot measures
- **xsec_token**: XiaoHongShu requires tokens from search results; cannot use bare note IDs

## When NOT to Use OpenCLI

- **Form filling / write operations** → Use `playwright-form-filler` instead
- **Platform has a dedicated CLI** (e.g., `bili search` for Bilibili search) → Use that first
- **No login needed** → Use `curl` + Jina Reader or platform's public API
