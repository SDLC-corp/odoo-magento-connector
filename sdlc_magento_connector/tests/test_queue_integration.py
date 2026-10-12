"""1.1 wiring: sync buttons, scheduled syncs and automatic pushes go through magento.job."""
from datetime import timedelta

import requests

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import MagentoTestCommon, build_product, make_response

JOB_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_job"
PUSH_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_product"
QUEUE = {"magento_sync_queue": True}


@tagged("post_install", "-at_install", "magento")
class TestQueueIntegration(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.attribute_set = cls.env["magento.attribute.set"].with_context(**ctx).create({
            "name": "Default", "magento_id": "4", "instance_id": cls.instance.id,
        })
        cls.Job = cls.env["magento.job"]
        cls.Issue = cls.env["magento.sync.issue"]

    def setUp(self):
        super().setUp()
        self.product = self.create_product("Q-PUSH", "Queue Bag")
        self.product_map = self.create_map(self.product, 77, attribute_set_id=self.attribute_set.id)

    # ------------------------------------------------------------------ helpers
    def run_jobs(self):
        with mute_logger(JOB_LOGGER, PUSH_LOGGER):
            return self.Job._cron_run_jobs()

    def sync_job(self, entity, instance=None):
        instance = instance or self.instance
        return self.Job.search([("identity_key", "=", "sync-%s-%s" % (instance.id, entity))])

    def push_jobs(self, record):
        return self.Job.search([
            ("identity_key", "in", ("push-%s-%s" % (record._name, record.id),
                                    "push-%s-%s-next" % (record._name, record.id))),
        ])

    def patch_catalog(self, products):
        by_sku = {p["sku"]: p for p in products}
        state = {"called": 0}

        def _get_products(api, updated_from=None, **kwargs):
            state["called"] += 1
            return list(products)

        self.patch_api("get_products", side_effect=_get_products)
        self.patch_api("get_product", side_effect=lambda api, sku, *a, **kw: by_sku[sku])
        self.patch_api("get_product_media", return_value={})
        return state

    def patch_empty_listings(self):
        self.patch_api("get_categories", return_value={})
        for name in ("get_products", "get_orders", "get_customers"):
            self.patch_api(name, return_value=[])
        self.patch_order_side_calls()

    def maps(self, sku):
        return self.env["magento.product.map"].with_context(active_test=False).search([
            ("instance_id", "=", self.instance.id), ("sku", "=", sku)])

    # ------------------------------------------------------------------ syncs
    def test_action_sync_products_enqueues_once_and_runner_syncs(self):
        state = self.patch_catalog([build_product(901, "Q-SYNC-1", "Queued Product")])
        instance = self.instance.with_context(**QUEUE)
        Report = self.env["magento.sync.report"]
        reports_before = Report.search([("instance_id", "=", self.instance.id)])

        action = instance.action_sync_products()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn("Queued: products sync", action["params"]["message"])
        self.assertIn("Job Queue", action["params"]["message"])
        job = self.sync_job("products")
        self.assertEqual(len(job), 1)
        self.assertEqual(job.channel, "magento.%s.products" % self.instance.id)
        self.assertEqual(job.method_name, "_magento_run_sync_job")
        self.assertEqual(job.instance_id, self.instance)
        self.assertEqual(state["called"], 0, "Nothing is synced inside the request")
        self.assertFalse(self.maps("Q-SYNC-1"))
        self.assertGreaterEqual(self.instance.pending_job_count, 1)

        again = instance.action_sync_products()
        self.assertIn("Already queued", again["params"]["message"])
        self.assertEqual(self.sync_job("products"), job, "A double click does not duplicate the job")

        self.run_jobs()
        self.assertEqual(job.state, "done", job.last_error)
        self.assertEqual(state["called"], 1)
        self.assertIn("Product sync: 1 synced", job.result)
        self.assertEqual(len(self.maps("Q-SYNC-1")), 1)
        report = Report.search([("instance_id", "=", self.instance.id)]) - reports_before
        self.assertEqual(report.sync_type, "product")
        self.assertEqual(report.mode, "manual")
        self.assertEqual(report.products_created, 1)

    def test_sync_all_enqueues_one_job_per_entity(self):
        action = self.instance.with_context(**QUEUE).sync_all_and_report()
        self.assertIn("Queued:", action["params"]["message"])
        for entity in ("categories", "products", "orders", "customers"):
            job = self.sync_job(entity)
            self.assertEqual(len(job), 1, entity)
            self.assertEqual(job.channel, "magento.%s.%s" % (self.instance.id, entity))
            self.assertEqual(job.args, [entity])
        self.assertEqual(len({j.channel for j in self.Job.search(
            [("identity_key", "like", "sync-%s-%%" % self.instance.id)])}), 4)
        for name, entity in (("action_sync_categories", "categories"),
                             ("action_sync_customers", "customers"),
                             ("action_sync_orders", "orders")):
            getattr(self.instance.with_context(**QUEUE), name)()
            self.assertEqual(len(self.sync_job(entity)), 1, "%s reuses the waiting job" % name)
        self.instance.with_context(**QUEUE).action_sync_attribute_sets()
        self.assertEqual(self.sync_job("attribute_sets").channel,
                         "magento.%s.attribute_sets" % self.instance.id)
        self.assertFalse(self.http_attempts)

    def test_background_off_or_sync_now_runs_synchronously(self):
        state = self.patch_catalog([build_product(902, "Q-SYNC-2")])
        self.instance.run_syncs_in_background = False
        action = self.instance.with_context(**QUEUE).action_sync_products()
        self.assertIn("Product sync complete", action["params"]["message"])
        self.assertEqual(state["called"], 1)
        self.assertFalse(self.sync_job("products"))
        self.assertEqual(len(self.maps("Q-SYNC-2")), 1)

        self.instance.run_syncs_in_background = True
        action = self.instance.with_context(magento_sync_queue=True, magento_sync_now=True) \
            .action_sync_products()
        self.assertIn("Product sync complete", action["params"]["message"])
        self.assertEqual(state["called"], 2)
        self.assertFalse(self.sync_job("products"))

    def test_actions_are_synchronous_under_tests_by_default(self):
        state = self.patch_catalog([build_product(903, "Q-SYNC-3")])
        action = self.instance.action_sync_products()
        self.assertIn("Product sync complete", action["params"]["message"])
        self.assertEqual(state["called"], 1)
        self.assertFalse(self.sync_job("products"))

    def test_cron_sync_all_enqueues_scope_and_runner_reports_as_cron(self):
        self.patch_empty_listings()
        self.instance.auto_sync_customers = False
        self.env["magento.instance"].with_context(**QUEUE)._cron_sync_all()
        self.assertFalse(self.http_attempts)
        for entity in ("categories", "products", "orders"):
            job = self.sync_job(entity)
            self.assertEqual(len(job), 1, entity)
            self.assertEqual(job.kwargs, {"source_action": "cron_sync_all", "mode": "cron"})
        self.assertFalse(self.sync_job("customers"), "Out-of-scope entities are not queued")
        self.assertFalse(self.env["magento.sync.report"].search(
            [("instance_id", "=", self.instance.id)]), "Nothing ran inside the cron")

        self.env["magento.instance"].with_context(**QUEUE)._cron_sync_all()
        self.assertEqual(len(self.sync_job("products")), 1, "The next cron run reuses waiting jobs")

        self.run_jobs()
        reports = self.env["magento.sync.report"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(set(reports.mapped("sync_type")), {"category", "product", "order"})
        self.assertEqual(set(reports.mapped("mode")), {"cron"})
        self.assertEqual(set(reports.mapped("source_action")), {"cron_sync_all"})

    def test_per_instance_cron_enqueues(self):
        own = self.create_instance("Own Schedule Queue", state="connected")
        own.write({"auto_sync_enabled": True, "auto_sync_products": True,
                   "auto_sync_categories": False, "auto_sync_orders": False,
                   "auto_sync_customers": False})
        own.with_context(**QUEUE).cron_run_auto_sync()
        self.assertEqual(self.sync_job("products", own).kwargs["source_action"], "cron_auto_scope")
        self.assertFalse(self.sync_job("orders", own))
        self.assertFalse(self.http_attempts)

    # ------------------------------------------------------------------ pushes
    def test_product_edit_enqueues_one_push_job(self):
        update = self.patch_api("update_product", return_value={})
        self.product.write({"name": "Queue Bag v2"})
        job = self.push_jobs(self.product_map)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.identity_key, "push-magento.product.map-%s" % self.product_map.id)
        self.assertEqual(job.channel, "magento.%s.push" % self.instance.id)
        self.assertEqual(job.method_name, "_magento_auto_push")
        self.assertEqual(job.res_ids, [self.product_map.id])
        self.assertEqual(job.args, [[]])
        self.assertFalse(update.called, "No Magento call inside write()")

        self.product.write({"list_price": 70.0})
        self.product_map._magento_enqueue_push({"image"})
        self.assertEqual(self.push_jobs(self.product_map), job, "Edits are deduplicated")
        self.assertEqual(job.args, [["image"]], "Flags of later edits are merged")

        self.patch_api("get_product", return_value={})
        self.run_jobs()
        self.assertEqual(job.state, "done", job.last_error)
        self.assertEqual(update.call_count, 1)
        payload = update.call_args.args[2]
        self.assertEqual(payload["product"]["name"], "Queue Bag v2")
        self.assertEqual(payload["product"]["price"], 70.0)

    def test_edit_while_push_runs_queues_a_follow_up(self):
        self.product.write({"name": "Queue Bag v3"})
        job = self.push_jobs(self.product_map)
        job.sudo().write({"state": "started", "date_started": fields.Datetime.now()})
        self.product.write({"name": "Queue Bag v4"})
        follow_up = self.push_jobs(self.product_map) - job
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up.identity_key, job.identity_key + "-next")
        self.product.write({"name": "Queue Bag v5"})
        self.assertEqual(self.push_jobs(self.product_map), job | follow_up)

    def test_push_success_clears_error_and_resolves_issue(self):
        self.product_map._magento_set_push_error("old failure")
        issue = self.Issue._record_failure(
            self.instance, "product", "Q-PUSH", requests.exceptions.HTTPError(
                "HTTP 400", response=make_response(400)))
        self.patch_api("update_product", return_value={})
        self.product.write({"name": "Queue Bag ok"})
        self.run_jobs()
        self.assertEqual(self.push_jobs(self.product_map).state, "done")
        self.assertFalse(self.product_map.magento_last_error)
        self.assertEqual(issue.state, "resolved")

    def test_transient_push_failure_retries(self):
        update = self.patch_api(
            "update_product", side_effect=requests.exceptions.ConnectionError("Magento is down"))
        self.product.write({"name": "Queue Bag retry"})
        job = self.push_jobs(self.product_map)
        self.run_jobs()
        self.assertEqual(update.call_count, 1)
        self.assertEqual((job.state, job.error_class, job.attempts), ("pending", "transient", 1))
        self.assertGreater(job.eta, fields.Datetime.now())
        self.assertIn("Magento is down", self.product_map.magento_last_error)
        self.assertFalse(self.Issue.search([("instance_id", "=", self.instance.id),
                                            ("entity", "=", "product"),
                                            ("magento_ref", "=", "Q-PUSH")]),
                         "A push that will be retried does not open an issue yet")

        # Magento is back: the retry succeeds.
        update.side_effect = None
        update.return_value = {}
        job.eta = fields.Datetime.now() - timedelta(seconds=1)
        self.run_jobs()
        self.assertEqual((job.state, job.attempts), ("done", 2))
        self.assertFalse(self.product_map.magento_last_error)

    def test_transient_push_failure_out_of_retries_opens_issue(self):
        self.patch_api(
            "update_product", side_effect=requests.exceptions.ConnectionError("Magento is down"))
        self.product.write({"name": "Queue Bag retry"})
        job = self.push_jobs(self.product_map)
        job.sudo().max_attempts = 1
        self.run_jobs()
        self.assertEqual(job.state, "failed")
        issue = self.Issue.search([("trace_key", "=", "%s-product-Q-PUSH" % self.instance.id)])
        self.assertEqual((issue.state, issue.severity), ("open", "transient"))

    def test_permanent_push_failure_opens_issue(self):
        self.patch_api("update_product", side_effect=requests.exceptions.HTTPError(
            "400 Client Error", response=make_response(400, {"message": "Invalid attribute"})))
        self.product.write({"name": "Queue Bag bad"})
        job = self.push_jobs(self.product_map)
        self.run_jobs()
        self.assertEqual((job.state, job.error_class, job.attempts), ("failed", "permanent", 1))
        self.assertIn("Invalid attribute", self.product_map.magento_last_error)
        issue = self.Issue.search([("trace_key", "=", "%s-product-Q-PUSH" % self.instance.id),
                                   ("state", "=", "open")])
        self.assertEqual(len(issue), 1)
        self.assertEqual(issue.severity, "permanent")
        self.assertEqual(issue.job_id, job)
        self.assertEqual(issue.record_ref, self.product_map)
        self.assertGreaterEqual(self.instance.failed_job_count, 1)

    def test_retrying_push_issue_replays_the_push_not_a_pull(self):
        magento_fixed = []
        pushes = []

        def _update_product(api, *args, **kwargs):
            pushes.append(args)
            if not magento_fixed:
                raise requests.exceptions.HTTPError(
                    "400 Client Error", response=make_response(400, {"message": "Invalid attribute"}))
            return {}

        self.patch_api("update_product", side_effect=_update_product)
        resync = self.patch_api("get_product", side_effect=AssertionError("Retry must not pull"))
        self.product.write({"name": "Queue Bag fixed later"})
        failed = self.push_jobs(self.product_map)
        self.run_jobs()
        issue = self.Issue.search([("trace_key", "=", "%s-product-Q-PUSH" % self.instance.id),
                                   ("state", "=", "open")])
        self.assertEqual(issue.job_id, failed)

        # The cause is fixed in Magento; Retry must push the Odoo data again.
        magento_fixed.append(True)
        pushes.clear()
        issue.action_retry()
        retry_job = issue.job_id
        self.assertNotEqual(retry_job, failed)
        self.assertEqual(retry_job.method_name, "_magento_auto_push")
        self.run_jobs()
        self.assertEqual(retry_job.state, "done", retry_job.last_error)
        self.assertTrue(pushes, "Retry must push the Odoo data again")
        self.assertFalse(resync.called)
        self.assertEqual(issue.state, "resolved")
        self.assertFalse(self.product_map.magento_last_error)

    def test_skip_push_context_enqueues_nothing(self):
        self.product.with_context(magento_skip_push=True).write({"name": "Queue Bag skip"})
        self.product_map.with_context(magento_skip_push=True).write({"price": 65.0})
        self.product_map.with_context(skip_magento_sync=True).write({"price": 66.0})
        self.assertFalse(self.push_jobs(self.product_map))
        # Bookkeeping fields never trigger a push either.
        self.product_map._magento_set_push_error("x")
        self.product_map.write({"magento_image_entry_id": "5"})
        self.assertFalse(self.push_jobs(self.product_map))

    def test_rollback_leaves_no_job(self):
        with self.assertRaises(RuntimeError), self.env.cr.savepoint():
            self.product.write({"name": "Queue Bag rolled back"})
            self.assertTrue(self.push_jobs(self.product_map))
            self.env.flush_all()
            raise RuntimeError("abort the edit")
        self.env.invalidate_all()
        self.assertFalse(self.push_jobs(self.product_map), "The job is rolled back with the edit")
        self.assertEqual(self.product.name, "Queue Bag")

    def test_customer_and_category_pushes_are_queued(self):
        ctx = self.no_push_ctx()
        customer = self.env["magento.customer"].with_context(**ctx).create({
            "instance_id": self.instance.id, "magento_id": "601", "email": "q@example.com",
            "firstname": "Queue", "lastname": "Customer", "website_id": 1,
        }).with_env(self.env)
        category = self.env["magento.category"].with_context(**ctx).create({
            "instance_id": self.instance.id, "magento_id": "41", "name": "Bags",
        }).with_env(self.env)

        customer.write({"firstname": "Queued"})
        customer.write({"city": "Calder"})
        job = self.push_jobs(customer)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.args, [["address"]])
        self.assertEqual(job.channel, "magento.%s.push" % self.instance.id)

        category.write({"name": "Bags & Packs"})
        cat_job = self.push_jobs(category)
        self.assertEqual(len(cat_job), 1)
        self.assertFalse(self.http_attempts)

        self.patch_api("update_category", side_effect=requests.exceptions.HTTPError(
            "404 Not Found", response=make_response(404, {"message": "No such entity"})))
        self.patch_api("update_customer", return_value={"id": 601})
        self.run_jobs()
        self.assertEqual(job.state, "done", job.last_error)
        self.assertEqual(cat_job.state, "failed")
        self.assertTrue(category.magento_last_error)
        issue = self.Issue.search([("trace_key", "=", "%s-category-41" % self.instance.id)])
        self.assertEqual(issue.state, "open")

    def test_foreground_push_runs_after_commit(self):
        """Background off: the queued push runs right after the commit (fast path)."""
        update = self.patch_api("update_product", return_value={})
        self.instance.run_syncs_in_background = False
        self.product.write({"name": "Queue Bag now"})
        job = self.push_jobs(self.product_map)
        self.assertFalse(update.called)
        self.env.flush_all()
        with self.enter_registry_test_mode(), mute_logger(JOB_LOGGER, PUSH_LOGGER):
            self.env.cr.postcommit.run()
        job.invalidate_recordset()
        self.assertEqual(job.state, "done", job.last_error)
        self.assertEqual(update.call_count, 1)
