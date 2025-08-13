![MC Pigeon](./logo.png)
# MC Pigeon

> An IMAP-first, Django-powered email campaign engine with SMTP send, IMAP APPEND, open/click tracking, and one-click unsubscribe. Markdown-only templates with auto text fallback and an auto-appended tracking pixel. Built to work great from the Django Admin and with MCP tools for agents.

*   **Stack**: Python 3.13, Django 5+, Celery 5+, PostgreSQL (recommended)
*   **Why IMAP-first?**: Your mail lives in your own mailbox, making it auditable and portable. No vendor lock-in. All sends are automatically saved to your `Sent` folder.

---

## Features

*   **Campaigns & Recipients**: Manage campaigns and per-recipient message tracking.
*   **SMTP & IMAP**: Sends via SMTP and appends a copy to your IMAP `Sent` folder.
*   **IMAP Sync**: Scans your `bounce_folder` for bounces and replies, updating campaign stats.
*   **Tracking**:
    *   **Opens**: A tracking pixel is auto-appended to all HTML emails.
    *   **Clicks**: All links are automatically rewritten for tracking.
*   **Unsubscribe**: A visible unsubscribe link and `List-Unsubscribe` headers are added automatically.
*   **Asynchronous Sending**: All campaigns are sent in the background using Celery for reliability and scale.
*   **Admin UI**: A clean Django Admin interface with actions to enqueue campaigns and view detailed stats.
*   **MCP Tools**: A full suite of tools for programmatic control by AI agents.

---

## Quickstart

This project is a standard Django app. The quickest way to get started is to clone the repository and set up the environment.

### 1. Setup

```bash
# Clone the repository
git clone <repository_url> mcpigeon
cd mcpigeon

# Create and activate a virtual environment
python3 -m venv env
source env/bin/activate

# Install dependencies
pip install -r requirements.in
```

### 2. Configuration

In your `MCPigeon/settings.py`:

```python
# settings.py

# Required for tracking links, unsubscribe URLs, etc.
CAMPAIGNS_PUBLIC_BASE_URL = "https://your.domain.com"

# Celery configuration (example using Redis)
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"

# Batch size for sending emails via Celery
CAMPAIGNS_BATCH_SIZE = 100
```

### 3. Database & Server

```bash
# Run database migrations
python manage.py migrate

# Create a superuser to access the admin
python manage.py createsuperuser

# Start the Django development server
python manage.py runserver
```

### 4. Run Celery Worker

In a separate terminal, start the Celery worker:

```bash
# Make sure your virtual environment is activated
source env/bin/activate

# Start the worker
celery -A MCPigeon worker -l info
```

You can now access the Django Admin at `http://127.0.0.1:8000/admin/`.

---

## Usage

1.  **Create a Mailbox**: In the admin, go to `Campaigns > Mailboxes` and add your SMTP/IMAP credentials. This is where your emails will be sent from.
2.  **Create a Campaign**: Go to `Campaigns > Campaigns` and create a new campaign. Write your email content in Markdown.
3.  **Add Recipients**: Add recipients to your campaign directly in the admin interface.
4.  **Enqueue for Sending**: From the `Campaigns` list view, select your campaign and choose the **"Enqueue selected campaigns for sending"** action. The Celery worker will pick it up and start sending.

---

## Data Model

*   **Mailbox**: Stores SMTP & IMAP credentials, `sent_folder`, and `bounce_folder`.
*   **Campaign**: The core email to be sent, including subject and Markdown content.
*   **Recipient**: An email address associated with a campaign.
*   **MessageInstance**: A per-recipient record tracking the status of an email (sent, opened, bounced, etc.).
*   **Link / LinkClick**: For tracking URL clicks.
*   **OpenEvent**: For tracking email opens via the pixel.
*   **DeliveryEvent**: For recording bounces, replies, and other delivery-related events from the IMAP sync.

---

## Templates (Markdown + tracking helpers)

Enable the tag library by placing `campaigns/templatetags/campaigns.py`.

