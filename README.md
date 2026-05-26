# Navi Agent

Navi Agent is a local command-line coding agent for navigating projects, reading
and editing files, running short verification commands, loading skills, and
keeping lightweight session history.

## Features

- Interactive `navi` chat mode with slash commands.
- File tools for listing, reading, writing, and patching project files.
- Command execution tool for short, non-interactive verification commands.
- Skill loading from the bundled `navi_agent/skills` directory.
- Session storage under `.light_agent/sessions`.
- DeepSeek-compatible OpenAI client configuration.

## Requirements

- Python 3.11 or newer
- A DeepSeek API key

## Setup

Create and activate a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Install the project in editable mode:

```powershell
pip install -e .
```

Create a `.env` file with your API key:

```env
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`DEEPSEEK_BASE_URL` is optional. If omitted, Navi uses
`https://api.deepseek.com`.

## Usage

Start the interactive CLI:

```powershell
navi
```

Run against a specific workspace:

```powershell
navi --workspace E:\path\to\project
```

Useful slash commands:

```text
/help      Show help
/tools     Show available tools
/skills    Show available skills
/sessions  Show recent sessions
/clear     Clear the screen
/exit      Exit Navi
```

## Project Layout

```text
navi_agent/
  cli.py              CLI entry point
  runtime.py          Agent runtime and tool loop
  tool.py             Local file, command, skill, and history tools
  session_store.py    Session persistence and search
  context_manager.py  System and environment prompt builder
  skills/             Bundled skills loaded by Navi
```

## Notes

Navi stores local run history in `.light_agent/sessions`. Secrets, local session
data, virtual environments, caches, and build outputs are intentionally ignored
by git.
