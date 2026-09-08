from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .browser import PersistentBrowser
from .config import load_config
from .credentials import CREDENTIAL_NAMES, Credentials, KeychainCredentialStore
from .db import Database
from .runner import run_crawl
from .server import serve_calendar
from .scheduler import (
    LABEL,
    LAUNCH_AGENT_PATH,
    NATIVE_ROOT,
    SERVER_LAUNCH_AGENT_PATH,
    deploy_native_runtime,
    write_launch_agent,
)
from .view import render_dashboard


PROJECT_ROOT = Path(os.environ.get("COURSEWORK_CRAWLER_HOME", Path.cwd())).resolve()
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "sources.toml"
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "coursework.sqlite3"
DEFAULT_PROFILE = PROJECT_ROOT / "browser-profile"
DEFAULT_DASHBOARD = PROJECT_ROOT / "output" / "index.html"


def run_headless(force_headed: bool, stdin_is_tty: bool) -> bool:
    """Manual terminal runs are interactive; unattended runs stay headless."""
    return not (force_headed or stdin_is_tty)


def initialize(config_path: Path, database_path: Path) -> None:
    config = load_config(config_path)
    database = Database(database_path)
    database.initialize()
    database.sync_sources(config.sources)
    database.sync_class_meetings(config.classes)
    render_dashboard(database_path, DEFAULT_DASHBOARD)
    print(f"Initialized {len(config.sources)} sources in {database_path}")
    print(f"Dashboard: {DEFAULT_DASHBOARD}")


def authenticate(url: str, profile_dir: Path) -> None:
    print("A dedicated Chrome window will open. Sign in interactively, then return here.")
    with PersistentBrowser(profile_dir, headless=False).open() as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        expected_host = urlsplit(url).hostname
        while True:
            input("Press Enter after the course page is fully visible: ")
            candidates = [
                candidate for candidate in context.pages
                if urlsplit(candidate.url).hostname == expected_host
            ]
            observed_urls: list[str] = []
            for candidate in reversed(candidates):
                current_url = candidate.url
                observed_urls.append(current_url)
                body = candidate.locator("body").inner_text(timeout=5_000)
                login_markers = (
                    "/login" in current_url.lower()
                    or "/signin" in current_url.lower()
                    or "Not logged in." in body
                    or "Log in to Gradescope" in body
                )
                if not login_markers:
                    print(f"Authenticated page verified: {candidate.title()} ({current_url})")
                    return
            observed = ", ".join(observed_urls) if observed_urls else "no matching platform tab"
            print(f"Still signed out ({observed}). Complete login in this window and try again.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate explicit coursework deadlines")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    auth_parser = subparsers.add_parser("authenticate")
    auth_parser.add_argument("url")
    auth_parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    view_parser = subparsers.add_parser("render")
    view_parser.add_argument("--output", type=Path, default=DEFAULT_DASHBOARD)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    run_parser.add_argument("--source", action="append", dest="sources")
    run_parser.add_argument("--headed", action="store_true")
    run_parser.add_argument("--include-tophat-lectures", action="store_true",
                            help="Also scan Top Hat lecture slides and files (slow; skipped by default).")
    schedule_parser = subparsers.add_parser("write-schedule")
    schedule_parser.add_argument("--hour", type=int, default=6)
    schedule_parser.add_argument("--minute", type=int, default=0)
    schedule_parser.add_argument(
        "--destination",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist",
    )
    native_parser = subparsers.add_parser("install-native-schedule")
    native_parser.add_argument("--hour", type=int, default=6)
    native_parser.add_argument("--minute", type=int, default=0)
    native_parser.add_argument("--runtime-root", type=Path, default=NATIVE_ROOT)
    native_parser.add_argument("--launch-agent", type=Path, default=LAUNCH_AGENT_PATH)
    native_parser.add_argument(
        "--server-launch-agent", type=Path, default=SERVER_LAUNCH_AGENT_PATH
    )
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--directory", type=Path, default=DEFAULT_DASHBOARD.parent)
    credentials_parser = subparsers.add_parser("credentials")
    credentials_commands = credentials_parser.add_subparsers(dest="credentials_command", required=True)
    credentials_set = credentials_commands.add_parser("set")
    credentials_set.add_argument("name", choices=CREDENTIAL_NAMES)
    credentials_commands.add_parser("status")
    args = parser.parse_args()

    if args.command == "init":
        initialize(args.config, args.database)
    elif args.command == "authenticate":
        authenticate(args.url, args.profile)
    elif args.command == "render":
        render_dashboard(args.database, args.output)
    elif args.command == "run":
        config = load_config(args.config)
        database = Database(args.database)
        exit_code = run_crawl(
            config,
            database,
            args.profile,
            set(args.sources) if args.sources else None,
            headless=run_headless(args.headed, sys.stdin.isatty()),
            include_tophat_lectures=args.include_tophat_lectures,
        )
        render_dashboard(args.database, DEFAULT_DASHBOARD)
        raise SystemExit(exit_code)
    elif args.command == "write-schedule":
        deploy_native_runtime(
            project_root=PROJECT_ROOT,
            runtime_root=NATIVE_ROOT,
            launch_agent_path=args.destination,
            server_launch_agent_path=SERVER_LAUNCH_AGENT_PATH,
            hour=args.hour,
            minute=args.minute,
        )
        print(f"Wrote daily schedule to {args.destination}")
        print(f"Deployed native runtime to {NATIVE_ROOT}")
    elif args.command == "install-native-schedule":
        deploy_native_runtime(
            project_root=PROJECT_ROOT,
            runtime_root=args.runtime_root,
            launch_agent_path=args.launch_agent,
            server_launch_agent_path=args.server_launch_agent,
            hour=args.hour,
            minute=args.minute,
        )
        print(f"Deployed native runtime to {args.runtime_root}")
        print(f"Wrote LaunchAgent to {args.launch_agent}")
        print(f"Wrote local calendar server LaunchAgent to {args.server_launch_agent}")
    elif args.command == "serve":
        serve_calendar(args.directory, args.host, args.port)
    elif args.command == "credentials":
        store = KeychainCredentialStore()
        if args.credentials_command == "set":
            username = input(f"{args.name} username/email: ").strip()
            password = getpass.getpass(f"{args.name} password: ")
            store.set(args.name, Credentials(username, password))
            print(f"Stored {args.name} credentials in macOS Keychain.")
        elif args.credentials_command == "status":
            for name in CREDENTIAL_NAMES:
                try:
                    state = "configured" if store.get(name) else "missing"
                except RuntimeError:
                    state = "invalid — run credentials set again"
                print(f"{name}: {state}")


if __name__ == "__main__":
    main()
