from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.db import OperationalError
from smtplib import SMTPException  # if you send email

@shared_task(
    bind=True,
    # Retry only transient stuff; NOT TypeError/ValueError/etc.
    autoretry_for=(OperationalError, SMTPException),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    name="campaigns.send_campaign",
)
def send_campaign_task(self, campaign_id: int) -> dict:
    """
    Celery task to send an entire campaign by batching all its recipients.
    """
    from .models import Campaign, Recipient
    from .sender import send_campaign_batch

    # Coerce a passed model instance to its pk (defensive)
    if isinstance(campaign_id, Campaign):
        campaign_id = campaign_id.pk

    if not isinstance(campaign_id, int):
        raise TypeError(f"campaign_id must be an int. Got: {type(campaign_id).__name__} -> {repr(campaign_id)}")

    try:
        campaign = Campaign.objects.select_related("mailbox").get(pk=campaign_id)
        campaign.status = Campaign.SENDING
        campaign.save(update_fields=["status"])

        recipient_ids = list(
            Recipient.objects
            .filter(campaign=campaign, unsubscribed=False)
            .values_list("id", flat=True)
        )

        if not recipient_ids:
            campaign.status = Campaign.SENT
            campaign.save(update_fields=["status"])
            return {"status": "complete", "reason": "no_recipients", "sent": 0}

        batch_size = getattr(settings, "CAMPAIGNS_BATCH_SIZE", 100)
        total_sent = 0

        for i in range(0, len(recipient_ids), batch_size):
            batch_ids = recipient_ids[i:i + batch_size]
            sent_in_batch = send_campaign_batch(campaign.id, batch_ids)
            total_sent += sent_in_batch

        campaign.status = Campaign.SENT
        campaign.save(update_fields=["status"])
        return {"status": "complete", "sent": total_sent}

    except (OperationalError, SMTPException) as e:
        # transient → retry
        try:
            campaign.status = Campaign.FAILED
            campaign.save(update_fields=["status"])
        except Exception:
            pass
        raise self.retry(exc=e)

    except Exception:
        # non-transient → mark failed and re-raise without retry
        try:
            campaign.status = Campaign.FAILED
            campaign.save(update_fields=["status"])
        except Exception:
            pass
        raise
