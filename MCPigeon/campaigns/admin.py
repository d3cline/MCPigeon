from __future__ import annotations

import csv
import io

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.http import HttpResponse

from .auth import generate_pat_for_user
from .models import (Campaign, DeliveryEvent, Link, LinkClick, Mailbox,
                   MessageInstance, OpenEvent, Recipient)
from .tasks import send_campaign_task


# ────────────────────────────────────────────────────────────
# Forms
# ────────────────────────────────────────────────────────────

class MailboxForm(forms.ModelForm):
    class Meta:
        model = Mailbox
        fields = "__all__"
        widgets = {
            "smtp_password": forms.PasswordInput(render_value=True),
            "imap_password": forms.PasswordInput(render_value=True),
        }


# ────────────────────────────────────────────────────────────
# Inline helpers
# ────────────────────────────────────────────────────────────

class RecipientInline(admin.TabularInline):
    model = Recipient
    extra = 1
    fields = ("email", "name", "unsubscribed")
    show_change_link = True


class MessageInstanceInline(admin.TabularInline):
    model = MessageInstance
    extra = 0
    readonly_fields = ("message_id", "sent_at", "opened_at", "bounced_at", "clicks", "last_event_at")
    fields = ("message_id", "sent_at", "opened_at", "bounced_at", "clicks")
    can_delete = False
    show_change_link = True


# ────────────────────────────────────────────────────────────
# Admin actions
# ────────────────────────────────────────────────────────────

@admin.action(description="Enqueue selected campaigns for sending")
def action_enqueue_campaigns(modeladmin, request, queryset):
    """Enqueues selected campaigns to be sent by Celery."""
    enqueued_count = 0
    for campaign in queryset:
        if campaign.status in [Campaign.DRAFT, Campaign.PAUSED]:
            send_campaign_task.delay(campaign.id)
            campaign.status = Campaign.SENDING
            campaign.save(update_fields=["status"])
            enqueued_count += 1
        else:
            messages.warning(request, f"Campaign '{campaign.name}' is not in a sendable state (is {campaign.status}).")
    
    if enqueued_count > 0:
        messages.success(request, f"Successfully enqueued {enqueued_count} campaign(s).")


