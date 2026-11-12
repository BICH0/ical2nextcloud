#!/usr/bin/env python3
"""
ical2nextcloud.py — Syncs external .ical feeds into Nextcloud calendars.

New events are created, changed events are updated and events removed from a feed are deleted,
but only if they were imported by this script and have not finished yet.
Credentials are read from environment variables or a .env file next to the script.
"""

import argparse
import copy
import hashlib
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests
from dateutil.rrule import rrulestr
from icalendar import Calendar

SOURCE_PROP = "X-I2N-SOURCE"   # hash of the feed URL the event was imported from
HASH_PROP = "X-I2N-HASH"       # hash of the source event content at import time
MASS_DELETE_MIN = 5
MASS_DELETE_RATIO = 0.5
SAFE_UID = re.compile(r"^[A-Za-z0-9@._-]+$")
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
NEVER = datetime.min.replace(tzinfo=timezone.utc)

REPORT_BODY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class SyncError(Exception):
    pass


def load_dotenv(path: str | None = None):
    """Loads a .env file (default: next to this script) without any external dependencies."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def _decode_emoji(value: str) -> str:
    """Converts Unicode escapes like \\U0001F3D6 or \\u2615 to actual characters."""
    return re.sub(r"\\U([0-9a-fA-F]{8})|\\u([0-9a-fA-F]{4})", lambda m: chr(int((m.group(1) or m.group(2)), 16)), value)


def _parse_calendars(raw: str) -> list[dict]:
    entries = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split("|")
        if len(parts) < 2:
            log.warning(f"Skipping malformed CALENDARS entry (expected 'name|url'): {item!r}")
            continue
        name, url = parts[0].strip(), parts[1].strip()
        emoji = _decode_emoji(parts[2].strip()) if len(parts) >= 3 else ""
        entries.append({"caldav-name": name, "url": url, "emoji": emoji})
    return entries


def _load_accounts() -> list[dict]:
    """Builds the list of accounts to sync from environment variables.

    Supports two formats:
      - Multi-user:  USER_1_BASE, USER_1_USER, USER_1_PASS, USER_1_CALENDARS, ...
      - Single-user: NEXTCLOUD_BASE, NEXTCLOUD_USER, NEXTCLOUD_PASS, CALENDARS  (legacy)
    """
    accounts = []

    i = 1
    while True:
        user = os.environ.get(f"USER_{i}_USER", "")
        if not user:
            break
        base = os.environ.get(f"USER_{i}_BASE", "").rstrip("/")
        passwd = os.environ.get(f"USER_{i}_PASS", "")
        calendars = _parse_calendars(os.environ.get(f"USER_{i}_CALENDARS", ""))

        missing = [k for k, v in {f"USER_{i}_BASE": base, f"USER_{i}_PASS": passwd}.items() if not v]
        if missing:
            log.error(f"Account {i}: missing variables: {', '.join(missing)}")
            i += 1
            continue
        if not calendars:
            log.error(f"Account {i}: no calendars configured in USER_{i}_CALENDARS")
            i += 1
            continue

        accounts.append({"label": user, "auth": (user, passwd), "caldav_root": f"{base}/remote.php/dav/calendars/{user}", "calendars": calendars})
        i += 1

    if not accounts:
        base = os.environ.get("NEXTCLOUD_BASE", "").rstrip("/")
        user = os.environ.get("NEXTCLOUD_USER", "")
        passwd = os.environ.get("NEXTCLOUD_PASS", "")
        calendars = _parse_calendars(os.environ.get("CALENDARS", ""))

        if not all([base, user, passwd]):
            log.error("Missing environment variables: NEXTCLOUD_BASE, NEXTCLOUD_USER, NEXTCLOUD_PASS")
            sys.exit(1)
        if not calendars:
            log.error("No calendars configured. Set CALENDARS in .env:\n  CALENDARS=caldav-name-1|https://url1,caldav-name-2|https://url2")
            sys.exit(1)

        accounts.append({"label": user, "auth": (user, passwd), "caldav_root": f"{base}/remote.php/dav/calendars/{user}", "calendars": calendars})

    return accounts


# ── Event helpers ──────────────────────────────────────────────────────────────

def _to_aware(value) -> datetime:
    """Dates become local midnight and floating datetimes local time, so they can be compared."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    return datetime(value.year, value.month, value.day).astimezone()


