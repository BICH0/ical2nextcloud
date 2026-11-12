# ical2nextcloud

Imports events from external `.ics` / iCal feeds into your Nextcloud calendars via CalDAV. Run it manually or schedule it with cron to keep your Nextcloud calendars in sync with any public or private iCal URL.

## How it works

The script downloads each configured iCal feed and compares it against the target Nextcloud calendar:

- **New events** are created.
- **Changed events** (time, title, location…) are updated. Every imported event carries an `X-I2N-SOURCE` marker (a hash of the feed URL) and an `X-I2N-HASH` of its content, so unchanged events are not re-uploaded.
- **Events removed from the feed** (or marked `STATUS:CANCELLED`) are deleted, but only if they were imported from that feed **and have not finished yet**. Past events are kept, since many feeds only publish a limited time window.
- **Events you created by hand** in the same calendar have no marker and are never touched.

Recurring events and their modified occurrences are uploaded together as a single object, and the `VTIMEZONE` definitions they use are included, so feeds with non-standard time zones (e.g. Outlook's `Romance Standard Time`) keep their times.

### Safety checks

Deletions for a calendar are skipped (and the run exits non-zero) when:

- the feed download, parsing or the Nextcloud listing fails;
- the feed suddenly has no events;
- more than half of the upcoming imported events (and more than 5) would be deleted at once.

Use `--allow-mass-delete` to bypass the last two checks when the change is intended.

### Upgrading from older versions

Events imported by older versions have no marker. On the first run they are *adopted* (updated with the marker) if they are still in the feed. Events that had already disappeared from the feed before upgrading cannot be told apart from hand-made ones, so they must be removed manually once.

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
pip install requests icalendar python-dateutil
```

## Configuration

Copy the template and fill in your values:

```bash
cp .env.example .env
```

### Single user

| Variable         | Description                                                              |
|------------------|--------------------------------------------------------------------------|
| `NEXTCLOUD_BASE` | Base URL of your Nextcloud instance, e.g. `https://cloud.example.com`   |
| `NEXTCLOUD_USER` | Your Nextcloud username                                                  |
| `NEXTCLOUD_PASS` | A Nextcloud **App Password** (not your login password — see note below)  |
| `CALENDARS`      | Comma-separated list of `caldav-name\|ical-url[|emoji]` entries          |

### Multiple users

Use a `USER_N_` prefix for each user (N = 1, 2, 3, …). Users can belong to the same or different Nextcloud instances.

| Variable            | Description                                      |
|---------------------|--------------------------------------------------|
| `USER_N_BASE`       | Base URL of the Nextcloud instance for user N    |
| `USER_N_USER`       | Username for user N                              |
| `USER_N_PASS`       | App Password for user N                          |
| `USER_N_CALENDARS`  | Calendars for user N (same format as `CALENDARS`)|

```
USER_1_BASE=https://cloud.example.com
USER_1_USER=alice
USER_1_PASS=alice_app_password
USER_1_CALENDARS=work|https://example.com/work.ics|💼

USER_2_BASE=https://cloud.example.com
USER_2_USER=bob
USER_2_PASS=bob_app_password
USER_2_CALENDARS=personal|https://example.com/personal.ics|🏠
```

### CALENDARS format

```
CALENDARS=local-cal-name|https://url-to-feed.ics[|emoji],another-cal|https://other-feed.ics
```

- `local-cal-name` — the URL name of an existing calendar in Nextcloud (case-sensitive). This is not always the display name: check it in Nextcloud Calendar → calendar menu → *Copy private link* (the last path segment, e.g. `personal`).
- `https://url-to-feed.ics` — the iCal feed URL to import from.
- `emoji` — (optional) prepended to every imported event's title. Accepts a literal emoji or a Unicode escape (`\U0001F4BC`).

Example:

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
python ical2nextcloud.py              # sync
python ical2nextcloud.py --dry-run    # show what would change, without touching Nextcloud
```

`.env` is read from the script's directory, so it works no matter where it is launched from. The script logs progress to stdout:

```
2026-11-11 10:00:00 [INFO] Starting sync for 1 account(s), 1 calendar(s)
2026-11-11 10:00:00 [INFO] ── Syncing 'work' from https://...
2026-11-11 10:00:01 [INFO]   Events in feed: 43, in Nextcloud: 51
2026-11-11 10:00:02 [INFO]   ✓ Added: 💼 Kick-off (2026-11-20)
2026-11-11 10:00:02 [INFO]   ↻ Updated: 💼 Offsite (2026-12-01)
2026-11-11 10:00:02 [INFO]   ✗ Deleted: 💼 Old meeting (2026-11-15)
2026-11-11 10:00:02 [INFO]   Result — added: 1, updated: 1, deleted: 1, unchanged: 41, errors: 0
2026-11-11 10:00:02 [INFO] Sync completed.
```

Changes made by hand in Nextcloud to an imported event are kept until that event changes in the feed, at which point the feed version wins.

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