# ical2nextcloud

Imports events from external `.ics` / iCal feeds into your Nextcloud calendars via CalDAV. Run it manually or schedule it with cron to keep your Nextcloud calendars in sync with any public or private iCal URL.

## How it works

The script downloads each configured iCal feed, compares its events against those already present in the target Nextcloud calendar, and uploads only the new ones. Existing events are never deleted or modified.

## Requirements

- Python 3.10+
- A running Nextcloud instance with CalDAV enabled
- The target calendars must already exist in Nextcloud before running the script

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/BiCH0/ical2nextcloud.git
cd ical2nextcloud

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install requests icalendar
```

## Configuration

Edit `.env` (already included in the repo with placeholder values):

| Variable         | Description                                                                 |
|------------------|-----------------------------------------------------------------------------|
| `NEXTCLOUD_BASE` | Base URL of your Nextcloud instance, e.g. `https://cloud.example.com`      |
| `NEXTCLOUD_USER` | Your Nextcloud username                                                     |
| `NEXTCLOUD_PASS` | A Nextcloud **App Password** (not your login password — see note below)     |
| `CALENDARS`      | Comma-separated list of `caldav-name\|ical-url` pairs (see format below)    |

### CALENDARS format

```
CALENDARS=local-cal-name|https://url-to-feed.ics[|emoji],another-cal|https://other-feed.ics
```

- `local-cal-name` — the exact name of an existing calendar in Nextcloud (case-sensitive).
- `https://url-to-feed.ics` — the iCal feed URL to import from.
- `emoji` — (optional) prepended to every imported event's title.

Example with two calendars:

```
CALENDARS=Work|https://calendar.google.com/calendar/ical/abc123/basic.ics|💼,Holidays|https://www.example.com/holidays.ics|🎉
```

### Creating a Nextcloud App Password

Using your real password in `.env` is discouraged. Generate an App Password instead:

1. Log in to Nextcloud.
2. Go to **Settings > Security > Devices & sessions**.
3. Under **App passwords**, enter a name (e.g. `ical2nextcloud`) and click **Create new app password**.
4. Copy the generated password into `NEXTCLOUD_PASS`.

## Usage

```bash
python ical2nextcloud.py
```

The script logs progress to stdout:

```
2026-03-20 10:00:00 [INFO] Starting sync for 2 calendar(s)
2026-03-20 10:00:00 [INFO] ── Syncing 'Work' from https://...
2026-03-20 10:00:01 [INFO]   Events already in Nextcloud: 42
2026-03-20 10:00:02 [INFO]   ✓ Added: event-uid-xyz
2026-03-20 10:00:02 [INFO]   Result — added: 1, already existed: 42, errors: 0
2026-03-20 10:00:02 [INFO] Sync completed.
```

## Scheduling with cron (Linux / macOS)

Run the sync automatically every hour:

```bash
crontab -e
```

Add:

```cron
0 * * * * /path/to/.venv/bin/python /path/to/ical2nextcloud/ical2nextcloud.py >> /var/log/ical2nextcloud.log 2>&1
```

## Security

- **Never commit `.env`** — it is already listed in `.gitignore`.
- Always use an App Password instead of your Nextcloud account password.