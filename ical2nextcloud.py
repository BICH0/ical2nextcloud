#!/usr/bin/env python3
"""
ical_sync.py — Syncs external .ical calendars into local Nextcloud calendars.
Credentials are read from environment variables or a .env file.
"""

import os
import sys
import logging
import requests
from icalendar import Calendar

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

def _parse_calendars(raw: str) -> list[dict]:
    entries = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "|" not in item:
            log.warning(f"Skipping malformed CALENDARS entry (expected 'name|url'): {item!r}")
            continue
        name, _, url = item.partition("|")
        entries.append({"caldav-name": name.strip(), "url": url.strip()})
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
        for line in r.text.splitlines():
            if ".ics" in line:
                start = line.rfind("/") + 1
                end = line.find(".ics", start)
                if start > 0 and end > 0:
                    uids.add(line[start:end])
        return uids
    except requests.RequestException as e:
        log.warning(f"Could not fetch existing UIDs: {e}")
        return set()


def upload_event(caldav_url: str, uid: str, ical_bytes: bytes, auth: tuple) -> bool:
    """Uploads a single event to the Nextcloud CalDAV calendar."""
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
            return True
        else:
            log.warning(f"  UID {uid}: unexpected response {r.status_code}")
            return False
    except requests.RequestException as e:
        log.error(f"  Error uploading UID {uid}: {e}")
        return False


def sync_calendar(cal_name: str, ical_url: str, auth: tuple):
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

        wrapper = Calendar()
        wrapper.add("prodid", "-//ical_sync//EN")
        wrapper.add("version", "2.0")
        wrapper.add_component(component)

        if upload_event(caldav_url, uid, wrapper.to_ical(), auth):
            log.info(f"  ✓ Added: {uid}")
            added += 1
        else:
            errors += 1

    log.info(f"  Result — added: {added}, already existed: {skipped}, errors: {errors}")

def main():
    auth = (NEXTCLOUD_USER, NEXTCLOUD_PASS)
    log.info(f"Starting sync for {len(CALENDARS)} calendar(s)")

    for entry in CALENDARS:
        sync_calendar(entry["caldav-name"], entry["url"], auth)

    log.info("Sync completed.")


if __name__ == "__main__":
    main()