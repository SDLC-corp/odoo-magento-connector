import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import MagentoTestCommon, make_response

JOB_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_job"
ALERT_SUMMARY = "Magento sync alert"


def http_error(status):
    return requests.exceptions.HTTPError("HTTP %s" % status, response=make_response(status))


@tagged("post_install", "-at_install", "magento")
class TestMagentoErrorCenter(MagentoTestCommon):
    """1.2 error center and 1.8 bounded logs and alerts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Issue = cls.env["magento.sync.issue"]
        cls.alert_user = cls.create_user("mg_alert_mgr", "sdlc_magento_connector.group_magento_manager")
        cls.mg_user = cls.create_user("mg_ec_user", "sdlc_magento_connector.group_magento_user")

    def setUp(self):
        super().setUp()
        self.resync_calls = []
        test = self

        def _resync(instance, entity, magento_ref):
            test.resync_calls.append((instance.id, entity, magento_ref))
            return True

        self.startPatcher(patch.object(
            type(self.env["magento.instance"]), "_magento_resync_one", _resync, create=True
        ))

    def alerts(self, instance=None):
        return self.env["mail.activity"].search([
            ("res_model", "=", "magento.instance"),
            ("res_id", "=", (instance or self.instance).id),
            ("summary", "=", ALERT_SUMMARY),
        ])

    # ------------------------------------------------------------------
    def test_record_failure_upserts_and_counts(self):
        issue = self.Issue._record_failure(
            self.instance, "order", "000000042", requests.exceptions.ConnectionError("reset"),
            payload={"entity_id": 42, "increment_id": "000000042"},
        )
        self.assertEqual(issue.state, "open")
        self.assertEqual(issue.severity, "transient")
        self.assertEqual(issue.occurrences, 1)
        self.assertEqual(issue.trace_key, "%s-order-000000042" % self.instance.id)
        self.assertEqual(json.loads(issue.payload)["entity_id"], 42)
        self.assertIn('"entity_id": 42', issue.payload_pretty)
        self.assertIn("\n", issue.payload_pretty, "payload is pretty-printed")

        again = self.Issue._record_failure(self.instance, "order", "000000042", UserError("Unknown SKU 24-MB01"))
        self.assertEqual(again, issue, "Same trace key upserts the open issue")
        self.assertEqual(issue.occurrences, 2)
        self.assertEqual(issue.severity, "permanent")
        self.assertIn("Unknown SKU", issue.message)
        self.assertEqual(json.loads(issue.payload)["entity_id"], 42, "payload kept when not given")
        self.assertTrue(issue.first_seen <= issue.last_seen)

        other = self.Issue._record_failure(self.instance, "order", "000000043", "plain message")
        self.assertNotEqual(other, issue)
        second_instance = self.create_instance("Second Store")
        elsewhere = self.Issue._record_failure(second_instance, "order", "000000042", "x")
        self.assertNotEqual(elsewhere, issue, "Trace key is per instance")
        self.assertEqual(self.Issue.search_count([("trace_key", "=", issue.trace_key)]), 1)

    def test_record_success_auto_resolves(self):
        issue = self.Issue._record_failure(self.instance, "product", "24-MB01", UserError("bad"))
        self.assertFalse(self.Issue._record_success(self.instance, "product", "OTHER"))
        resolved = self.Issue._record_success(self.instance, "product", "24-MB01")
        self.assertEqual(resolved, issue)
        self.assertEqual(issue.state, "resolved")
        self.assertFalse(issue.resolved_by, "auto-resolved")
        self.assertTrue(issue.resolved_date)
        # A new failure opens a new issue; the resolved one stays as history.
        new = self.Issue._record_failure(self.instance, "product", "24-MB01", UserError("bad again"))
        self.assertNotEqual(new, issue)
        self.assertEqual(new.occurrences, 1)

    def test_first_failure_posts_on_record_chatter(self):
        partner = self.env["res.partner"].create({"name": "Veronica Costello"})
        before = len(partner.message_ids)
        issue = self.Issue._record_failure(self.instance, "customer", "1", UserError("bad email"),
                                           record=partner)
        self.assertEqual(len(partner.message_ids), before + 1)
        self.assertEqual(issue.record_ref, partner)
        self.Issue._record_failure(self.instance, "customer", "1", UserError("bad email"), record=partner)
        self.assertEqual(len(partner.message_ids), before + 1, "Only the first failure is posted")
        action = issue.action_open_record()
        self.assertEqual((action["res_model"], action["res_id"]), ("res.partner", partner.id))

    def test_bulk_retry_enqueues_deduplicated_jobs(self):
        issues = (self.Issue._record_failure(self.instance, "order", "10", UserError("a"))
                  | self.Issue._record_failure(self.instance, "product", "24-MB01", UserError("b")))
        issues.with_user(self.mg_user).action_retry()
        jobs = issues.job_id
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            set(jobs.mapped("identity_key")), {"retry-%s" % key for key in issues.mapped("trace_key")}
        )
        self.assertTrue(all(j.method_name == "_magento_resync_one" for j in jobs))
        self.assertEqual(jobs.mapped("user_id"), self.mg_user)

        issues.action_retry()
        self.assertEqual(issues.job_id, jobs, "Retrying again reuses the pending jobs")
        self.assertEqual(self.env["magento.job"].search_count([("identity_key", "like", "retry-%")]), 2)

        with mute_logger(JOB_LOGGER):
            self.env["magento.job"]._cron_run_jobs()
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(
            sorted(self.resync_calls),
            sorted([(self.instance.id, "order", "10"), (self.instance.id, "product", "24-MB01")]),
        )

    def test_bulk_resolve_and_ignore(self):
        a = self.Issue._record_failure(self.instance, "order", "1", UserError("a"))
        b = self.Issue._record_failure(self.instance, "order", "2", UserError("b"))
        c = self.Issue._record_failure(self.instance, "order", "3", UserError("c"))
        (a | b).with_user(self.mg_user).action_resolve()
        c.with_user(self.mg_user).action_ignore()
        self.assertEqual((a.state, b.state, c.state), ("resolved", "resolved", "ignored"))
        self.assertEqual(a.resolved_by, self.mg_user)
        c.action_reopen()
        self.assertEqual(c.state, "open")

    def test_cleanup_deletes_old_logs_keeps_open_issues(self):
        Job = self.env["magento.job"]
        old = fields.Datetime.now() - timedelta(days=40)
        self.instance.log_retention_days = 30
        instance_cls = type(self.env["magento.instance"])
        with patch.object(instance_cls, "_mg_noop", lambda recs: True, create=True):
            old_done = Job.enqueue(self.instance, "_mg_noop")
            recent_done = Job.enqueue(self.instance, "_mg_noop")
            old_failed = Job.enqueue(self.instance, "_mg_noop")
            very_old_failed = Job.enqueue(self.instance, "_mg_noop")
        old_done.write({"state": "done", "date_done": old})
        recent_done.write({"state": "done", "date_done": fields.Datetime.now()})
        old_failed.write({"state": "failed", "date_done": old})
        very_old_failed.write({"state": "failed", "date_done": fields.Datetime.now() - timedelta(days=100)})

        open_issue = self.Issue._record_failure(self.instance, "order", "1", "x")
        open_issue.write({"first_seen": old, "last_seen": old})
        resolved_issue = self.Issue._record_failure(self.instance, "order", "2", "x")
        resolved_issue.write({"state": "resolved", "resolved_date": old, "last_seen": old})
        recent_resolved = self.Issue._record_failure(self.instance, "order", "3", "x")
        recent_resolved.action_resolve()

        Report = self.env["magento.sync.report"]
        old_report = Report.create({"instance_id": self.instance.id, "synced_at": old})
        new_report = Report.create({"instance_id": self.instance.id})

        totals = self.env["magento.instance"]._cron_cleanup_logs()
        self.assertFalse(old_done.exists())
        self.assertFalse(very_old_failed.exists())
        self.assertFalse(resolved_issue.exists())
        self.assertFalse(old_report.exists())
        self.assertTrue(recent_done.exists())
        self.assertTrue(old_failed.exists(), "failed jobs are kept 90 days")
        self.assertTrue(open_issue.exists(), "open issues are never deleted")
        self.assertTrue(recent_resolved.exists())
        self.assertTrue(new_report.exists())
        self.assertGreaterEqual(totals["jobs"], 2)

    def test_auth_error_flags_instance_and_alerts_once(self):
        self.instance.write({"alert_user_ids": [(6, 0, self.alert_user.ids)], "state": "connected"})
        issue = self.Issue._record_failure(self.instance, "order", "7", http_error(401))
        self.assertTrue(issue.is_auth_error)
        self.assertEqual(issue.severity, "permanent")
        self.assertEqual(self.instance.state, "error")
        self.assertIn("Authentication failed", self.instance.last_connection_message)
        self.Issue._record_failure(self.instance, "order", "8", http_error(403))
        self.Issue._record_failure(self.instance, "product", "24-MB01", http_error(401))
        activities = self.alerts()
        self.assertEqual(len(activities), 1, "At most one open alert activity per instance and user")
        self.assertEqual(activities.user_id, self.alert_user)
        self.assertEqual(activities.activity_type_id, self.env.ref("mail.mail_activity_data_todo"))
        self.assertTrue(self.instance.last_alert_at)
        self.assertEqual(self.instance.open_issue_count, 3)

        # Once the activity is done, the next alert schedules a new one.
        activities.action_feedback(feedback="Token renewed")
        self.Issue._record_failure(self.instance, "order", "9", http_error(401))
        self.assertEqual(len(self.alerts()), 1)

    def test_permanent_alert_first_time_only(self):
        self.instance.write({"alert_user_ids": [(6, 0, self.alert_user.ids)]})
        transient = self.Issue._record_failure(self.instance, "order", "1",
                                               requests.exceptions.Timeout("slow"))
        self.assertEqual(transient.severity, "transient")
        self.assertFalse(self.alerts(), "Transient failures do not alert")
        messages_before = len(self.instance.message_ids)
        self.Issue._record_failure(self.instance, "order", "1", UserError("Invalid order"))
        self.assertEqual(len(self.alerts()), 1, "Becoming permanent alerts")
        self.assertEqual(len(self.instance.message_ids), messages_before + 1)
        self.assertEqual(self.instance.state, "draft", "Only auth errors change the connection state")

        other = self.create_instance("Quiet Store", alert_on_permanent=False,
                                     alert_user_ids=[(6, 0, self.alert_user.ids)])
        self.Issue._record_failure(other, "order", "1", UserError("Invalid order"))
        self.assertFalse(self.alerts(other), "alert_on_permanent off")

    def test_magento_user_access(self):
        issue = self.Issue._record_failure(self.instance, "order", "1", UserError("x"))
        as_user = issue.with_user(self.mg_user)
        self.assertEqual(as_user.message, "x")
        as_user.action_resolve()
        self.assertEqual(issue.state, "resolved")
        self.assertTrue(self.env["magento.job"].with_user(self.mg_user).search_count([]) >= 0)

    def test_issue_job_context_links_job(self):
        instance_cls = type(self.env["magento.instance"])
        Issue = self.Issue

        def _failing(instance):
            Issue.with_env(instance.env)._record_failure(instance, "stock", "SKU-1", "stock push failed")
            return True

        with patch.object(instance_cls, "_mg_fail_one", _failing, create=True):
            job = self.env["magento.job"].enqueue(self.instance, "_mg_fail_one")
            with mute_logger(JOB_LOGGER):
                self.env["magento.job"]._cron_run_jobs()
        issue = self.Issue.search([("trace_key", "=", "%s-stock-SKU-1" % self.instance.id)])
        self.assertEqual(issue.job_id, job)
