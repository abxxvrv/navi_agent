import os
from pathlib import Path


def get_navi_home() -> Path:
    navi_home = Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()
    (navi_home / "skills").mkdir(parents=True, exist_ok=True)
    return navi_home


def get_agents_dir() -> Path:
    agents_dir = get_navi_home() / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir


def get_config_path() -> Path:
    return get_navi_home() / "config.json"


def load_navi_dotenv() -> None:
    from dotenv import load_dotenv

    env_path = get_navi_home() / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
