from __future__ import annotations

import logging
import uuid
from typing import List, Tuple

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import OperationalError, transaction
from django.utils.timezone import now

from smtplib import SMTPException
from billiard.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded

logger = logging.getLogger(__name__)

# Tunables (override in settings.py if you like)
SOFT_LIMIT = getattr(settings, "CAMPAIGNS_SOFT_TIME_LIMIT", 600)  # 10m
HARD_LIMIT  = getattr(settings, "CAMPAIGNS_HARD_TIME_LIMIT", SOFT_LIMIT + 60)
REPORT_TO   = getattr(settings, "CAMPAIGNS_FAILURE_REPORT_TO", "sales@opalstack.com")
REPORT_FROM = getattr(settings, "CAMPAIGNS_FAILURE_REPORT_FROM", getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@opalstack.com"))

def _gen_message_id(mailbox_from_email: str, campaign_id: int, recipient_id: int) -> str:
    """
    RFC 5322 Message-ID we can deterministically associate to this (campaign, recipient).
    Using UUID4 inside to avoid accidental collisions while still being stable per MessageInstance row.
    """
    domain = (mailbox_from_email.split("@", 1)[-1] or "opalstack.com").lower()
    return f"<{uuid.uuid4().hex}.{campaign_id}.{recipient_id}@{domain}>"

def _send_failure_report(*, campaign, total:int, sent:int, failures:List[Tuple[str, str]]) -> None:
    failed_count = len(failures)
    subject = f"[MCPigeon] Campaign #{campaign.id} delivery report — {sent}/{total} sent, {failed_count} failed"
    lines = [
        f"Campaign ID: {campaign.id}",
        f"Name: {getattr(campaign, 'name', '')}",
        f"Mailbox: {campaign.mailbox.from_email if getattr(campaign, 'mailbox', None) else 'N/A'}",
        f"Generated: {now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Total recipients: {total}",
        f"Sent successfully: {sent}",
        f"Failed: {failed_count}",
        "",
    ]
    if failed_count:
        lines.append("Failures (email — reason):")
        for email, reason in failures[:5000]:
            lines.append(f"- {email} — {reason}")
    else:
        lines.append("No failures 🎉")
    body = "\n".join(lines)
    try:
        EmailMultiAlternatives(subject=subject, body=body, from_email=REPORT_FROM, to=[REPORT_TO]).send(fail_silently=True)
    except Exception:
        logger.exception("Failed to send failure report email for campaign %s", campaign.id)

@shared_task(
    bind=True,
    # Only infra/db errors cause a task-level retry.
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    name="campaigns.send_campaign",
    soft_time_limit=SOFT_LIMIT,
    time_limit=HARD_LIMIT,
)
def send_campaign_task(self, campaign_id: int) -> dict:
    """
    Sends a campaign idempotently:
      - Reads/creates MessageInstance per recipient (unique per (campaign, recipient))
      - Skips recipients whose MessageInstance.sent_at is already set
      - On crash/timeouts, re-running picks up where it left off
      - Per-recipient errors are recorded; sending continues
      - At the end, emails a failure report to REPORT_TO
    """
    from .models import Campaign, Recipient, MessageInstance
    # Prefer single-recipient sender if available; otherwise fall back to batch-of-1
    try:
        from .sender import send_campaign_single as _send_one
        _supports_message_id = True
    except Exception:
        _send_one = None
        _supports_message_id = False
    if _send_one is None:
        from .sender import send_campaign_batch as _send_batch

    # Defensive: coerce instance to pk if someone passed a Campaign object
    if hasattr(campaign_id, "pk"):
        campaign_id = int(campaign_id.pk)
    if not isinstance(campaign_id, int):
        raise TypeError(f"campaign_id must be an int. Got: {type(campaign_id).__name__} -> {repr(campaign_id)}")

    # Load campaign
    from django.core.exceptions import ObjectDoesNotExist
    try:
        campaign = Campaign.objects.select_related("mailbox").get(pk=campaign_id)
    except ObjectDoesNotExist:
        logger.error("Campaign %s not found", campaign_id)
        return {"status": "error", "reason": "not_found"}

    # Mark SENDING (best-effort)
    try:
        campaign.status = Campaign.SENDING
        campaign.save(update_fields=["status"])
    except Exception:
        logger.warning("Could not set campaign %s status=SENDING", campaign.id)

    # Build working list: all subscribers for this campaign
    # (We’ll create MessageInstance rows lazily per recipient.)
    recipients = list(
        Recipient.objects
        .filter(campaign=campaign, unsubscribed=False)
        .values_list("id", "email")
    )
    total = len(recipients)

    if total == 0:
        try:
            campaign.status = Campaign.DONE
            campaign.save(update_fields=["status"])
        except Exception:
            pass
        _send_failure_report(campaign=campaign, total=0, sent=0, failures=[])
        return {"status": "complete", "reason": "no_recipients", "sent": 0, "failed": 0, "total": 0, "failures": []}

    sent_ok = 0
    failures: List[Tuple[str, str]] = []

    # Main loop — one recipient at a time so a single bad address never aborts the run
    for rid, email in recipients:
        try:
            # Ensure exactly one MessageInstance per (campaign, recipient).
            # If you add the unique constraint suggested below, this is fully race-safe.
            with transaction.atomic():
                mi, created = MessageInstance.objects.select_for_update().get_or_create(
                    campaign=campaign,
                    recipient_id=rid,
                    defaults={"message_id": _gen_message_id(campaign.mailbox.from_email, campaign.id, rid)}
                )
                # If already sent, skip (idempotent)
                if mi.sent_at:
                    continue
                message_id = mi.message_id

            # Send one (prefer exact Message-ID control if your sender supports it)
            if _send_one:
                # Signature idea: send_campaign_single(campaign_id, recipient_id, *, message_id=None, extra_headers=None) -> bool
                ok = bool(_send_one(campaign.id, rid, message_id=message_id))
                if not ok:
                    raise SMTPException("send_campaign_single returned False")
            else:
                # Fallback: call the existing batch sender with a single id
                sent_count = int(_send_batch(campaign.id, [rid]) or 0)
                if sent_count != 1:
                    raise SMTPException(f"send_campaign_batch returned {sent_count} for recipient {rid}")

            # Mark as sent (fast, outside the select_for_update lock)
            MessageInstance.objects.filter(pk=mi.pk, sent_at__isnull=True).update(sent_at=now())
            sent_ok += 1

        except (SMTPException, SoftTimeLimitExceeded, TimeLimitExceeded) as e:
            logger.error("Recipient %s failed in campaign %s: %r", email, campaign.id, e)
            failures.append((email, e.__class__.__name__))
            # Continue to next recipient

        except OperationalError as e:
            # Transient DB hiccup — re-raise so the *task* retries (progress is persisted).
            logger.warning("OperationalError during campaign %s: %r (will retry)", campaign.id, e)
            # Send partial report so ops sees current state
            _send_failure_report(campaign=campaign, total=total, sent=sent_ok, failures=failures)
            raise

        except Exception as e:
            # Any other per-recipient error — record and keep going
            logger.exception("Unexpected error sending to %s in campaign %s", email, campaign.id)
            failures.append((email, f"{e.__class__.__name__}: {e}"))
            continue

    # Wrap-up: mark DONE and report
    try:
        campaign.status = Campaign.DONE
        campaign.save(update_fields=["status"])
    except Exception:
        logger.warning("Could not set campaign %s status=DONE", campaign.id)

    _send_failure_report(campaign=campaign, total=total, sent=sent_ok, failures=failures)

    return {
        "status": "complete",
        "total": total,
        "sent": sent_ok,
        "failed": len(failures),
        "failures": failures,
    }
