# campaigns/mcp.py
# MC Pigeon — CRUD-style MCP tools (one tool per model, `action` switch).
# No HTTP transport; works directly with Django models. Assumes admin auth is
# provided to the MCP layer elsewhere.

import logging
from typing import Any, Dict, Literal, Optional

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.forms.models import model_to_dict
from django.utils.crypto import get_random_string
from mcp_server import MCPToolset

from .models import (Campaign, DeliveryEvent, Link, LinkClick, Mailbox,
                   MessageInstance, OpenEvent, Recipient)
from .tasks import send_campaign_task

log = logging.getLogger("mcp.campaigns")


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _redact_mailbox(d: Dict[str, Any]) -> Dict[str, Any]:
    """Hide secrets in Mailbox dicts."""
    if "smtp_password" in d:
        d["smtp_password"] = "********"
    if "imap_password" in d:
        d["imap_password"] = "********"
    return d

def _serialize(obj) -> Dict[str, Any]:
    """Serialize model to a plain dict (FKs as *_id)."""
    data = model_to_dict(obj)
    if isinstance(obj, Mailbox):
        data = _redact_mailbox(data)
    # Expose primary key explicitly as `id`
    data["id"] = obj.pk
    return data

def _apply_updates(obj, payload: Dict[str, Any], *, allowed: Optional[set] = None):
    """Update model fields with a payload; ignores unknown keys."""
    fields = allowed or {f.name for f in obj._meta.get_fields() if getattr(f, "concrete", False)}
    # Never allow overriding PK
    fields.discard("id")
    for k, v in payload.items():
        if k in fields:
            setattr(obj, k, v)
        # accept FK via <field>_id convenience
        elif k.endswith("_id") and k[:-3] in fields:
            setattr(obj, k, v)
    obj.full_clean()
    obj.save()
    return obj

def _get(model, id_):
    try:
        return model.objects.get(pk=id_)
    except ObjectDoesNotExist:
        raise

def _maybe_limit(qs, payload: Optional[Dict[str, Any]]):
    limit = None
    if payload and isinstance(payload, dict):
        limit = payload.get("limit")
        filters = payload.get("filters")  # optional dict of django ORM filters
        if isinstance(filters, dict) and filters:
            qs = qs.filter(**filters)
    if isinstance(limit, int) and limit > 0:
        qs = qs[: min(limit, 500)]
    else:
        qs = qs[:500]
    return qs


# ────────────────────────────────────────────────────────────
# Generic CRUD Toolset Factory
# ────────────────────────────────────────────────────────────

def create_crud_toolset(model_class, tool_name, order_by="-id"):
    """Factory to create a generic MCPToolset for a given model."""

    class GenericCrudTools(MCPToolset):
        def _list(self, payload):
            qs = model_class.objects.all().order_by(order_by)
            return [_serialize(m) for m in _maybe_limit(qs, payload)]

        def _read(self, payload):
            return _serialize(_get(model_class, payload["id"]))

        def _create(self, payload):
            # Special handling for Link token generation
            if model_class == Link and "token" not in payload:
                payload["token"] = get_random_string(40)
            return _serialize(model_class.objects.create(**payload))

        def _update(self, payload):
            obj_id = payload.pop("id")
            return _serialize(_apply_updates(_get(model_class, obj_id), payload))

        def _delete(self, payload):
            obj = _get(model_class, payload["id"])
            obj.delete()
            return {"ok": True}

        def tool(self, action: Literal["list", "read", "create", "update", "delete"], payload: Any | None = None):
            payload = payload or {}
            try:
                action_map = {
                    "list": self._list,
                    "read": self._read,
                    "create": self._create,
                    "update": self._update,
                    "delete": self._delete,
                }
                return action_map[action](payload)
            except (ValidationError, KeyError, ObjectDoesNotExist) as e:
                return {"ok": False, "error": str(e)}

        tool.__name__ = tool_name
        tool.__doc__ = f"""
        ---
        name: {tool_name}s
        description: CRUD for {model_class.__name__} rows.
        ...
        """

    # We need to return the method from an *instance* of the class.
    return getattr(GenericCrudTools(), "tool")

# ────────────────────────────────────────────────────────────
# Toolset Instances
# ────────────────────────────────────────────────────────────

