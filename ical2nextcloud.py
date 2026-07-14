#!/usr/bin/env python3
"""
ical_sync.py — Syncs external .ical calendars into local Nextcloud calendars.
Credentials are read from environment variables or a .env file.
"""

import os
import re
import sys
import logging
import requests
from icalendar import Calendar
from xml.etree import ElementTree as ET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

def load_dotenv(path=".env"):
    """Loads a .env file without any external dependencies."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

load_dotenv()


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


def fetch_ical(url: str) -> bytes | None:
    """Downloads a .ical file and returns its content as bytes."""
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.error(f"Could not download {url}: {e}")
        return None


def get_existing_uids(caldav_url: str, auth: tuple) -> set:
    """Fetches the UIDs of events already present in the CalDAV calendar."""
    try:
        r = requests.request(
            "PROPFIND",
            caldav_url,
            auth=auth,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data="""<?xml version="1.0"?>
            <d:propfind xmlns:d="DAV:">
              <d:prop><d:getetag/></d:prop>
            </d:propfind>""",
            timeout=15,
        )
        uids = set()
        root = ET.fromstring(r.text)
        for href in root.iter("{DAV:}href"):
            if href.text and href.text.endswith(".ics"):
                uid = href.text.rsplit("/", 1)[-1][:-4]
                uids.add(uid)
        return uids
    except requests.RequestException as e:
        log.warning(f"Could not fetch existing UIDs: {e}")
        return set()


def upload_event(caldav_url: str, uid: str, ical_bytes: bytes, auth: tuple) -> str:
    """Uploads a single event. Returns 'added', 'skipped', or 'error'."""
    url = f"{caldav_url}{uid}.ics"
    try:
        r = requests.put(
            url,
            data=ical_bytes,
            auth=auth,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
            timeout=15,
        )
        if r.status_code in (201, 204):
            return "added"
        if r.status_code == 400 and "already exists" in r.text:
            return "skipped"
        log.warning(f"  UID {uid}: unexpected response {r.status_code} — {r.text[:300]}")
        return "error"
    except requests.RequestException as e:
        log.error(f"  Error uploading UID {uid}: {e}")
        return "error"


def sync_calendar(caldav_root: str, cal_name: str, ical_url: str, auth: tuple, emoji: str = "") -> int:
    """Syncs a .ical feed into its corresponding local Nextcloud calendar.

    Returns the number of errors encountered (download/parse/upload failures).
    """
    caldav_url = f"{caldav_root}/{cal_name}/"
    log.info(f"── Syncing '{cal_name}' from {ical_url}")

    ical_data = fetch_ical(ical_url)
    if ical_data is None:
        log.error(f"  Skipping '{cal_name}' due to download error.")
        return 1

    try:
        cal = Calendar.from_ical(ical_data)
    except Exception as e:
        log.error(f"  Error parsing .ical for '{cal_name}': {e}")
        return 1

    existing_uids = get_existing_uids(caldav_url, auth)
    log.info(f"  Events already in Nextcloud: {len(existing_uids)}")

    added = skipped = errors = 0

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID", "")).strip()
        if not uid:
            log.warning("  Event has no UID, skipping.")
            continue
        if uid in existing_uids:
            skipped += 1
            continue

        if emoji:
            summary = str(component.get("SUMMARY", ""))
            component["SUMMARY"] = f"{emoji} {summary}" if summary else emoji

        wrapper = Calendar()
        wrapper.add("prodid", "-//ical_sync//EN")
        wrapper.add("version", "2.0")
        wrapper.add_component(component)

        result = upload_event(caldav_url, uid, wrapper.to_ical(), auth)
        if result == "added":
            summary = str(component.get("SUMMARY", uid))
            dtstart = component.get("DTSTART")
            date_str = dtstart.dt.strftime("%Y-%m-%d") if dtstart else "?"
            log.info(f"  ✓ Added: {summary} ({date_str})")
            added += 1
        elif result == "skipped":
            skipped += 1
        else:
            errors += 1

    log.info(f"  Result — added: {added}, already existed: {skipped}, errors: {errors}")
    return errors


def main():
    accounts = _load_accounts()
    total_cals = sum(len(a["calendars"]) for a in accounts)
    log.info(f"Starting sync for {len(accounts)} account(s), {total_cals} calendar(s)")

    total_errors = 0
    for account in accounts:
        if len(accounts) > 1:
            log.info(f"━━ Account: {account['label']}")
        for entry in account["calendars"]:
            total_errors += sync_calendar(account["caldav_root"], entry["caldav-name"], entry["url"], account["auth"], entry["emoji"])

    log.info("Sync completed.")
    if total_errors:
        log.error(f"{total_errors} error(s) during sync")
        sys.exit(1)


if __name__ == "__main__":
    main()