```markdown
Hey {{ recipient.name|default:"there" }}!

[Open link]({% track_url campaign recipient 'https://example.com' %})

<img src="{% tracking_pixel message %}" width="1" height="1" style="display:none" alt="">
```

Context available in templates (provided automatically at send time):

- `campaign`, `recipient`, `message`
- `public_base_url` — from `settings.CAMPAIGNS_PUBLIC_BASE_URL`
- `unsubscribe_url` — `${public_base_url}/campaigns/unsub/{{ recipient.id }}/`
- `open_url` — `${public_base_url}/campaigns/o/{{ message.id }}/p.png` (pixel URL)
- `tracking_pixel` — `<img src="{{ open_url }}" alt="" width="1" height="1" style="display:none">` (uses pixel URL)

Notes:

- `{% track_url %}` returns a redirect URL for click tracking (use inside Markdown links).
- `{% tracking_pixel %}` is optional now. A tracking pixel is auto-appended to the HTML if the pixel URL (`open_url`) isn’t already present, so you can omit it in content.
- We render Django Template first, then convert Markdown → HTML; plaintext is auto-derived from HTML.

Authentication for MCP/CLI:

- Personal access tokens (PATs) are derived deterministically from the Django `User.password` hash using HMAC-SHA256.
- Each user’s read-only PAT is visible on their User admin page; it rotates when the password changes.

---

## Management Commands (CLI)

### 1) Send a campaign

Sends via SMTP and APPENDs raw message to your IMAP **Sent** folder.

```bash
# By numeric ID
python manage.py campaign_send --campaign 123

# Dry-run (render/build but DO NOT send/append)
python manage.py campaign_send --campaign 123 --dry-run

# By fuzzy name (uses most recent match)
python manage.py campaign_send --name "Vibe Deploy"

# Tune throughput for this run
python manage.py campaign_send --campaign 123 --batch-size 100 --sleep 1.5
```

What it does:

* Marks status `SENDING` → `DONE` when finished.
* Uses `CAMPAIGNS_BATCH_SIZE` and `CAMPAIGNS_SLEEP_BETWEEN_BATCHES_SEC` unless overridden.
* Calls the same core function used by Admin actions: `campaigns.sender.send_campaign(...)`.

Performance notes:

- The sender reuses SMTP and IMAP connections within chunks to reduce connection overhead.
- For very large lists, enqueue chunks via Celery (see below) instead of one long synchronous run.

### 2) IMAP sync (bounces & replies)

Polls your `bounce_folder` (often `INBOX`) and records delivery events.

```bash
python manage.py campaign_imap_sync
```

What it does:

* Tries to match messages to `MessageInstance` via `In-Reply-To/References` or the custom `Message-ID` pattern `<campaignId.recipientId.random@domain>`.
* Sets `bounced_at` timestamps on DSNs.
* Creates `DeliveryEvent` rows (`BOUNCE`, `REPLY`, `DEFERRED` — basic heuristic).

### Suggested scheduling

**systemd (recommended)**

```ini
# /etc/systemd/system/mcpigeon-imap-sync.service
[Unit]
Description=MC Pigeon IMAP sync

[Service]
Type=oneshot
WorkingDirectory=/srv/app             # ← change me
Environment="DJANGO_SETTINGS_MODULE=myproj.settings"
ExecStart=/srv/app/venv/bin/python manage.py campaign_imap_sync
```

```ini
# /etc/systemd/system/mcpigeon-imap-sync.timer
[Unit]
Description=Run IMAP sync every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=mcpigeon-imap-sync.service

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now mcpigeon-imap-sync.timer
```

**Cron (simple alternative)**

```cron
*/5 * * * * cd /srv/app && /srv/app/venv/bin/python manage.py campaign_imap_sync >> /var/log/mcpigeon_imap.log 2>&1
```

---

## Admin

Drop in `campaigns/admin.py` (provided). Highlights:

* Campaign list shows recipient/sent/open/bounce counts & open rate
* Actions:

  * **Send selected campaigns (DRY RUN)**
  * **Send selected campaigns (LIVE)**
  * **Enqueue selected campaigns (Celery, chunked)**
  * **IMAP sync now**
  * **Export recipients (CSV)**

User admin:

* The API PAT token is shown read-only on each user page (derived from the password hash; it rotates on password change).

Passwords are masked in forms but stored as provided (use secrets management in production).

---

## Asynchronous sending (Celery)

For high volume, run chunked sends via Celery workers.

1) Configure a broker (example: Redis) and install Celery (see Install).
2) Set environment and run a worker that loads the in-module app at `campaigns.sender:app`.

Environment (example):

```bash
export DJANGO_SETTINGS_MODULE=MCPigeon.settings
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/0
```

Run worker:

```bash
celery -A campaigns.sender:app worker -l info
```

Enqueue chunks from CLI:

```bash
# Option 1: one-shot enqueue via dedicated command
python manage.py campaign_enqueue --campaign 123 --chunk-size 200

# Option 2: send command with enqueue flag (skips SMTP/IMAP locally and just enqueues)
python manage.py campaign_send --campaign 123 --enqueue
```

Details:

- Tasks live in `campaigns/sender.py`; the Celery task name is `campaigns.send_campaign_chunk`.
- The worker process must have `DJANGO_SETTINGS_MODULE` set so the ORM works.
- `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` can be provided via env; settings read them and pass to Celery.

---

## HTTP Endpoints

Already wired by `campaigns/urls.py`:

* `GET /campaigns/t/<token>/r/<recipient_id>/` → 302 redirect + click log
* `GET /campaigns/o/<message_id>/p.png` → 1×1 tracking pixel + open log (no-cache)
* `GET /campaigns/unsub/<recipient_id>/` → marks unsubscribed

Headers added automatically:

```
List-Unsubscribe: <https://your.app/campaigns/unsub/{recipient_id}/>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

Notes:

- The unsubscribe headers are only added when `CAMPAIGNS_PUBLIC_BASE_URL` is configured.

---

## Deliverability Checklist

* Configure **SPF**, **DKIM**, **DMARC** on your sending domain
* Warm up with batch throttling (`CAMPAIGNS_BATCH_SIZE`, sleeps)
* Include visible **unsubscribe** & physical address
* Keep a clean list; honor `unsubscribed` and bounces

---

## Security, Privacy & Compliance

* Treat recipient data and activity logs as **PII**
* Enforce **TLS** on SMTP/IMAP
* Disclose **tracking**; offer text-only alternative
* Follow CAN-SPAM/GDPR/etc. for your jurisdiction

---

## Scaling notes

* Run sends via **Celery/RQ** (wrap `send_campaign`) for large lists
* Add DB indexes on `MessageInstance(campaign_id, recipient_id)` and event timestamps
* Optionally front pixel/redirect endpoints with a CDN (cache-bypass pixel)

---

## MCP

MCP tools are included for CRUD and ops (send/status). Campaigns use `template_markdown` (Markdown-only) in the tool schemas. The MCP manifest exposes `env.public_base_url` so agents can generate correct tracking/unsubscribe links and tracking pixels in Markdown.

* `campaigns` (CRUD), `recipients` (CRUD), `messages`, `links`, `linkclicks`, `openevents`, `deliveryevents`, `campaign_ops` (send/status/add_recipient)

These call the same core sender/stats functions as the Admin/CLI. Wire once you’re happy with the core.

---

## Troubleshooting

**No messages in “Sent”**

* Verify IMAP creds & `sent_folder` spelling/casing
* Confirm `_imap_append` runs after `_smtp_send`

**Links don’t track**

* Ensure `CAMPAIGNS_PUBLIC_BASE_URL` is correct & routable
* Use `{% track_url %}` inside Markdown link URLs: `[text]({% track_url ... %})`

**Opens look low**

* Many clients block images; opens are a floor, not truth

**IMAP sync finds nothing**

* Check `bounce_folder` and remove `UNSEEN` filter temporarily for testing

---

## License

**GPL-3.0** — strong copyleft for a community-friendly, self-hosted tool.

---

## Credits

Built for agentic workflows by folks who like their email where they can see it — **in the mailbox**.
Mascot & brand: **MC Pigeon** 🐦🎤
