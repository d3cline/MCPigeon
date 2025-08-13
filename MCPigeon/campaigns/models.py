# campaigns/models.py
from django.db import models
from django.core.validators import validate_email
from django.contrib.auth import get_user_model

class Mailbox(models.Model):
    name = models.CharField(max_length=100)
    from_name = models.CharField(max_length=200, blank=True)
    from_email = models.EmailField()
    # SMTP (submission)
    smtp_host = models.CharField(max_length=255)
    smtp_port = models.IntegerField(default=587)
    smtp_starttls = models.BooleanField(default=True)
    smtp_username = models.CharField(max_length=255)
    smtp_password = models.CharField(max_length=255)
    # IMAP
    imap_host = models.CharField(max_length=255)
    imap_port = models.IntegerField(default=993)
    imap_ssl = models.BooleanField(default=True)
    imap_username = models.CharField(max_length=255)
    imap_password = models.CharField(max_length=255)
    sent_folder = models.CharField(max_length=255, default="Sent")
    bounce_folder = models.CharField(max_length=255, default="INBOX")

    def __str__(self): return f"{self.name} <{self.from_email}>"

class Campaign(models.Model):
    DRAFT, SENDING, PAUSED, DONE, FAILED, SENT = "DRAFT","SENDING","PAUSED","DONE","FAILED","SENT"
    STATUS_CHOICES = [
        (DRAFT,"Draft"),
        (SENDING,"Sending"),
        (PAUSED,"Paused"),
        (DONE,"Done"),
        (FAILED,"Failed"),
        (SENT,"Sent")
    ]

    name = models.CharField(max_length=200)
    subject = models.CharField(max_length=255)
    mailbox = models.ForeignKey(Mailbox, on_delete=models.PROTECT)
    template_markdown = models.TextField()  # Markdown source; rendered to HTML at send time
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    created_by = models.ForeignKey(get_user_model(), null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.name

class Recipient(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    email = models.EmailField(validators=[validate_email])
    name = models.CharField(max_length=200, blank=True)
    unsubscribed = models.BooleanField(default=False)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("campaign","email")

class MessageInstance(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE)
    recipient = models.ForeignKey(Recipient, on_delete=models.CASCADE)
    message_id = models.CharField(max_length=255, unique=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicks = models.IntegerField(default=0)
    last_event_at = models.DateTimeField(null=True, blank=True)

class Link(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE)
    url = models.URLField()
    token = models.CharField(max_length=40, unique=True)

class LinkClick(models.Model):
    message = models.ForeignKey(MessageInstance, on_delete=models.CASCADE)
    link = models.ForeignKey(Link, on_delete=models.CASCADE)
    ts = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    ua = models.TextField(blank=True)

class OpenEvent(models.Model):
    message = models.ForeignKey(MessageInstance, on_delete=models.CASCADE)
    ts = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    ua = models.TextField(blank=True)

class DeliveryEvent(models.Model):
    DELIVERED="delivered"; BOUNCE="bounce"; REPLY="reply"; COMPLAINT="complaint"; DEFERRED="deferred"
    message = models.ForeignKey(MessageInstance, on_delete=models.CASCADE, null=True, blank=True)
    type = models.CharField(max_length=16)
    payload = models.JSONField(default=dict, blank=True)
    ts = models.DateTimeField(auto_now_add=True)
    
    