@admin.action(description="Export recipients (CSV)")
def action_export_recipients(modeladmin, request, queryset):
    """
    Export all recipients of selected campaigns into a single CSV.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["campaign_id", "campaign_name", "email", "name", "unsubscribed"])
    for c in queryset:
        for r in c.recipients.all().only("email", "name", "unsubscribed"):
            writer.writerow([c.id, c.name, r.email, r.name, r.unsubscribed])
    resp = HttpResponse(buffer.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="recipients.csv"'
    return resp


@admin.action(description="IMAP sync now (scan bounce/reply folder)")
def action_imap_sync_now(modeladmin, request, queryset):
    """
    Quick trigger for the management command. For testing only.
    Runs a global sync regardless of which mailbox rows are selected.
    """
    try:
        call_command("campaign_imap_sync")
        messages.success(request, "IMAP sync completed.")
    except Exception as e:
        messages.error(request, f"IMAP sync failed: {e}")


# ────────────────────────────────────────────────────────────
# ModelAdmins
# ────────────────────────────────────────────────────────────

@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    form = MailboxForm
    list_display = ("name", "from_email", "smtp_host", "imap_host", "sent_folder", "bounce_folder")
    search_fields = ("name", "from_email", "smtp_host", "imap_host")
    actions = [action_imap_sync_now]


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "subject",
        "mailbox",
        "status",
        "created_at",
        "recipients_count",
        "sent_count",
        "opened_count",
        "bounced_count",
        "open_rate",
    )
    list_filter = ("status", "mailbox")
    search_fields = ("name", "subject", "mailbox__from_email")
    date_hierarchy = "created_at"
    inlines = [RecipientInline, MessageInstanceInline]
    actions = [action_enqueue_campaigns, action_export_recipients]

    @admin.display(description="#Recipients")
    def recipients_count(self, obj: Campaign) -> int:
        return obj.recipients.count()

    @admin.display(description="#Sent")
    def sent_count(self, obj: Campaign) -> int:
        return MessageInstance.objects.filter(campaign=obj, sent_at__isnull=False).count()

    @admin.display(description="#Opened")
    def opened_count(self, obj: Campaign) -> int:
        return MessageInstance.objects.filter(campaign=obj, opened_at__isnull=False).count()

    @admin.display(description="#Bounced")
    def bounced_count(self, obj: Campaign) -> int:
        return MessageInstance.objects.filter(campaign=obj, bounced_at__isnull=False).count()

    @admin.display(description="Open rate")
    def open_rate(self, obj: Campaign) -> str:
        sent = self.sent_count(obj)
        opened = self.opened_count(obj)
        return f"{(opened / sent * 100):.1f}%" if sent else "—"


@admin.register(Recipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "campaign", "unsubscribed")
    list_filter = ("unsubscribed", "campaign")
    search_fields = ("email", "name")
    autocomplete_fields = ("campaign",)
    actions = ["mark_unsubscribed", "mark_subscribed"]

    @admin.action(description="Mark as unsubscribed")
    def mark_unsubscribed(self, request, queryset):
        updated = queryset.update(unsubscribed=True)
        messages.success(request, f"Marked {updated} recipient(s) unsubscribed.")

    @admin.action(description="Mark as subscribed")
    def mark_subscribed(self, request, queryset):
        updated = queryset.update(unsubscribed=False)
        messages.success(request, f"Re-subscribed {updated} recipient(s).")


@admin.register(MessageInstance)
class MessageInstanceAdmin(admin.ModelAdmin):
    list_display = ("campaign", "recipient_email", "message_id", "sent_at", "opened_at", "bounced_at", "clicks")
    list_filter = ("campaign",)
    search_fields = ("message_id", "recipient__email")
    readonly_fields = ("message_id", "sent_at", "opened_at", "bounced_at", "clicks", "last_event_at")
    autocomplete_fields = ("campaign", "recipient")
    date_hierarchy = "sent_at"

    @admin.display(description="Recipient")
    def recipient_email(self, obj: MessageInstance) -> str:
        return obj.recipient.email


@admin.register(Link)
class LinkAdmin(admin.ModelAdmin):
    list_display = ("campaign", "url", "token")
    search_fields = ("url", "token")
    list_filter = ("campaign",)
    autocomplete_fields = ("campaign",)


@admin.register(LinkClick)
class LinkClickAdmin(admin.ModelAdmin):
    list_display = ("campaign", "message", "link", "ts", "ip")
    list_filter = ("link__campaign",)
    search_fields = ("message__message_id", "link__url", "ip", "ua")
    date_hierarchy = "ts"
    autocomplete_fields = ("message", "link")

    @admin.display(description="Campaign")
    def campaign(self, obj: LinkClick):
        return obj.link.campaign


@admin.register(OpenEvent)
class OpenEventAdmin(admin.ModelAdmin):
    list_display = ("campaign", "message", "ts", "ip")
    list_filter = ("message__campaign",)
    search_fields = ("message__message_id", "ip", "ua")
    date_hierarchy = "ts"
    autocomplete_fields = ("message",)

    @admin.display(description="Campaign")
    def campaign(self, obj: OpenEvent):
        return obj.message.campaign


@admin.register(DeliveryEvent)
class DeliveryEventAdmin(admin.ModelAdmin):
    list_display = ("type", "campaign", "message", "ts")
    list_filter = ("type", "message__campaign")
    search_fields = ("message__message_id", "payload")
    date_hierarchy = "ts"
    autocomplete_fields = ("message",)

    @admin.display(description="Campaign")
    def campaign(self, obj: DeliveryEvent):
        return obj.message.campaign if obj.message else None
# ────────────────────────────────────────────────────────────
# User admin extension: show PAT token (read-only) on user page
# ────────────────────────────────────────────────────────────

try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    readonly_fields = getattr(DjangoUserAdmin, "readonly_fields", tuple()) + ("pat_token",)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("API Token", {"fields": ("pat_token",)}),
    )

    @admin.display(description="PAT token (copy for config)")
    def pat_token(self, obj: User) -> str:
        try:
            return generate_pat_for_user(obj)
        except Exception as e:
            return f"<error: {e}>"