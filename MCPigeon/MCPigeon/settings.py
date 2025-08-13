# Base URL for tracking/redirects in links & pixels
CAMPAIGNS_PUBLIC_BASE_URL = "https://opalstack.com"  # ← change me

DJANGO_MCP_GLOBAL_SERVER_CONFIG = {
    "name":"MCPigeon",
        "instructions": (
            """
version: 1
name: MCPigeon
purpose: |
    Minimal MCP toolset for internal admin/ops of email campaigns.
    Operates directly on Django models via an MCP server. No public HTTP API.
scope:
    intended_use:
        - Manage mailboxes (SMTP/IMAP credentials; secrets redacted)
        - Manage campaigns, recipients, and message instances
        - Manage tracked links, link clicks, open and delivery events
        - Operational actions: send campaigns, get status snapshot, add recipient
    non_goals:
        - End-user surface or arbitrary ORM queries
        - Authentication/authorization (enforced by host MCP server)
        - Long-running background orchestration beyond send() op
security:
    safeguards:
        - Mailbox passwords redacted on list/read
        - List operations capped at 500 unless a lower limit is provided
        - Updates ignore unknown fields and never allow primary key override
        - Transport/authn is the responsibility of the host MCP server
env:
    public_base_url: """ + CAMPAIGNS_PUBLIC_BASE_URL + """
usage_model:
    interface: action-switch per tool
    request_shape:
        type: object
        properties:
            action: [list, read, create, update, delete, send, status, add_recipient]
            payload: object | null
    response_shape:
        - JSON-serializable dicts/arrays
        - On validation errors, tools return { ok: false, error: <string> } where implemented
tools:
    - name: mailboxes
        summary: CRUD for SMTP/IMAP mailboxes (passwords redacted in responses)
        actions:
            list:
                payload?: { filters?: object, limit?: integer }
                notes: Max 500 results; filters use Django ORM lookups
            read: { id: integer }
            create: {
                name: string,
                from_email: email,
                smtp_host: string, smtp_port?: 465|587, smtp_starttls?: boolean,
                smtp_username: string, smtp_password: string,
                imap_host: string, imap_port?: integer, imap_ssl?: boolean,
                imap_username: string, imap_password: string,
                from_name?: string, sent_folder?: string, bounce_folder?: string
            }
            update: { id: integer, ...fields }
            delete: { id: integer }
    - name: campaigns
        summary: CRUD for campaigns (subject, Markdown template, mailbox)
        actions:
            list: { payload?: { filters?: object, limit?: integer } }
            read: { id: integer }
            create: { name: string, subject: string, template_markdown: string, mailbox_id: integer }
            update: { id: integer, ...fields }
            delete: { id: integer }
    - name: recipients
        summary: CRUD for recipients attached to a campaign
        actions:
            list: { payload?: { filters?: object, limit?: integer } }
            read: { id: integer }
            create: { campaign_id: integer, email: email, name?: string, unsubscribed?: boolean }
            update: { id: integer, ...fields }
            delete: { id: integer }
    - name: messages
        summary: CRUD for MessageInstance (inspection/testing)
        actions:
            list: { payload?: { filters?: object, limit?: integer } }
            read: { id: integer }
            create: { campaign_id: integer, recipient_id: integer, message_id: string }
            update: { id: integer, ...fields }
            delete: { id: integer }
    - name: links
        summary: CRUD for tracked links; token auto-generates if omitted
        actions:
            list: { payload?: { filters?: object, limit?: integer } }
            read: { id: integer }
            create: { campaign_id: integer, url: uri, token?: string }
            update: { id: integer, ...fields }
            delete: { id: integer }
    - name: linkclicks
        summary: CRUD for link click events (inspection)
        actions: [list, read, create, update, delete]
    - name: openevents
        summary: CRUD for open events (inspection)
        actions: [list, read, create, update, delete]
    - name: deliveryevents
        summary: CRUD for delivery events (delivered, bounce, reply, complaint, deferred)
        actions: [list, read, create, update, delete]
    - name: campaign_ops
        summary: Operational actions for campaigns
        actions:
            send: { campaign_id: integer, dry_run?: boolean, batch_size?: integer|null, sleep?: number|null }
            status: { campaign_id: integer }
            add_recipient: { campaign_id: integer, email: email, name?: string }
template_authoring:
    context:
        - template_markdown is rendered with Django Template(context={ campaign, recipient, message }) then converted to HTML
        - Plaintext body is auto-derived from the rendered HTML
    custom_tags:
        - name: track_url
          usage: "{% track_url campaign recipient 'https://example.com/path' %}"
          effect: Generates a redirect URL that records a LinkClick and then 302s to the target URL
        - name: tracking_pixel
          usage: "{% tracking_pixel message %}"
          effect: Returns a per-message pixel URL that records an OpenEvent when fetched
    notes:
    - Public base URL: """ + CAMPAIGNS_PUBLIC_BASE_URL + """
        - List-Unsubscribe headers are added automatically by the sender
    html_example: |
        <p>Hey {{ recipient.name|default:"there" }}, meet Vibe Deploy.</p>
        <p><a href="{% track_url campaign recipient 'https://opalstack.com/mcp' %}">Read how</a></p>
        <img src="{% tracking_pixel message %}" width="1" height="1" style="display:none" alt="">
examples:
    - title: List campaigns containing "august" (max 50)
        tool: campaigns
        action: list
        payload: { filters: { name__icontains: "august" }, limit: 50 }
    - title: Send a campaign in dry-run with small batch
        tool: campaign_ops
        action: send
        payload: { campaign_id: 42, dry_run: true, batch_size: 25 }
"""
    ),
    "stateless": False
}
DJANGO_MCP_AUTHENTICATION_CLASSES=["campaigns.auth.PATAuthentication"]

# Celery Configuration
# ------------------------------------------------------------------------------
# Using Redis/Valkey as the broker and result backend.
# Ensure your Redis/Valkey server is running.
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"


# Default rate limiting / batch size
CAMPAIGNS_BATCH_SIZE = 100
CAMPAIGNS_SLEEP_BETWEEN_BATCHES_SEC = 2

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "campaigns.sender": {
            "handlers": ["console"],
            "level": "DEBUG",  # INFO for less noise, DEBUG for full trace
        },
    },
}


from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-g$0-tapyq+fsmao6kzra&ncum2e=hu%l0)k@)48a)=92@q#*y+'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'campaigns',
    'mcp_server',
    'axes',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    "axes.middleware.AxesMiddleware"
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesBackend",  # keep first
]


ROOT_URLCONF = 'MCPigeon.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'MCPigeon.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Celery (optional). Configure via env or override here. Namespace is CELERY_* for celery.Celery(config_from_object).
import os as _os
_broker = _os.getenv("CELERY_BROKER_URL")
_backend = _os.getenv("CELERY_RESULT_BACKEND")
if _broker:
    CELERY_BROKER_URL = _broker
if _backend:
    CELERY_RESULT_BACKEND = _backend
CELERY_TASK_DEFAULT_QUEUE = _os.getenv("CELERY_TASK_DEFAULT_QUEUE", "mcpigeon")

# Optional but sensible:
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours
AXES_RESET_ON_SUCCESS = True