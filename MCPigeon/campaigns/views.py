# campaigns/views.py
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseNotFound
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.utils import timezone
from .models import Link, Recipient, MessageInstance, LinkClick, OpenEvent

@never_cache
def track_redirect(request, token, recipient_id):
    try:
        link = Link.objects.get(token=token)
        msg = MessageInstance.objects.get(recipient_id=recipient_id, campaign=link.campaign)
    except (Link.DoesNotExist, MessageInstance.DoesNotExist):
        return HttpResponseNotFound("Unknown link")

    LinkClick.objects.create(
        message=msg, link=link,
        ip=request.META.get("REMOTE_ADDR"),
        ua=request.META.get("HTTP_USER_AGENT","")
    )
    msg.clicks += 1
    msg.last_event_at = timezone.now()
    msg.save(update_fields=["clicks","last_event_at"])
    return HttpResponseRedirect(link.url)

TRANSPARENT_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                   b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                   b"\x00\x00\x00\nIDATx\xda\x63\x00\x01\x00\x00\x05\x00\x01"
                   b"\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

@never_cache
def pixel(request, message_id):
    msg = get_object_or_404(MessageInstance, id=message_id)
    OpenEvent.objects.create(
        message=msg,
        ip=request.META.get("REMOTE_ADDR"),
        ua=request.META.get("HTTP_USER_AGENT","")
    )
    if not msg.opened_at:
        from django.utils import timezone
        msg.opened_at = timezone.now()
        msg.last_event_at = msg.opened_at
        msg.save(update_fields=["opened_at","last_event_at"])
    resp = HttpResponse(TRANSPARENT_PNG, content_type="image/png")
    resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

def unsub(request, recipient_id):
    r = get_object_or_404(Recipient, id=recipient_id)
    r.unsubscribed = True
    r.save(update_fields=["unsubscribed"])
    return HttpResponse("You’ve been unsubscribed. Sorry to see you go.")
from django.shortcuts import render

# Create your views here.
