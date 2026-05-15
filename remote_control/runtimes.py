import shutil
from collections.abc import Mapping
from typing import Any

from .claude_runner import ClaudeRunner
from .codex_runner import CodexRunner
from .models import RuntimeConfig


class RuntimeRegistry:
    def __init__(
        self,
        runners: Mapping[str, Any],
        default_runtime: str = "codex",
        configs: Mapping[str, RuntimeConfig] | None = None,
    ):
        if default_runtime not in runners:
            raise ValueError(f"default_runtime {default_runtime!r} is not configured")
        self._runners = dict(runners)
        self.default_runtime = default_runtime
        self.configs = dict(configs or {})

    def get(self, runtime: str | None = None) -> Any:
        name = runtime or self.default_runtime
        runner = self._runners.get(name)
        if runner is None:
            raise KeyError(name)
        return runner

    def names(self) -> list[str]:
        return list(self._runners)

    def has(self, runtime: str) -> bool:
        return runtime in self._runners

    def status_lines(self) -> list[str]:
        lines: list[str] = []
        for name in self.names():
            config = self.configs.get(name)
            runtime_type = config.type if config else name
            bin_name = config.bin if config else getattr(self._runners[name], "bin", name)
            marker = " (default)" if name == self.default_runtime else ""
            ok = bool(shutil.which(bin_name))
            lines.append(f"- {name}{marker}: type={runtime_type} bin={bin_name} available={'yes' if ok else 'no'}")
        return lines


def build_runtime_registry(config: Any) -> RuntimeRegistry:
    runners: dict[str, Any] = {}
    runtime_configs = config.runtimes or {}
    for name, runtime_config in runtime_configs.items():
        if runtime_config.type == "codex":
            runners[name] = CodexRunner(
                runtime_config.bin,
                runtime_config.sandbox or config.default_sandbox,
                profile=runtime_config.profile,
            )
        elif runtime_config.type == "claude":
            runners[name] = ClaudeRunner(
                claude_bin=runtime_config.bin,
                permission_mode=runtime_config.permission_mode,
                model=runtime_config.model,
                extra_args=runtime_config.extra_args,
            )
    if not runners:
        runners["codex"] = CodexRunner(config.codex_bin, config.default_sandbox, profile=config.codex_profile)
    return RuntimeRegistry(runners, default_runtime=config.default_runtime, configs=runtime_configs)