def _component_end(component) -> datetime | None:
    """Returns when the event (or its whole series) ends, or None if it recurs forever."""
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return NEVER
    start = dtstart.dt
    if "DTEND" in component:
        end = component["DTEND"].dt
    elif "DURATION" in component:
        end = start + component["DURATION"].dt
    elif not isinstance(start, datetime):
        end = start + timedelta(days=1)
    else:
        end = start

    rrule = component.get("RRULE")
    if rrule is None:
        return _to_aware(end)

    duration = _to_aware(end) - _to_aware(start)
    try:
        if "UNTIL" in rrule:
            return _to_aware(rrule["UNTIL"][0]) + duration
        if "COUNT" in rrule:
            rule = rrulestr(rrule.to_ical().decode(), dtstart=_to_aware(start))
            return list(rule)[-1] + duration
    except Exception as e:
        log.warning(f"  Could not evaluate RRULE of {component.get('UID')}: {e}")
        return NEVER
    return None


def _object_end(events: list) -> datetime | None:
    ends = [_component_end(e) for e in events]
    return None if any(e is None for e in ends) else max(ends, default=NEVER)


def _is_finished(end: datetime | None, now: datetime) -> bool:
    return end is not None and end <= now


def _referenced_tzids(component) -> set[str]:
    tzids = set()
    for value in component.values():
        for item in value if isinstance(value, list) else [value]:
            tzid = getattr(item, "params", {}).get("TZID")
            if tzid:
                tzids.add(str(tzid))
    return tzids


def _describe(events: list, emoji: str = "") -> str:
    master = next((e for e in events if "RECURRENCE-ID" not in e), events[0])
    summary = str(master.get("SUMMARY", master.get("UID", "?")))
    if emoji and not summary.startswith(emoji):
        summary = f"{emoji} {summary}"
    dtstart = master.get("DTSTART")
    return f"{summary} ({dtstart.dt.strftime('%Y-%m-%d') if dtstart else '?'})"


def _href_for(uid: str) -> str:
    if SAFE_UID.match(uid):
        return f"{uid}.ics"
    return f"{hashlib.sha1(uid.encode('utf-8')).hexdigest()}.ics"


def source_id(ical_url: str) -> str:
    return hashlib.sha256(ical_url.encode("utf-8")).hexdigest()[:16]


# ── Feed side ──────────────────────────────────────────────────────────────────

def fetch_ical(url: str) -> bytes | None:
    """Downloads a .ical file and returns its content as bytes."""
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.error(f"Could not download {url}: {e}")
        return None


def group_feed(cal: Calendar) -> tuple[dict[str, list], dict[str, object]]:
    """Groups the feed's VEVENTs by UID (a series and its overrides share one) and drops cancelled ones."""
    groups: dict[str, list] = {}
    timezones = {}
    for component in cal.subcomponents:
        if component.name == "VTIMEZONE":
            timezones[str(component.get("TZID"))] = component
        elif component.name == "VEVENT":
            uid = str(component.get("UID", "")).strip()
            if not uid:
                log.warning("  Event has no UID, skipping.")
                continue
            groups.setdefault(uid, []).append(component)

    for uid in list(groups):
        master = [e for e in groups[uid] if "RECURRENCE-ID" not in e]
        if master and str(master[0].get("STATUS", "")).upper() == "CANCELLED":
            del groups[uid]
    return groups, timezones


def build_object(events: list, timezones: dict, emoji: str, source: str) -> tuple[bytes, str]:
    """Wraps one UID's events (plus the VTIMEZONEs they use) into a calendar object; returns it and its content hash."""
    tzids = set().union(*(_referenced_tzids(e) for e in events))
    used_tz = [timezones[t] for t in sorted(tzids) if t in timezones]

    digest = hashlib.sha256(emoji.encode("utf-8"))
    for component in [*used_tz, *events]:
        clean = copy.deepcopy(component)
        for prop in ("DTSTAMP", SOURCE_PROP, HASH_PROP):
            clean.pop(prop, None)
        digest.update(clean.to_ical())
    content_hash = digest.hexdigest()[:32]

    wrapper = Calendar()
    wrapper.add("prodid", "-//ical2nextcloud//EN")
    wrapper.add("version", "2.0")
    for tz in used_tz:
        wrapper.add_component(tz)
    for component in events:
        event = copy.deepcopy(component)
        for prop in (SOURCE_PROP, HASH_PROP):
            event.pop(prop, None)
        if emoji:
            summary = str(event.get("SUMMARY", ""))
            event.pop("SUMMARY", None)
            event.add("SUMMARY", f"{emoji} {summary}" if summary else emoji)
        event.add(SOURCE_PROP, source)
        event.add(HASH_PROP, content_hash)
        wrapper.add_component(event)
    return wrapper.to_ical(), content_hash


