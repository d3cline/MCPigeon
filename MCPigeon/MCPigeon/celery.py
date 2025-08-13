from __future__ import annotations

import os
from celery import Celery
from django.conf import settings

# Optional: transient error classes for global/targeted annotations
from django.db import OperationalError
from smtplib import SMTPException

# Point Celery at Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "MCPigeon.settings")

app = Celery("MCPigeon")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks from installed apps
app.autodiscover_tasks()

# ---- Hardening / sane defaults ----
# Enforce JSON only (no pickle) so you can't pass ORM objects by accident.
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,  # keep Django/logging sane
    task_track_started=True,
    timezone=getattr(settings, "TIME_ZONE", "UTC"),
    enable_utc=getattr(settings, "USE_TZ", True),
)

# (Optional) Put retry policy here so you don’t have to repeat it in every task.
# IMPORTANT: Don’t blanket-retry on Exception. Limit to transient errors.
app.conf.task_annotations = {
    "campaigns.send_campaign": {
        "autoretry_for": (OperationalError, SMTPException),
        "retry_backoff": True,
        "retry_jitter": True,
        "max_retries": 3,
    }
    # You can add other tasks here with their own transient error classes.
}

@app.task(bind=True)
def debug_task(self):  # pragma: no cover
    print(f"Request: {self.request!r}")
