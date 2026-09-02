from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "com.local.vanderbilt-coursework-crawler"
SERVER_LABEL = "com.local.vanderbilt-coursework-calendar"
NATIVE_ROOT = Path.home() / "Library" / "Application Support" / "Unified Class Calendar"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SERVER_LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVER_LABEL}.plist"


def launch_agent_payload(runtime_root: Path, hour: int, minute: int) -> dict[str, object]:
    runner = runtime_root / "bin" / "run-daily"
    data_dir = runtime_root / "data"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(runner)],
        "WorkingDirectory": str(runtime_root),
        "EnvironmentVariables": {"COURSEWORK_CRAWLER_HOME": str(runtime_root)},
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(data_dir / "launcher.log"),
        "StandardErrorPath": str(data_dir / "launcher-error.log"),
        "ProcessType": "Background",
        "ThrottleInterval": 60,
    }


def calendar_server_payload(runtime_root: Path, port: int = 8765) -> dict[str, object]:
    data_dir = runtime_root / "data"
    executable = runtime_root / ".venv" / "bin" / "coursework-crawler"
    return {
        "Label": SERVER_LABEL,
        "ProgramArguments": [
            str(executable), "serve", "--host", "127.0.0.1", "--port", str(port),
        ],
        "WorkingDirectory": str(runtime_root),
        "EnvironmentVariables": {"COURSEWORK_CRAWLER_HOME": str(runtime_root)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(data_dir / "calendar-server.log"),
        "StandardErrorPath": str(data_dir / "calendar-server-error.log"),
        "ProcessType": "Background",
        "ThrottleInterval": 10,
    }


def daily_runner_script(runtime_root: Path) -> str:
    root = str(runtime_root)
    return f'''#!/bin/zsh
set -u

RUNTIME_ROOT={root!r}
DATA_DIR="$RUNTIME_ROOT/data"
GUARD_FILE="$DATA_DIR/last-run-date"
SUCCESS_FILE="$DATA_DIR/last-success-date"
TODAY=$(/bin/date +%F)
FORCE=0

export COURSEWORK_CRAWLER_HOME="$RUNTIME_ROOT"
cd "$RUNTIME_ROOT" || exit 1

if [[ "${{1:-}}" == "--force" ]]; then
  FORCE=1
fi

/bin/mkdir -p "$DATA_DIR"
if [[ "$FORCE" -eq 0 && -f "$GUARD_FILE" && "$(/bin/cat "$GUARD_FILE")" == "$TODAY" ]]; then
  exit 0
fi

/usr/bin/printf '%s\n' "$TODAY" > "$GUARD_FILE.tmp"
/bin/mv "$GUARD_FILE.tmp" "$GUARD_FILE"

RUN_LOG=$(/usr/bin/mktemp "$DATA_DIR/current-run.XXXXXX")
"$RUNTIME_ROOT/.venv/bin/coursework-crawler" run > >(tee -a "$DATA_DIR/crawler.log" "$RUN_LOG") 2> >(tee -a "$DATA_DIR/crawler-error.log" "$RUN_LOG" >&2)
STATUS=$?

if /usr/bin/grep -q 'login_required' "$RUN_LOG"; then
  /usr/bin/osascript -e 'display notification "Open Terminal and run the manual authentication command." with title "Coursework sign-in required"'
elif [[ "$STATUS" -ne 0 ]]; then
  /usr/bin/osascript -e 'display notification "The local crawl failed. Check crawler-error.log." with title "Coursework update failed"'
else
  /usr/bin/printf '%s\n' "$TODAY" > "$SUCCESS_FILE"
fi

/bin/rm -f "$RUN_LOG"
exit "$STATUS"
'''


def write_launch_agent(runtime_root: Path, destination: Path, hour: int, minute: int) -> None:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Schedule must use a valid 24-hour time")
    runner = runtime_root / "bin" / "run-daily"
    if not runner.exists():
        raise FileNotFoundError(f"Native runner not found: {runner}")
    (runtime_root / "data").mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(launch_agent_payload(runtime_root, hour, minute), handle, sort_keys=False)


def write_calendar_server_launch_agent(
    runtime_root: Path,
    destination: Path,
    port: int = 8765,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Calendar server port must be between 1 and 65535")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(calendar_server_payload(runtime_root, port), handle, sort_keys=False)


def _copy_runtime_state(project_root: Path, runtime_root: Path) -> None:
    (runtime_root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / "config" / "sources.toml", runtime_root / "config" / "sources.toml")

    runtime_files = (
        (project_root / "data" / "coursework.sqlite3", runtime_root / "data" / "coursework.sqlite3"),
        (project_root / "output" / "index.html", runtime_root / "output" / "index.html"),
    )
    for source, destination in runtime_files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not destination.exists():
            shutil.copy2(source, destination)

    source_profile = project_root / "browser-profile"
    destination_profile = runtime_root / "browser-profile"
    if not destination_profile.exists() and source_profile.exists():
        ignored = shutil.ignore_patterns(
            "Singleton*",
            "Cache",
            "Code Cache",
            "GPUCache",
            "DawnCache",
            "GrShaderCache",
            "ShaderCache",
            "Crashpad",
            "component_crx_cache",
        )
        shutil.copytree(source_profile, destination_profile, ignore=ignored)
    destination_profile.mkdir(parents=True, exist_ok=True)
    auth_state = destination_profile / "auth-state.json"
    if auth_state.exists():
        os.chmod(auth_state, 0o600)


def deploy_native_runtime(
    project_root: Path,
    runtime_root: Path = NATIVE_ROOT,
    launch_agent_path: Path = LAUNCH_AGENT_PATH,
    server_launch_agent_path: Path = SERVER_LAUNCH_AGENT_PATH,
    hour: int = 6,
    minute: int = 0,
) -> None:
    project_root = project_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    _copy_runtime_state(project_root, runtime_root)

    virtualenv = runtime_root / ".venv"
    if not (virtualenv / "bin" / "python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(virtualenv)], check=True)
    subprocess.run(
        [str(virtualenv / "bin" / "python"), "-m", "pip", "install", "--upgrade", str(project_root)],
        check=True,
    )

    runner = runtime_root / "bin" / "run-daily"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(daily_runner_script(runtime_root), encoding="utf-8")
    os.chmod(runner, 0o755)
    write_launch_agent(runtime_root, launch_agent_path, hour, minute)
    write_calendar_server_launch_agent(runtime_root, server_launch_agent_path)