# ── Server side ────────────────────────────────────────────────────────────────

def list_objects(session: requests.Session, caldav_url: str) -> list[dict]:
    """Lists every event object in the calendar with its href, etag, UID, sync markers and end."""
    try:
        r = session.request(
            "REPORT",
            caldav_url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            data=REPORT_BODY.encode("utf-8"),
            timeout=60,
        )
    except requests.RequestException as e:
        raise SyncError(f"could not list calendar: {e}")
    if r.status_code != 207:
        raise SyncError(f"could not list calendar: HTTP {r.status_code} — {r.text[:200]}")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise SyncError(f"invalid listing response: {e}")

    objects = []
    for response in root.findall("d:response", NS):
        href = response.findtext("d:href", default="", namespaces=NS)
        data = response.findtext(".//c:calendar-data", default="", namespaces=NS)
        if not href or not data:
            continue
        obj = {"href": urljoin(caldav_url, href), "etag": response.findtext(".//d:getetag", default="", namespaces=NS),
               "uid": "", "source": "", "hash": "", "end": NEVER, "label": href}
        try:
            events = [c for c in Calendar.from_ical(data).subcomponents if c.name == "VEVENT"]
        except Exception as e:
            log.warning(f"  Could not parse server object {href}: {e}")
            objects.append(obj)
            continue
        if events:
            obj["uid"] = str(events[0].get("UID", "")).strip()
            obj["source"] = next((str(e[SOURCE_PROP]) for e in events if SOURCE_PROP in e), "")
            obj["hash"] = next((str(e[HASH_PROP]) for e in events if HASH_PROP in e), "")
            obj["end"] = _object_end(events)
            obj["label"] = _describe(events)
        objects.append(obj)
    return objects


def plan_sync(
    feed: dict[str, tuple[bytes, str, str]],
    server: list[dict],
    source: str,
    now: datetime,
    allow_mass_delete: bool = False,
) -> dict:
    """Decides what to create, update and delete. feed maps UID -> (object bytes, content hash, label)."""
    by_uid = {o["uid"]: o for o in server if o["uid"]}
    plan = {"create": [], "update": [], "delete": [], "unchanged": 0, "conflicts": [], "delete_blocked": ""}

    for uid, (data, content_hash, label) in feed.items():
        existing = by_uid.get(uid)
        if existing is None:
            plan["create"].append({"uid": uid, "data": data, "label": label})
        elif existing["source"] and existing["source"] != source:
            plan["conflicts"].append(label)
        elif existing["source"] == source and existing["hash"] == content_hash:
            plan["unchanged"] += 1
        else:
            plan["update"].append({"uid": uid, "data": data, "label": label, "href": existing["href"], "etag": existing["etag"]})

    active = [o for o in server if o["source"] == source and not _is_finished(o["end"], now)]
    deletes = [o for o in active if o["uid"] not in feed]

    if deletes and not allow_mass_delete:
        if not feed:
            plan["delete_blocked"] = "the feed has no events"
        elif len(deletes) > MASS_DELETE_MIN and len(deletes) > MASS_DELETE_RATIO * len(active):
            plan["delete_blocked"] = f"{len(deletes)} of {len(active)} upcoming events would be deleted"
    if not plan["delete_blocked"]:
        plan["delete"] = deletes
    return plan


def _put(session: requests.Session, url: str, data: bytes, condition: dict) -> requests.Response:
    headers = {"Content-Type": "text/calendar; charset=utf-8", **condition}
    return session.put(url, data=data, headers=headers, timeout=15)


