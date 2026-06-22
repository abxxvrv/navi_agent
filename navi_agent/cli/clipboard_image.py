from __future__ import annotations

from pathlib import Path


_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_supported_image_path(path: Path) -> bool:
    return path.suffix.lower() in _SUPPORTED_SUFFIXES


def has_clipboard_image() -> bool:
    try:
        from PIL import ImageGrab

        return hasattr(ImageGrab.grabclipboard(), "save")
    except Exception:
        return False


def save_clipboard_image(path: Path) -> bool:
    try:
        from PIL import ImageGrab

        image = ImageGrab.grabclipboard()
        if not hasattr(image, "save"):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return True
    except Exception:
        return False
