from django.core.management.base import BaseCommand, CommandError
from campaigns.models import Campaign
from campaigns.sender import send_campaign
from campaigns.tasks import send_campaign_chunk
from django.conf import settings


class Command(BaseCommand):
    help = "Send a campaign by ID (or by fuzzy name match). Uses SMTP + IMAP APPEND."

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--campaign", type=int, help="Campaign ID to send")
        g.add_argument("--name", type=str, help="Fuzzy name match (uses latest match)")

        parser.add_argument("--dry-run", action="store_true",
                            help="Render/build messages but DO NOT send or append to IMAP")
        parser.add_argument("--batch-size", type=int, default=None,
                            help="Override CAMPAIGNS_BATCH_SIZE for this run")
        parser.add_argument("--sleep", type=float, default=None,
                            help="Seconds to sleep between batches (override setting)")
        parser.add_argument("--enqueue", action="store_true",
                            help="Enqueue chunked sending via Celery instead of synchronous send")

    def handle(self, *args, **opts):
        # Resolve campaign
        c = None
        if opts.get("campaign"):
            c = Campaign.objects.filter(id=opts["campaign"]).first()
        else:
            # latest matching name
            c = (Campaign.objects
                 .filter(name__icontains=opts["name"])
                 .order_by("-created_at")
                 .first())

        if not c:
            raise CommandError("Campaign not found. Use --campaign <id> or --name '<partial>'")

        dry = bool(opts["dry_run"])
        bsize = opts.get("batch_size")
        nap = opts.get("sleep")

        self.stdout.write(
            f"Sending campaign [{c.id}] '{c.name}' "
            f"(dry_run={dry}, batch_size={bsize or 'default'}, sleep={nap or 'default'})"
        )

        if opts.get("enqueue") and not dry:
            # Enqueue via Celery: split recipients and schedule chunk tasks
            from campaigns.models import Recipient
            qs = Recipient.objects.filter(campaign=c, unsubscribed=False).only("id").order_by("id")
            ids = list(qs.values_list("id", flat=True))
            if not ids:
                self.stdout.write(self.style.WARNING("No active recipients to send."))
                return
            size = bsize or int(getattr(settings, "CAMPAIGNS_BATCH_SIZE", 300))
            chunks = 0
            try:
                for i in range(0, len(ids), size):
                    chunk = ids[i:i+size]
                    send_campaign_chunk.delay(c.id, chunk)  # type: ignore[attr-defined]
                    chunks += 1
            except Exception as e:
                raise CommandError(f"Failed to enqueue chunks: {e}")
            self.stdout.write(self.style.SUCCESS(f"Enqueued {chunks} chunk(s)."))
            return
        else:
            ok = send_campaign(
                campaign_id=c.id,
                batch_size=bsize,
                sleep_between_batches=nap,
                dry_run=dry,
            )

            if ok:
                self.stdout.write(self.style.SUCCESS("Done."))
            else:
                raise CommandError("Send failed (send_campaign returned falsy).")
