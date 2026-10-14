"""Real-time ingress (roadmap 3.3): signed pushes from the SDLC_OdooConnector Magento module.

The free companion Magento module (``magento_extension/SDLC/OdooConnector``) records a tiny
event (entity, id, SKU or increment id, event type, time) in an outbox table when an order,
invoice, shipment, credit memo, product, customer or stock item is saved. A Magento cron
sends the outbox in batches::

    POST /magento/webhook/<instance webhook_uuid>
    X-SDLC-Signature: sha256=<hex HMAC-SHA256 of the raw body, key = shared secret>
    X-SDLC-Timestamp: <unix seconds>
    X-SDLC-Event-Id:  <delivery id>
    {"delivery_id": "...", "timestamp": 1760000000, "events": [{"id": "...", ...}]}

The body repeats the timestamp and delivery id, so the signature covers them too.

Odoo never trusts the payload for data: an event only says *which* record changed. Each
event queues ``instance._magento_resync_one(entity, ref)`` (invoices, shipments and credit
memos resync their order), which re-fetches the record through the REST API. Identity
keys ``rt-<instance>-<entity>-<ref>`` make a burst of events for one record a single job.
Stock events queue ``_magento_webhook_refresh_stock(sku)``.

Polling (``updated_at`` watermarks) stays the reconciliation fallback for missed events;
``webhook_reduces_polling`` lets the scheduled poll run less often while pushes are healthy.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import mute_logger

from ..services.magento_api import MagentoAPI, quote
from .sql_compat import Constraint, UniqueIndex
from . import sql_compat
from . import orm_compat

_logger = logging.getLogger(__name__)

WEBHOOK_ROUTE = "/magento/webhook/%s"
SIGNATURE_PREFIX = "sha256="
MAX_SKEW_SECONDS = 300  # older or newer requests are rejected (replay window)
MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 500
EVENT_RETENTION_DAYS = 3  # must stay far above MAX_SKEW_SECONDS (replay protection)
REJECTION_LOG_THROTTLE = timedelta(seconds=60)
HEALTHY_AFTER_LAST_EVENT = timedelta(hours=24)
RT_JOB_PRIORITY = 5  # before the next page of a bulk sync on the same channel
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
_SKIP_PUSH_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}

EVENT_ENTITIES = [
    ("order", "Order"),
    ("invoice", "Invoice"),
    ("shipment", "Shipment"),
    ("creditmemo", "Credit Memo"),
    ("product", "Product"),
    ("customer", "Customer"),
    ("stock", "Stock"),
    ("other", "Other"),
]
EVENT_STATES = [
    ("queued", "Queued"),
    ("ignored", "Ignored"),
    ("rejected", "Rejected"),
]
# Order documents resync their order (the order import brings its documents along).
_ORDER_DOCUMENTS = ("invoice", "shipment", "creditmemo")
# Real-time job channel = the entity's bulk sync channel, so a pushed record is never
# imported by two jobs at once; stock has its own channel.
_SYNC_CHANNEL_ENTITY = {"order": "orders", "product": "products", "customer": "customers"}


def _new_uuid():
    return uuid.uuid4().hex


def _new_secret():
    return secrets.token_urlsafe(32)


def sign_body(secret, body):
    """``sha256=<hex>`` HMAC of ``body`` (bytes) with ``secret``; what Magento sends."""
    if isinstance(body, str):
        body = body.encode()
    return SIGNATURE_PREFIX + hmac.new((secret or "").encode(), body, hashlib.sha256).hexdigest()


def _clean(value, limit=128):
    """A short single-line string, or "" (never trust the size of pushed values)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())[:limit]


def _clean_ids(value, limit=200):
    """``"12,13"`` from a list of integer ids (anything else is dropped)."""
    if not isinstance(value, list):
        return ""
    ids = []
    for item in value[:limit]:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and str(number) not in ids:
            ids.append(str(number))
    return ",".join(ids)


