# campaigns/sender.py
import email.utils
import imaplib
import logging
import os
import smtplib
import socket
import ssl
import time
from contextlib import contextmanager
from email.message import EmailMessage
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
from django.conf import settings
from django.template import Context, Template
from django.utils import timezone
from django.utils.crypto import get_random_string

if TYPE_CHECKING:
    from .models import Campaign, MessageInstance, Recipient

logger = logging.getLogger("campaigns.sender")

# Connection timeouts (connect/handshake). Operation timeouts are handled separately.
SMTP_CONNECT_TIMEOUT = float(os.getenv("CAMPAIGNS_SMTP_TIMEOUT", "30"))
IMAP_CONNECT_TIMEOUT = float(os.getenv("CAMPAIGNS_IMAP_TIMEOUT", "30"))

# Per-operation timeout for IMAP APPEND specifically (prevents hangs)
IMAP_APPEND_TIMEOUT = float(os.getenv("CAMPAIGNS_IMAP_APPEND_TIMEOUT", "8"))

SMTP_DEBUG_WIRE = bool(int(os.getenv("CAMPAIGNS_SMTP_DEBUG", "0")))

try:
    import markdown as _markdown_lib
except ImportError:
    _markdown_lib = None

@contextmanager
def socket_timeout(seconds: float):
    """
    Sets the *global* default socket timeout for NEW sockets. Note: this does NOT
    affect already-open sockets (e.g., an existing IMAP connection). Kept for
    connect-time limits; not used for per-op timeouts.
    """
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(prev)

@contextmanager
def imap_op_timeout(imap_conn, seconds: float):
    """
    Temporarily set a timeout on the underlying IMAP socket for a single operation.
    This *does* affect ongoing reads/writes on that IMAP connection.
    """
    sock = getattr(imap_conn, "sock", None)
    prev = None
    try:
        if sock is not None:
            try:
                prev = sock.gettimeout()
            except Exception:
                prev = None
            try:
                sock.settimeout(seconds)
            except Exception:
                pass
        yield
    finally:
        if sock is not None:
            try:
                sock.settimeout(prev)
            except Exception:
                pass

def _to_html_from_markdown(md_text: str) -> str:
    if _markdown_lib:
        try:
            return _markdown_lib.markdown(md_text, extensions=["extra", "smarty"])
        except Exception:
            logger.exception("Markdown conversion failed; falling back to <pre> wrapper")
    else:
        logger.debug("Markdown package not installed; sending body wrapped in <pre> (install 'markdown' for rich rendering)")
    from html import escape
    return f"<pre>{escape(md_text)}</pre>"

def _render(campaign: "Campaign", recipient: "Recipient", message_obj: "MessageInstance") -> tuple[str, str]:
    base = getattr(settings, "CAMPAIGNS_PUBLIC_BASE_URL", "").rstrip("/")
    unsubscribe_url = f"{base}/campaigns/unsub/{recipient.id}/" if base else ""
    pixel_url = f"{base}/campaigns/o/{message_obj.id}/p.png" if base else ""

    ctx = Context({
        "campaign": campaign,
        "recipient": recipient,
        "message": message_obj,
        "public_base_url": base,
        "unsubscribe_url": unsubscribe_url,
        "open_url": pixel_url,
        "tracking_pixel": f'<img src="{pixel_url}" alt="" width="1" height="1" style="display:none" />' if pixel_url else "",
    })
    md_tmpl = Template(campaign.template_markdown)
    md_text = md_tmpl.render(ctx)
    html = _to_html_from_markdown(md_text)

    if pixel_url and pixel_url not in html:
        html = f'{html}\n<img src="{pixel_url}" alt="" width="1" height="1" style="display:none" />'

    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return text, html

def _build_email(campaign: "Campaign", recipient: "Recipient", message_obj: "MessageInstance") -> EmailMessage:
    m = EmailMessage()
    from_name = campaign.mailbox.from_name or campaign.mailbox.from_email
    from_addr = campaign.mailbox.from_email
    to_name = recipient.name or ""
    to_addr = recipient.email

    m["From"] = email.utils.formataddr((from_name, from_addr))
    m["To"] = email.utils.formataddr((to_name, to_addr))
    m["Subject"] = campaign.subject
    m["Message-ID"] = message_obj.message_id
    m["Date"] = email.utils.formatdate(localtime=True)

    base = getattr(settings, "CAMPAIGNS_PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        m["List-Unsubscribe"] = f"<{base}/campaigns/unsub/{recipient.id}/>"
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    text, html = _render(campaign, recipient, message_obj)
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    return m

@contextmanager
def smtp_session(mailbox):
    host = mailbox.smtp_host
    port = int(mailbox.smtp_port)
    user = mailbox.smtp_username or ""
    mode = "SSL" if port == 465 else ("STARTTLS" if (port == 587 or getattr(mailbox, "smtp_starttls", True)) else "PLAIN")
    ctx = ssl.create_default_context()
    s = None
    try:
        # Connection-level timeout
        if mode == "SSL":
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=SMTP_CONNECT_TIMEOUT)
        else:
            s = smtplib.SMTP(host, port, timeout=SMTP_CONNECT_TIMEOUT)

        if SMTP_DEBUG_WIRE:
            s.set_debuglevel(1)

        s.ehlo()
        if mode == "STARTTLS":
            s.starttls(context=ctx)
            s.ehlo()
        if user:
            s.login(user, mailbox.smtp_password)
        yield s
    finally:
        if s:
            try:
                s.quit()
            except Exception:
                pass

@contextmanager
def imap_session(mailbox):
    host = mailbox.imap_host
    port = int(mailbox.imap_port)
    use_ssl = bool(getattr(mailbox, "imap_ssl", True))
    imap = None
    try:
        # Connection-level timeout for connect/login only
        with socket_timeout(IMAP_CONNECT_TIMEOUT):
            imap = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        imap.login(mailbox.imap_username or "", mailbox.imap_password)
        yield imap
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass

def send_campaign_batch(campaign_id: int, recipient_ids: list[int]) -> int:
    """
    Send to the given recipient IDs for a campaign.
    Returns the count of SMTP successes. IMAP 'Sent' archiving is best-effort and
    does NOT affect the success count (to avoid hangs causing false negatives).
    """
    from .models import Campaign, Recipient, MessageInstance

    try:
        campaign = Campaign.objects.select_related("mailbox").get(id=campaign_id)
    except Campaign.DoesNotExist:
        logger.error("Campaign %s not found for sending batch.", campaign_id)
        return 0

    sent_ok = 0
    with smtp_session(campaign.mailbox) as smtp_conn, imap_session(campaign.mailbox) as imap_conn:
        # Ensure sent folder exists (ignore if it already does)
        try:
            # Some servers return ('OK', [b'...']) if it exists; others error -> ignore.
            imap_conn.create(campaign.mailbox.sent_folder)
        except imaplib.IMAP4.error:
            pass
        except Exception as e:
            # Folder creation failure should never block sending
            logger.warning("IMAP create folder failed (ignored): %s", e)

        recipients = Recipient.objects.filter(
            id__in=recipient_ids,
            unsubscribed=False,
            campaign=campaign
        )

        for r in recipients:
            # Idempotent message row; reuses previous Message-ID on retries
            msg_id = f"<{campaign.id}.{r.id}.{get_random_string(12)}@{campaign.mailbox.from_email.split('@')[-1]}>"
            mi, _created = MessageInstance.objects.get_or_create(
                campaign=campaign,
                recipient=r,
                defaults={"message_id": msg_id},
            )
            if mi.sent_at:
                # Already sent; skip
                continue

            try:
                # Build MIME and raw bytes once
                m = _build_email(campaign, r, mi)
                raw = m.as_bytes()

                # SMTP send (count as success if server accepts it)
                to_addrs = [addr for _, addr in email.utils.getaddresses(m.get_all("To", []) or [])]
                smtp_conn.send_message(m, from_addr=campaign.mailbox.from_email, to_addrs=to_addrs)
                sent_ok += 1

                # Best-effort IMAP append with per-op timeout. Failure here does NOT decrement sent count.
                try:
                    with imap_op_timeout(imap_conn, IMAP_APPEND_TIMEOUT):
                        imap_conn.append(
                            campaign.mailbox.sent_folder,
                            r"(\Seen)",
                            imaplib.Time2Internaldate(time.time()),
                            raw,
                        )
                except Exception as e:
                    logger.warning("IMAP APPEND failed (ignored) for %s: %s", r.email, e)

                # Mark as sent
                now_ts = timezone.now()
                mi.sent_at = now_ts
                mi.last_event_at = now_ts
                mi.save(update_fields=["sent_at", "last_event_at"])

            except Exception as e:
                # SMTP failure or other build/send error: log and continue with others
                logger.exception("Failed to send to recipient %s for campaign %s: %s", r.email, campaign.id, e)

    return sent_ok
