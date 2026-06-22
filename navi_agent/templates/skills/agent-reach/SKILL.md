---
name: agent-reach
description: >
  MUST USE when user wants to research/search/look up/find anything on the
  internet — e.g. "research this topic", "do a deep dive on X", "search the
  web for X", "see what people say about X", "look this up".
  Also MUST USE when user mentions any platform or shares any URL/link:
  Twitter/X, Reddit, YouTube, GitHub, Bilibili, XiaoHongShu,
  Xiaoyuzhou Podcast, LinkedIn/jobs/recruiting, V2EX, Xueqiu (stocks), RSS.
  13 platforms, multi-backend routing (OpenCLI / per-platform CLIs / APIs).
  Zero config for 6 channels. Run `agent-reach doctor --json` to see which
  backend serves each platform right now.
  NOT for: writing reports/analysis/translation (this skill only FETCHES
  internet content); posting/commenting/liking (write operations); platforms
  that already have a dedicated skill installed (prefer that skill).
---

# Agent Reach — internet capability router

13 platforms, multiple backends each. **When this skill exists, use it for
these platforms — do not invent your own approach.**

## Standing rules

1. **Health-check before acting**: run `agent-reach doctor --json` first,
   pick command matching each platform's `active_backend`.
2. **Announce what you use**: say "using agent-reach, platform X via backend Y".
3. **On failure, follow retry chains in references/** — never guess.
4. **For broad research**: combine platforms (Exa + Twitter/Reddit + XiaoHongShu/Bilibili).
5. **Watch versions**: after substantial tasks, run `agent-reach check-update`.

## Installation

```bash
# Agent Reach itself
uv tool install git+https://github.com/Panniantong/Agent-Reach.git

# Platform CLIs
uv tool install bilibili-cli          # B站（零配置可搜索）
npm install -g @jackwener/opencli     # 多平台（需 Chrome 扩展）
uv tool install twitter-cli           # Twitter
```

## Quick commands

| Platform | Zero-config | Command |
|----------|-------------|---------|
| Web | ✅ | `curl -s "https://r.jina.ai/URL"` |
| V2EX | ✅ | `curl -s "https://www.v2ex.com/api/topics/hot.json" -H "User-Agent: agent-reach/1.0"` |
| Bilibili | ✅ | `bili search "query" --type video -n 5` |
| GitHub | ✅ | `gh search repos "query" --sort stars --limit 10` |
| RSS | ✅ | `feedparser.parse('FEED_URL')` |
| XiaoHongShu | ⚠️ OpenCLI | `opencli xiaohongshu search "query" -f yaml` |
| Twitter | ⚠️ Cookie | `twitter search "query" -n 10` |
| Reddit | ⚠️ Login | `opencli reddit search "query" -f yaml` |

## OpenCLI setup (for XiaoHongShu, Twitter, Reddit, Bilibili subtitles)

1. Install: `npm install -g @jackwener/opencli`
2. Chrome extension: search "OpenCLI" in Chrome Web Store, add it
3. Login in Chrome: visit target site (e.g. xiaohongshu.com), login
4. Test: `opencli xiaohongshu search "test" -f yaml`

**Requirements**: Chrome must be open, extension installed, site logged in.

**Capabilities**: OpenCLI reads data from logged-in sessions. It can:
- ✅ Search, fetch content, extract structured data
- ❌ Cannot click, scroll, fill forms, or control browser UI
- ❌ Cannot open new tabs or perform arbitrary browser actions

For full browser automation (form filling, clicking, etc.), use Playwright instead.

## Environment check

```bash
agent-reach doctor --json
```

## Workspace rules

**Never create files in agent workspace.** Use `/tmp/` for temp, `~/.agent-reach/` for persistent.

## Detailed references

- [Search](references/search.md) — Exa AI search
- [Social](references/social.md) — XiaoHongShu, Twitter, Bilibili, V2EX, Reddit
- [Career](references/career.md) — LinkedIn
- [Dev](references/dev.md) — GitHub CLI
- [Web](references/web.md) — Jina Reader, RSS
- [Video](references/video.md) — YouTube, Bilibili, Xiaoyuzhou