def apply_plan(session: requests.Session, caldav_url: str, plan: dict, dry_run: bool) -> tuple[int, int, int, int]:
    """Executes the plan against the server; returns (added, updated, deleted, errors)."""
    added = updated = deleted = errors = 0
    prefix = "[dry-run] " if dry_run else ""

    for item in plan["create"]:
        if not dry_run:
            try:
                r = _put(session, urljoin(caldav_url, _href_for(item["uid"])), item["data"], {"If-None-Match": "*"})
            except requests.RequestException as e:
                log.error(f"  Error creating {item['label']}: {e}")
                errors += 1
                continue
            if r.status_code not in (201, 204):
                log.warning(f"  Could not create {item['label']}: HTTP {r.status_code} — {r.text[:300]}")
                errors += 1
                continue
        log.info(f"  {prefix}✓ Added: {item['label']}")
        added += 1

    for item in plan["update"]:
        if not dry_run:
            condition = {"If-Match": item["etag"]} if item["etag"] else {}
            try:
                r = _put(session, item["href"], item["data"], condition)
            except requests.RequestException as e:
                log.error(f"  Error updating {item['label']}: {e}")
                errors += 1
                continue
            if r.status_code not in (200, 201, 204):
                log.warning(f"  Could not update {item['label']}: HTTP {r.status_code} — {r.text[:300]}")
                errors += 1
                continue
        log.info(f"  {prefix}↻ Updated: {item['label']}")
        updated += 1

    for obj in plan["delete"]:
        if not dry_run:
            headers = {"If-Match": obj["etag"]} if obj["etag"] else {}
            try:
                r = session.delete(obj["href"], headers=headers, timeout=15)
            except requests.RequestException as e:
                log.error(f"  Error deleting {obj['label']}: {e}")
                errors += 1
                continue
            if r.status_code not in (200, 204, 404):
                log.warning(f"  Could not delete {obj['label']}: HTTP {r.status_code} — {r.text[:300]}")
                errors += 1
                continue
        log.info(f"  {prefix}✗ Deleted: {obj['label']}")
        deleted += 1

    return added, updated, deleted, errors


def sync_calendar(
    session: requests.Session,
    caldav_root: str,
    cal_name: str,
    ical_url: str,
    emoji: str = "",
    dry_run: bool = False,
    allow_mass_delete: bool = False,
) -> int:
    """Syncs a .ical feed into its Nextcloud calendar; returns the number of errors encountered."""
    caldav_url = f"{caldav_root}/{cal_name}/"
    log.info(f"── Syncing '{cal_name}' from {ical_url}")

    ical_data = fetch_ical(ical_url)
    if ical_data is None:
        log.error(f"  Skipping '{cal_name}' due to download error.")
        return 1

    try:
        groups, timezones = group_feed(Calendar.from_ical(ical_data))
    except Exception as e:
        log.error(f"  Error parsing .ical for '{cal_name}': {e}")
        return 1

    try:
        server = list_objects(session, caldav_url)
    except SyncError as e:
        log.error(f"  Skipping '{cal_name}': {e}")
        return 1
    log.info(f"  Events in feed: {len(groups)}, in Nextcloud: {len(server)}")

    source = source_id(ical_url)
    feed = {}
    for uid, events in groups.items():
        data, content_hash = build_object(events, timezones, emoji, source)
        feed[uid] = (data, content_hash, _describe(events, emoji))

    plan = plan_sync(feed, server, source, datetime.now(timezone.utc), allow_mass_delete)
    errors = 0
    for label in plan["conflicts"]:
        log.warning(f"  Skipping {label}: its UID is already managed by another feed in this calendar")
    if plan["delete_blocked"]:
        log.error(f"  Deletions skipped for safety: {plan['delete_blocked']} (use --allow-mass-delete to force)")
        errors += 1

    added, updated, deleted, apply_errors = apply_plan(session, caldav_url, plan, dry_run)
    errors += apply_errors
    log.info(f"  Result — added: {added}, updated: {updated}, deleted: {deleted}, unchanged: {plan['unchanged']}, errors: {errors}")
    return errors


def main():
    parser = argparse.ArgumentParser(description="Sync iCal feeds into Nextcloud calendars.")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without touching Nextcloud")
    parser.add_argument("--allow-mass-delete", action="store_true", help="skip the safety check that blocks large deletions")
    args = parser.parse_args()

    load_dotenv()
    accounts = _load_accounts()
    total_cals = sum(len(a["calendars"]) for a in accounts)
    log.info(f"Starting sync for {len(accounts)} account(s), {total_cals} calendar(s){' (dry run)' if args.dry_run else ''}")

    total_errors = 0
    for account in accounts:
        if len(accounts) > 1:
            log.info(f"━━ Account: {account['label']}")
        with requests.Session() as session:
            session.auth = account["auth"]
            for entry in account["calendars"]:
                total_errors += sync_calendar(
                    session, account["caldav_root"], entry["caldav-name"], entry["url"], entry["emoji"],
                    args.dry_run, args.allow_mass_delete,
                )

    log.info("Sync completed.")
    if total_errors:
        log.error(f"{total_errors} error(s) during sync")
        sys.exit(1)


if __name__ == "__main__":
    main()
