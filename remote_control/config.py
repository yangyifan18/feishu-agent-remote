from pathlib import Path
from typing import Any

from .models import RemoteConfig, RepoConfig


DEFAULT_CONFIG_PATH = Path("~/.feishu-agent-remote/config.yaml").expanduser()


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RemoteConfig:
    config_path = Path(path).expanduser()
    raw = _read_yaml_subset(config_path)
    owner = str(raw.get("owner_open_id", "")).strip()
    if not owner:
        raise ValueError("config requires owner_open_id")

    repos_raw = raw.get("repos") or {}
    if not isinstance(repos_raw, dict) or not repos_raw:
        raise ValueError("config requires at least one repo in repos")

    repos = {
        str(alias): RepoConfig(alias=str(alias), path=Path(str(repo_path)).expanduser())
        for alias, repo_path in repos_raw.items()
    }

    default_repo = str(raw.get("default_repo") or next(iter(repos)))
    if default_repo not in repos:
        raise ValueError(f"default_repo {default_repo!r} is not in repos")

    authorized = raw.get("authorized_open_ids") or [owner]
    if isinstance(authorized, str):
        authorized = [authorized]
    authorized_set = frozenset(str(user_id).strip() for user_id in authorized if str(user_id).strip())
    if owner not in authorized_set:
        authorized_set = frozenset([*authorized_set, owner])

    return RemoteConfig(
        owner_open_id=owner,
        authorized_open_ids=authorized_set,
        repos=repos,
        default_repo=default_repo,
        codex_bin=str(raw.get("codex_bin", "codex")),
        codex_profile=_optional_str(raw.get("codex_profile")),
        lark_cli_bin=str(raw.get("lark_cli_bin", "lark-cli")),
        default_sandbox=str(raw.get("default_sandbox", "workspace-write")),
        bot_names=tuple(str(name) for name in raw.get("bot_names", ["your-bot-name"])),
        config_path=config_path,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_yaml_subset(path: Path) -> dict[str, Any]:
    text = path.read_text()
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded or {}
    except ModuleNotFoundError:
        return _parse_small_yaml(text)


def _parse_small_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_kind: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        if not line.startswith(" "):
            key, value = _split_key_value(line)
            if value == "":
                current_key = key
                current_kind = None
                data[key] = None
            else:
                current_key = None
                current_kind = None
                data[key] = _coerce(value)
            continue

        if current_key is None:
            raise ValueError(f"unexpected indented config line: {raw_line!r}")

        stripped = line.strip()
        if stripped.startswith("- "):
            if current_kind is None:
                data[current_key] = []
                current_kind = "list"
            if current_kind != "list":
                raise ValueError(f"mixed config collection for {current_key}")
            data[current_key].append(_coerce(stripped[2:].strip()))
            continue

        child_key, child_value = _split_key_value(stripped)
        if current_kind is None:
            data[current_key] = {}
            current_kind = "dict"
        if current_kind != "dict":
            raise ValueError(f"mixed config collection for {current_key}")
        data[current_key][child_key] = _coerce(child_value)

    return data


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"expected key: value config line, got {line!r}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _coerce(value: str) -> str | bool | int:
    if value in ("true", "false"):
        return value == "true"
    if value.isdigit():
        return int(value)
    return value.strip("\"'")
