from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .semantics import canonical_event_key


DISPLAY_TIMEZONE = ZoneInfo("America/Chicago")


def _display_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone(DISPLAY_TIMEZONE).strftime(
            "%a %b %-d, %-I:%M %p %Z"
        )
    except ValueError:
        return value


def _normalize_title(value: str) -> str:
    value = re.sub(r"\s+-\s+(?:due|available|availability ends)$", "", value, flags=re.I)
    value = re.sub(r"\s+-\s+brightspace$", "", value, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _class_occurrences(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for row in rows:
        current = date.fromisoformat(row["starts_on"])
        final = date.fromisoformat(row["ends_on"])
        days = {int(value) for value in row["days"].split(",") if value}
        start_clock = time.fromisoformat(row["start_time"])
        end_clock = time.fromisoformat(row["end_time"])
        while current <= final:
            if current.weekday() in days:
                starts_at = datetime.combine(current, start_clock, DISPLAY_TIMEZONE)
                ends_at = datetime.combine(current, end_clock, DISPLAY_TIMEZONE)
                events.append(
                    {
                        "id": f"class:{row['id']}:{current.isoformat()}",
                        "calendar": "classes",
                        "courseId": row["course_id"],
                        "courseCode": row["course_code"],
                        "title": row["title"],
                        "start": starts_at.isoformat(),
                        "end": ends_at.isoformat(),
                        "allDay": False,
                        "kind": "class",
                        "location": row["location"],
                        "url": "",
                        "source": "Class schedule",
                    }
                )
            current += timedelta(days=1)
    return events


def _course_codes(class_rows: list[sqlite3.Row], source_rows: list[sqlite3.Row]) -> dict[str, str]:
    codes = {row["course_id"]: row["course_code"] for row in class_rows}
    for row in source_rows:
        codes.setdefault(row["course_id"], row["course_name"].split(" - ", 1)[0])
    return codes


def _meeting_on(
    class_rows: list[sqlite3.Row],
    course_id: str,
    event_date: date,
) -> sqlite3.Row | None:
    for row in class_rows:
        if row["course_id"] != course_id:
            continue
        if event_date.weekday() in {int(value) for value in row["days"].split(",") if value}:
            return row
    return None


def _coursework_events(
    calendar_rows: list[sqlite3.Row],
    deadline_rows: list[sqlite3.Row],
    class_rows: list[sqlite3.Row],
    codes: dict[str, str],
) -> list[dict[str, object]]:
    def related_links(row: sqlite3.Row) -> list[dict[str, str]]:
        try:
            values = json.loads(row["related_links"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return [
            {"source": str(label), "url": str(url), "kind": "resource"}
            for label, url in values
            if label and url
        ]

    def source_rank(event: dict[str, object]) -> int:
        source = str(event.get("source", "")).lower()
        adapter = str(event.get("sourceAdapter", "")).lower()
        if source == "gradescope" or adapter == "gradescope":
            return 0
        if source in {"webwork", "zybooks"} or adapter in {"webwork", "zybooks"}:
            return 1
        if adapter == "brightspace_assignments":
            return 2
        if adapter in {"brightspace_quizzes", "brightspace_content"}:
            return 3
        if source == "course schedule":
            return 4
        return 5

    def moment_key(event: dict[str, object]) -> tuple[str, str]:
        start = datetime.fromisoformat(str(event["start"]))
        end = datetime.fromisoformat(str(event["end"]))
        if event["allDay"]:
            return (
                start.astimezone(DISPLAY_TIMEZONE).date().isoformat(),
                end.astimezone(DISPLAY_TIMEZONE).date().isoformat(),
            )
        return (
            start.astimezone(UTC).replace(second=0, microsecond=0).isoformat(),
            end.astimezone(UTC).replace(second=0, microsecond=0).isoformat(),
        )

    events: list[dict[str, object]] = []
    for row in calendar_rows:
        starts_at = datetime.fromisoformat(row["starts_at"])
        ends_at = datetime.fromisoformat(row["ends_at"])
        all_day = bool(row["all_day"])
        if all_day and row["event_kind"] == "exam":
            meeting = _meeting_on(class_rows, row["course_id"], starts_at.date())
            if meeting is not None:
                starts_at = datetime.combine(
                    starts_at.astimezone(DISPLAY_TIMEZONE).date(),
                    time.fromisoformat(meeting["start_time"]),
                    DISPLAY_TIMEZONE,
                )
                ends_at = datetime.combine(
                    starts_at.date(),
                    time.fromisoformat(meeting["end_time"]),
                    DISPLAY_TIMEZONE,
                )
                all_day = False
        events.append(
            {
                "id": f"calendar:{row['source_id']}:{row['source_event_id']}",
                "calendar": "coursework",
                "courseId": row["course_id"],
                "courseCode": codes.get(row["course_id"], row["course_id"].upper()),
                "title": row["title"],
                "start": starts_at.isoformat(),
                "end": ends_at.isoformat(),
                "allDay": all_day,
                "kind": row["event_kind"],
                "canonicalKey": row["canonical_key"] or canonical_event_key(
                    row["title"], row["event_kind"]
                ),
                "location": row["location"] or "",
                "url": row["details_url"],
                "source": (
                    "Course schedule"
                    if row["source_adapter"] == "brightspace_schedule"
                    else "Brightspace calendar"
                ),
                "sourceAdapter": row["source_adapter"],
                "provenance": [{
                    "source": (
                        "Course schedule"
                        if row["source_adapter"] == "brightspace_schedule"
                        else "Brightspace calendar"
                    ),
                    "url": row["details_url"],
                    "kind": "source",
                }],
            }
        )

    for row in deadline_rows:
        due_at = datetime.fromisoformat(row["due_at"])
        is_exam = bool(re.search(r"\b(exam|midterm|test)\b", row["title"], re.I))
        kind = "exam" if is_exam else "due"
        events.append(
            {
                "id": f"deadline:{row['source_id']}:{row['source_item_id']}",
                "calendar": "coursework",
                "courseId": row["course_id"],
                "courseCode": codes.get(row["course_id"], row["course_id"].upper()),
                "title": row["title"],
                "start": due_at.isoformat(),
                "end": due_at.isoformat(),
                "allDay": False,
                "kind": kind,
                "canonicalKey": row["canonical_key"] or canonical_event_key(row["title"], kind),
                "location": "",
                "url": row["details_url"],
                "source": row["source_platform"],
                "sourceAdapter": row["source_adapter"],
                "description": row["description"] or "",
                "timingText": row["timing_text"] or "",
                "availableFrom": row["available_from"] or "",
                "availableUntil": row["available_until"] or "",
                "lateDueAt": row["late_due_at"] or "",
                "componentKind": row["component_kind"] or "",
                "provenance": [{
                    "source": row["source_platform"],
                    "url": row["details_url"],
                    "kind": "source",
                    "dueAt": row["due_at"] or "",
                    "lateDueAt": row["late_due_at"] or "",
                    "availableFrom": row["available_from"] or "",
                    "availableUntil": row["available_until"] or "",
                    "componentKind": row["component_kind"] or "",
                }, *related_links(row)],
            }
        )

    def local_date(event: dict[str, object]) -> date:
        return datetime.fromisoformat(str(event["start"])).astimezone(DISPLAY_TIMEZONE).date()

    def equivalent(left: dict[str, object], right: dict[str, object]) -> bool:
        if left["courseId"] != right["courseId"] or left.get("canonicalKey") != right.get("canonicalKey"):
            return False
        left_moment, right_moment = moment_key(left), moment_key(right)
        if left["kind"] == "due" and right["kind"] == "due":
            return left_moment[0] == right_moment[0]
        if left_moment == right_moment and left["allDay"] == right["allDay"]:
            return True
        if bool(left["allDay"]) != bool(right["allDay"]):
            all_day = left if left["allDay"] else right
            timed = right if left["allDay"] else left
            day = local_date(timed)
            span_start = local_date(all_day)
            span_end = datetime.fromisoformat(str(all_day["end"])).astimezone(
                DISPLAY_TIMEZONE
            ).date()
            return span_start <= day < span_end
        return False

    merged: list[dict[str, object]] = []
    for event in events:
        current = next((candidate for candidate in merged if equivalent(candidate, event)), None)
        if current is None:
            merged.append(event)
            continue
        provenance = current["provenance"]
        for item in event["provenance"]:
            if item not in provenance:
                provenance.append(item)
        current["source"] = " + ".join(dict.fromkeys(
            str(item["source"]) for item in provenance if item.get("kind") != "resource"
        ))
        prefer_schedule = event["source"] == "Course schedule"
        keep_multiday_break = str(current.get("canonicalKey", "")).startswith("break:") and current["allDay"]
        prefer_primary = source_rank(event) < source_rank(current)
        if (prefer_schedule and not keep_multiday_break) or prefer_primary:
            current["title"] = event["title"]
            current["url"] = event["url"]
            current["sourceAdapter"] = event.get("sourceAdapter", "")
            if prefer_schedule and not keep_multiday_break:
                current["kind"] = event["kind"]
                current["start"] = event["start"]
                current["end"] = event["end"]
                current["allDay"] = event["allDay"]
        for field in ("description", "timingText", "availableFrom", "availableUntil", "lateDueAt", "componentKind"):
            if not current.get(field) and event.get(field):
                current[field] = event[field]

    strong_prefixes = ("exam:", "due:", "break:", "term:", "course:", "quiz:", "tips:")
    by_identity: dict[tuple[str, str], list[dict[str, object]]] = {}
    for event in merged:
        key = str(event.get("canonicalKey", ""))
        if key.startswith(strong_prefixes):
            by_identity.setdefault((str(event["courseId"]), key), []).append(event)
    for group in by_identity.values():
        if len(group) <= 1:
            continue
        values = ", ".join(sorted({
            (
                datetime.fromisoformat(str(event["start"])).astimezone(DISPLAY_TIMEZONE).strftime("%b %-d")
                if event["allDay"]
                else datetime.fromisoformat(str(event["start"])).astimezone(DISPLAY_TIMEZONE).strftime(
                    "%b %-d, %-I:%M %p"
                )
            )
            for event in group
        }))
        for event in group:
            event["conflict"] = True
            event["conflictMessage"] = f"Authoritative sources disagree: {values}"
    return merged


def render_dashboard(database_path: Path, output_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        deadline_rows = connection.execute(
            """
            SELECT i.*, s.course_name, s.adapter AS source_adapter
            FROM items i JOIN sources s ON s.id=i.source_id
            WHERE i.active=1 AND i.due_at IS NOT NULL
            ORDER BY i.due_at ASC
            """
        ).fetchall()
        unscheduled = connection.execute(
            """
            SELECT i.*, s.course_name
            FROM items i JOIN sources s ON s.id=i.source_id
            WHERE i.active=1 AND i.due_at IS NULL AND i.timing_text IS NOT NULL
            ORDER BY s.course_name, i.title
            """
        ).fetchall()
        calendar_rows = connection.execute(
            """
            SELECT ce.*, s.adapter AS source_adapter
            FROM calendar_events ce JOIN sources s ON s.id=ce.source_id
            WHERE ce.active=1 ORDER BY ce.starts_at ASC
            """
        ).fetchall()
        class_rows = connection.execute(
            "SELECT * FROM class_meetings ORDER BY start_time, course_code"
        ).fetchall()
        source_rows = connection.execute("SELECT * FROM sources").fetchall()
        health = connection.execute(
            """
            SELECT s.*, c.checked_at, c.health, c.item_count, c.message,
                   (
                       SELECT sc2.checked_at FROM source_checks sc2
                       WHERE sc2.source_id=s.id
                         AND sc2.health IN ('success', 'verified_zero')
                       ORDER BY sc2.checked_at DESC LIMIT 1
                   ) AS last_successful_at
            FROM sources s
            LEFT JOIN source_checks c ON c.id=(
                SELECT id FROM source_checks sc
                WHERE sc.source_id=s.id ORDER BY sc.checked_at DESC LIMIT 1
            )
            ORDER BY s.enabled DESC, s.course_name, s.platform
            """
        ).fetchall()
        issues = connection.execute(
            """
            SELECT si.*, s.course_name
            FROM source_issues si JOIN sources s ON s.id=si.source_id
            WHERE si.active=1
            ORDER BY si.severity DESC, s.course_name, si.title
            """
        ).fetchall()
        changes = connection.execute(
            """
            SELECT c.*, e.title, e.details_url, s.course_name
            FROM calendar_event_changes c
            JOIN calendar_events e ON e.id=c.event_id
            JOIN sources s ON s.id=e.source_id
            WHERE c.field_name NOT IN ('canonical_key', 'raw_date_label')
            ORDER BY c.changed_at DESC LIMIT 30
            """
        ).fetchall()
    finally:
        connection.close()

    codes = _course_codes(class_rows, source_rows)
    events = _class_occurrences(class_rows)
    events.extend(_coursework_events(calendar_rows, deadline_rows, class_rows, codes))
    events.sort(key=lambda event: (str(event["start"]), str(event["courseCode"]), str(event["title"])))
    payload = json.dumps(events, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    def configured_health(row: sqlite3.Row) -> str:
        if not row["enabled"]:
            return row["configured_status"] if row["configured_status"] != "active" else "disabled"
        return row["health"] or "never checked"

    health_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row['course_name'])}</td>"
        f"<td>{html.escape(row['platform'])}</td>"
        f"<td><span class=\"health {html.escape(configured_health(row).replace(' ', '_'))}\">{html.escape(configured_health(row))}</span></td>"
        f"<td>{html.escape(_display_time(row['last_successful_at']))}</td>"
        f"<td>{html.escape(row['message'] or '')}</td>"
        "</tr>"
        for row in health
    )
    issue_parts = [
        f'<a class="notice" href="{html.escape(row["details_url"])}" target="_blank" rel="noopener">'
        f'<strong>{html.escape(row["course_name"])}</strong>'
        f'<span>{html.escape(row["title"])}</span>'
        f'<small>{html.escape(row["message"])}</small></a>'
        for row in issues
    ]
    seen_conflicts: set[tuple[str, str]] = set()
    for event in events:
        if not event.get("conflict"):
            continue
        key = (str(event["courseId"]), str(event.get("canonicalKey", event["title"])))
        if key in seen_conflicts:
            continue
        seen_conflicts.add(key)
        issue_parts.append(
            f'<a class="notice" href="{html.escape(str(event["url"]))}" target="_blank" rel="noopener">'
            f'<strong>{html.escape(str(event["courseCode"]))} · Source conflict</strong>'
            f'<span>{html.escape(str(event["title"]))}</span>'
            f'<small>{html.escape(str(event.get("conflictMessage", "Explicit sources disagree.")))}</small></a>'
        )
    issue_rows = "".join(issue_parts) or '<p class="empty compact">No unresolved source ambiguities.</p>'
    unscheduled_rows = "".join(
        f'<a class="notice unscheduled" href="{html.escape(row["details_url"])}" target="_blank" rel="noopener">'
        f'<strong>{html.escape(row["course_name"])} · Required, no exact due time</strong>'
        f'<span>{html.escape(row["title"])}</span>'
        f'<small>{html.escape(row["timing_text"])}</small></a>'
        for row in unscheduled
    ) or '<p class="empty compact">No unscheduled required work.</p>'
    change_labels = {
        "added": "Added",
        "starts_at": "Date/time changed",
        "ends_at": "End time changed",
        "title": "Title changed",
        "missing": "Missing from latest successful check",
        "removed": "Removed after repeated successful checks",
        "restored": "Restored",
    }

    def change_detail(row: sqlite3.Row) -> str:
        if row["field_name"] in {"starts_at", "ends_at"}:
            return (
                f"{_display_time(row['old_value'])} → {_display_time(row['new_value'])} · "
                f"recorded {_display_time(row['changed_at'])}"
            )
        if row["old_value"] is not None and row["new_value"] is not None:
            return (
                f"{row['old_value']} → {row['new_value']} · "
                f"recorded {_display_time(row['changed_at'])}"
            )
        return _display_time(row["changed_at"])

    change_rows = "".join(
        f'<a class="change" href="{html.escape(row["details_url"])}" target="_blank" rel="noopener">'
        f'<strong>{html.escape(change_labels.get(row["field_name"], row["field_name"].replace("_", " ").title()))}</strong>'
        f'<span>{html.escape(row["course_name"])} · {html.escape(row["title"])}</span>'
        f'<small>{html.escape(change_detail(row))}</small></a>'
        for row in changes
    ) or '<p class="empty compact">No recorded schedule changes yet.</p>'
    generated = datetime.now(UTC).astimezone(DISPLAY_TIMEZONE).strftime(
        "%a, %b %-d at %-I:%M %p %Z"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _document(payload, generated, health_rows, issue_rows, unscheduled_rows, change_rows),
        encoding="utf-8",
    )


def _document(
    payload: str,
    generated: str,
    health_rows: str,
    issue_rows: str,
    unscheduled_rows: str,
    change_rows: str,
) -> str:
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>Vanderbilt Calendar</title>
<style>
:root{{--ink:#f5f7fb;--muted:#98a4b5;--panel:#151b24;--line:#2b3544;--class:#28a9e2;--class-bg:#123e52;--work:#ff9d27;--work-bg:#573207;--exam:#ffbd63;--now:#ff5164;color-scheme:dark;font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;background:#0d1118;color:var(--ink);min-width:320px}}button,a{{font:inherit}}button{{color:inherit}}
.app-header{{min-height:72px;padding:0 22px;display:flex;align-items:center;gap:18px;border-bottom:1px solid var(--line);background:#111721;position:sticky;top:0;z-index:20}}
.brand{{display:flex;align-items:center;gap:12px;min-width:235px}}.brand-mark{{width:34px;height:34px;border-radius:10px;background:linear-gradient(145deg,#d3b06e,#8a6b32);display:grid;place-items:center;color:#121820;font-weight:900}}.brand h1{{font-size:18px;margin:0;letter-spacing:-.02em}}.brand p{{font-size:11px;color:var(--muted);margin:2px 0 0}}
.toolbar{{display:flex;align-items:center;gap:8px;flex:1}}.toolbar button,.seg button{{border:1px solid var(--line);background:#171e29;border-radius:8px;padding:8px 11px;cursor:pointer}}.toolbar button:hover,.seg button:hover{{background:#222c39}}.period{{font-size:18px;font-weight:750;min-width:220px;margin-left:6px}}.seg{{display:flex;margin-left:auto}}.seg button{{border-radius:0}}.seg button:first-child{{border-radius:8px 0 0 8px}}.seg button:last-child{{border-radius:0 8px 8px 0;margin-left:-1px}}.seg .active{{background:#344154}}
.layout{{display:grid;grid-template-columns:minmax(0,1fr) 300px;min-height:calc(100vh - 72px)}}.main{{min-width:0;border-right:1px solid var(--line)}}.filters{{height:50px;display:flex;align-items:center;gap:9px;padding:0 18px;border-bottom:1px solid var(--line);background:#121822}}.filter{{display:flex;align-items:center;gap:7px;border:1px solid var(--line);background:#171e29;border-radius:999px;padding:6px 11px;cursor:pointer;font-size:13px}}.filter input{{position:absolute;opacity:0}}.dot{{width:9px;height:9px;border-radius:50%}}.classes-dot{{background:var(--class)}}.coursework-dot{{background:var(--work)}}.filter:has(input:not(:checked)){{opacity:.45}}
.calendar-wrap{{overflow-x:auto;overflow-y:hidden;background:var(--panel)}}.week{{min-width:900px}}.two-day{{width:100%;min-width:320px}}.day-heads,.all-day,.time-row{{display:grid;grid-template-columns:64px repeat(var(--day-count),minmax(112px,1fr))}}.day-heads{{height:64px;border-bottom:1px solid var(--line);background:#151c26;position:sticky;top:0;z-index:8}}.day-head{{padding:10px 8px;text-align:center;border-left:1px solid var(--line);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}.day-head strong{{display:block;color:var(--ink);font-size:19px;letter-spacing:0;margin-top:4px}}.day-head.today strong{{display:inline-grid;place-items:center;background:#d0ad68;color:#10151d;width:30px;height:30px;border-radius:50%}}
.all-day{{min-height:34px;border-bottom:1px solid var(--line);background:#131a23}}.all-label{{font-size:10px;color:var(--muted);padding:9px 7px;text-align:right}}.all-cell{{border-left:1px solid var(--line);padding:3px;display:flex;flex-direction:column;gap:3px}}.all-event,.month-event{{display:block;width:100%;overflow:hidden;text-align:left;text-overflow:ellipsis;white-space:nowrap;border:0;border-radius:4px;padding:3px 6px;font-size:11px;font-weight:700;color:#fff;background:var(--work-bg);border-left:3px solid var(--work);cursor:pointer}}
.time-row{{height:960px;position:relative}}.time-axis{{position:relative;background:#121820}}.hour-label{{position:absolute;right:8px;transform:translateY(-7px);color:#7d8998;font-size:10px}}.day-col{{position:relative;border-left:1px solid var(--line);background:repeating-linear-gradient(to bottom,transparent 0,transparent 59px,var(--line) 59px,var(--line) 60px)}}.timed-event{{position:absolute;z-index:2;border:0;border-radius:5px;padding:4px 6px;overflow:hidden;text-align:left;color:#fff;font-size:11px;line-height:1.25;background:var(--work-bg);border-left:4px solid var(--work);box-shadow:0 2px 7px #0005;cursor:pointer}}.timed-event.classes{{background:var(--class-bg);border-left-color:var(--class)}}.timed-event.exam{{background:#5a3608;border-left-color:var(--exam)}}.timed-event.conflict,.all-event.conflict,.month-event.conflict{{outline:2px solid #ff5d6c;outline-offset:-1px}}.timed-event:hover,.all-event:hover,.month-event:hover,.timed-event:focus-visible,.all-event:focus-visible,.month-event:focus-visible{{filter:brightness(1.17);z-index:5;outline:2px solid #fff;outline-offset:1px}}.event-time{{display:block;font-size:10px;opacity:.8}}.event-title{{font-weight:750}}.event-source{{display:block;font-size:9px;opacity:.62;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.event-location{{display:block;opacity:.65;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;margin-top:2px}}.now-line{{position:absolute;left:0;right:0;height:2px;background:var(--now);z-index:6;pointer-events:none}}.now-line:before{{content:"";position:absolute;width:8px;height:8px;border-radius:50%;background:var(--now);left:-4px;top:-3px}}
.month{{display:grid;grid-template-columns:repeat(7,minmax(115px,1fr));min-width:820px}}.month-weekday{{height:36px;padding:10px;text-align:center;color:var(--muted);font-size:11px;text-transform:uppercase;border-bottom:1px solid var(--line);border-left:1px solid var(--line)}}.month-day{{min-height:128px;border-left:1px solid var(--line);border-bottom:1px solid var(--line);padding:7px;background:#151b24}}.month-day.outside{{opacity:.38}}.month-number{{font-size:12px;color:var(--muted);margin:0 0 6px 3px}}.month-number.today{{display:grid;place-items:center;width:25px;height:25px;border-radius:50%;background:#d0ad68;color:#111;font-weight:800}}.month-events{{display:flex;flex-direction:column;gap:3px}}.month-event.classes{{background:var(--class-bg);border-left-color:var(--class)}}.more{{font-size:10px;color:var(--muted);padding:2px 5px}}
.sidebar{{background:#111721;padding:18px;overflow:auto}}.sidebar h2{{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:4px 0 14px}}.upcoming{{display:flex;flex-direction:column;gap:8px}}.upcoming-item{{display:grid;width:100%;grid-template-columns:43px 1fr;gap:10px;padding:10px;border:1px solid var(--line);border-radius:9px;background:#171e29;color:inherit;text-align:left;cursor:pointer}}.upcoming-item:hover,.upcoming-item:focus-visible{{background:#202a37;outline:2px solid #fff;outline-offset:1px}}.date-box{{text-align:center;color:var(--muted);font-size:10px;text-transform:uppercase}}.date-box strong{{display:block;color:var(--ink);font-size:20px}}.upcoming-title{{font-size:12px;font-weight:700;line-height:1.3}}.upcoming-meta{{font-size:10px;color:var(--muted);margin-top:4px}}.empty{{color:var(--muted);font-size:13px;padding:18px 2px}}.empty.compact{{padding:9px 0;margin:0}}details{{margin-top:24px;border-top:1px solid var(--line);padding-top:15px}}summary{{cursor:pointer;color:var(--muted);font-size:12px}}.notice,.change{{display:flex;flex-direction:column;gap:3px;color:inherit;text-decoration:none;border:1px solid var(--line);border-radius:7px;padding:8px;margin-top:8px;background:#171e29}}.notice{{border-left:3px solid #ffbd63}}.notice strong,.change strong{{font-size:10px;color:#ffbd63;text-transform:uppercase}}.notice span,.change span{{font-size:11px;font-weight:700}}.notice small,.change small{{font-size:10px;color:var(--muted);line-height:1.35}}.updated{{font-size:11px;color:var(--muted);line-height:1.5;margin-top:18px}}.health-table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:10px}}.health-table td{{padding:6px 3px;border-bottom:1px solid var(--line);vertical-align:top}}.health-table td:nth-child(4),.health-table td:nth-child(5){{display:none}}.health{{display:inline-block;padding:2px 5px;border-radius:999px;background:#59647744}}.success,.verified_zero{{background:#299d6f44}}.ambiguous{{background:#d99a3144}}.login_required,.parser_failed,.unavailable{{background:#d5535344}}
.event-popover{{position:fixed;z-index:50;width:min(390px,calc(100vw - 24px));max-height:min(620px,calc(100vh - 24px));overflow:auto;border:1px solid #3a4657;border-radius:14px;background:#1b222c;color:var(--ink);box-shadow:0 18px 55px #000b;padding:20px}}.event-popover[hidden]{{display:none}}.popover-close{{position:absolute;right:12px;top:12px;border:0;background:transparent;color:var(--muted);font-size:22px;line-height:1;padding:6px;cursor:pointer;border-radius:50%}}.popover-close:hover,.popover-close:focus-visible{{background:#2a3442;color:var(--ink);outline:none}}.popover-kicker{{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.08em;padding-right:35px}}.popover-kicker .dot{{flex:0 0 auto}}.event-popover h2{{font-size:20px;line-height:1.25;margin:13px 34px 5px 0;letter-spacing:-.02em}}.popover-course{{color:#cbd4e0;font-size:13px;font-weight:650;margin:0 0 17px}}.popover-details{{display:grid;gap:12px;margin:0 0 18px}}.detail-row{{display:grid;grid-template-columns:24px 1fr;gap:9px;align-items:start;color:#dbe2eb;font-size:13px;line-height:1.45}}.detail-icon{{color:var(--muted);font-size:15px;text-align:center}}.conflict-note{{border-left:3px solid #ff5d6c;background:#4a2229;padding:9px 11px;border-radius:6px;font-size:12px;margin-bottom:16px}}.primary-action{{display:flex;align-items:center;justify-content:center;width:100%;padding:10px 14px;border-radius:8px;background:#d0ad68;color:#111820;text-decoration:none;font-size:13px;font-weight:800}}.primary-action:hover,.primary-action:focus-visible{{background:#e0c181;outline:2px solid #fff;outline-offset:2px}}.source-caption{{display:block;color:var(--muted);font-size:10px;text-align:center;margin-top:6px}}.other-sources{{border-top:1px solid var(--line);margin-top:16px;padding-top:14px}}.other-sources h3{{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin:0 0 7px}}.source-link{{display:flex;justify-content:space-between;align-items:center;color:#9fd8f2;text-decoration:none;padding:8px 0;font-size:12px}}.source-link:hover,.source-link:focus-visible{{color:#fff;text-decoration:underline;outline:none}}.source-only{{color:var(--muted);font-size:12px;margin:12px 0 0}}
@media(max-width:980px){{.layout{{grid-template-columns:1fr}}.sidebar{{border-top:1px solid var(--line)}}.brand{{min-width:0}}.brand p{{display:none}}.period{{min-width:0;font-size:15px}}}}@media(max-width:650px){{.app-header{{height:auto;min-height:70px;flex-wrap:wrap;padding:12px}}.toolbar{{order:2;width:100%;flex-wrap:wrap}}.period{{flex:1;min-width:150px}}.filters{{padding:0 10px}}.seg{{margin-left:0}}.seg button{{padding:7px}}.event-popover{{left:8px!important;right:8px!important;bottom:8px!important;top:auto!important;width:auto;max-height:min(70vh,620px);border-radius:16px}}}}
</style></head><body>
<header class="app-header"><div class="brand"><div class="brand-mark">V</div><div><h1>Vanderbilt Calendar</h1><p>Classes + Coursework</p></div></div><div class="toolbar"><button id="prev" aria-label="Previous period">←</button><button id="today">Today</button><button id="next" aria-label="Next period">→</button><div class="period" id="period"></div><div class="seg" aria-label="Calendar view"><button id="weekBtn" class="active">Week</button><button id="twoDayBtn">2 Day</button><button id="monthBtn">Month</button></div></div></header>
<div class="layout"><main class="main"><div class="filters"><label class="filter"><input id="classesToggle" type="checkbox" checked><span class="dot classes-dot"></span>Classes</label><label class="filter"><input id="courseworkToggle" type="checkbox" checked><span class="dot coursework-dot"></span>Coursework</label></div><div class="calendar-wrap" id="calendar"></div></main>
<aside class="sidebar"><h2>Coming up</h2><div class="upcoming" id="upcoming"></div><details open><summary>Required without exact time</summary>{unscheduled_rows}</details><details open><summary>Schedule ambiguities</summary>{issue_rows}</details><details><summary>Recent schedule changes</summary>{change_rows}</details><details><summary>Source health</summary><table class="health-table"><tbody>{health_rows}</tbody></table></details><p class="updated">Updated {html.escape(generated)}<br>Refresh this page after the morning update to see the latest data.</p></aside></div>
<section class="event-popover" id="eventPopover" role="dialog" aria-labelledby="popoverTitle" hidden><button class="popover-close" id="popoverClose" type="button" aria-label="Close event details">×</button><div id="popoverContent"></div></section>
<script>const ALL_EVENTS={payload};
const $=id=>document.getElementById(id);let cursor=new Date();let mode='week';const state={{classes:true,coursework:true}};
const sameDay=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();const startDay=d=>new Date(d.getFullYear(),d.getMonth(),d.getDate());const addDays=(d,n)=>{{const x=new Date(d);x.setDate(x.getDate()+n);return x}};const weekStart=d=>{{const x=startDay(d),day=(x.getDay()+6)%7;return addDays(x,-day)}};const fmtTime=d=>d.toLocaleTimeString([],{{hour:'numeric',minute:'2-digit'}});const filtered=()=>ALL_EVENTS.filter(e=>state[e.calendar]);const eventDate=e=>new Date(e.start);const eventOccursOn=(e,d)=>{{if(!e.allDay)return sameDay(eventDate(e),d);const start=startDay(new Date(e.start)),end=startDay(new Date(e.end));return d>=start&&d<end}};const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}}[c]));
const sourceName=value=>({{brightspace:'Brightspace',gradescope:'Gradescope',webwork:'WeBWorK',zybooks:'zyBooks','Brightspace calendar':'Brightspace calendar','Course schedule':'Course schedule','Class schedule':'Class schedule'}}[value]||String(value||'Source').replace(/\\b\\w/g,c=>c.toUpperCase()));
const kindName=value=>({{class:'Class',due:'Assignment due',exam:'Exam',lecture:'Lecture topic',quiz:'Quiz',week_topic:'Weekly topic',course_event:'Course event',available:'Available',availability_end:'Availability ends',break:'Break',review:'Review'}}[value]||String(value||'Event').replaceAll('_',' ').replace(/\\b\\w/g,c=>c.toUpperCase()));
function eventCard(e,cls,style=''){{const sources=e.provenance?.map(p=>sourceName(p.source)).join(' + ')||sourceName(e.source),warning=e.conflict?' ⚠':'',label=e.courseCode+' · '+e.title+(e.conflictMessage?' · '+e.conflictMessage:'');return `<button type="button" data-event-id="${{escapeHtml(e.id)}}" class="${{cls}} ${{e.calendar}} ${{e.kind}} ${{e.conflict?'conflict':''}}" style="${{style}}" aria-label="${{escapeHtml(label)}}" title="${{escapeHtml(label)}}"><span class="event-time">${{e.allDay?'All day':fmtTime(new Date(e.start))}}${{warning}}</span><span class="event-title">${{escapeHtml(e.courseCode)}} · ${{escapeHtml(e.title)}}</span>${{sources?`<span class="event-source">${{escapeHtml(sources)}}</span>`:''}}${{e.location?`<span class="event-location">${{escapeHtml(e.location)}}</span>`:''}}</button>`}}
function renderTimeView(firstDay,count,viewClass){{const days=Array.from({{length:count}},(_,i)=>addDays(firstDay,i)),now=new Date(),last=days.at(-1);$('period').textContent=count===7?`${{days[0].toLocaleDateString([],{{month:'short',day:'numeric'}})}} – ${{last.toLocaleDateString([],{{month:'short',day:'numeric',year:'numeric'}})}}`:`${{days[0].toLocaleDateString([],{{weekday:'short',month:'short',day:'numeric'}})}} – ${{last.toLocaleDateString([],{{weekday:'short',month:'short',day:'numeric',year:'numeric'}})}}`;const heads=days.map(d=>`<div class="day-head ${{sameDay(d,now)?'today':''}}">${{d.toLocaleDateString([],{{weekday:'short'}})}}<strong>${{d.getDate()}}</strong></div>`).join('');const all=days.map(d=>`<div class="all-cell">${{filtered().filter(e=>e.allDay&&eventOccursOn(e,d)).map(e=>eventCard(e,'all-event')).join('')}}</div>`).join('');const axis=Array.from({{length:17}},(_,i)=>`<span class="hour-label" style="top:${{i*60}}px">${{new Date(2020,0,1,8+i).toLocaleTimeString([],{{hour:'numeric'}})}}</span>`).join('');const cols=days.map(d=>{{const ev=filtered().filter(e=>!e.allDay&&eventOccursOn(e,d));const blocks=ev.map(e=>{{const s=new Date(e.start),rawEnd=new Date(e.end),sm=s.getHours()*60+s.getMinutes(),em=Math.max(sm+30,rawEnd.getHours()*60+rawEnd.getMinutes());const top=Math.min(928,Math.max(0,sm-480)),height=Math.max(28,Math.min(960-top,em-sm));const overlaps=ev.filter(o=>{{const os=new Date(o.start),oe=new Date(o.end),a=os.getHours()*60+os.getMinutes(),b=Math.max(a+30,oe.getHours()*60+oe.getMinutes());return a<em&&b>sm}});const idx=Math.max(0,overlaps.findIndex(o=>o.id===e.id)),width=100/overlaps.length;return eventCard(e,'timed-event',`top:${{top}}px;height:${{height}}px;left:calc(${{idx*width}}% + 2px);width:calc(${{width}}% - 4px)`);}}).join('');let nowLine='';if(sameDay(d,now)){{const m=now.getHours()*60+now.getMinutes();if(m>=480&&m<=1440)nowLine=`<div class="now-line" style="top:${{m-480}}px"></div>`}}return `<div class="day-col">${{blocks}}${{nowLine}}</div>`}}).join('');$('calendar').innerHTML=`<div class="${{viewClass}}" style="--day-count:${{count}}"><div class="day-heads"><div></div>${{heads}}</div><div class="all-day"><div class="all-label">all day</div>${{all}}</div><div class="time-row"><div class="time-axis">${{axis}}</div>${{cols}}</div></div>`}}
const renderWeek=()=>renderTimeView(weekStart(cursor),7,'week');const renderTwoDay=()=>renderTimeView(startDay(cursor),2,'two-day');
function renderMonth(){{const first=new Date(cursor.getFullYear(),cursor.getMonth(),1),gridStart=weekStart(first),now=new Date();$('period').textContent=first.toLocaleDateString([],{{month:'long',year:'numeric'}});const headers=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(x=>`<div class="month-weekday">${{x}}</div>`).join('');const cells=Array.from({{length:42}},(_,i)=>{{const d=addDays(gridStart,i),ev=filtered().filter(e=>eventOccursOn(e,d)).sort((a,b)=>a.start.localeCompare(b.start)),shown=ev.slice(0,4);return `<div class="month-day ${{d.getMonth()!==cursor.getMonth()?'outside':''}}"><div class="month-number ${{sameDay(d,now)?'today':''}}">${{d.getDate()}}</div><div class="month-events">${{shown.map(e=>eventCard(e,'month-event')).join('')}}${{ev.length>4?`<div class="more">+${{ev.length-4}} more</div>`:''}}</div></div>`}}).join('');$('calendar').innerHTML=`<div class="month">${{headers}}${{cells}}</div>`}}
function renderUpcoming(){{const now=new Date(),until=addDays(now,14),ev=filtered().filter(e=>eventDate(e)>=startDay(now)&&eventDate(e)<until).sort((a,b)=>a.start.localeCompare(b.start)).slice(0,14);$('upcoming').innerHTML=ev.length?ev.map(e=>{{const d=eventDate(e);return `<button type="button" data-event-id="${{escapeHtml(e.id)}}" class="upcoming-item"><span class="date-box">${{d.toLocaleDateString([],{{weekday:'short'}})}}<strong>${{d.getDate()}}</strong></span><span><span class="upcoming-title">${{escapeHtml(e.courseCode)}} · ${{escapeHtml(e.title)}}</span><span class="upcoming-meta">${{e.allDay?'All day':fmtTime(d)}} · ${{e.calendar==='classes'?'Class':sourceName(e.source)}}</span></span></button>`}}).join(''):'<div class="empty">Nothing scheduled in the next two weeks.</div>'}}
const formatEventTime=e=>{{const start=new Date(e.start),end=new Date(e.end),dateOptions={{weekday:'long',month:'long',day:'numeric',year:'numeric'}};if(e.allDay){{const final=addDays(startDay(end),-1);return sameDay(start,final)?`${{start.toLocaleDateString([],dateOptions)}} · All day`:`${{start.toLocaleDateString([],{{month:'long',day:'numeric'}})}} – ${{final.toLocaleDateString([],dateOptions)}} · All day`}}const date=start.toLocaleDateString([],dateOptions);return end>start?`${{date}} · ${{fmtTime(start)}} – ${{fmtTime(end)}}`:`${{date}} · ${{fmtTime(start)}}`}};const fmtDateTime=value=>value?new Date(value).toLocaleString([],{{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}}):'';
function eventSources(e){{const seen=new Set(),links=[];const add=(url,source,primary=false)=>{{if(!url||seen.has(url))return;seen.add(url);links.push({{url,source:sourceName(source),primary}})}};const primarySource=e.provenance?.find(p=>p.url===e.url)?.source||e.source;add(e.url,primarySource,true);for(const item of e.provenance||[])add(item.url,item.source,false);return links}}
let lastEventTrigger=null;function positionPopover(anchor){{if(innerWidth<=650)return;const pop=$('eventPopover'),rect=anchor.getBoundingClientRect(),gap=10,width=pop.offsetWidth,height=pop.offsetHeight;let left=Math.min(rect.left,innerWidth-width-12),top=rect.bottom+gap;if(top+height>innerHeight-12)top=Math.max(12,rect.top-height-gap);pop.style.left=`${{Math.max(12,left)}}px`;pop.style.top=`${{top}}px`;pop.style.right='auto';pop.style.bottom='auto'}}
function openEventDetails(id,anchor){{const e=ALL_EVENTS.find(item=>item.id===id);if(!e)return;lastEventTrigger=anchor;const links=eventSources(e),primary=links.find(item=>item.primary),others=links.filter(item=>!item.primary),sourceSummary=[...new Set((e.provenance?.length?e.provenance:[{{source:e.source}}]).filter(item=>item.kind!=='resource').map(item=>sourceName(item.source)))].join(', '),action=e.kind==='due'?'Open assignment details':e.kind==='exam'?'Open exam details':'Open event details';const extra=`${{e.lateDueAt?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">↳</span><span>Late deadline: ${{escapeHtml(fmtDateTime(e.lateDueAt))}}</span></div>`:''}}${{e.availableFrom?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">◴</span><span>Available: ${{escapeHtml(fmtDateTime(e.availableFrom))}}</span></div>`:''}}${{e.availableUntil?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">◵</span><span>Availability ends: ${{escapeHtml(fmtDateTime(e.availableUntil))}}</span></div>`:''}}${{e.timingText?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">≋</span><span>${{escapeHtml(e.timingText)}}</span></div>`:''}}${{e.componentKind?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">◇</span><span>Component: ${{escapeHtml(e.componentKind)}}</span></div>`:''}}${{e.description?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">i</span><span>${{escapeHtml(e.description)}}</span></div>`:''}}`;$('popoverContent').innerHTML=`<div class="popover-kicker"><span class="dot ${{e.calendar==='classes'?'classes-dot':'coursework-dot'}}"></span>${{escapeHtml(e.calendar==='classes'?'Classes':'Coursework')}} · ${{escapeHtml(kindName(e.kind))}}</div><h2 id="popoverTitle">${{escapeHtml(e.title)}}</h2><p class="popover-course">${{escapeHtml(e.courseCode)}}</p><div class="popover-details"><div class="detail-row"><span class="detail-icon" aria-hidden="true">◷</span><span>${{escapeHtml(formatEventTime(e))}}</span></div>${{e.location?`<div class="detail-row"><span class="detail-icon" aria-hidden="true">⌖</span><span>${{escapeHtml(e.location)}}</span></div>`:''}}${{extra}}<div class="detail-row"><span class="detail-icon" aria-hidden="true">↗</span><span>${{escapeHtml(sourceSummary)}}</span></div></div>${{e.conflictMessage?`<div class="conflict-note">${{escapeHtml(e.conflictMessage)}}</div>`:''}}${{primary?`<a class="primary-action" href="${{escapeHtml(primary.url)}}" target="_blank" rel="noopener">${{action}}</a><small class="source-caption">Primary source: ${{escapeHtml(primary.source)}}</small>`:`<p class="source-only">Source: ${{escapeHtml(sourceName(e.source))}}</p>`}}${{others.length?`<div class="other-sources"><h3>Other sources</h3>${{others.map(item=>`<a class="source-link" href="${{escapeHtml(item.url)}}" target="_blank" rel="noopener"><span>Open ${{escapeHtml(item.source)}}</span><span aria-hidden="true">↗</span></a>`).join('')}}</div>`:''}}`;$('eventPopover').hidden=false;positionPopover(anchor);$('popoverClose').focus()}}
function closeEventDetails(){{$('eventPopover').hidden=true;$('eventPopover').removeAttribute('style');const trigger=lastEventTrigger;lastEventTrigger=null;if(trigger?.isConnected)trigger.focus()}}
function render(){{closeEventDetails();mode==='week'?renderWeek():mode==='two'?renderTwoDay():renderMonth();renderUpcoming();$('weekBtn').classList.toggle('active',mode==='week');$('twoDayBtn').classList.toggle('active',mode==='two');$('monthBtn').classList.toggle('active',mode==='month')}}
$('prev').onclick=()=>{{cursor=mode==='week'?addDays(cursor,-7):mode==='two'?addDays(cursor,-1):new Date(cursor.getFullYear(),cursor.getMonth()-1,1);render()}};$('next').onclick=()=>{{cursor=mode==='week'?addDays(cursor,7):mode==='two'?addDays(cursor,1):new Date(cursor.getFullYear(),cursor.getMonth()+1,1);render()}};$('today').onclick=()=>{{cursor=new Date();render()}};$('weekBtn').onclick=()=>{{mode='week';render()}};$('twoDayBtn').onclick=()=>{{mode='two';render()}};$('monthBtn').onclick=()=>{{mode='month';render()}};$('classesToggle').onchange=e=>{{state.classes=e.target.checked;render()}};$('courseworkToggle').onchange=e=>{{state.coursework=e.target.checked;render()}};$('popoverClose').onclick=closeEventDetails;document.addEventListener('click',event=>{{const trigger=event.target.closest('[data-event-id]');if(trigger){{openEventDetails(trigger.dataset.eventId,trigger);return}}if(!$('eventPopover').hidden&&!$('eventPopover').contains(event.target))closeEventDetails()}});document.addEventListener('keydown',event=>{{if(event.key==='Escape'&&!$('eventPopover').hidden)closeEventDetails()}});addEventListener('resize',()=>{{if(!$('eventPopover').hidden&&lastEventTrigger)positionPopover(lastEventTrigger)}});render();
</script></body></html>'''
