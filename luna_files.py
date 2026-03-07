"""
Luna file access: read, write, modify — only inside the "Luna projects" folder next to this project.
All paths are resolved and checked to prevent access outside that folder.
"""
from pathlib import Path

# "Luna projects" folder next to the folder containing this file (so it works even if project is moved)
_THIS_DIR = Path(__file__).resolve().parent
ALLOWED_BASE = (_THIS_DIR / "Luna projects").resolve()


def safe_path(relative_path: str) -> Path | None:
    """
    Resolve relative_path to an absolute path under ALLOWED_BASE.
    Returns None if the path would escape the allowed folder.
    """
    if not relative_path or not relative_path.strip():
        return None
    # Normalize: no leading slash, use forward slashes then convert
    p = relative_path.strip().replace("/", "\\").lstrip("\\")
    if ".." in p or p.startswith(".."):
        return None
    try:
        full = (ALLOWED_BASE / p).resolve()
        full.relative_to(ALLOWED_BASE.resolve())
        return full
    except (ValueError, Exception):
        return None


def read_file(relative_path: str) -> tuple[bool, str]:
    """Read file content. Returns (success, content_or_error). Only inside Luna projects."""
    path = safe_path(relative_path)
    if path is None:
        return False, "Path not allowed (only inside Luna projects)."
    if not path.is_file():
        return False, f"Not a file or not found: {relative_path}"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return True, text
    except Exception as e:
        return False, str(e)


def write_file(relative_path: str, content: str) -> tuple[bool, str]:
    """Write content to file. Creates parent dirs if needed. Only inside Luna projects.
    On success returns (True, absolute_path_str). On failure returns (False, error_message)."""
    path = safe_path(relative_path)
    if path is None:
        return False, "Path not allowed (only inside Luna projects)."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True, str(path.resolve())
    except Exception as e:
        return False, str(e)


def list_dir(relative_path: str = "") -> tuple[bool, str]:
    """List files and folders. relative_path empty = root of Luna projects."""
    path = safe_path(relative_path) if relative_path.strip() else ALLOWED_BASE
    if path is None:
        return False, "Path not allowed (only inside Luna projects)."
    if not path.is_dir():
        return False, f"Not a directory or not found: {relative_path or '.'}"
    try:
        names = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"  {'[dir]' if p.is_dir() else ''} {p.name}" for p in names]
        return True, "\n".join(lines) if lines else "(empty)"
    except Exception as e:
        return False, str(e)


def modify_file(relative_path: str, old_substring: str, new_substring: str) -> tuple[bool, str]:
    """Replace first occurrence of old_substring with new_substring in file. Only inside Luna projects."""
    ok, content = read_file(relative_path)
    if not ok:
        return False, content
    if old_substring not in content:
        return False, f"Text not found in file: {relative_path}"
    try:
        new_content = content.replace(old_substring, new_substring, 1)
        return write_file(relative_path, new_content)
    except Exception as e:
        return False, str(e)
