from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.models.magento_job import (
    backoff_delay,
    classify_exception,
)

from .common import MagentoTestCommon, make_response

JOB_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_job"


def http_error(status):
    return requests.exceptions.HTTPError("HTTP %s" % status, response=make_response(status))


@tagged("post_install", "-at_install", "magento")
class TestMagentoJobQueue(MagentoTestCommon):
    """1.1: magento.job enqueue, runner, retries and channels."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.side_effect = None
        test = self

        def _probe(records, *args, **kwargs):
            test.calls.append({
                "ids": records.ids, "args": args, "kwargs": kwargs,
                "uid": records.env.uid, "company": records.env.company,
                "job_id": records.env.context.get("magento_job_id"),
                "cron_ctx": records.env.context.get("ir_cron_progress_id"),
            })
            if test.side_effect:
                raise test.side_effect
            return {"ok": True}

        instance_cls = type(self.env["magento.instance"])
        self.startPatcher(patch.object(instance_cls, "_mg_test_job", _probe, create=True))
        self.Job = self.env["magento.job"]

    def run_jobs(self, limit=50):
        with mute_logger(JOB_LOGGER):
            return self.Job._cron_run_jobs(limit=limit)

    def enqueue(self, **kw):
        args = kw.pop("args", ("a", 1))
        return self.Job.enqueue(self.instance, "_mg_test_job", args=args, **kw)

    # ------------------------------------------------------------------
    def test_enqueue_fields_and_dedup_by_identity_key(self):
        job = self.enqueue(identity_key="sync-orders-1", kwargs={"full": True}, priority=3,
                           description="Sync orders")
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.instance_id, self.instance)
        self.assertEqual(job.company_id, self.instance.company_id)
        self.assertEqual(job.model_name, "magento.instance")
        self.assertEqual(job.res_ids, [self.instance.id])
        self.assertEqual(job.args, ["a", 1])
        self.assertEqual(job.kwargs, {"full": True})
        self.assertEqual(job.user_id, self.env.user)
        self.assertEqual(job.name, "Sync orders")

        again = self.enqueue(identity_key="sync-orders-1")
        self.assertEqual(again, job, "Same identity key must return the pending job")
        self.assertEqual(self.Job.search_count([("identity_key", "=", "sync-orders-1")]), 1)

        # Once done, the key is free again.
        self.run_jobs()
        self.assertEqual(job.state, "done")
        fresh = self.enqueue(identity_key="sync-orders-1")
        self.assertNotEqual(fresh, job)
        self.assertEqual(fresh.state, "pending")

    def test_identity_key_unique_index_on_active_jobs(self):
        job = self.enqueue(identity_key="k1")
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.Job.sudo().create({
                "name": "dup", "model_name": "magento.instance", "method_name": "_mg_test_job",
                "identity_key": "k1",
            }).flush_recordset()
        job.sudo().write({"state": "cancelled"})
        job.flush_recordset()
        dup =self.Job.sudo().create({
            "name": "dup", "model_name": "magento.instance", "method_name": "_mg_test_job",
            "identity_key": "k1",
        })
        dup.flush_recordset()
        self.assertTrue(dup)

    def test_enqueue_rejects_unknown_method_and_non_json_args(self):
        with self.assertRaises(ValueError):
            self.Job.enqueue(self.instance, "no_such_method")
        with self.assertRaises(ValueError):
            self.Job.enqueue(self.instance, "_mg_test_job", args=(self.instance,))

    def test_runner_executes_job_as_its_user(self):
        user = self.create_user("mg_queue_user", "sdlc_magento_connector.group_magento_user")
        job = self.Job.with_user(user).enqueue(
            self.instance.with_user(user), "_mg_test_job", args=("x",), kwargs={"y": 2}
        )
        self.assertEqual(job.sudo().user_id, user)
        self.assertEqual(self.run_jobs(), 1)
        job = job.sudo()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.attempts, 1)
        self.assertTrue(job.date_started and job.date_done)
        self.assertIn("ok", job.result)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["ids"], [self.instance.id])
        self.assertEqual(call["args"], ("x",))
        self.assertEqual(call["kwargs"], {"y": 2})
        self.assertEqual(call["uid"], user.id)
        self.assertEqual(call["company"], self.instance.company_id)
        self.assertEqual(call["job_id"], job.id)
        self.assertFalse(call["cron_ctx"])

    def test_priority_order(self):
        low = self.enqueue(priority=20, channel="a")
        high = self.enqueue(priority=1, channel="b")
        self.run_jobs(limit=1)
        self.assertEqual(high.state, "done")
        self.assertEqual(low.state, "pending")

    def test_future_eta_not_run(self):
        job = self.enqueue(eta=fields.Datetime.now() + timedelta(hours=1))
        self.assertEqual(self.run_jobs(), 0)
        self.assertEqual(job.state, "pending")

    def test_transient_error_backoff_then_fail(self):
        self.side_effect = requests.exceptions.ConnectionError("Magento is down")
        job = self.enqueue(max_attempts=3)
        for attempt in (1, 2):
            before = fields.Datetime.now()
            self.run_jobs()
            self.assertEqual(job.state, "pending", "Transient errors are retried")
            self.assertEqual(job.attempts, attempt)
            self.assertEqual(job.error_class, "transient")
            self.assertIn("Magento is down", job.last_error)
            expected = timedelta(seconds=backoff_delay(attempt))
            self.assertEqual(expected, timedelta(seconds=min(2 ** attempt * 30, 3600)))
            self.assertGreaterEqual(job.eta, before + expected - timedelta(seconds=2))
            self.assertLessEqual(job.eta, fields.Datetime.now() + expected + timedelta(seconds=2))
            # Not due yet: the runner leaves it alone.
            self.assertEqual(self.run_jobs(), 0)
            job.eta = fields.Datetime.now() - timedelta(seconds=1)
        self.run_jobs()
        self.assertEqual(job.state, "failed", "Fails after max_attempts")
        self.assertEqual(job.attempts, 3)
        self.assertTrue(job.date_done)
        self.assertIn("ConnectionError", job.exc_info)
        self.assertEqual(backoff_delay(10), 3600, "Backoff is capped at one hour")

    def test_http_429_and_5xx_are_transient(self):
        for status in (429, 500, 503):
            self.side_effect = http_error(status)
            job = self.enqueue()
            self.run_jobs()
            self.assertEqual((job.state, job.error_class), ("pending", "transient"), status)
            job.sudo().write({"state": "cancelled"})

    def test_permanent_error_fails_immediately(self):
        for exc in (UserError("Bad SKU"), ValidationError("Invalid"), http_error(404),
                    ValueError("boom")):
            self.side_effect = exc
            job = self.enqueue()
            self.run_jobs()
            self.assertEqual(job.state, "failed", exc)
            self.assertEqual(job.attempts, 1)
            self.assertEqual(job.error_class, "permanent")

    def test_chained_transient_cause_is_retried(self):
        """A UserError chained from a timeout (ambiguous document push) is transient."""
        try:
            try:
                raise requests.exceptions.ReadTimeout("read timed out")
            except requests.exceptions.ReadTimeout as inner:
                raise UserError("Shipment push failed and was not found on re-check") from inner
        except UserError as exc:
            self.side_effect = exc
        job = self.enqueue()
        self.run_jobs()
        self.assertEqual((job.state, job.error_class), ("pending", "transient"))
        self.assertEqual(job.attempts, 1)

    def test_classify_exception_walks_cause_and_context(self):
        def chained(outer, inner, explicit=True):
            try:
                try:
                    raise inner
                except Exception as err:
                    if explicit:
                        raise outer from err
                    raise outer  # noqa: B904 - implicit __context__ on purpose
            except Exception as exc:  # noqa: BLE001
                return exc

        self.assertEqual(classify_exception(UserError("x"))[0], "permanent")
        self.assertEqual(classify_exception(chained(UserError("x"), http_error(503)))[0], "transient")
        self.assertEqual(
            classify_exception(chained(UserError("x"), requests.exceptions.ConnectionError(), False))[0],
            "transient", "__context__ is walked too")
        self.assertEqual(
            classify_exception(chained(UserError("x"), chained(ValueError("y"), http_error(429))))[0],
            "transient", "any transient link in a longer chain")
        self.assertEqual(classify_exception(chained(UserError("x"), http_error(400)))[0], "permanent")
        self.assertEqual(classify_exception(http_error(401)), ("permanent", True, 401))
        self.assertEqual(classify_exception(chained(UserError("x"), http_error(403)))[:2],
                         ("permanent", True))
        self.assertEqual(classify_exception(requests.exceptions.SSLError("cert"))[0], "permanent")
        self.assertEqual(classify_exception("Magento HTTP 401: denied")[:2], ("permanent", True))
        self.assertEqual(classify_exception("plain text")[:2], ("permanent", False))

    def test_channel_concurrency(self):
        busy = self.enqueue(channel="orders")
        busy.sudo().write({"state": "started", "date_started": fields.Datetime.now()})
        blocked = self.enqueue(channel="orders")
        other = self.enqueue(channel="root")
        self.run_jobs()
        self.assertEqual(other.state, "done")
        self.assertEqual(blocked.state, "pending", "One started job per channel at a time")
        busy.sudo().write({"state": "done"})
        self.run_jobs()
        self.assertEqual(blocked.state, "done")

    def test_stale_started_job_is_requeued(self):
        job = self.enqueue()
        job.sudo().write({"state": "started", "attempts": 1,
                          "date_started": fields.Datetime.now() - timedelta(hours=2)})
        self.run_jobs()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.attempts, 2)

    def test_actions_retry_cancel_run_now(self):
        self.side_effect = UserError("nope")
        job = self.enqueue()
        self.run_jobs()
        self.assertEqual(job.state, "failed")
        self.side_effect = None
        job.action_retry()
        self.assertEqual((job.state, job.attempts), ("pending", 0))
        job.action_cancel()
        self.assertEqual(job.state, "cancelled")
        job.action_retry()
        job.action_run_now()
        self.assertEqual(job.state, "done")

        user = self.create_user("mg_queue_user2", "sdlc_magento_connector.group_magento_user")
        pending = self.enqueue()
        with self.assertRaises(AccessError):
            pending.with_user(user).action_run_now()
        with self.assertRaises(AccessError):
            pending.with_user(user).write({"priority": 1})

        action = pending.action_open_record()
        self.assertEqual((action["res_model"], action["res_id"]), ("magento.instance", self.instance.id))

    def test_call_is_immutable_for_non_superuser(self):
        manager = self.create_user("mg_queue_mgr", "sdlc_magento_connector.group_magento_manager")
        job = self.enqueue()
        with self.assertRaises(AccessError):
            job.with_user(manager).write({"method_name": "unlink"})
        created = self.Job.with_user(manager).create({
            "name": "x", "model_name": "magento.instance", "method_name": "_mg_test_job",
            "user_id": self.env.ref("base.user_admin").id,
        })
        self.assertEqual(created.sudo().user_id, manager, "Run-as user is always the creator")

    def test_auth_error_job_flags_instance(self):
        self.side_effect = http_error(401)
        job = self.enqueue()
        self.run_jobs()
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.instance.state, "error")
