"""
Small colored console helpers (no external dependencies).
"""

import sys

_COLORS = {
    "green": "32",
    "yellow": "33",
    "red": "31",
    "cyan": "36",
    "bright_red": "91",
    "bright_green": "92",
}
_BOLD = "1"
_RESET = "0"


def _use_color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in __import__("os").environ


def _paint(text: str, color: str, bold: bool = False) -> str:
    if not _use_color():
        return text
    code = _COLORS.get(color, "0")
    prefix = f"\033[{_BOLD};{code}m" if bold else f"\033[{code}m"
    return f"{prefix}{text}\033[{_RESET}m"


def section(label: str, text: str) -> None:
    """Section header in the style of netlab's log.section_header()."""
    print(flush=True)
    print(_paint(f"===== [{label}] ", "cyan", bold=True) + _paint(text, "cyan", bold=True), flush=True)
    print("=" * 60, flush=True)


def info(text: str) -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    print(_paint("[OK] ", "green", bold=True) + text, flush=True)


def warn(text: str) -> None:
    print(_paint("[WARN] ", "yellow", bold=True) + text, flush=True)


def err(text: str) -> None:
    print(_paint("[ERROR] ", "red", bold=True) + text, file=sys.stderr, flush=True)


def fail(text: str) -> None:
    print(_paint("[FATAL] ", "bright_red", bold=True) + text, file=sys.stderr, flush=True)


def dim(text: str) -> str:
    if not _use_color():
        return text
    return f"\033[2m{text}\033[{_RESET}m"
