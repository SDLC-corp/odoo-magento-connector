import json
from unittest.mock import patch

import requests

from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.models import cron_compat
from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MagentoTestCommon, make_response

# Fetch methods a full sync starts with; whichever of them the cron calls,
# the MagentoAPI instance passed as ``self`` tells us which instance synced.
FETCH_METHODS = {
    "get_products": [],
    "get_orders": [],
    "get_customers": [],
    "get_categories": {},
    "get_attribute_sets": [],
}


@tagged("post_install", "-at_install", "magento")
class TestCron(MagentoTestCommon):
    """A3: crons really sync active instances."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance.write({"state": "connected"})
        cls.archived_instance = cls.create_instance("Archived Magento", state="connected")
        cls.archived_instance.write({"active": False})

    def setUp(self):
        super().setUp()
        # Cron runs commit progress per instance (cron_compat.commit_progress); a test
        # transaction must never commit.
        self.commit_progress = self.startPatcher(patch.object(
            cron_compat, "commit_progress", return_value=float("inf")))

    def _patch_fetches(self):
        mocks = {}
        for name, value in FETCH_METHODS.items():
            if hasattr(MagentoAPI, name):
                mocks[name] = self.patch_api(name, return_value=value)
        self.patch_order_side_calls()
        return mocks

    @staticmethod
    def _synced_instances(mocks):
        synced = set()
        for mock in mocks.values():
            for call in mock.call_args_list:
                if call.args:
                    synced.add(call.args[0].instance.id)
        return synced

    def test_a3_cron_sync_all_exists_and_syncs_active_instances(self):
        Instance = self.env["magento.instance"]
        self.assertTrue(hasattr(Instance, "_cron_sync_all"),
                        "magento.instance._cron_sync_all() must exist")
        mocks = self._patch_fetches()

        Instance._cron_sync_all()

        synced = self._synced_instances(mocks)
        self.assertIn(self.instance.id, synced, "The active instance must be synced by the cron")
        self.assertNotIn(self.archived_instance.id, synced, "Archived instances must be skipped")

    def test_a3_cron_sync_all_skips_instances_with_own_schedule(self):
        own = self.create_instance("Own Schedule", state="connected")
        own.write({"auto_sync_enabled": True})
        mocks = self._patch_fetches()
        self.env["magento.instance"]._cron_sync_all()
        synced = self._synced_instances(mocks)
        self.assertIn(self.instance.id, synced)
        self.assertNotIn(own.id, synced, "Instances with their own auto-sync cron are not synced twice")

    def test_a3_static_crons_are_not_noops(self):
        legacy = self.env["ir.cron"].with_context(active_test=False).search([
            ("model_id.model", "=", "magento.instance"),
            ("code", "in", ["model.sync_products()", "model.sync_orders()"]),
        ])
        self.assertFalse(legacy, "Legacy no-op crons (sync_* on an empty recordset) must be gone")
        crons = self.env["ir.cron"].with_context(active_test=False)
        for xmlid in ("sdlc_magento_connector.cron_magento_sync_all",
                      "sdlc_magento_connector.cron_magento_product_sync",
                      "sdlc_magento_connector.cron_magento_order_sync"):
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron:
                crons |= cron
        for cron in crons:
            mocks = self._patch_fetches()
            cron.ir_actions_server_id.sudo().run()
            self.assertIn(
                self.instance.id, self._synced_instances(mocks),
                "Static cron '%s' must sync the active instance (it is a no-op)" % cron.name,
            )


@tagged("post_install", "-at_install", "magento")
class TestMagentoApiComments(MagentoTestCommon):
    """A4: order status/comments go through POST /V1/orders/{id}/comments."""

    def test_a4_add_order_comment_posts_status_history(self):
        self.assertTrue(hasattr(MagentoAPI, "add_order_comment"),
                        "MagentoAPI.add_order_comment must exist")
        sent = []

        def _send(session, prepared, **kwargs):
            sent.append(prepared)
            return make_response(200, True, url=prepared.url)

        self.startPatcher(patch.object(requests.sessions.Session, "send", _send))

        api = MagentoAPI(self.instance)
        api.add_order_comment(42, comment="Shipped from Odoo", status="complete", notify=True)

        comment_calls = [p for p in sent if "/orders/42/comments" in p.url]
        self.assertTrue(comment_calls, "No request to /V1/orders/42/comments (got %s)"
                        % [p.url for p in sent])
        req = comment_calls[0]
        self.assertEqual(req.method, "POST")
        self.assertIn("/V1/orders/42/comments", req.url)
        body = json.loads(req.body)
        self.assertIn("statusHistory", body)
        history = body["statusHistory"]
        self.assertEqual(history.get("comment"), "Shipped from Odoo")
        self.assertEqual(history.get("status"), "complete")
        self.assertTrue(history.get("is_customer_notified"))
        self.assertFalse([p for p in sent if p.method == "PUT"],
                         "Status changes must not PUT the whole order")

    def test_a4_add_order_comment_defaults(self):
        sent = []

        def _send(session, prepared, **kwargs):
            sent.append(prepared)
            return make_response(200, True, url=prepared.url)

        self.startPatcher(patch.object(requests.sessions.Session, "send", _send))
        MagentoAPI(self.instance).add_order_comment(7)
        req = [p for p in sent if "/orders/7/comments" in p.url][0]
        history = json.loads(req.body)["statusHistory"]
        self.assertFalse(history.get("is_customer_notified"))

    def test_a4_make_product_saleable_for_cart_removed(self):
        self.assertFalse(hasattr(MagentoAPI, "make_product_saleable_for_cart"),
                         "make_product_saleable_for_cart rewrote Magento stock config; it must be gone")


@tagged("post_install", "-at_install", "magento")
class TestConnectionState(MagentoTestCommon):
    """A5: action_test_connection records the outcome in `state`."""

    def test_a5_success_sets_connected(self):
        self.patch_api("get_websites", return_value=[{"id": 1, "code": "base", "name": "Main Website"}])
        self.patch_api("get_store_configs", return_value=[{"id": 1, "code": "default"}])
        self.instance.action_test_connection()
        self.assertEqual(self.instance.state, "connected")

    def test_a5_failure_sets_error(self):
        self.instance.write({"state": "connected"})
        refused = requests.exceptions.ConnectionError("Connection refused")
        self.patch_api("get_websites", side_effect=refused)
        self.patch_api("get_store_configs", side_effect=refused)
        # Must not raise: a raised error would roll back the state write.
        self.instance.action_test_connection()
        self.assertEqual(self.instance.state, "error")