class _RestockFlagApi:
    """Delegates to a ``MagentoAPI``; adds the ``return_to_stock_items`` pushed by the
    companion module to the credit memos it returns (Magento's REST API does not expose
    which refunded items went back to stock). Data Magento returns itself wins."""

    def __init__(self, api, flags):
        self._api = api
        self._flags = flags  # {credit memo id: [order item ids]}

    def __getattr__(self, name):
        return getattr(self._api, name)

    def _flag(self, memo):
        if not isinstance(memo, dict):
            return memo
        credit_id = str(memo.get("entity_id") or memo.get("id") or "")
        ids = self._flags.get(credit_id)
        extension = memo.get("extension_attributes") or {}
        if ids and not memo.get("return_to_stock_items") and not extension.get("return_to_stock_items"):
            memo = dict(memo, return_to_stock_items=list(ids))
        return memo

    def get_credit_memos(self, *args, **kwargs):
        return [self._flag(memo) for memo in self._api.get_credit_memos(*args, **kwargs) or []]

    def get_credit_memo(self, *args, **kwargs):
        return self._flag(self._api.get_credit_memo(*args, **kwargs))


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _magento_sync_credit_memos(self, api=None, settle=False):
        """Hand the credit memo import the return-to-stock flags pushed in real time."""
        self.ensure_one()
        if self.magento_order_id and self.magento_instance_id:
            flags = self.magento_instance_id._magento_webhook_restock_flags(
                [self.magento_order_id])
            if flags:
                api = _RestockFlagApi(api or self._magento_api(), flags)
        return super()._magento_sync_credit_memos(api=api, settle=settle)


