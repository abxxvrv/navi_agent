from __future__ import annotations

import json
import platform
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..paths import get_config_path, get_navi_home
from ..storage.history_store import HistoryStore


console = Console()

PROVIDERS: dict[str, dict[str, Any]] = {
    "mimo": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "models": {
            "mimo-v2.5-pro": {"context_window": 1048576},
            "mimo-v2.5-pro-ultraspeed": {"context_window": 1048576},
            "mimo-v2.5": {"context_window": 1048576, "multimodal": True},
        },
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "models": {
            "deepseek-v4-flash": {"context_window": 1048576},
            "deepseek-v4-pro": {"context_window": 1048576},
        },
    },
}


def _copy_tree_missing(source, target: Path) -> None:
    if target.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            _copy_tree_missing(item, destination)
        else:
            with item.open("rb") as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _prompt_provider_config(title: str, existing: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    names = list(PROVIDERS)
    console.print(f"\n[bold]{title}[/bold]")
    for index, name in enumerate(names, start=1):
        console.print(f"{index}. {name}")

    selected = ""
    while selected not in names:
        raw = typer.prompt("Select provider", default="1").strip().lstrip("\ufeff")
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            selected = names[int(raw) - 1]
        elif raw in names:
            selected = raw
        else:
            console.print("[yellow]Please choose a listed provider.[/yellow]")

    provider_defaults = PROVIDERS[selected]
    current_provider = existing.get("providers", {}).get(selected, {})
    old_key = str(current_provider.get("api_key", ""))
    prompt = "API key"
    if old_key:
        prompt = "API key (leave empty to keep existing)"
    api_key = typer.prompt(prompt, default="", hide_input=sys.stdin.isatty(), show_default=False).strip()
    if not api_key:
        api_key = old_key
    while not api_key:
        console.print("[yellow]API key is required.[/yellow]")
        api_key = typer.prompt("API key", default="", hide_input=sys.stdin.isatty(), show_default=False).strip()

    base_url = typer.prompt(
        "Base URL",
        default=str(current_provider.get("base_url") or provider_defaults["base_url"]),
    ).strip()

    models = list(provider_defaults["models"])
    default_model = str(existing.get("default_model") or models[0])
    if default_model not in models:
        default_model = models[0]

    console.print("\n[bold]Select model[/bold]")
    for index, model in enumerate(models, start=1):
        marker = " [dim](default)[/dim]" if model == default_model else ""
        console.print(f"{index}. {model}{marker}")

    selected_model = ""
    while selected_model not in models:
        raw = typer.prompt("Select model", default=str(models.index(default_model) + 1)).strip().lstrip("\ufeff")
        if raw.isdigit() and 1 <= int(raw) <= len(models):
            selected_model = models[int(raw) - 1]
        elif raw in models:
            selected_model = raw
        else:
            console.print("[yellow]Please choose a listed model.[/yellow]")

    return selected, selected_model, {
        "api_key": api_key,
        "base_url": base_url,
        "models": provider_defaults["models"],
    }


def run_init() -> None:
    navi_home = get_navi_home()
    config_path = get_config_path()
    console.print(f"Navi home: [bold]{navi_home}[/bold]")

    for name in ("skills", "sessions", "memories", "agents", "logs"):
        (navi_home / name).mkdir(parents=True, exist_ok=True)

    template_root = files("navi_agent").joinpath("templates")
    home_templates = template_root.joinpath("navi_home")
    for item in home_templates.iterdir():
        if item.is_file():
            target = navi_home / item.name
            if not target.exists():
                with item.open("rb") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

    skills_template = template_root.joinpath("skills")
    for item in skills_template.iterdir():
        if item.is_dir():
            _copy_tree_missing(item, navi_home / "skills" / item.name)

    HistoryStore.for_querying(navi_home / "history.sqlite3")

    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            console.print(f"[red]Invalid JSON in {config_path}[/red]")
            raise typer.Exit(code=1)
        if not typer.confirm("config.json already exists. Reconfigure model settings?", default=False):
            console.print("[green]Navi initialized.[/green]")
            return

    provider, model, provider_config = _prompt_provider_config("Main model", config)
    providers = dict(config.get("providers", {}))
    providers[provider] = provider_config

    compression_provider = provider
    compression_model = model
    if not typer.confirm("Use the main model for compression?", default=True):
        compression_provider, compression_model, compression_config = _prompt_provider_config("Compression model", config)
        providers[compression_provider] = compression_config

    config["default_provider"] = provider
    config["default_model"] = model
    config["compression"] = {
        "provider": compression_provider,
        "model": compression_model,
    }
    config["providers"] = providers
    config.setdefault("mcp_servers", {})

    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    console.print("[green]Navi initialized.[/green]")


def run_doctor() -> bool:
    navi_home = get_navi_home()
    config_path = get_config_path()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python", sys.version_info >= (3, 11), platform.python_version()))
    checks.append(("Navi home", navi_home.is_dir(), str(navi_home)))

    for filename in ("system.md", "compact-prompt.md", "memory-review-prompt.md", "skill-review-prompt.md"):
        path = navi_home / filename
        checks.append((filename, path.is_file(), str(path)))

    skills_dir = navi_home / "skills"
    skill_count = len(list(skills_dir.glob("*/SKILL.md"))) if skills_dir.is_dir() else 0
    checks.append(("Skills", skill_count > 0, f"{skill_count} skill(s)"))

    try:
        HistoryStore.for_querying(navi_home / "history.sqlite3")
        checks.append(("History database", True, str(navi_home / "history.sqlite3")))
    except Exception as exc:
        checks.append(("History database", False, str(exc)))

    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            checks.append(("config.json", True, str(config_path)))
        except json.JSONDecodeError as exc:
            checks.append(("config.json", False, str(exc)))
    else:
        checks.append(("config.json", False, str(config_path)))

    providers = config.get("providers", {})
    default_provider = str(config.get("default_provider", ""))
    default_model = str(config.get("default_model", ""))
    default_entry = providers.get(default_provider, {}) if isinstance(providers, dict) else {}
    default_models = default_entry.get("models", {}) if isinstance(default_entry, dict) else {}
    checks.append(("Default provider", default_provider in providers, default_provider or "missing"))
    checks.append(("Default model", default_model in default_models, default_model or "missing"))

    for provider_name, entry in providers.items() if isinstance(providers, dict) else []:
        if provider_name not in PROVIDERS:
            continue
        checks.append((f"{provider_name} API key", bool(entry.get("api_key")), "configured" if entry.get("api_key") else "missing"))
        checks.append((f"{provider_name} Base URL", bool(entry.get("base_url")), str(entry.get("base_url", ""))))
        checks.append((f"{provider_name} models", bool(entry.get("models")), ", ".join(entry.get("models", {}).keys())))

    compression = config.get("compression", {})
    compression_provider = str(compression.get("provider", "")) if isinstance(compression, dict) else ""
    compression_model = str(compression.get("model", "")) if isinstance(compression, dict) else ""
    compression_entry = providers.get(compression_provider, {}) if isinstance(providers, dict) else {}
    compression_models = compression_entry.get("models", {}) if isinstance(compression_entry, dict) else {}
    checks.append(("Compression provider", compression_provider in providers, compression_provider or "missing"))
    checks.append(("Compression model", compression_model in compression_models, compression_model or "missing"))

    table = Table(title="Navi Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for name, ok, detail in checks:
        table.add_row(name, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)
    console.print(table)
    return all(ok for _, ok, _ in checks)
