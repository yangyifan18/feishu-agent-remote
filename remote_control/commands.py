from dataclasses import dataclass


ALIASES = {
    "remote-codex": "agents",
    "recent-codex": "codex-sessions",
    "close": "detach",
    "sessions": "bindings",
    "summarize": "handoff",
}


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    raw_name: str
    raw_text: str
    is_command: bool


def parse_command(content: str, bot_names: tuple[str, ...]) -> Command:
    text = strip_bot_mention(content, bot_names)
    if not text.startswith("/"):
        return Command("message", text, "message", text, False)

    parts = text[1:].split(maxsplit=1)
    raw_name = parts[0] if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""
    name = ALIASES.get(raw_name, raw_name)
    if name == "repo":
        name = "repos" if not args else "switch-repo"
    return Command(name=name, args=args, raw_name=raw_name, raw_text=text, is_command=True)


def strip_bot_mention(text: str, bot_names: tuple[str, ...]) -> str:
    stripped = text.strip()
    for name in bot_names:
        for prefix in (f"@{name}", name):
            if stripped.startswith(prefix):
                return stripped.removeprefix(prefix).strip()
    return stripped