class MagentoWebhookEvent(models.Model):
    """One event pushed by Magento (accepted or rejected). Kept a few days for replay
    protection and troubleshooting; holds ids only, never customer data."""
    _name = "magento.webhook.event"
    _description = "Magento Real-time Event"
    _order = "id desc"

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade",
                                  readonly=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    event_uid = fields.Char(string="Event ID", readonly=True, index=True,
                            help="Id Magento gave the event; a second delivery of it is ignored.")
    delivery_id = fields.Char(string="Delivery", readonly=True)
    entity = fields.Selection(EVENT_ENTITIES, readonly=True)
    event_type = fields.Char(string="Event", readonly=True)
    magento_id = fields.Char(string="Magento ID", readonly=True)
    magento_ref = fields.Char(string="Reference", readonly=True,
                              help="What Odoo re-fetches: order id, SKU or customer id.")
    occurred_at = fields.Datetime(string="Occurred", readonly=True)
    received_at = fields.Datetime(string="Received", default=fields.Datetime.now, readonly=True,
                                  index=True)
    state = fields.Selection(EVENT_STATES, required=True, default="queued", readonly=True, index=True)
    job_id = fields.Many2one("magento.job", string="Job", readonly=True, ondelete="set null")
    job_state = fields.Selection(related="job_id.state", string="Job Status")
    note = fields.Char(readonly=True)
    return_to_stock_items = fields.Char(
        string="Returned to Stock", readonly=True,
        help="Credit memos: Magento order item ids returned to stock (comma separated). Magento "
             "does not keep this flag on the stored credit memo; the credit memo import uses it.")

    _event_uid_uniq = Constraint(
        "UNIQUE(instance_id, event_uid)",
        "This Magento event was already received.",
    )

    def name_get(self):
        # Odoo 16 labels many2one values through name_get().
        self._compute_display_name()
        return [(record.id, record.display_name) for record in self]

    @api.depends("entity", "magento_ref", "event_type")
    def _compute_display_name(self):
        labels = dict(EVENT_ENTITIES)
        for event in self:
            event.display_name = " ".join(filter(None, (
                labels.get(event.entity) or _("Event"), event.magento_ref or event.magento_id,
                event.event_type and "(%s)" % event.event_type,
            )))

    def action_open_job(self):
        self.ensure_one()
        if not self.job_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": "magento.job",
            "res_id": self.job_id.id,
            "view_mode": "form",
        }


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    webhook_enabled = fields.Boolean(
        string="Real-time Push", copy=False,
        help="Accept signed pushes from the SDLC_OdooConnector Magento module. Scheduled "
             "polling keeps running as the fallback for missed events.",
    )
    webhook_uuid = fields.Char(
        string="Webhook Key", copy=False, readonly=True, index=True,
        help="Random, unguessable part of the webhook URL that identifies this instance.",
    )
    webhook_secret = fields.Char(
        string="Shared Secret", copy=False, readonly=True, groups=_MANAGER_GROUP,
        help="HMAC-SHA256 key: paste it into Magento (Stores > Configuration > Services > "
             "Odoo Connector). Regenerating it breaks pushes until Magento has the new one.",
    )
    webhook_base_url = fields.Char(
        string="Public Odoo URL",
        help="Odoo address as Magento reaches it (e.g. https://erp.example.com). Empty = the "
             "web.base.url system parameter.",
    )
    webhook_url = fields.Char(string="Webhook URL", compute="_compute_webhook_url",
                              help="Paste into Magento: Odoo Connector > Odoo Webhook URL.")
    webhook_reduces_polling = fields.Boolean(
        string="Poll Less While Healthy",
        help="While pushes are healthy (an event accepted in the last 24 hours and no rejected "
             "push since), the scheduled sync of this instance runs at most once per "
             "'Fallback Poll Interval' instead of at every cron run.",
    )
    webhook_poll_interval_hours = fields.Integer(
        string="Fallback Poll Interval (h)", default=6,
        help="Minimum hours between two scheduled polls while pushes are healthy.",
    )
    webhook_last_poll_at = fields.Datetime(string="Last Scheduled Poll", readonly=True, copy=False)
    last_webhook_at = fields.Datetime(string="Last Push", compute="_compute_webhook_stats")
    last_webhook_rejected_at = fields.Datetime(string="Last Rejected Push",
                                               compute="_compute_webhook_stats")
    last_webhook_rejection = fields.Char(string="Last Rejection", compute="_compute_webhook_stats")
    webhook_event_count = fields.Integer(string="Events (24 h)", compute="_compute_webhook_stats")
    webhook_queued_count = fields.Integer(string="Queued (24 h)", compute="_compute_webhook_stats")
    webhook_rejected_count = fields.Integer(string="Rejected (24 h)", compute="_compute_webhook_stats")
    webhook_health = fields.Selection(
        [("off", "Off"), ("waiting", "Waiting for First Push"), ("healthy", "Healthy"),
         ("stale", "No Recent Push"), ("rejecting", "Rejecting Pushes")],
        string="Real-time Status", compute="_compute_webhook_stats",
    )
    webhook_recent_event_ids = fields.Many2many(
        "magento.webhook.event", string="Latest Events", compute="_compute_webhook_recent_events")

    _webhook_uuid_uniq = UniqueIndex(
        "(webhook_uuid) WHERE webhook_uuid IS NOT NULL",
        "Two Magento instances cannot share a webhook key.",
    )

    # ------------------------------------------------------------------
    # Keys: generated per record (a field default would give every existing
    # instance the same key when the column is added).
    # ------------------------------------------------------------------
    def init(self):
        super().init()
        sql_compat.execute(self.env.cr, 
            "SELECT id FROM magento_instance WHERE webhook_uuid IS NULL OR webhook_secret IS NULL")
        for (instance_id,) in self.env.cr.fetchall():
            sql_compat.execute(self.env.cr, 
                """UPDATE magento_instance
                      SET webhook_uuid = COALESCE(webhook_uuid, %s),
                          webhook_secret = COALESCE(webhook_secret, %s)
                    WHERE id = %s""",
                (_new_uuid(), _new_secret(), instance_id),
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("webhook_uuid", _new_uuid())
            vals.setdefault("webhook_secret", _new_secret())
        return super().create(vals_list)

    def _check_webhook_manager(self):
        if not self.env.su and not self.env.user.has_group(_MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can manage real-time push keys."))

    def action_webhook_regenerate_secret(self):
        self._check_webhook_manager()
        for instance in self:
            instance.sudo().webhook_secret = _new_secret()
            instance.message_post(body=_("Real-time push secret regenerated; update it in Magento."))
        return True

    def action_webhook_regenerate_uuid(self):
        self._check_webhook_manager()
        for instance in self:
            instance.sudo().webhook_uuid = _new_uuid()
            instance.message_post(body=_("Real-time push URL regenerated; update it in Magento."))
        return True

    @api.depends("webhook_uuid", "webhook_base_url")
    def _compute_webhook_url(self):
        default_base = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        for instance in self:
            base = (instance.webhook_base_url or default_base).strip().rstrip("/")
            instance.webhook_url = (
                "%s%s" % (base, WEBHOOK_ROUTE % instance.webhook_uuid) if instance.webhook_uuid else False
            )

    def _compute_webhook_stats(self):
        now = fields.Datetime.now()
        since = now - timedelta(hours=24)
        Event = self.env["magento.webhook.event"].sudo()
        counts = {}
        for instance, state, count in orm_compat.read_group(Event, 
            [("instance_id", "in", self.ids), ("received_at", ">=", since)],
            ["instance_id", "state"], ["__count"],
        ):
            counts[(instance.id, state)] = count
        accepted_rows = {inst.id: at for inst, at in orm_compat.read_group(Event, 
            [("instance_id", "in", self.ids), ("state", "!=", "rejected")],
            ["instance_id"], ["received_at:max"])}
        rejected_rows = {inst.id: at for inst, at in orm_compat.read_group(Event, 
            [("instance_id", "in", self.ids), ("state", "=", "rejected")],
            ["instance_id"], ["received_at:max"])}
        for instance in self:
            key = instance._origin.id
            accepted_at = accepted_rows.get(key)
            rejected_at = rejected_rows.get(key)
            instance.last_webhook_at = accepted_at or False
            instance.last_webhook_rejected_at = rejected_at or False
            instance.last_webhook_rejection = False
            if rejected_at:
                instance.last_webhook_rejection = Event.search(
                    [("instance_id", "=", key), ("state", "=", "rejected")], limit=1).note
            queued = counts.get((key, "queued"), 0)
            ignored = counts.get((key, "ignored"), 0)
            rejected = counts.get((key, "rejected"), 0)
            instance.webhook_event_count = queued + ignored
            instance.webhook_queued_count = queued
            instance.webhook_rejected_count = rejected
            if not instance.webhook_enabled:
                health = "off"
            elif rejected_at and (not accepted_at or rejected_at > accepted_at):
                health = "rejecting"
            elif not accepted_at:
                health = "waiting"
            elif accepted_at < now - HEALTHY_AFTER_LAST_EVENT:
                health = "stale"
            else:
                health = "healthy"
            instance.webhook_health = health

    def _compute_webhook_recent_events(self):
        Event = self.env["magento.webhook.event"]
        for instance in self:
            instance.webhook_recent_event_ids = Event.search(
                [("instance_id", "=", instance._origin.id)], limit=10) if instance._origin.id else Event

    def action_view_webhook_events(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_webhook_event")
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"default_instance_id": self.id}
        return action

    # ------------------------------------------------------------------
    # Ingress (called by the controller as the technical superuser)
    # ------------------------------------------------------------------
    @api.model
    def _magento_webhook_handle(self, webhook_uuid, body, headers):
        """Authenticate and ingest one push. Returns ``(http status, json dict)``.

        404 unknown/disabled instance, 401 bad signature or stale timestamp, 400 malformed
        body, 413 too large, 200 accepted. Responses never contain data.
        """
        not_found = (404, {"error": "not found"})
        webhook_uuid = (webhook_uuid or "").strip().lower()
        if not _UUID_RE.match(webhook_uuid):
            return not_found
        instance = self.sudo().with_context(active_test=False).search(
            [("webhook_uuid", "=", webhook_uuid)], limit=1)
        if not instance or not instance.active or not instance.webhook_enabled or not instance.webhook_secret:
            return not_found
        instance = instance.with_company(instance.company_id)
        body = body or b""
        if len(body) > MAX_BODY_BYTES:
            return 413, {"error": "payload too large"}
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        signature = headers.get("x-sdlc-signature", "").strip()
        expected = sign_body(instance.webhook_secret, body)
        if not signature or not hmac.compare_digest(expected.encode(), signature.encode()):
            return instance._magento_webhook_reject(401, "invalid signature")
        try:
            timestamp = int(headers.get("x-sdlc-timestamp", ""))
        except ValueError:
            return instance._magento_webhook_reject(401, "missing or invalid timestamp")
        if abs(time.time() - timestamp) > MAX_SKEW_SECONDS:
            return instance._magento_webhook_reject(401, "stale timestamp")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"error": "invalid json"}
        if not isinstance(payload, dict):
            return 400, {"error": "invalid json"}
        delivery_id = _clean(headers.get("x-sdlc-event-id"), 64)
        # The signature covers the body only; the body must repeat the signed headers.
        if str(payload.get("timestamp")) != str(timestamp) or (
                delivery_id and _clean(payload.get("delivery_id"), 64) != delivery_id):
            return instance._magento_webhook_reject(401, "headers do not match the signed body")
        events = payload.get("events")
        if not isinstance(events, list):
            return 400, {"error": "events must be a list"}
        if len(events) > MAX_EVENTS:
            return 413, {"error": "too many events"}
        result = instance._magento_webhook_ingest(events, delivery_id)
        return 200, dict(status="ok", **result)

    def _magento_webhook_reject(self, status, reason):
        """Log a rejected push (at most one stored row per instance a minute: an
        unauthenticated caller must not be able to fill the table)."""
        self.ensure_one()
        _logger.warning("Magento real-time push rejected for instance %s: %s", self.id, reason)
        Event = self.env["magento.webhook.event"].sudo()
        recent = Event.search_count([
            ("instance_id", "=", self.id), ("state", "=", "rejected"),
            ("received_at", ">=", fields.Datetime.now() - REJECTION_LOG_THROTTLE),
        ], limit=1)
        if not recent:
            Event.create({"instance_id": self.id, "state": "rejected", "entity": "other",
                          "note": "HTTP %s: %s" % (status, reason)})
        return status, {"error": reason}

    def _magento_webhook_ingest(self, events, delivery_id=None):
        """Store each new event and queue its resync. Returns counts."""
        self.ensure_one()
        Event = self.env["magento.webhook.event"].sudo()
        uids = [_clean(raw.get("id"), 64) for raw in events if isinstance(raw, dict)]
        known = set(Event.search([
            ("instance_id", "=", self.id), ("event_uid", "in", [u for u in uids if u]),
        ]).mapped("event_uid")) if uids else set()
        counts = {"accepted": 0, "duplicates": 0, "ignored": 0, "jobs": 0}
        jobs = self.env["magento.job"]
        for raw in events:
            uid = _clean(raw.get("id"), 64) if isinstance(raw, dict) else ""
            if not uid:
                counts["ignored"] += 1
                continue
            if uid in known:
                counts["duplicates"] += 1
                continue
            known.add(uid)
            entity = _clean(raw.get("entity"), 32).lower()
            vals = {
                "instance_id": self.id,
                "event_uid": uid,
                "delivery_id": delivery_id or False,
                "entity": entity if entity in dict(EVENT_ENTITIES) else "other",
                "event_type": _clean(raw.get("event"), 32) or False,
                "magento_id": _clean(raw.get("entity_id"), 64) or False,
                "occurred_at": self._magento_webhook_datetime(raw.get("occurred_at")),
            }
            if entity == "creditmemo":
                vals["return_to_stock_items"] = _clean_ids(raw.get("return_to_stock_items")) or False
            kind, ref, note = self._magento_webhook_route(entity, raw)
            vals.update(magento_ref=ref or False, note=note or False,
                        state="queued" if kind else "ignored")
            try:
                with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                    event = Event.create(vals)
                    event.flush_recordset()
            except psycopg2.IntegrityError:
                counts["duplicates"] += 1  # the same event raced in through a retry
                continue
            if not kind:
                counts["ignored"] += 1
                continue
            job = self._magento_webhook_enqueue(kind, ref)
            event.job_id = job.id
            jobs |= job
            counts["accepted"] += 1
        counts["jobs"] = len(jobs)
        return counts

    @staticmethod
    def _magento_webhook_datetime(value):
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None, microsecond=0)
            text = _clean(value, 40).replace("T", " ").rstrip("Z")
            return fields.Datetime.to_datetime(text[:19]) if text else False
        except (ValueError, OverflowError, OSError):
            return False

    def _magento_webhook_route(self, entity, raw):
        """``(kind, ref, note)``: what to re-fetch for one event, or ``(None, None, why)``."""
        event_type = _clean(raw.get("event"), 32).lower()
        entity_id = _clean(raw.get("entity_id"), 64)
        if entity == "order":
            return ("order", entity_id, None) if entity_id else (None, None, _("No order id."))
        if entity in _ORDER_DOCUMENTS:
            order_id = _clean(raw.get("order_id"), 64)
            if not order_id:
                return None, None, _("No order id.")
            return "order", order_id, None
        if entity in ("product", "stock"):
            sku = _clean(raw.get("sku"), 64)
            if not sku:
                return None, None, _("No SKU.")
            if entity == "product" and event_type == "deleted":
                # Re-fetching would only fail; the product binding is left as is and the
                # reconciliation report shows it.
                return None, sku, _("Product deleted in Magento; the Odoo product is kept.")
            return entity, sku, None
        if entity == "customer":
            return ("customer", entity_id, None) if entity_id else (None, None, _("No customer id."))
        return None, None, _("Unsupported entity.")

    def _magento_webhook_enqueue(self, kind, ref):
        """Queue (or join) the real-time job of one record.

        Identity ``rt-<instance>-<kind>-<ref>``: every event for a record that arrives while
        its job is pending joins that job. While the job runs (it may already have fetched
        the record) a second key ``...-next`` queues one follow-up, like the stock delta push.
        Jobs run as the technical superuser in the instance company.
        """
        self.ensure_one()
        Job = self.env["magento.job"].with_user(SUPERUSER_ID).with_company(self.company_id)
        base = "rt-%s-%s-%s" % (self.id, kind, ref)
        keys = (base, base + "-next")
        active = Job.sudo().search([("identity_key", "in", keys), ("state", "in", ("pending", "started"))])
        pending = active.filtered(lambda job: job.state == "pending")
        if pending:
            return pending[:1]
        free = [key for key in keys if key not in set(active.mapped("identity_key"))]
        if not free:
            return active[:1]
        if kind == "stock":
            method, args, channel = "_magento_webhook_refresh_stock", (ref,), "stock-%s" % self.id
            description = _("Real-time: stock of %(sku)s (%(name)s)", sku=ref, name=self.name)
        else:
            method, args = "_magento_resync_one", (kind, ref)
            channel = self._magento_sync_channel(_SYNC_CHANNEL_ENTITY[kind])
            description = _("Real-time: %(kind)s %(ref)s (%(name)s)", kind=kind, ref=ref, name=self.name)
        return Job.enqueue(
            self, method, args=args, instance=self, channel=channel,
            priority=RT_JOB_PRIORITY, identity_key=free[0], description=description,
        )

    def _magento_webhook_restock_flags(self, order_ids):
        """``{credit memo id: [order item ids returned to stock]}`` pushed for these
        Magento orders (newest event wins)."""
        self.ensure_one()
        events = self.env["magento.webhook.event"].sudo().search([
            ("instance_id", "=", self.id), ("entity", "=", "creditmemo"),
            ("magento_ref", "in", [str(order_id) for order_id in order_ids]),
            ("return_to_stock_items", "!=", False),
        ], order="id")
        return {event.magento_id: [int(i) for i in event.return_to_stock_items.split(",") if i]
                for event in events if event.magento_id}

    def _magento_webhook_refresh_stock(self, sku):
        """Job body of a stock event: read the SKU's Magento stock item and store it on the
        product binding. Odoo's own stock push (when enabled) stays the owner of Magento's
        quantity; nothing is pushed back from here."""
        self.ensure_one()
        sku = (sku or "").strip()
        maps = self.env["magento.product.map"].sudo().search([
            ("instance_id", "=", self.id), ("sku", "=", sku)])
        if not maps:
            return "SKU %s is not mapped in Odoo; nothing to refresh." % sku
        safe_sku = quote(sku, safe="")
        item = MagentoAPI(self)._request_with_prefix_fallback(
            "get", lambda prefix: "%s/stockItems/%s" % (prefix, safe_sku))
        if not isinstance(item, dict) or not item:
            raise UserError(_("Magento returned no stock item for SKU %s.", sku))
        vals = {
            "quantity": float(item.get("qty") or 0.0),
            "stock_status": "in_stock" if item.get("is_in_stock") else "out_of_stock",
        }
        if "magento_backorders" in maps._fields and item.get("backorders") is not None:
            backorders = str(item.get("backorders"))
            if backorders in dict(maps._fields["magento_backorders"].selection):
                vals["magento_backorders"] = backorders
        maps.with_context(**_SKIP_PUSH_CTX).write(vals)
        return {"sku": sku, "quantity": vals["quantity"], "stock_status": vals["stock_status"],
                "bindings": len(maps)}

    # ------------------------------------------------------------------
    # Polling fallback: poll less while pushes are healthy
    # ------------------------------------------------------------------
    def _magento_webhook_poll_due(self, now=None):
        """False when this instance's scheduled poll can be skipped (pushes healthy and
        the last poll is recent enough)."""
        self.ensure_one()
        if not (self.webhook_enabled and self.webhook_reduces_polling):
            return True
        if self.webhook_health != "healthy":
            return True
        now = now or fields.Datetime.now()
        interval = timedelta(hours=max(int(self.webhook_poll_interval_hours or 0), 1))
        return not self.webhook_last_poll_at or self.webhook_last_poll_at <= now - interval

    def _run_scheduled_sync(self, source_action):
        now = fields.Datetime.now()
        due = self.filtered(lambda inst: inst._magento_webhook_poll_due(now))
        for instance in self - due:
            _logger.info("Magento instance %s: real-time push healthy, scheduled poll skipped "
                         "(last poll %s).", instance.name, instance.webhook_last_poll_at)
        polled = due.filtered(lambda inst: inst.webhook_enabled and inst.webhook_reduces_polling)
        if polled:
            polled.sudo().write({"webhook_last_poll_at": now})
        return super(MagentoInstance, due)._run_scheduled_sync(source_action)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------
    @api.model
    def _cron_cleanup_logs(self):
        totals = super()._cron_cleanup_logs()
        cutoff = fields.Datetime.now() - timedelta(days=EVENT_RETENTION_DAYS)
        deleted = self._ops_delete_batched("magento.webhook.event", [("received_at", "<", cutoff)])
        if isinstance(totals, dict):
            totals["webhook_events"] = deleted
        return totals
