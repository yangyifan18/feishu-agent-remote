from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoConfig:
    alias: str
    path: Path


@dataclass(frozen=True)
class RuntimeConfig:
    name: str
    type: str
    bin: str
    profile: str | None = None
    sandbox: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    description: str
    prompt: str
    runtime: str | None = None


@dataclass(frozen=True)
class RemoteConfig:
    owner_open_id: str
    authorized_open_ids: frozenset[str]
    repos: dict[str, RepoConfig]
    default_repo: str
    codex_bin: str = "codex"
    codex_profile: str | None = None
    lark_cli_bin: str = "lark-cli"
    default_sandbox: str = "workspace-write"
    bot_names: tuple[str, ...] = ("your-bot-name",)
    config_path: Path | None = None
    default_runtime: str = "codex"
    runtimes: dict[str, RuntimeConfig] | None = None
    agent_templates: dict[str, AgentTemplate] | None = None
    workspace_id: str = "default"
    display_name: str | None = None
    lark_cli_args: tuple[str, ...] = ()
    features: "RemoteFeatures" | None = None
    default_workspace: str = "default"


@dataclass(frozen=True)
class RemoteFeatures:
    progress_replies: bool = False
    card_replies: bool = False
    runtime_streaming: bool = False
    multi_workspace: bool = False
    card_update_min_interval_seconds: float = 5.0


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    content: str
    thread_id: str | None = None
    root_message_id: str | None = None
    workspace_id: str = "default"

    @property
    def thread_key(self) -> str:
        return self.thread_id or self.root_message_id or self.message_id


@dataclass(frozen=True)
class SessionBinding:
    chat_id: str
    thread_key: str
    repo_alias: str
    repo_path: Path
    codex_session_id: str
    status: str
    agent_id: str | None = None
    title: str | None = None
    last_run_id: str | None = None
    last_error: str | None = None
    runtime: str = "codex"
    runtime_session_id: str | None = None
    workspace_id: str = "default"

    def __post_init__(self) -> None:
        if self.runtime_session_id is None:
            object.__setattr__(self, "runtime_session_id", self.codex_session_id)


@dataclass(frozen=True)
class Confirmation:
    id: str
    action: str
    requester_id: str
    chat_id: str
    message_id: str
    payload: dict
    status: str
    workspace_id: str = "default"


@dataclass(frozen=True)
class CodexRunResult:
    session_id: str | None
    summary: str
    status: str = "succeeded"


RuntimeRunResult = CodexRunResult


@dataclass(frozen=True)
class CodexSessionMeta:
    session_id: str
    cwd: Path
    timestamp: str
    source: str
    path: Path
    runtime: str = "codex"


RuntimeSessionMeta = CodexSessionMeta


@dataclass(frozen=True)
class RemoteAgent:
    id: str
    title: str
    repo_alias: str
    repo_path: Path
    codex_session_id: str
    status: str
    chat_id: str
    thread_key: str
    created_at: str
    updated_at: str
    last_run_id: str | None = None
    last_error: str | None = None
    runtime: str = "codex"
    runtime_session_id: str | None = None
    workspace_id: str = "default"

    def __post_init__(self) -> None:
        if self.runtime_session_id is None:
            object.__setattr__(self, "runtime_session_id", self.codex_session_id)


@dataclass(frozen=True)
class RunRecord:
    id: str
    agent_id: str | None
    chat_id: str
    message_id: str
    repo_alias: str
    repo_path: Path
    codex_session_id: str | None
    prompt: str
    status: str
    pid: int | None
    summary: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    runtime: str = "codex"
    runtime_session_id: str | None = None
    thread_key: str | None = None
    workspace_id: str = "default"

    def __post_init__(self) -> None:
        if self.runtime_session_id is None:
            object.__setattr__(self, "runtime_session_id", self.codex_session_id)