mailbox = create_crud_toolset(Mailbox, "mailbox", order_by="id")
recipient = create_crud_toolset(Recipient, "recipient", order_by="id")
message = create_crud_toolset(MessageInstance, "message", order_by="-id")
link = create_crud_toolset(Link, "link", order_by="-id")
linkclick = create_crud_toolset(LinkClick, "linkclick", order_by="-ts")
openevent = create_crud_toolset(OpenEvent, "openevent", order_by="-ts")
deliveryevent = create_crud_toolset(DeliveryEvent, "deliveryevent", order_by="-ts")


# ────────────────────────────────────────────────────────────
# Campaign Tool — a comprehensive tool for managing campaigns
# ────────────────────────────────────────────────────────────

class CampaignTools(MCPToolset):
    """A comprehensive tool for managing email campaigns."""

    def _list(self, payload):
        qs = Campaign.objects.all().order_by("-created_at")
        return [_serialize(m) for m in _maybe_limit(qs, payload)]

    def _read(self, payload):
        return _serialize(_get(Campaign, payload["id"]))

    def _create(self, payload):
        return _serialize(Campaign.objects.create(**payload))

    def _update(self, payload):
        obj_id = payload.pop("id")
        return _serialize(_apply_updates(_get(Campaign, obj_id), payload))

    def _delete(self, payload):
        obj = _get(Campaign, payload["id"])
        obj.delete()
        return {"ok": True}

    def _send(self, payload: Dict[str, Any]):
        campaign_id = payload["campaign_id"]
        dry_run = bool(payload.get("dry_run", False))
        batch_size = payload.get("batch_size")
        sleep = payload.get("sleep")

        try:
            campaign = _get(Campaign, campaign_id)
        except ObjectDoesNotExist:
            return {"ok": False, "error": "Campaign not found", "id": campaign_id}

        if dry_run:
            recipient_count = campaign.recipients.filter(unsubscribed=False).count()
            return {
                "ok": True,
                "dry_run": True,
                "message": f"Dry run: Campaign {campaign.id} would be sent to {recipient_count} recipients.",
                "recipient_count": recipient_count,
            }

        # Always pass campaign.id to the task
        task = send_campaign_task.delay(campaign.id, batch_size=batch_size, sleep=sleep)
        campaign.status = Campaign.SENDING
        campaign.save()

        return {
            "ok": True,
            "status": "queued",
            "campaign_id": campaign.id,
            "task_id": task.id,
            "message": f"Campaign {campaign.id} has been queued for sending.",
        }

    def _status(self, payload: Dict[str, Any]):
        cid = payload["campaign_id"]
        try:
            c = _get(Campaign, cid)
        except ObjectDoesNotExist:
            return {"ok": False, "error": "Campaign not found", "id": cid}
        qs = MessageInstance.objects.filter(campaign_id=cid)
        sent = qs.filter(sent_at__isnull=False).count()
        opened = qs.filter(opened_at__isnull=False).count()
        bounced = qs.filter(bounced_at__isnull=False).count()
        clicks = list(qs.values_list("clicks", flat=True))
        total_clicks = sum(clicks) if clicks else 0
        last = qs.order_by("-last_event_at").values_list("last_event_at", flat=True).first()
        return {
            "ok": True,
            "campaign_id": c.id,
            "name": c.name,
            "subject": c.subject,
            "status": c.status,
            "totals": {
                "recipients": c.recipients.count(),
                "sent": sent, "opened": opened, "bounced": bounced, "clicks": total_clicks,
            },
            "last_event_at": last.isoformat() if last else None,
        }

    def _add_recipient(self, payload: Dict[str, Any]):
        try:
            r, created = Recipient.objects.get_or_create(
                campaign_id=payload["campaign_id"],
                email=payload["email"].strip(),
                defaults={"name": payload.get("name", "").strip()},
            )
            return {"ok": True, "recipient_id": r.id, "existed": (not created)}
        except ObjectDoesNotExist:
            return {"ok": False, "error": "Campaign not found", "id": payload.get("campaign_id")}
        except ValidationError as ve:
            return {"ok": False, "error": f"Invalid recipient: {ve.message}", "email": payload.get("email")}

    def _list_recipients(self, payload: Dict[str, Any]):
        cid = payload["campaign_id"]
        try:
            campaign = _get(Campaign, cid)
            qs = campaign.recipients.all()
            return [_serialize(r) for r in _maybe_limit(qs, payload)]
        except ObjectDoesNotExist:
            return {"ok": False, "error": "Campaign not found", "id": cid}

    def _clone(self, payload: Dict[str, Any]):
        cid = payload["campaign_id"]
        new_name = payload.get("new_name")
        try:
            original = _get(Campaign, cid)
            clone = original
            clone.pk = None
            clone.status = Campaign.Status.DRAFT
            clone.name = new_name or f"Clone of {original.name}"
            clone.save() # save to get a PK for recipient relations
            
            # Also clone recipients
            recipients = original.recipients.all()
            for r in recipients:
                r.pk = None
                r.campaign = clone
                r.save()
            
            return _serialize(clone)
        except ObjectDoesNotExist:
            return {"ok": False, "error": "Campaign not found", "id": cid}


    def campaigns(
        self,
        action: Literal[
            "list", "read", "create", "update", "delete",
            "send", "status", "add_recipient", "list_recipients", "clone"
        ],
        payload: Dict[str, Any] | None = None,
    ):
        """
        ---
        name: campaigns
        description: |
          A comprehensive tool to manage email campaigns, from creation and
          recipient management to sending and status tracking.

        parameters:
          type: object
          properties:
            action:
              type: string
              enum:
                - list
                - read
                - create
                - update
                - delete
                - send
                - status
                - add_recipient
                - list_recipients
                - clone
            payload: { type: object }
          required: [action]

        actions:
          list:
            summary: Returns a list of all campaigns.
            payload:
              type: object
              properties:
                limit: {type: integer, description: "Max results, default 500."}
                filters: {type: object, description: "Django ORM filters."}
          read:
            summary: Fetch a single campaign by its ID.
            payload:
              type: object
              properties:
                id: {type: integer}
              required: [id]
          create:
            summary: Create a new campaign.
            payload:
              type: object
              properties:
                name: {type: string}
                subject: {type: string}
                mailbox_id: {type: integer}
                template_markdown: {type: string}
              required: [name, subject, mailbox_id, template_markdown]
          update:
            summary: Update an existing campaign.
            payload:
              type: object
              properties:
                id: {type: integer}
              required: [id]
          delete:
            summary: Remove a campaign.
            payload:
              type: object
              properties:
                id: {type: integer}
              required: [id]
          send:
            summary: Send a campaign now (SMTP + IMAP APPEND).
            payload:
              type: object
              properties:
                campaign_id: {type: integer}
                dry_run: {type: boolean, default: false}
                batch_size: {type: integer, nullable: true}
                sleep: {type: number, nullable: true}
              required: [campaign_id]
            response:
              type: object
              properties:
                ok: {type: boolean}
          status:
            summary: Snapshot counters for a campaign.
            payload:
              type: object
              properties:
                campaign_id: {type: integer}
              required: [campaign_id]
          add_recipient:
            summary: Attach a recipient to a campaign (idempotent).
            payload:
              type: object
              properties:
                campaign_id: {type: integer}
                email: {type: string, format: email}
                name: {type: string}
              required: [campaign_id, email]
          list_recipients:
            summary: List all recipients for a given campaign.
            payload:
              type: object
              properties:
                campaign_id: {type: integer}
                limit: {type: integer, description: "Max results, default 500."}
              required: [campaign_id]
          clone:
            summary: Clones an existing campaign and its recipients.
            payload:
              type: object
              properties:
                campaign_id: {type: integer}
                new_name: {type: string, description: "Optional new name for the cloned campaign."}
              required: [campaign_id]
        ...
        """
        payload = payload or {}
        try:
            action_map = {
                "list": self._list,
                "read": self._read,
                "create": self._create,
                "update": self._update,
                "delete": self._delete,
                "send": self._send,
                "status": self._status,
                "add_recipient": self._add_recipient,
                "list_recipients": self._list_recipients,
                "clone": self._clone,
            }
            return action_map[action](payload)
        except (KeyError, ValidationError, ObjectDoesNotExist) as e:
            return {"ok": False, "error": str(e)}

# Make an instance of the tool available.
campaigns = CampaignTools().campaigns