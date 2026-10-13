"""Magento Console, Phase 1 additions: Error Center and Job Queue figures."""
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from .common import MagentoTestCommon

USER_GROUP = "sdlc_magento_connector.group_magento_user"
ERROR_CENTER_ACTION = "sdlc_magento_connector.action_magento_sync_issue"
JOB_QUEUE_ACTION = "sdlc_magento_connector.action_magento_job"
ISSUE_ACCESS_USER = "sdlc_magento_connector.access_magento_sync_issue_user"


@tagged("post_install", "-at_install", "magento")
class TestDashboardPhase1(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "Magento Company B (P1)"})
        cls.instance.write({"state": "connected"})
        cls.instance_b = cls.create_instance("Store B", company=cls.company_b, state="connected")

        def issue(instance, entity, ref, severity="permanent", state="open", auth=False):
            return cls.env["magento.sync.issue"].create({
                "instance_id": instance.id,
                "entity": entity,
                "magento_ref": ref,
                "severity": severity,
                "state": state,
                "is_auth_error": auth,
                "message": "HTTP 401 Unauthorized" if auth else "Sync failed",
            })

        cls.auth_issue = issue(cls.instance, "other", "auth", auth=True)
        cls.perm_product_1 = issue(cls.instance, "product", "SKU-1")
        cls.perm_product_2 = issue(cls.instance, "product", "SKU-2")
        cls.perm_order = issue(cls.instance, "order", "000000101")
        cls.trans_product = issue(cls.instance, "product", "SKU-3", severity="transient")
        cls.trans_customer = issue(cls.instance, "customer", "42", severity="transient")
        cls.permanent = cls.auth_issue | cls.perm_product_1 | cls.perm_product_2 | cls.perm_order
        cls.transient = cls.trans_product | cls.trans_customer
        cls.open_issues = cls.permanent | cls.transient
        # Closed issues and another company's issue must never be counted.
        issue(cls.instance, "product", "SKU-4", state="resolved")
        issue(cls.instance, "order", "000000102", state="ignored")
        cls.issue_b = issue(cls.instance_b, "product", "SKU-B")

        def job(instance, state, **extra):
            vals = {
                "name": "Test job %s" % state,
                "model_name": "magento.instance",
                "method_name": "action_test_connection",
                "instance_id": instance.id if instance else False,
                "state": state,
            }
            vals.update(extra)
            return cls.env["magento.job"].create(vals)

        now = fields.Datetime.now()
        cls.job_pending = job(cls.instance, "pending")
        cls.job_started = job(cls.instance, "started", date_started=now)
        cls.job_failed = job(cls.instance, "failed", date_done=now, last_error="boom")
        job(cls.instance, "done", date_done=now)
        job(cls.instance, "failed", date_done=now - timedelta(days=200))  # outside every period
        job(cls.instance_b, "pending")
        job(cls.instance_b, "failed", date_done=now)

        cls.user = cls.create_user("mg_p1_console", USER_GROUP, company=cls.company_a)

    def _data(self, user=None, **kwargs):
        return self.env["magento.dashboard"].with_user(user or self.user).get_dashboard_data(**kwargs)

    @staticmethod
    def _kpi(data, key):
        return {k["key"]: k for k in data["kpis"]}.get(key)

    @staticmethod
    def _item(data, key):
        return {a["key"]: a for a in data["attention"]}.get(key)

    def _search(self, drill, user=None):
        return self.env[drill["res_model"]].with_user(user or self.user).search(drill["domain"])

    # ------------------------------------------------------------------
    def test_open_issues_kpi_value_and_tone(self):
        kpi = self._kpi(self._data(), "open_issues")
        self.assertTrue(kpi, "Open issues replaces Failed syncs on the rail")
        self.assertIsNone(self._kpi(self._data(), "failed_syncs"))
        self.assertEqual(kpi["value"], len(self.open_issues))
        self.assertEqual(kpi["tone"], "crit", "Permanent issues make the tile critical")
        self.assertEqual(kpi["drill"]["xml_id"], ERROR_CENTER_ACTION)

        self.permanent.write({"state": "resolved"})
        kpi = self._kpi(self._data(), "open_issues")
        self.assertEqual(kpi["value"], len(self.transient))
        self.assertEqual(kpi["tone"], "warn", "Only transient issues: a warning")

        self.transient.write({"state": "ignored"})
        kpi = self._kpi(self._data(), "open_issues")
        self.assertEqual(kpi["value"], 0)
        self.assertEqual(kpi["tone"], "")

    def test_auth_failing_item(self):
        self.instance.write({"state": "error", "last_connection_message": "Authentication failed: HTTP 401"})
        data = self._data()
        item = self._item(data, "auth_failing_%s" % self.instance.id)
        self.assertTrue(item, "An open auth issue raises an Authentication failing item")
        self.assertEqual(item["tone"], "crit")
        self.assertIn(self.instance.name, item["label"])
        self.assertEqual(item["count"], 1)
        self.assertEqual(self._search(item["drill"]), self.auth_issue)
        self.assertIsNone(self._item(data, "instance_error_%s" % self.instance.id),
                          "The auth item replaces the generic connection error of the same instance")
        self.assertEqual(data["attention"][0]["key"], item["key"], "Authentication comes first")

        self.auth_issue.write({"state": "resolved"})
        data = self._data()
        self.assertIsNone(self._item(data, "auth_failing_%s" % self.instance.id))
        self.assertTrue(self._item(data, "instance_error_%s" % self.instance.id))

    def test_permanent_and_failed_job_items(self):
        data = self._data()
        perm = self._item(data, "permanent_issues")
        self.assertEqual(perm["tone"], "crit")
        self.assertEqual(perm["count"], len(self.permanent))
        self.assertIn("Product 2", perm["detail"], "Counted by entity")
        self.assertIn("Order 1", perm["detail"])
        self.assertEqual(self._search(perm["drill"]), self.permanent)

        failed = self._item(data, "failed_jobs")
        self.assertEqual(failed["tone"], "warn")
        self.assertEqual(self._search(failed["drill"]), self.job_failed)
        self.assertEqual(failed["count"], 1)
        self.assertEqual(failed["drill"]["xml_id"], JOB_QUEUE_ACTION)

    def test_entity_panel_counts(self):
        rows = {row["key"]: row for row in self._data()["by_entity"]}
        self.assertEqual(
            {key: (row["count"], row["permanent"], row["transient"], row["tone"]) for key, row in rows.items()},
            {
                "product": (3, 2, 1, "crit"),
                "order": (1, 1, 0, "crit"),
                "other": (1, 1, 0, "crit"),
                "customer": (1, 0, 1, "warn"),
            },
        )
        self.assertEqual(self._data()["by_entity"][0]["key"], "product", "Largest entity first")
        for key, row in rows.items():
            found = self._search(row["drill"])
            self.assertEqual(len(found), row["count"])
            self.assertEqual(set(found.mapped("entity")), {key})
            self.assertEqual(set(found.mapped("state")), {"open"})

    def test_drill_domains_return_the_counted_records(self):
        data = self._data(instance_ids=[self.instance.id])
        issues_kpi = self._kpi(data, "open_issues")
        self.assertEqual(self._search(issues_kpi["drill"]), self.open_issues)

        queue = self._kpi(data, "queue")
        self.assertEqual(queue["value"], 2, "Pending + started jobs of the instance")
        self.assertEqual(queue["tone"], "warn", "A job failed in the period")
        self.assertEqual(self._search(queue["drill"]), self.job_pending | self.job_started)
        self.assertEqual(queue["drill"]["xml_id"], JOB_QUEUE_ACTION)

        info = {row["id"]: row for row in data["instances"]}[self.instance.id]
        self.assertEqual(info["open_issues"], len(self.open_issues))
        self.assertEqual(self._search(info["issues_drill"]), self.open_issues)

        # Every issue/job figure on screen opens exactly the records it counts.
        for item in data["kpis"] + data["attention"] + data["by_entity"]:
            if item["drill"]["res_model"] in ("magento.sync.issue", "magento.job"):
                expected = item.get("value", item.get("count"))
                self.assertEqual(len(self._search(item["drill"])), expected, item["key"])

        # Unscoped, jobs without an instance count too; still exactly what the drill opens.
        unbound = self.env["magento.job"].create({
            "name": "Unbound", "model_name": "magento.instance",
            "method_name": "action_test_connection", "state": "pending",
        })
        queue_all = self._kpi(self._data(), "queue")
        found = self._search(queue_all["drill"])
        self.assertIn(unbound, found)
        self.assertEqual(queue_all["value"], len(found))
        self.assertNotIn(unbound, self._search(queue["drill"]))

    def test_company_isolation_of_issues(self):
        data = self._data()
        self.assertEqual(self._kpi(data, "open_issues")["value"], len(self.open_issues))
        self.assertNotIn(self.issue_b, self._search(self._kpi(data, "open_issues")["drill"]))
        self.assertEqual({r["key"]: r["count"] for r in data["by_entity"]}["product"], 3)
        self.assertNotIn(self.instance_b.id, {row["id"] for row in data["instances"]})
        # Naming the other company's instance must not widen the scope.
        data_b = self._data(instance_ids=[self.instance_b.id])
        self.assertEqual(self._kpi(data_b, "open_issues")["value"], len(self.open_issues))

        # A user of both companies sees company B only when it is open.
        both = self.create_user("mg_p1_both", USER_GROUP, company=self.company_a,
                                companies=self.company_a | self.company_b)
        console = self.env["magento.dashboard"].with_user(both)
        only_a = console.with_context(allowed_company_ids=[self.company_a.id]).get_dashboard_data()
        self.assertEqual(self._kpi(only_a, "open_issues")["value"], len(self.open_issues))
        a_and_b = console.with_context(
            allowed_company_ids=[self.company_a.id, self.company_b.id]).get_dashboard_data()
        kpi = self._kpi(a_and_b, "open_issues")
        self.assertEqual(kpi["value"], len(self.open_issues) + 1)
        found = self.env["magento.sync.issue"].with_user(both).with_context(
            allowed_company_ids=[self.company_a.id, self.company_b.id]).search(kpi["drill"]["domain"])
        self.assertIn(self.issue_b, found)

    def test_stale_sync_marker_warning(self):
        now = fields.Datetime.now()
        key = "stale_marker_%s" % self.instance.id
        self.instance.write({
            "auto_sync_enabled": True,
            "auto_sync_orders": True,
            "orders_sync_watermark": now - timedelta(hours=30),
        })
        item = self._item(self._data(), key)
        self.assertTrue(item, "Auto-sync on and the orders marker 30h old: stale")
        self.assertEqual(item["tone"], "warn")
        self.assertIn(self.instance.name, item["label"])
        self.assertEqual(item["drill"]["res_model"], "magento.instance")
        self.assertEqual(item["drill"]["res_id"], self.instance.id)

        self.instance.orders_sync_watermark = now - timedelta(hours=2)
        self.assertIsNone(self._item(self._data(), key), "A recent marker is fine")

        self.instance.write({"orders_sync_watermark": now - timedelta(hours=30), "auto_sync_enabled": False})
        self.assertIsNone(self._item(self._data(), key), "No auto-sync: the marker is not expected to move")

    def test_user_without_issue_rights_sees_no_issue_figures(self):
        self.env.ref(ISSUE_ACCESS_USER).perm_read = False
        self.assertFalse(self.env["magento.sync.issue"].with_user(self.user).has_access("read"))
        data = self._data()
        self.assertTrue(data["issues_hidden"])
        self.assertIsNone(self._kpi(data, "open_issues"))
        self.assertEqual(data["by_entity"], [])
        self.assertFalse([a for a in data["attention"] if a["drill"]["res_model"] == "magento.sync.issue"])
        info = {row["id"]: row for row in data["instances"]}[self.instance.id]
        self.assertFalse(info["open_issues"])
        self.assertFalse(info["issues_drill"])
        # The job figures do not depend on issue rights.
        self.assertTrue(self._kpi(data, "queue"))
        links = {link["xml_id"] for group in self.env["magento.dashboard"].with_user(self.user).get_nav_links()
                 for link in group["links"]}
        self.assertNotIn(ERROR_CENTER_ACTION, links)
        self.assertIn(JOB_QUEUE_ACTION, links)
