import os
from pathlib import Path


def get_navi_home() -> Path:
    navi_home = Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()
    (navi_home / "skills").mkdir(parents=True, exist_ok=True)
    (navi_home / "sessions").mkdir(parents=True, exist_ok=True)
    return navi_home
