# campaigns/management/commands/campaign_imap_sync.py
from django.core.management.base import BaseCommand
from email import message_from_bytes
import imaplib, re
from django.utils import timezone
from campaigns.models import Campaign, MessageInstance, DeliveryEvent

class Command(BaseCommand):
    help = "Poll IMAP for DSNs/replies and update message status."

    def handle(self, *args, **opts):
        for mbox in Campaign.objects.values_list("mailbox", flat=True).distinct():
            self._sync_mailbox_id(mbox_id=mbox)

    def _sync_mailbox_id(self, mbox_id):
        from campaigns.models import Mailbox
        mb = Mailbox.objects.get(id=mbox_id)
        imap = imaplib.IMAP4_SSL(mb.imap_host, mb.imap_port) if mb.imap_ssl else imaplib.IMAP4(mb.imap_host, mb.imap_port)
        try:
            imap.login(mb.imap_username, mb.imap_password)
            imap.select(mb.bounce_folder)
            typ, data = imap.search(None, 'UNSEEN')
            ids = data[0].split()
            for i in ids:
                typ, msgdata = imap.fetch(i, '(RFC822)')
                raw = msgdata[0][1]
                em = message_from_bytes(raw)
                self._handle_message(em)
                imap.store(i, '+FLAGS', '\\Seen')
        finally:
            try: imap.logout()
            except: pass

    def _handle_message(self, em):
        # Try to match our Message-ID pattern: <campaignId.recipientId.rnd@domain>
        in_reply_to = em.get("In-Reply-To") or em.get("References") or em.get("Original-Message-ID")
        msgid = (in_reply_to or em.get("Message-ID") or "").strip()
        m = re.search(r"<(\d+)\.(\d+)\.[^@]+@[^>]+>", msgid)
        mi = None
        if m:
            c_id, r_id = int(m.group(1)), int(m.group(2))
            mi = MessageInstance.objects.filter(campaign_id=c_id, recipient_id=r_id).first()

        payload = {"subject": em.get("Subject"), "from": em.get("From")}
        typ = None
        ctype = (em.get_content_type() or "").lower()
        if "delivery-status" in ctype or "dsn" in ctype or "failure" in (em.get("Subject","").lower()):
            typ = DeliveryEvent.BOUNCE
            if mi and not mi.bounced_at:
                mi.bounced_at = timezone.now(); mi.last_event_at = mi.bounced_at; mi.save(update_fields=["bounced_at","last_event_at"])
        elif em.get("Auto-Submitted","").lower() in ("auto-replied","auto-generated"):
            typ = DeliveryEvent.DEFERRED
        else:
            typ = DeliveryEvent.REPLY

        DeliveryEvent.objects.create(message=mi, type=typ, payload=payload)
