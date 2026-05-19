from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import load_workspace_configs
from .lark_gateway import LarkGateway
from .models import IncomingMessage, RemoteConfig
from .router import RemoteRouter
from .runtimes import build_runtime_registry
from .state import StateStore


@dataclass(frozen=True)
class WorkspaceFeatures:
    progress_replies: bool = False
    card_replies: bool = False
    runtime_streaming: bool = False
    multi_workspace: bool = False


@dataclass
class WorkspaceRuntime:
    workspace_id: str
    config: RemoteConfig
    state: StateStore
    gateway: LarkGateway
    router: RemoteRouter


class WorkspaceManager:
    def __init__(self, runtimes: dict[str, WorkspaceRuntime], default_workspace: str = "default"):
        if default_workspace not in runtimes:
            raise ValueError(f"default workspace {default_workspace!r} is not configured")
        self.runtimes = dict(runtimes)
        self.default_workspace = default_workspace

    def runtime_for(self, workspace_id: str | None = None) -> WorkspaceRuntime:
        key = workspace_id or self.default_workspace
        runtime = self.runtimes.get(key)
        if runtime is None:
            raise KeyError(key)
        return runtime

    async def handle_event(self, workspace_id: str | None, event: dict, incoming_factory: Callable[[dict, str], IncomingMessage | None]) -> None:
        runtime = self.runtime_for(workspace_id)
        msg = incoming_factory(event, runtime.workspace_id)
        if msg is None:
            return
        await runtime.router.handle(msg)


def build_workspace_manager(config_path: str | Path, state_path: str | Path, session_finder: Any | None = None) -> WorkspaceManager:
    configs = load_workspace_configs(config_path)
    runtimes: dict[str, WorkspaceRuntime] = {}
    for workspace_id, config in configs.items():
        state = StateStore(state_path)
        gateway = LarkGateway(config.lark_cli_bin, config.lark_cli_args)
        router = RemoteRouter(config, state, build_runtime_registry(config), gateway, session_finder=session_finder)
        runtimes[workspace_id] = WorkspaceRuntime(workspace_id, config, state, gateway, router)
    default = next(iter(configs.values())).default_workspace
    return WorkspaceManager(runtimes, default_workspace=default)
