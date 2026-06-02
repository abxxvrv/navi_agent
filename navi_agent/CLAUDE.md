# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Navi Agent — a local CLI coding assistant. LangGraph agent loop + OpenAI-compatible API (DeepSeek by default). Reads/writes files, runs commands, loads skills, records session history.

## Setup & Run

```bash
pip install -e .
# .env: DEEPSEEK_API_KEY=...  DEEPSEEK_BASE_URL=https://api.deepseek.com (optional)

navi                                          # interactive chat
navi run "fix the import error"               # one-shot
navi --workspace ./proj --approval strict      # with options
```

Entry point: `navi_agent/cli:main` (console script `navi`).

## Architecture

```
cli.py  →  runtime.py  →  context_manager.py  (system prompt, AGENTS.md, skills)
                        →  tool_registry.py   (ToolSpec → OpenAI tools format)
                        →  tool.py            (list_dir, read_file, write_file, patch_file, run_command, skill_view)
                        →  session_store.py   (session metadata and index)
                        →  approval.py        (strict/normal/open risk-based approval)
```

- **runtime.py**: `AgentRuntime` owns the LangGraph graph (llm_node ↔ tool_node), tool registry, context manager, session store, approval manager. `_invoke_agent` is the single entry point for `run_task` and `run_turn`.
- **paths.py**: `get_navi_home()` → `NAVI_HOME` env or `~/.navi`. Creates `skills/` and `sessions/` on first call.

## Data Locations

| Path | Content |
|------|---------|
| `~/.navi/skills/` | Skills (`<name>/SKILL.md`) |
| `~/.navi/sessions/` | Session metadata (`meta.json`, `index.jsonl`) |
| `~/.navi/chat_history.txt` | prompt_toolkit input history |
| `<workspace>/AGENTS.md` | LLM behavioral guidelines |

## Extending

- **Add a tool**: implement callable in `tool.py`, register in `runtime.py:_register_tools()`, add approval rules in `approval.py` if needed.
- **Add a slash command**: handle in `cli.py:handle_slash_command()`, add to `SLASH_COMMANDS` list.
- **Add a key binding**: add in `cli.py:create_prompt_key_bindings()`.

## Coding Guidelines

**If the user didn't ask for code, don't write any.** Answer questions, explain concepts, discuss approaches — but only produce code when explicitly requested.

**Don't assume. Don't hide confusion. Surface tradeoffs.**
- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

**Minimum code that solves the problem. Nothing speculative.**
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

**Touch only what you must. Clean up only your own mess.**
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.
- The test: every changed line should trace directly to the user's request.

**Define success criteria. Loop until verified.**
- "Add validation" → write tests for invalid inputs, then make them pass.
- "Fix the bug" → write a test that reproduces it, then make it pass.
- "Refactor X" → ensure tests pass before and after.
- For multi-step tasks, state a brief plan with verification steps.
