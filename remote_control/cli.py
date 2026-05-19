from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import CANONICAL_CONFIG, CANONICAL_DIR, CANONICAL_STATE, LEGACY_CONFIG, LEGACY_STATE, resolve_config_path, resolve_state_path

LABEL = "com.feishu-agent-remote"
PLIST_PATH = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
LOG_DIR = CANONICAL_DIR / "logs"
OUT_LOG = LOG_DIR / "service.out.log"
ERR_LOG = LOG_DIR / "service.err.log"


@dataclass(frozen=True)
class MigrationResult:
    copied: list[tuple[Path, Path]]
    skipped: list[Path]
    backups: list[Path]
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="far", description="Feishu Agent Remote local utility")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate-config", help="migrate legacy ~/.yyf-codex files to ~/.feishu-agent-remote")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--force", action="store_true")

    service = sub.add_parser("service", help="manage macOS launchd service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    for name in ("install", "uninstall", "start", "stop", "restart", "status"):
        service_sub.add_parser(name)
    logs = service_sub.add_parser("logs")
    logs.add_argument("--lines", "-n", type=int, default=80)

    args = parser.parse_args(argv)
    if args.command == "migrate-config":
        result = migrate_config(dry_run=args.dry_run, force=args.force)
        print(_format_migration(result))
        return 0
    if args.command == "service":
        return _service(args.service_command, getattr(args, "lines", 80))
    return 2


def migrate_config(dry_run: bool = False, force: bool = False) -> MigrationResult:
    copied: list[tuple[Path, Path]] = []
    skipped: list[Path] = []
    backups: list[Path] = []
    for src, dst in ((LEGACY_CONFIG, CANONICAL_CONFIG), (LEGACY_STATE, CANONICAL_STATE)):
        if not src.exists():
            skipped.append(src)
            continue
        if dst.exists() and not force:
            skipped.append(dst)
            continue
        if dry_run:
            copied.append((src, dst))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and force:
            backup = dst.with_suffix(dst.suffix + ".bak")
            counter = 1
            while backup.exists():
                backup = dst.with_suffix(dst.suffix + f".bak{counter}")
                counter += 1
            shutil.copy2(dst, backup)
            backups.append(backup)
        shutil.copy2(src, dst)
        copied.append((src, dst))
    return MigrationResult(copied=copied, skipped=skipped, backups=backups, dry_run=dry_run)


def build_plist(repo_root: Path | None = None, python: str | None = None) -> dict:
    repo_root = repo_root or Path.cwd()
    python = python or sys.executable
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(repo_root / "main.py")],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "FAR_CONFIG": str(resolve_config_path()),
            "FAR_STATE": str(resolve_state_path()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(OUT_LOG),
        "StandardErrorPath": str(ERR_LOG),
    }


def _service(command: str, lines: int) -> int:
    if sys.platform != "darwin":
        print("service commands are only supported on macOS launchd")
        return 1
    if command == "install":
        return _install_service()
    if command == "uninstall":
        _launchctl("bootout", "gui/%s/%s" % (os.getuid(), LABEL), check=False)
        if PLIST_PATH.exists():
            PLIST_PATH.unlink()
        print(f"uninstalled {LABEL}")
        return 0
    if command == "start":
        return _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH), check=False)
    if command == "stop":
        return _launchctl("bootout", "gui/%s/%s" % (os.getuid(), LABEL), check=False)
    if command == "restart":
        _launchctl("bootout", "gui/%s/%s" % (os.getuid(), LABEL), check=False)
        return _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH), check=False)
    if command == "status":
        return _status_service()
    if command == "logs":
        _print_logs(lines)
        return 0
    return 2


def _install_service() -> int:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = build_plist()
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist, fh)
    print(f"installed {LABEL}: {PLIST_PATH}")
    return _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH), check=False)


def _status_service() -> int:
    print(f"label: {LABEL}")
    print(f"plist: {PLIST_PATH} ({'exists' if PLIST_PATH.exists() else 'missing'})")
    print(f"config: {resolve_config_path()}")
    print(f"state: {resolve_state_path()}")
    print(f"stdout: {OUT_LOG}")
    print(f"stderr: {ERR_LOG}")
    if PLIST_PATH.exists():
        _launchctl("print", "gui/%s/%s" % (os.getuid(), LABEL), check=False)
    return 0


def _launchctl(*args: str, check: bool = True) -> int:
    proc = subprocess.run(["launchctl", *args], text=True, capture_output=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if check and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def _print_logs(lines: int) -> None:
    for path in (OUT_LOG, ERR_LOG):
        print(f"==> {path}")
        if not path.exists():
            print("(missing)")
            continue
        content = path.read_text(errors="replace").splitlines()[-max(1, lines) :]
        print("\n".join(content) if content else "(empty)")


def _format_migration(result: MigrationResult) -> str:
    prefix = "DRY RUN: " if result.dry_run else ""
    lines = [prefix + "config migration result"]
    lines.extend(f"copied: {src} -> {dst}" for src, dst in result.copied)
    lines.extend(f"backup: {path}" for path in result.backups)
    lines.extend(f"skipped: {path}" for path in result.skipped)
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
