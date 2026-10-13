"""Error center (roadmap 1.2): one open row per (instance, entity, Magento reference).

The sync loops call ``_record_failure`` / ``_record_success`` per record. A failure
upserts the open issue (occurrences + 1); a later success auto-resolves it by its trace
key. Issues can be retried in bulk (queued ``magento.job``), resolved or ignored.
"""
import json
import logging

import psycopg2

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import mute_logger

from .magento_job import classify_exception, exception_message

_logger = logging.getLogger(__name__)

ENTITIES = [
    ("product", "Product"),
    ("category", "Category"),
    ("customer", "Customer"),
    ("order", "Order"),
    ("invoice", "Invoice"),
    ("shipment", "Shipment"),
    ("creditmemo", "Credit Memo"),
    ("stock", "Stock"),
    ("attribute_set", "Attribute Set"),
    ("other", "Other"),
]
# Stored payloads are for replay/diagnosis, not archives.
_MAX_PAYLOAD_CHARS = 100000


class MagentoSyncIssue(models.Model):
    _name = "magento.sync.issue"
    _description = "Magento Sync Issue"
    _order = "last_seen desc, id desc"
    _rec_name = "display_label"

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    entity = fields.Selection(ENTITIES, required=True, default="other", readonly=True)
    magento_ref = fields.Char(string="Magento Reference", required=True, readonly=True)
    trace_key = fields.Char(compute="_compute_trace_key", store=True, index=True, readonly=True)
    display_label = fields.Char(compute="_compute_display_label")
    state = fields.Selection(
        [("open", "Open"), ("resolved", "Resolved"), ("ignored", "Ignored")],
        default="open", required=True, index=True, readonly=True,
    )
    severity = fields.Selection(
        [("transient", "Transient"), ("permanent", "Permanent")],
        default="permanent", required=True, readonly=True,
    )
    is_auth_error = fields.Boolean(string="Authentication Error", readonly=True)
    message = fields.Text(readonly=True)
    payload = fields.Text(readonly=True, help="Request or record payload, kept for replay.")
    payload_pretty = fields.Text(string="Payload (formatted)", compute="_compute_payload_pretty")
    occurrences = fields.Integer(default=1, readonly=True)
    first_seen = fields.Datetime(default=fields.Datetime.now, readonly=True)
    last_seen = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    res_model = fields.Char(string="Odoo Model", readonly=True)
    res_id = fields.Many2oneReference(string="Odoo Record ID", model_field="res_model", readonly=True)
    record_ref = fields.Reference(
        selection="_selection_record_model", string="Odoo Record", compute="_compute_record_ref"
    )
    job_id = fields.Many2one("magento.job", string="Retry Job", readonly=True, ondelete="set null")
    job_state = fields.Selection(related="job_id.state", string="Retry State")
    resolved_by = fields.Many2one("res.users", readonly=True, ondelete="set null",
                                  help="Empty on a resolved issue: resolved automatically.")
    resolved_date = fields.Datetime(readonly=True)

    _trace_key_open_uniq = models.UniqueIndex(
        "(trace_key) WHERE state = 'open'",
        "There is already an open issue for this Magento record.",
    )

    @api.model
    def _make_trace_key(self, instance, entity, magento_ref):
        return "%s-%s-%s" % (instance.id if instance else 0, entity or "other", magento_ref or "")

    @api.depends("instance_id", "entity", "magento_ref")
    def _compute_trace_key(self):
        for issue in self:
            issue.trace_key = self._make_trace_key(issue.instance_id, issue.entity, issue.magento_ref)

    @api.depends("entity", "magento_ref")
    def _compute_display_label(self):
        labels = dict(ENTITIES)
        for issue in self:
            issue.display_label = "%s %s" % (labels.get(issue.entity, issue.entity or ""), issue.magento_ref or "")

    @api.depends("payload")
    def _compute_payload_pretty(self):
        for issue in self:
            text = issue.payload or ""
            try:
                text = json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
            issue.payload_pretty = text or False

    @api.model
    def _selection_record_model(self):
        return [(m.model, m.name) for m in self.env["ir.model"].sudo().search([])]

    @api.depends("res_model", "res_id")
    def _compute_record_ref(self):
        for issue in self:
            ref = False
            if issue.res_model and issue.res_id and issue.res_model in self.env:
                record = self.env[issue.res_model].browse(issue.res_id).exists()
                if record:
                    ref = "%s,%s" % (issue.res_model, issue.res_id)
            issue.record_ref = ref

    # ------------------------------------------------------------------
    # API used by the sync loops (cheap, per record)
    # ------------------------------------------------------------------
    @api.model
    def _serialize_payload(self, payload):
        if payload is None or payload is False:
            return None
        if not isinstance(payload, str):
            try:
                payload = json.dumps(payload, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                payload = repr(payload)
        return payload[:_MAX_PAYLOAD_CHARS]

    @api.model
    def _record_failure(self, instance, entity, magento_ref, exc_or_message, *, payload=None,
                        record=None, job=None, severity=None):
        """Upsert the open issue of (instance, entity, magento_ref); return it.

        Severity is classified from the exception (transient: connection errors, timeouts,
        HTTP 429/5xx, DB serialization; permanent: the rest) unless given. Alerts the
        instance when the issue becomes permanent for the first time, and on any 401/403.
        """
        instance = instance.sudo()
        issues = self.sudo()
        auto_severity, is_auth, _status = classify_exception(exc_or_message)
        severity = severity or auto_severity
        message = exception_message(exc_or_message) or _("Unknown error")
        magento_ref = str(magento_ref if magento_ref not in (None, False) else "")
        entity = entity if entity in dict(ENTITIES) else "other"
        key = self._make_trace_key(instance, entity, magento_ref)
        if job is None and self.env.context.get("magento_job_id"):
            job = self.env["magento.job"].sudo().browse(self.env.context["magento_job_id"])
        now = fields.Datetime.now()
        vals = {"message": message, "severity": severity, "last_seen": now, "is_auth_error": is_auth}
        payload = self._serialize_payload(payload)
        if payload is not None:
            vals["payload"] = payload
        if record:
            vals.update(res_model=record._name, res_id=record[:1].id)
        if job:
            vals["job_id"] = job.id

        issue = issues.search([("trace_key", "=", key), ("state", "=", "open")], limit=1)
        created = False
        if issue:
            became_permanent = severity == "permanent" and issue.severity != "permanent"
            vals["occurrences"] = issue.occurrences + 1
            issue.write(vals)
        else:
            vals.update(instance_id=instance.id, entity=entity, magento_ref=magento_ref,
                        first_seen=now, occurrences=1, state="open")
            try:
                with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                    issue = issues.create(vals)
                    issue.flush_recordset()
                created = True
            except psycopg2.IntegrityError:
                # Concurrent upsert of the same trace key.
                issue = issues.search([("trace_key", "=", key), ("state", "=", "open")], limit=1)
                if not issue:
                    raise
                issue.write({"occurrences": issue.occurrences + 1, "last_seen": now, "message": message})
            became_permanent = created and severity == "permanent"

        if created and record:
            issue._post_on_record(record, message)
        if is_auth:
            instance._magento_mark_auth_error(message, issue=issue)
        elif became_permanent and instance.alert_on_permanent:
            instance._magento_raise_alert(
                _("Permanent sync failure: %(what)s", what=issue.display_label),
                message, issue=issue,
            )
        return issue

    @api.model
    def _record_success(self, instance, entity, magento_ref):
        """Auto-resolve the open issue of (instance, entity, magento_ref), if any."""
        key = self._make_trace_key(instance, entity, str(magento_ref if magento_ref not in (None, False) else ""))
        issue = self.sudo().search([("trace_key", "=", key), ("state", "=", "open")], limit=1)
        if issue:
            issue.write({"state": "resolved", "resolved_by": False, "resolved_date": fields.Datetime.now()})
        return issue

    def _post_on_record(self, record, message):
        record = record[:1].sudo().exists()
        if not record or not hasattr(record, "message_post"):
            return
        try:
            with self.env.cr.savepoint():
                record.message_post(
                    body=_("Magento sync failed (%(what)s): %(message)s",
                           what=self.display_label, message=message),
                    subtype_xmlid="mail.mt_note",
                )
        except Exception as exc:  # noqa: BLE001 - a chatter problem must never break a sync
            _logger.info("Magento issue %s: could not post on %s: %s", self.id, record, exc)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry(self):
        """Queue ``instance._magento_resync_one(entity, magento_ref)`` per issue (deduplicated)."""
        jobs = self.env["magento.job"]
        if not hasattr(type(self.env["magento.instance"]), "_magento_resync_one"):
            raise UserError(_("Retrying single records is not available in this version of the connector."))
        queued = skipped = 0
        for issue in self.filtered(lambda i: i.state == "open"):
            job = issue._enqueue_retry_job(jobs)
            if not job:
                skipped += 1
                continue
            issue.job_id = job.id
            queued += 1
        message = _("%s retry job(s) queued.", queued)
        if skipped:
            message += " " + _("%s issue(s) cannot be retried automatically; fix the cause and "
                               "resolve them.", skipped)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Error Center"),
                "message": message,
                "type": "warning" if skipped else "info",
                "sticky": bool(skipped),
            },
        }

    # Outgoing operations are retried by replaying them, never by pulling the
    # record back from Magento (which would overwrite the Odoo change).
    _REPLAYABLE_METHODS = ("_magento_auto_push", "_magento_push_stock_bulk")

    def _enqueue_retry_job(self, jobs):
        """Queue the right retry for this issue, or return an empty recordset."""
        self.ensure_one()
        failed = self.job_id.sudo()
        if failed and failed.method_name in self._REPLAYABLE_METHODS and failed.model_name in self.env:
            records = self.env[failed.model_name].browse(failed.res_ids or []).exists()
            if not records:
                return jobs.browse()
            return jobs.enqueue(
                records, failed.method_name, args=tuple(failed.args or ()),
                kwargs=failed.kwargs or None, instance=self.instance_id,
                channel=failed.channel, priority=failed.priority,
                identity_key=failed.identity_key or "retry-%s" % self.trace_key,
                description=_("Retry %(what)s", what=self.display_label),
            )
        if self.entity == "stock":
            if not hasattr(type(self.instance_id), "_magento_push_stock_bulk"):
                return jobs.browse()
            return jobs.enqueue(
                self.instance_id, "_magento_push_stock_bulk", instance=self.instance_id,
                channel="stock-%s" % self.instance_id.id,
                identity_key="stock-push-%s" % self.instance_id.id,
                description=_("Retry stock push"),
            )
        ref = self.magento_ref or ""
        resyncable = self.entity in ("order", "product", "customer", "category") or (
            self.entity == "attribute_set" and ref == "list"
        )
        if not resyncable or ref.startswith(("odoo-", "bulk-")):
            return jobs.browse()
        return jobs.enqueue(
            self.instance_id, "_magento_resync_one",
            args=(self.entity, ref),
            instance=self.instance_id,
            identity_key="retry-%s" % self.trace_key,
            description=_("Retry %(what)s", what=self.display_label),
        )

    def _close(self, state):
        self.filtered(lambda i: i.state == "open").write({
            "state": state,
            "resolved_by": self.env.uid,
            "resolved_date": fields.Datetime.now(),
        })
        return True

    def action_resolve(self):
        return self._close("resolved")

    def action_ignore(self):
        return self._close("ignored")

    def action_reopen(self):
        for issue in self.filtered(lambda i: i.state != "open"):
            other = self.search([("trace_key", "=", issue.trace_key), ("state", "=", "open")], limit=1)
            if not other:
                issue.write({"state": "open", "resolved_by": False, "resolved_date": False})
        return True

    def action_open_record(self):
        self.ensure_one()
        if not self.record_ref:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }
