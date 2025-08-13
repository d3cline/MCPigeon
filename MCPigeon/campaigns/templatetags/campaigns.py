# campaigns/templatetags/campaigns.py
from django import template
from django.conf import settings
from django.utils.crypto import get_random_string
from campaigns.models import Link, MessageInstance

register = template.Library()

@register.simple_tag
def track_url(campaign, recipient, url):
    base = getattr(settings, "CAMPAIGNS_PUBLIC_BASE_URL", "")
    token = get_random_string(28)
    link, _ = Link.objects.get_or_create(campaign=campaign, url=url, defaults={"token": token})
    return f"{base}/campaigns/t/{link.token}/r/{recipient.id}/"

@register.simple_tag
def tracking_pixel(message: MessageInstance):
    base = getattr(settings, "CAMPAIGNS_PUBLIC_BASE_URL", "")
    return f'{base}/campaigns/o/{message.id}/p.png'
