import os
from pathlib import Path

CANONICAL_DIR = Path("~/.feishu-agent-remote").expanduser()
LEGACY_DIR = Path("~/.yyf-codex").expanduser()
CANONICAL_CONFIG = CANONICAL_DIR / "config.yaml"
CANONICAL_STATE = CANONICAL_DIR / "state.sqlite"
LEGACY_CONFIG = LEGACY_DIR / "config.yaml"
LEGACY_STATE = LEGACY_DIR / "state.sqlite"


def resolve_config_path() -> Path:
    explicit = os.getenv("FAR_CONFIG") or os.getenv("YYF_CODEX_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    if CANONICAL_CONFIG.exists() or not LEGACY_CONFIG.exists():
        return CANONICAL_CONFIG
    return LEGACY_CONFIG


def resolve_state_path() -> Path:
    explicit = os.getenv("FAR_STATE") or os.getenv("YYF_CODEX_STATE")
    if explicit:
        return Path(explicit).expanduser()
    if CANONICAL_STATE.exists() or not LEGACY_STATE.exists():
        return CANONICAL_STATE
    return LEGACY_STATE


def is_legacy_path(path: str | Path) -> bool:
    try:
        return Path(path).expanduser().resolve().is_relative_to(LEGACY_DIR.resolve())
    except OSError:
        return False
