# Unified Class Calendar

A local-first coursework aggregator for students whose assignments are scattered across multiple learning platforms. It checks authenticated sources, stores only dates explicitly stated by those sources, and renders classes and coursework in one private calendar.

The crawler is designed around a few strict rules:

- Never infer a deadline from cadence, ordering, or an availability window.
- Preserve due, availability, late-submission, and end dates separately.
- Keep previous records when authentication, retrieval, or parsing fails.
- Merge duplicate events while retaining links to every contributing source.
- Surface schedule conflicts and changes instead of silently choosing a value.

## Features

- Brightspace calendar, assignment, quiz, content, and schedule adapters
- Gradescope, WeBWorK, and zyBooks adapters
- Interactive authentication preflight with macOS Keychain integration
- SQLite observation and change history
- Week, two-day, and month calendar views
- Separate Classes and Coursework layers
- Event detail cards with clean primary and secondary source actions
- A local-only web server at `http://127.0.0.1:8765/`
- A token-free macOS LaunchAgent for daily and login-time refreshes

## Privacy model

The public repository contains only a fictional example configuration. Your real `config/sources.toml`, browser profile, cookies, database, generated dashboard, logs, screenshots, downloaded course material, and local development guidance are ignored by Git.

Credentials are stored as generic-password items in the macOS login Keychain. They are not written to configuration, SQLite, logs, browser storage, or command arguments. The crawler may additionally retain session cookies inside its ignored dedicated browser profile.

Before publishing a fork, inspect both the staged diff and the complete Git history. Removing private data in a later commit does not remove it from earlier commits.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config/sources.example.toml config/sources.toml
```

Edit the ignored `config/sources.toml` with your own classes and authenticated source URLs, then initialize the database and dashboard:

```bash
.venv/bin/coursework-crawler init
```

## Authentication

Establish or renew a dedicated browser session interactively:

```bash
.venv/bin/coursework-crawler authenticate 'https://your-platform.example/course'
```

The command keeps the browser open until you confirm that the course page is visible. Manual terminal runs also perform a full authentication preflight before crawling and keep every signed-out platform tab open together.

Optional Keychain-backed automatic login records can be configured with:

```bash
.venv/bin/coursework-crawler credentials set vanderbilt
.venv/bin/coursework-crawler credentials set webwork
.venv/bin/coursework-crawler credentials set gradescope
.venv/bin/coursework-crawler credentials set zybooks
.venv/bin/coursework-crawler credentials status
```

Automatic login is attempted once per signed-out platform. Rejected credentials are not repeatedly submitted, and MFA may still require user approval.

## Running the crawler

Run every enabled source:

```bash
.venv/bin/coursework-crawler run
```

Run one configured source while developing an adapter:

```bash
.venv/bin/coursework-crawler run --source brightspace-calendar-feed
```

The generated dashboard is written to `output/index.html`; crawl state and history are stored in `data/coursework.sqlite3`. Both paths are ignored.

## Token-free macOS automation

Install or update the native runtime outside the macOS-protected Documents directory:

```bash
.venv/bin/coursework-crawler install-native-schedule
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.local.vanderbilt-coursework-crawler.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.local.vanderbilt-coursework-calendar.plist"
```

The crawler runs at login and at 6:00 AM, with catch-up behavior after sleep and a once-per-day guard. The calendar server binds only to the loopback interface and never opens a browser automatically.

Open or bookmark [http://127.0.0.1:8765/](http://127.0.0.1:8765/) after installation.

To manually test the deployed daily wrapper:

```bash
"$HOME/Library/Application Support/Unified Class Calendar/bin/run-daily" --force
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The test suite uses `config/sources.example.toml`, so it does not depend on or expose a developer's live courses.
