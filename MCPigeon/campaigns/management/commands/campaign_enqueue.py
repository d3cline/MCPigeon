from django.core.management.base import BaseCommand, CommandError
from django.conf import settings


class Command(BaseCommand):
    help = "Enqueue a campaign for background send via Celery (chunked)."

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--campaign", type=int, help="Campaign ID to enqueue")
        g.add_argument("--name", type=str, help="Fuzzy name match (uses latest match)")

        parser.add_argument(
            "--chunk-size",
            type=int,
            default=None,
            help="Override CAMPAIGNS_BATCH_SIZE for chunk size",
        )

    def handle(self, *args, **opts):
        # Resolve campaign
        from campaigns.models import Campaign, Recipient  # local import
        from campaigns.tasks import send_campaign_chunk

        if opts.get("campaign"):
            c = Campaign.objects.filter(id=opts["campaign"]).first()
        else:
            c = (
                Campaign.objects
                .filter(name__icontains=opts["name"])  # type: ignore[index]
                .order_by("-created_at")
                .first()
            )

        if not c:
            raise CommandError("Campaign not found. Use --campaign <id> or --name '<partial>'")

        size = opts.get("chunk_size") or int(getattr(settings, "CAMPAIGNS_BATCH_SIZE", 300))
        ids = list(
            Recipient.objects.filter(campaign=c, unsubscribed=False).values_list("id", flat=True)
        )
        if not ids:
            self.stdout.write(self.style.WARNING("No active recipients to enqueue."))
            return

        self.stdout.write(f"Enqueuing campaign [{c.id}] '{c.name}' in chunks of {size}...")
        chunks = 0
        try:
            for i in range(0, len(ids), size):
                chunk = ids[i : i + size]
                send_campaign_chunk.delay(c.id, chunk)  # type: ignore[attr-defined]
                chunks += 1
        except Exception as e:
            raise CommandError(f"Failed to enqueue chunks: {e}")

        self.stdout.write(self.style.SUCCESS(f"Enqueued {chunks} chunk(s)."))
