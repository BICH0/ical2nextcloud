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

NEXTCLOUD_BASE = os.environ.get("NEXTCLOUD_BASE", "").rstrip("/")
NEXTCLOUD_USER = os.environ.get("NEXTCLOUD_USER", "")
NEXTCLOUD_PASS = os.environ.get("NEXTCLOUD_PASS", "")

if not all([NEXTCLOUD_BASE, NEXTCLOUD_USER, NEXTCLOUD_PASS]):
    log.error("Missing environment variables: NEXTCLOUD_BASE, NEXTCLOUD_USER, NEXTCLOUD_PASS")
    sys.exit(1)

CALDAV_ROOT = f"{NEXTCLOUD_BASE}/remote.php/dav/calendars/{NEXTCLOUD_USER}"

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

_raw_calendars = os.environ.get("CALENDARS", "")
CALENDARS = _parse_calendars(_raw_calendars)

if not CALENDARS:
    log.error(
        "No calendars configured. Set CALENDARS in .env:\n"
        "  CALENDARS=caldav-name-1|https://url1,caldav-name-2|https://url2"
    )
    sys.exit(1)


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


def sync_calendar(cal_name: str, ical_url: str, auth: tuple, emoji: str = ""):
    """Syncs a .ical feed into its corresponding local Nextcloud calendar."""
    caldav_url = f"{CALDAV_ROOT}/{cal_name}/"
    log.info(f"── Syncing '{cal_name}' from {ical_url}")

    ical_data = fetch_ical(ical_url)
    if ical_data is None:
        log.error(f"  Skipping '{cal_name}' due to download error.")
        return

    try:
        cal = Calendar.from_ical(ical_data)
    except Exception as e:
        log.error(f"  Error parsing .ical for '{cal_name}': {e}")
        return

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

def main():
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASS)
    log.info(f"Starting sync for {len(CALENDARS)} calendar(s)")

    for entry in CALENDARS:
        sync_calendar(entry["caldav-name"], entry["url"], auth, entry["emoji"])

    log.info("Sync completed.")


if __name__ == "__main__":
    main()