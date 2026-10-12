"""Operations side of ``magento.instance``: alerts, log retention and queue/error-center links
(roadmap 1.8)."""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.modules import module as odoo_module

_logger = logging.getLogger(__name__)

_ALERT_SUMMARY = "Magento sync alert"
# A chatter alert is not repeated for this long unless a new activity is scheduled.
_ALERT_REPOST_AFTER = timedelta(hours=1)
_CLEANUP_BATCH = 1000
# Failed jobs are kept at least this long, whatever the instance retention.
_FAILED_JOB_RETENTION_DAYS = 90
_DEFAULT_RETENTION_DAYS = 30


class MagentoInstance(models.Model):
    _name = "magento.instance"
    _inherit = ["magento.instance", "mail.activity.mixin"]

    log_retention_days = fields.Integer(
        string="Log Retention (days)", default=_DEFAULT_RETENTION_DAYS,
        help="Done and cancelled jobs, resolved and ignored issues and sync reports older than "
             "this are deleted daily. Failed jobs are kept at least 90 days; open issues are "
             "never deleted.",
    )
    alert_user_ids = fields.Many2many(
        "res.users", "magento_instance_alert_user_rel", "instance_id", "user_id",
        string="Alert Users", domain="[('share', '=', False)]",
        help="Receive a to-do activity on permanent sync failures and authentication errors.",
    )
    alert_on_permanent = fields.Boolean(
        string="Alert on Permanent Failures", default=True,
        help="Alert when a record fails with a non-retryable error. Authentication errors "
             "always alert.",
    )
    last_alert_at = fields.Datetime(readonly=True, copy=False)
    open_issue_count = fields.Integer(compute="_compute_ops_counts")
    failed_job_count = fields.Integer(compute="_compute_ops_counts")

    @api.constrains("log_retention_days")
    def _check_log_retention_days(self):
        for instance in self:
            if instance.log_retention_days < 1:
                raise ValidationError(_("Log retention must be at least one day."))

    def _compute_ops_counts(self):
        issue_rows = self.env["magento.sync.issue"]._read_group(
            [("instance_id", "in", self.ids), ("state", "=", "open")], ["instance_id"], ["__count"]
        )
        job_rows = self.env["magento.job"]._read_group(
            [("instance_id", "in", self.ids), ("state", "=", "failed")], ["instance_id"], ["__count"]
        )
        issues = {inst.id: n for inst, n in issue_rows}
        jobs = {inst.id: n for inst, n in job_rows}
        for instance in self:
            instance.open_issue_count = issues.get(instance.id, 0)
            instance.failed_job_count = jobs.get(instance.id, 0)

    def action_view_open_issues(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_sync_issue"
        )
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"search_default_open": 1, "default_instance_id": self.id}
        return action

    def action_view_failed_jobs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_job"
        )
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"search_default_failed": 1}
        return action

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    def _magento_raise_alert(self, subject, message, *, issue=None):
        """Alert the instance's alert users, de-duplicated.

        Each alert user gets at most one open "Magento sync alert" to-do on the instance.
        The chatter note is posted when a new activity is scheduled, or at most once an
        hour otherwise, so a sync with many failing records does not flood the chatter.
        """
        self.ensure_one()
        instance = self.sudo()
        now = fields.Datetime.now()
        users = instance.alert_user_ids.filtered("active")
        open_users = self.env["mail.activity"].sudo().search([
            ("res_model", "=", self._name),
            ("res_id", "=", instance.id),
            ("summary", "=", _ALERT_SUMMARY),
        ]).user_id
        new_users = users - open_users
        if new_users:
            note = Markup("%s<br/>%s") % (subject, message)
            for user in new_users:
                instance.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=_ALERT_SUMMARY,
                    note=note,
                    user_id=user.id,
                )
        elif instance.last_alert_at and instance.last_alert_at > now - _ALERT_REPOST_AFTER:
            return False
        instance.message_post(
            body=_("%(subject)s: %(message)s", subject=subject, message=message),
            subtype_xmlid="mail.mt_note",
        )
        instance.last_alert_at = now
        return True

    def _magento_mark_auth_error(self, message, *, issue=None):
        """A 401/403 from Magento: flag the connection as broken and alert."""
        self.ensure_one()
        instance = self.sudo()
        text = _("Authentication failed: %s", message)[:1000]
        if instance.state != "error" or instance.last_connection_message != text:
            instance.write({"state": "error", "last_connection_message": text})
        return self._magento_raise_alert(_("Magento authentication error"), message, issue=issue)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------
    def _ops_can_commit(self):
        in_test_mode = getattr(self.env.registry, "in_test_mode", None)
        return bool(
            self.env.context.get("ir_cron_progress_id")
            and self.env.context.get("cron_id")
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    def _ops_delete_batched(self, model, domain):
        records = self.env[model].sudo()
        deleted = 0
        while True:
            batch = records.search(domain, limit=_CLEANUP_BATCH, order="id")
            if not batch:
                break
            batch.unlink()
            deleted += len(batch)
            if self._ops_can_commit():
                self.env["ir.cron"]._commit_progress(len(batch))
            if len(batch) < _CLEANUP_BATCH:
                break
        return deleted

    @api.model
    def _cron_cleanup_logs(self):
        """Delete old jobs, closed issues and sync reports; never open issues."""
        now = fields.Datetime.now()
        totals = {"jobs": 0, "issues": 0, "reports": 0}
        instances = self.sudo().with_context(active_test=False).search([])
        for instance in instances:
            days = instance.log_retention_days or _DEFAULT_RETENTION_DAYS
            cutoff = now - timedelta(days=days)
            failed_cutoff = now - timedelta(days=max(days, _FAILED_JOB_RETENTION_DAYS))
            totals["jobs"] += self._ops_delete_batched("magento.job", [
                ("instance_id", "=", instance.id),
                "|",
                "&", ("state", "in", ("done", "cancelled")), ("date_done", "<", cutoff),
                "&", ("state", "=", "failed"), ("date_done", "<", failed_cutoff),
            ])
            totals["issues"] += self._ops_delete_batched("magento.sync.issue", [
                ("instance_id", "=", instance.id),
                ("state", "in", ("resolved", "ignored")),
                "|", ("resolved_date", "<", cutoff),
                "&", ("resolved_date", "=", False), ("last_seen", "<", cutoff),
            ])
            totals["reports"] += self._ops_delete_batched("magento.sync.report", [
                ("instance_id", "=", instance.id), ("synced_at", "<", cutoff),
            ])
        # Jobs not bound to an instance follow the default retention.
        cutoff = now - timedelta(days=_DEFAULT_RETENTION_DAYS)
        failed_cutoff = now - timedelta(days=_FAILED_JOB_RETENTION_DAYS)
        totals["jobs"] += self._ops_delete_batched("magento.job", [
            ("instance_id", "=", False),
            "|",
            "&", ("state", "in", ("done", "cancelled")), ("date_done", "<", cutoff),
            "&", ("state", "=", "failed"), ("date_done", "<", failed_cutoff),
        ])
        if any(totals.values()):
            _logger.info("Magento log cleanup: deleted %(jobs)s jobs, %(issues)s issues, "
                         "%(reports)s sync reports", totals)
        return totals
