![MC Pigeon](./logo.png)

# MC Pigeon

**MCP-native, IMAP-first email campaigns.**
Markdown in → thousands out. Drive everything from an AI agent (VS Code or any MCP client). Tracks opens/clicks, appends to your **Sent** via IMAP, and handles bounces/replies.&#x20;

* **Stack:** Python 3.13 • Django 5+ • Celery 5+ • Postgres (recommended)
* **Core surfaces:** **MCP tools** (primary), Django Admin, CLI.&#x20;

[📹➡️Demo Video Here➡️📹 ](https://www.opalstack.com/blog/wp-content/uploads/2025/08/2025-08-13-10-04-00.mp4)

---

## 1) Install

```bash
git clone <repo_url> mcpigeon
cd mcpigeon
python3 -m venv env && source env/bin/activate
pip install -r requirements.in
```

Minimal settings in `MCPigeon/settings.py`:

```python
# Public base for tracked URLs & pixel
CAMPAIGNS_PUBLIC_BASE_URL = "https://your.app"  # required by template tags

# Celery (example: Redis)
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"

# MCP recipient ingest per-call cap (default 1000)
MCP_MAX_RECIPIENT_BATCH = 1000
```

(Template tags build click/pixel URLs off `CAMPAIGNS_PUBLIC_BASE_URL`.)&#x20;

Database & superuser:

```bash
python manage.py migrate
python manage.py createsuperuser
```

Run web + worker:

```bash
python manage.py runserver 0.0.0.0:8000
# new shell
export DJANGO_SETTINGS_MODULE=MCPigeon.settings
celery -A campaigns.sender:app worker -l info
```

> The task pipeline is idempotent and retries on transient DB errors; it sends a failure report email when a run completes.&#x20;

---

## 2) Model shape (you’ll see these in Admin)

* **Mailbox**: SMTP/IMAP creds + `sent_folder`, `bounce_folder`.
* **Campaign**: `name`, `subject`, `mailbox`, `template_markdown`, `status`.
* **Recipient**: unique per (`campaign`,`email`), optional `name`, `unsubscribed`, `meta`.
* **MessageInstance**: one per recipient send; holds `message_id`, timestamps, click count.
* **Link / LinkClick / OpenEvent / DeliveryEvent**: tracking artifacts.&#x20;

---

## 3) MCP (the primary interface)

This repo exposes **MCP tools** you can mount in your MCP host (VS Code, Claude Desktop, Cursor/Cline/Continue, etc.). Tools:

* `mailboxes` — CRUD + verify creds + optional remote provisioning.&#x20;
* `campaigns` — CRUD + **send**, **status**, **list\_recipients**, **add\_recipient**, **clone**, **post\_recipients** (bulk).&#x20;
* `campaign_mailbox` — assign/switch a campaign’s mailbox.&#x20;

**Mounting (typical local config):**

* **Command:** your MCP host’s “add local tool/server” pointing at the Python that imports `campaigns.mcp` (stdio).
* **Env:** `DJANGO_SETTINGS_MODULE=MCPigeon.settings` (and your Django env vars).
* **CWD:** repo root.

### Common MCP calls

**Create a mailbox, then verify creds**

```json
{"tool":"mailboxes","action":"create","payload":{
  "name":"Sales","from_name":"Sales","from_email":"sales@your.app",
  "smtp_host":"smtp.your.app","smtp_port":587,"smtp_starttls":true,
  "smtp_username":"sales@your.app","smtp_password":"***",
  "imap_host":"imap.your.app","imap_port":993,"imap_ssl":true,
  "imap_username":"sales@your.app","imap_password":"***",
  "sent_folder":"Sent","bounce_folder":"INBOX"
}}
```

```json
{"tool":"mailboxes","action":"verify","payload":{"id":1}}
```

(Verify attempts real SMTP/IMAP logins and returns pass/fail + errors.)&#x20;

**Create a campaign**

```json
{"tool":"campaigns","action":"create","payload":{
  "name":"September Promo",
  "subject":"Save big this month",
  "mailbox_id":1,
  "template_markdown":"Hey {{ recipient.name|default:\"there\" }} — check this out!"
}}
```



**Bulk-import recipients (strings or objects)**

```json
{"tool":"campaigns","action":"post_recipients","payload":{
  "campaign_id":123,
  "on_conflict":"update_name",
  "recipients":[
    "Ada Lovelace <ada@ex.com>",
    {"email":"grace@ex.com","name":"Grace Hopper"},
    "alan@ex.com"
  ]
}}
```

* Accepts up to `MCP_MAX_RECIPIENT_BATCH` per call; returns `remaining` for pagination.
* Validates emails; dedupes within the batch; can update names on conflicts.&#x20;

**Send (queued via Celery)**

```json
{"tool":"campaigns","action":"send","payload":{"campaign_id":123}}
```

Check progress:

```json
{"tool":"campaigns","action":"status","payload":{"campaign_id":123}}
```

(Status reports sent/opened/bounced/clicks + last event.)&#x20;

**Switch a campaign’s mailbox**

```json
{"tool":"campaign_mailbox","action":"assign","payload":{"campaign_id":123,"mailbox_id":2}}
```



> Implementation note: each recipient gets a stable RFC5322 Message-ID like `<uuid.campaignId.recipientId@domain>`, which the IMAP sync uses to reconcile replies/bounces.

---

## 4) Django Admin (supporting surface)

* Add a **Mailbox** with working SMTP/IMAP creds.
* Create a **Campaign** (Markdown body).
* Add **Recipients** (or use MCP bulk ingest).
* Enqueue/send from Admin actions *or* use MCP/CLI.
  (Status and events are visible via related models.)&#x20;

---

## 5) CLI (supporting surface)

**Send (sync or enqueue)**

```bash
python manage.py campaign_send --campaign 123
python manage.py campaign_send --campaign 123 --dry-run
python manage.py campaign_send --name "September Promo" --enqueue --batch-size 200 --sleep 1.5
```

* `--enqueue` splits recipients and schedules chunk tasks.&#x20;

**Enqueue directly**

```bash
python manage.py campaign_enqueue --campaign 123 --chunk-size 300
```

(Uses `send_campaign_chunk.delay` per chunk.)&#x20;

**IMAP sync (bounces/replies)**

```bash
python manage.py campaign_imap_sync
```

* Logs into each campaign mailbox’s `bounce_folder`, processes **UNSEEN**, marks `\Seen`.
* Classifies **BOUNCE/DEFERRED/REPLY**; sets `bounced_at` and records `DeliveryEvent`.
* Matches messages by our `Message-ID` pattern `<campaignId.recipientId.rnd@domain>`.&#x20;

---

## 6) Writing templates (Markdown + tags)

Enable the tags by keeping `campaigns/templatetags/campaigns.py` in the app. Use:

```markdown
Hey {{ recipient.name|default:"there" }}!

[Open link]({% track_url campaign recipient "https://example.com" %})
<img src="{% tracking_pixel message %}" width="1" height="1" style="display:none" alt="">
```

* `{% track_url %}` rewrites to a tracked redirect under `CAMPAIGNS_PUBLIC_BASE_URL`.
* `{% tracking_pixel %}` emits the 1×1 open-tracking URL.&#x20;

---

## 7) Notes on sending & resilience

* Task `campaigns.send_campaign` is idempotent; re-runs skip already-sent recipients.
* Per-recipient failures don’t abort the run; a summary email is sent at the end.
* On DB hiccups, the task retries with backoff/jitter.&#x20;

---

## 8) Deliverability (bare minimum)

Set up **SPF/DKIM/DMARC**, warm up with smaller batches + sleeps, include unsubscribe, and honor bounces/unsubs. (Clicks/opens depend on client behavior.)

---

## License

Apache-2.0

---

Mail stays where it belongs: **your mailbox**. Your **agent** runs the show.
