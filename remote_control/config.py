from pathlib import Path
from typing import Any

from .models import RemoteConfig, RepoConfig, RuntimeConfig


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

    runtimes = _runtime_configs(raw)
    default_runtime = str(raw.get("default_runtime") or "codex").strip() or "codex"
    if default_runtime not in runtimes:
        raise ValueError(f"default_runtime {default_runtime!r} is not in runtimes")

    codex_runtime = runtimes.get("codex")
    codex_bin = codex_runtime.bin if codex_runtime else str(raw.get("codex_bin", "codex"))
    codex_profile = codex_runtime.profile if codex_runtime else _optional_str(raw.get("codex_profile"))
    default_sandbox = (codex_runtime.sandbox if codex_runtime and codex_runtime.sandbox else str(raw.get("default_sandbox", "workspace-write")))

    return RemoteConfig(
        owner_open_id=owner,
        authorized_open_ids=authorized_set,
        repos=repos,
        default_repo=default_repo,
        codex_bin=codex_bin,
        codex_profile=codex_profile,
        lark_cli_bin=str(raw.get("lark_cli_bin", "lark-cli")),
        default_sandbox=default_sandbox,
        bot_names=tuple(str(name) for name in raw.get("bot_names", ["your-bot-name"])),
        config_path=config_path,
        default_runtime=default_runtime,
        runtimes=runtimes,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_configs(raw: dict[str, Any]) -> dict[str, RuntimeConfig]:
    runtimes_raw = raw.get("runtimes")
    if not runtimes_raw:
        return {
            "codex": RuntimeConfig(
                name="codex",
                type="codex",
                bin=str(raw.get("codex_bin", "codex")),
                profile=_optional_str(raw.get("codex_profile")),
                sandbox=str(raw.get("default_sandbox", "workspace-write")),
            )
        }
    if not isinstance(runtimes_raw, dict):
        raise ValueError("runtimes must be a mapping")

    runtimes: dict[str, RuntimeConfig] = {}
    for name, value in runtimes_raw.items():
        runtime_name = str(name).strip()
        if not runtime_name:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"runtime {runtime_name!r} must be a mapping")
        runtime_type = str(value.get("type") or runtime_name).strip()
        runtimes[runtime_name] = RuntimeConfig(
            name=runtime_name,
            type=runtime_type,
            bin=str(value.get("bin") or runtime_type),
            profile=_optional_str(value.get("profile")),
            sandbox=_optional_str(value.get("sandbox")),
            permission_mode=_optional_str(value.get("permission_mode")),
            model=_optional_str(value.get("model")),
            extra_args=_tuple_str(value.get("extra_args")),
        )
    if not runtimes:
        raise ValueError("runtimes requires at least one runtime")
    return runtimes


def _tuple_str(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


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
    lines = [raw_line.split("#", 1)[0].rstrip() for raw_line in text.splitlines()]
    stack: list[tuple[int, Any]] = [(-1, data)]

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"unexpected list item in config line: {line!r}")
            parent.append(_coerce(stripped[2:].strip()))
            continue

        child_key, child_value = _split_key_value(stripped)
        if not isinstance(parent, dict):
            raise ValueError(f"unexpected mapping item in config line: {line!r}")
        if child_value == "":
            container: dict[str, Any] | list[Any] = [] if _next_content_line_is_list(lines, index, indent) else {}
            parent[child_key] = container
            stack.append((indent, container))
        else:
            parent[child_key] = _coerce(child_value)

    return data


def _next_content_line_is_list(lines: list[str], index: int, indent: int) -> bool:
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        next_indent = len(line) - len(line.lstrip(" "))
        if next_indent <= indent:
            return False
        return line.strip().startswith("- ")
    return False


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
