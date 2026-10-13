"""Phase 2.10: the "Connect a Magento store" setup wizard (HTTP mocked)."""
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.wizard import magento_setup_wizard as wizard_module
from odoo.addons.sdlc_magento_connector.wizard.magento_setup_wizard import normalize_store_url

from .common import MagentoTestCommon, make_response

USER_GROUP = "sdlc_magento_connector.group_magento_user"
MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
WIZARD_ACTION = "sdlc_magento_connector.action_magento_setup_wizard"
SHOP = "https://shop.example.test"

OAUTH = {
    "consumer_key": "ck-1",
    "consumer_secret": "cs-2",
    "access_token": "at-3",
    "access_token_secret": "ats-4",
}


def _error(status, message, parameters=None):
    payload = {"message": message}
    if parameters is not None:
        payload["parameters"] = parameters
    return make_response(status, payload)


class _Http:
    """Session.send replacement answering from ``responder(path, prepared, kwargs)``."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def __call__(self, session, prepared, **kwargs):
        self.requests.append((prepared, kwargs))
        response = self.responder(urlsplit(prepared.url).path, prepared, kwargs)
        if isinstance(response, Exception):
            raise response
        response.request = prepared
        response.url = prepared.url
        return response

    def paths(self):
        return [urlsplit(prepared.url).path for prepared, _kwargs in self.requests]


def _magento_ok(path, prepared, kwargs):
    """A Magento that grants every resource (MSI not installed)."""
    if path.endswith("/inventory/sources"):
        return _error(404, "Request does not match any route.")
    if path.endswith("/store/storeConfigs") and "Authorization" not in prepared.headers:
        return _error(401, "The consumer isn't authorized to access %resources.",
                      {"resources": "Magento_Backend::store"})
    return make_response(200, {"items": [], "total_count": 0})


@tagged("post_install", "-at_install", "magento")
class TestSetupWizard(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = cls.create_user("mg_wizard_manager", MANAGER_GROUP)
        cls.magento_user = cls.create_user("mg_wizard_user", USER_GROUP)
        cls.Wizard = cls.env["magento.setup.wizard"]

    def _http(self, responder):
        http = _Http(responder)
        self.startPatcher(patch.object(
            requests.sessions.Session, "send",
            lambda session, prepared, **kwargs: http(session, prepared, **kwargs)))
        return http

    def _wizard(self, **vals):
        return self.Wizard.create(dict({"url_input": SHOP}, **vals))

    def _connected_wizard(self, **vals):
        """Wizard resumed on ``self.instance`` (state connected)."""
        self.instance.sudo().write({"state": "connected", "last_connection_message": "ok"})
        defaults = self.Wizard.with_context(default_instance_id=self.instance.id).default_get(
            list(self.Wizard._fields))
        return self.Wizard.create(dict(defaults, **vals))

    # ------------------------------------------------------------------
    # 1. Store URL
    # ------------------------------------------------------------------
    def test_url_normalization_variants(self):
        cases = {
            "shop.com": "https://shop.com",
            "http://shop.com/admin": "http://shop.com",
            "https://shop.com/rest/V1/": "https://shop.com",
            "https://shop.com/index.php": "https://shop.com",
            "  HTTPS://Shop.COM/  ": "https://shop.com",
            "https://shop.com/magento/index.php/admin/dashboard/": "https://shop.com/magento",
            "https://shop.com/rest/default/V1/products?searchCriteria=": "https://shop.com",
            "shop.com:8443/admin_x1y2/": "https://shop.com:8443",
        }
        for value, expected in cases.items():
            self.assertEqual(normalize_store_url(value), expected, value)
        for bad in ("", "   ", "ftp://shop.com", "https://"):
            with self.assertRaises(UserError, msg=bad):
                normalize_store_url(bad)

    def test_url_reachable(self):
        http = self._http(lambda path, prepared, kwargs: _error(
            401, "The consumer isn't authorized to access %resources.", {"resources": "Magento_Backend::store"}))
        wizard = self._wizard(url_input="http://shop.example.test/admin/")
        wizard.action_check_url()
        self.assertEqual(wizard.url_status, "ok", wizard.url_message)
        self.assertEqual(wizard.base_url, "http://shop.example.test")
        self.assertEqual(wizard.name, "shop.example.test")
        prepared, kwargs = http.requests[0]
        self.assertEqual(urlsplit(prepared.url).path, "/rest/V1/store/storeConfigs")
        self.assertNotIn("Authorization", prepared.headers, "the reachability probe sends no credentials")
        self.assertTrue(kwargs["verify"])
        self.assertIn("ok", wizard.step_header.lower())
        self.assertIn("alert-success", str(wizard.step_alert))
        # Next moves on, the step is re-runnable and the header reflects it.
        wizard.action_next()
        self.assertEqual(wizard.current_step, "credentials")
        self.assertIn('aria-current="step"', str(wizard.step_header))
        self.assertGreater(wizard.progress, 0)

    def test_url_unreachable_and_not_magento(self):
        wizard = self._wizard()
        wizard.action_check_url()  # common.py blocks every HTTP send: connection error
        self.assertEqual(wizard.url_status, "error")
        self.assertIn("Cannot reach shop.example.test", wizard.url_message)
        wizard.action_next()
        self.assertEqual(wizard.current_step, "url", "Next stays on a failed step")
        self.assertIn("alert-danger", str(wizard.step_alert))

        self._http(lambda path, prepared, kwargs: _error(404, "Not found"))
        wizard.action_check_url()
        self.assertEqual(wizard.url_status, "error")
        self.assertIn("HTTP 404", wizard.url_message)

        wizard.url_input = "ftp://nope"
        wizard.action_check_url()
        self.assertEqual(wizard.url_status, "error")
        self.assertIn("http:// or https://", wizard.url_message)

    def test_url_ssl_problem_is_a_warning_with_toggle(self):
        def responder(path, prepared, kwargs):
            if kwargs.get("verify"):
                return requests.exceptions.SSLError("certificate verify failed: self-signed certificate")
            return _error(401, "The consumer isn't authorized to access %resources.")

        self._http(responder)
        wizard = self._wizard(url_input="shop.example.test")
        wizard.action_check_url()
        self.assertEqual(wizard.url_status, "warning")
        self.assertTrue(wizard.ssl_problem)
        self.assertIn("not trusted", wizard.url_message)
        self.assertIn("Verify SSL", wizard.url_message)
        wizard.action_next()
        self.assertEqual(wizard.current_step, "credentials", "a warning does not block")

        wizard.verify_ssl = False
        wizard.action_check_url()
        self.assertEqual(wizard.url_status, "ok")
        self.assertFalse(wizard.ssl_problem)
        self.assertIn("SSL verification is off", wizard.url_message)

    # ------------------------------------------------------------------
    # 2. Credentials
    # ------------------------------------------------------------------
    def test_credentials_reuse_diagnose_and_show_missing_acl(self):
        def responder(path, prepared, kwargs):
            if path.endswith("/products"):
                return _error(403, "The consumer isn't authorized to access %resources.",
                              {"resources": "Magento_Catalog::products"})
            return _magento_ok(path, prepared, kwargs)

        self._http(responder)
        wizard = self._wizard(name="Shop")
        wizard.action_next()
        self.assertEqual(wizard.url_status, "ok")
        self.assertEqual(wizard.current_step, "credentials")

        wizard.write({"consumer_key": "ck-1", "access_token": "at-3"})
        wizard.action_validate_credentials()
        self.assertEqual(wizard.credentials_status, "error")
        self.assertIn("Consumer Secret", wizard.credentials_message)
        self.assertFalse(wizard.instance_id, "nothing is created while values are missing")

        Instance = type(self.env["magento.instance"])
        original = Instance.action_diagnose_access
        with patch.object(Instance, "action_diagnose_access", autospec=True, side_effect=original) as diagnose:
            wizard.write(OAUTH)
            wizard.action_next()  # Next validates the step
        self.assertEqual(diagnose.call_count, 1, "the wizard reuses the instance diagnosis")
        instance = wizard.instance_id
        self.assertTrue(instance)
        self.assertEqual(instance.auth_method, "oauth1")
        self.assertEqual(instance.base_url, SHOP)
        self.assertEqual(instance.sudo().consumer_secret, "cs-2")
        self.assertEqual(wizard.credentials_status, "error")
        self.assertEqual(wizard.current_step, "credentials", "Next stays while access is missing")
        self.assertIn("Magento_Catalog::products", wizard.credentials_message)
        self.assertIn("System > Extensions > Integrations", wizard.credentials_message)
        html = str(wizard.diagnostics_html)
        self.assertIn("Magento API resource", html, "the diagnosis ACL table is shown")
        self.assertIn("Magento_Catalog::products", html)

        # Access granted in Magento: validating again updates the same instance.
        self._http(_magento_ok)
        wizard.action_validate_credentials()
        self.assertEqual(wizard.credentials_status, "ok", wizard.credentials_message)
        self.assertEqual(wizard.instance_id, instance)
        self.assertEqual(self.env["magento.instance"].search_count([("base_url", "=", SHOP)]), 1)
        self.assertEqual(instance.state, "connected")

    def test_bearer_under_advanced(self):
        self._http(_magento_ok)
        wizard = self._wizard(name="Bearer shop", show_advanced=True, auth_method="bearer", access_token="tok")
        wizard.action_check_url()
        wizard.action_validate_credentials()
        self.assertEqual(wizard.credentials_status, "ok", wizard.credentials_message)
        self.assertEqual(wizard.instance_id.auth_method, "bearer")

    # ------------------------------------------------------------------
    # 3. Stores
    # ------------------------------------------------------------------
    def _store_responder(self, path, prepared, kwargs):
        if path.endswith("/store/websites"):
            return make_response(200, [
                {"id": 0, "code": "admin", "name": "Admin"},
                {"id": 1, "code": "base", "name": "Main Website"},
                {"id": 2, "code": "eu", "name": "EU Website"},
            ])
        if path.endswith("/store/storeViews"):
            return make_response(200, [
                {"id": 0, "code": "admin", "name": "Admin", "website_id": 0, "is_active": 1},
                {"id": 1, "code": "default", "name": "Default Store View", "website_id": 1, "is_active": 1},
                {"id": 3, "code": "fr", "name": "French", "website_id": 2, "is_active": 1},
            ])
        return _magento_ok(path, prepared, kwargs)

    def test_store_step_without_discovery_method(self):
        self._http(self._store_responder)
        real_hasattr = hasattr
        self.startPatcher(patch.object(
            wizard_module, "hasattr", create=True,
            side_effect=lambda obj, name: False if name == "_magento_discover_stores" else real_hasattr(obj, name)))
        wizard = self._connected_wizard()
        self.assertEqual(wizard.current_step, "stores", "resumes on the first step not done")
        wizard._goto("stores")
        self.assertFalse(wizard.discovery_available)
        self.assertIn("store discovery available after update", wizard.stores_message.lower())
        self.assertEqual(sorted(wizard.store_line_ids.mapped("store_code")), ["default", "fr"],
                         "the admin store is never offered")
        self.assertEqual(wizard.stores_status, "warning", "two store views: the user picks one")
        line = wizard.store_line_ids.filtered(lambda l: l.store_code == "fr")
        line.action_select()
        self.assertEqual(wizard.stores_status, "ok")
        self.assertTrue(line.is_selected)
        self.assertEqual((self.instance.website_id, self.instance.store_id, self.instance.store_code), (2, 3, "fr"))

    def test_store_step_with_discovery_method(self):
        self._http(self._store_responder)
        Instance = type(self.env["magento.instance"])
        calls = []
        self.startPatcher(patch.object(
            Instance, "_magento_discover_stores", create=True,
            new=lambda instance: calls.append(instance.id)))
        self.instance.write({"store_code": "default", "store_id": 1, "website_id": 1})
        wizard = self._connected_wizard()
        wizard.action_discover_stores()
        self.assertEqual(calls, [self.instance.id], "the discovery method runs when installed")
        self.assertTrue(wizard.discovery_available)
        self.assertNotIn("available after update", wizard.stores_message or "")
        self.assertEqual(wizard.stores_status, "ok", "the configured store view is preselected")
        self.assertEqual(wizard.store_choice_id.store_code, "default")

    # ------------------------------------------------------------------
    # 4. Mappings
    # ------------------------------------------------------------------
    def test_mapping_proposals_create_mapping_records(self):
        company = self.env.company
        tax = self.env["account.tax"].create({
            "name": "Sales 8.25%", "amount": 8.25, "amount_type": "percent", "type_tax_use": "sale",
            "company_id": company.id, "price_include_override": "tax_excluded",
        })
        bank = self.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", company.id)], limit=1)
        cash = self.env["account.journal"].create({
            "name": "Magento Cash", "type": "cash", "code": "MGCSH", "company_id": company.id})
        self.env["magento.payment.map"].create({
            "instance_id": self.instance.id, "magento_method": "banktransfer", "journal_id": cash.id})
        orders = [
            {"entity_id": 1, "customer_group_id": 1, "shipping_description": "Flat Rate - Fixed",
             "payment": {"method": "checkmo", "additional_information": ["Check / Money order"]},
             "extension_attributes": {
                 "applied_taxes": [{"code": "US-CA-*-Rate 1", "title": "US-CA-*-Rate 1", "percent": 8.25}],
                 "shipping_assignments": [{"shipping": {"method": "flatrate_flatrate"}}]}},
            {"entity_id": 2, "customer_group_id": 0, "shipping_description": "UPS Ground",
             "payment": {"method": "checkmo"},
             "extension_attributes": {"shipping_assignments": [{"shipping": {"method": "ups_GND"}}]}},
            {"entity_id": 3, "customer_group_id": 1,
             "payment": {"method": "paypal_express", "additional_information": ["PayPal Express Checkout"]},
             "extension_attributes": {
                 "applied_taxes": [{"code": "US-NY-*-Rate 1", "title": "US-NY-*-Rate 1", "percent": 8.375}],
                 "shipping_assignments": [{"shipping": {"method": "flatrate_flatrate"}}]}},
        ]

        def responder(path, prepared, kwargs):
            if path.endswith("/orders"):
                query = parse_qs(urlsplit(prepared.url).query)
                self.assertEqual(query.get("searchCriteria[pageSize]"), ["50"])
                self.assertIn("payment", query["fields"][0])
                return make_response(200, {"items": orders, "total_count": 3})
            if path.endswith("/taxRates/search"):
                return _error(403, "The consumer isn't authorized to access %resources.",
                              {"resources": "Magento_Tax::manage_tax"})
            if path.endswith("/customerGroups/search"):
                return make_response(200, {"items": [
                    {"id": 0, "code": "NOT LOGGED IN", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
                    {"id": 1, "code": "General", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
                ], "total_count": 2})
            return _magento_ok(path, prepared, kwargs)

        self._http(responder)
        wizard = self._connected_wizard(default_journal_id=bank.id)
        self.assertEqual(wizard.mappings_status, "ok", "an existing mapping counts as done on resume")
        wizard._goto("mappings")
        self.assertFalse(wizard.payment_line_ids, "a step already done is not re-proposed on entry")
        wizard.action_propose_mappings()
        self.assertEqual(wizard.orders_sampled, 3)
        payments = {line.code: line for line in wizard.payment_line_ids}
        self.assertEqual(set(payments), {"checkmo", "paypal_express", "banktransfer"})
        self.assertEqual(payments["checkmo"].seen_count, 2)
        self.assertEqual(payments["checkmo"].label, "Check / Money order")
        self.assertEqual(payments["checkmo"].journal_id, bank, "the default journal is proposed")
        self.assertTrue(payments["banktransfer"].existing)
        self.assertEqual(payments["banktransfer"].journal_id, cash, "existing mappings pre-fill")
        self.assertEqual(set(wizard.carrier_line_ids.mapped("code")), {"flatrate_flatrate", "ups_GND"})
        taxes = {line.code: line for line in wizard.tax_line_ids}
        self.assertEqual(set(taxes), {"US-CA-*-Rate 1", "US-NY-*-Rate 1"})
        self.assertEqual(taxes["US-CA-*-Rate 1"].tax_id, tax, "an Odoo tax with the same rate is proposed")
        self.assertFalse(taxes["US-NY-*-Rate 1"].tax_id)
        self.assertIn("recent orders", wizard.mappings_message)
        self.assertEqual(sorted(wizard.group_line_ids.mapped("magento_ref")), [0, 1])

        payments["paypal_express"].accept = False
        wizard.action_next()  # Next creates the accepted mappings
        self.assertEqual(wizard.current_step, "catalog")
        self.assertIn(wizard.mappings_status, ("ok", "warning"), wizard.mappings_message)
        PaymentMap = self.env["magento.payment.map"]
        checkmo = PaymentMap.search([("instance_id", "=", self.instance.id), ("magento_method", "=", "checkmo")])
        self.assertEqual(checkmo.journal_id, bank)
        self.assertEqual(checkmo.name, "Check / Money order")
        self.assertFalse(PaymentMap.search([("instance_id", "=", self.instance.id),
                                            ("magento_method", "=", "paypal_express")]))
        self.assertEqual(PaymentMap.search([("instance_id", "=", self.instance.id),
                                            ("magento_method", "=", "banktransfer")]).journal_id, cash)
        carriers = self.env["magento.carrier.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(set(carriers.mapped("magento_code")), {"flatrate_flatrate", "ups_GND"})
        tax_maps = self.env["magento.tax.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(tax_maps.mapped("magento_code"), ["US-CA-*-Rate 1"], "rates without a tax are skipped")
        self.assertEqual(tax_maps.tax_id, tax)
        groups = self.env["magento.customer.group"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(sorted(groups.mapped("name")), ["General", "NOT LOGGED IN"])
        self.assertTrue(all(groups.mapped("from_magento")))
        self.assertEqual(self.instance.payment_journal_id, bank, "the default journal is stored")

        # Re-running is idempotent.
        wizard.action_apply_mappings()
        self.assertEqual(PaymentMap.search_count([("instance_id", "=", self.instance.id)]), 2)
        self.assertEqual(self.env["magento.carrier.map"].search_count([("instance_id", "=", self.instance.id)]), 2)

    def test_mapping_step_survives_unreadable_orders(self):
        def responder(path, prepared, kwargs):
            if path.endswith("/orders"):
                return _error(401, "The consumer isn't authorized to access %resources.",
                              {"resources": "Magento_Sales::sales"})
            if path.endswith("/taxRates/search"):
                return make_response(200, {"items": [{"id": 1, "code": "VAT 20", "rate": 20}], "total_count": 1})
            return _magento_ok(path, prepared, kwargs)

        self._http(responder)
        wizard = self._connected_wizard()
        wizard.action_propose_mappings()
        self.assertEqual(wizard.mappings_status, "warning")
        self.assertIn("Recent orders could not be read", wizard.mappings_message)
        self.assertEqual(wizard.tax_line_ids.mapped("code"), ["VAT 20"])

    # ------------------------------------------------------------------
    # 5. Catalog check
    # ------------------------------------------------------------------
    def test_catalog_check_detects_duplicates_and_barcode_collisions(self):
        self.create_product("DUP-1", name="Dup A")
        self.create_product("DUP-1", name="Dup B")
        self.create_product("OTHER", name="Scanner product", barcode="MG-BC")
        self.create_product("EANPROD", name="EAN holder", barcode="4006381333931")
        self.create_product("case-sku", name="Lowercase")
        self.create_product("OK-1", name="Fine")

        def item(sku, type_id="simple", price=10.0, ean=None):
            row = {"id": len(sku) * 1000 + sum(map(ord, sku)), "sku": sku, "name": sku, "type_id": type_id,
                   "price": price, "status": 1, "custom_attributes": []}
            if ean:
                row["custom_attributes"].append({"attribute_code": "ean", "value": ean})
            return row

        items = [
            item("DUP-1"), item("MG-BC"), item("CASE-SKU"), item("OK-1"),
            item("NEW-EAN", ean="4006381333931"), item("FREE", price=0), item("CONF-1", "configurable", None),
            item("BUNDLE-1", "bundle", None), item("Twin"), item("twin "),
        ]
        captured = {}

        def fake_iter(api, endpoint, **kwargs):
            captured.update(kwargs)
            captured["endpoint"] = endpoint("/rest/V1") if callable(endpoint) else endpoint
            kwargs["stats"]["total_count"] = 1234
            return iter(items)

        self.patch_api("iter_search", side_effect=fake_iter)
        wizard = self._connected_wizard()
        wizard._goto("catalog")
        self.assertEqual(wizard.catalog_status, "pending", "the catalog check runs on demand")
        wizard.action_check_catalog()
        self.assertEqual(captured["endpoint"], "/rest/V1/products")
        self.assertIn("sku", captured["fields"])
        self.assertEqual(wizard.catalog_sampled, len(items))
        self.assertEqual(wizard.catalog_total, 1234)
        self.assertEqual(wizard.catalog_status, "warning", "findings never block: warning, not error")

        def rows(category, sku=None):
            return wizard.check_line_ids.filtered(
                lambda l: l.category == category and (sku is None or l.sku == sku))

        dup = rows("duplicate_sku", "DUP-1")
        self.assertEqual(dup.severity, "error")
        self.assertIn("2 Odoo products", dup.message)
        self.assertEqual(rows("barcode", "MG-BC").product_id.default_code, "OTHER")
        ean = rows("barcode", "NEW-EAN")
        self.assertEqual(ean.severity, "error")
        self.assertEqual(ean.product_id.default_code, "EANPROD")
        would = rows("would_duplicate", "CASE-SKU")
        self.assertEqual(would.severity, "warning")
        self.assertEqual(would.product_id.default_code, "case-sku")
        self.assertTrue(rows("magento_duplicate"), "Twin / 'twin ' differ only by case and spaces")
        self.assertEqual(rows("no_price").mapped("sku"), ["FREE"], "configurable/bundle need no own price")
        types = " ".join(rows("type").mapped("message"))
        self.assertIn("1 configurable", types)
        self.assertIn("1 bundle", types)
        self.assertFalse(rows("duplicate_sku", "OK-1"))
        new = rows("new")
        self.assertEqual(len(new), 1, "SKUs new to Odoo are summed up in one row")
        self.assertIn("NEW-EAN", new.message)
        self.assertNotIn("OK-1", new.message)
        self.assertEqual(wizard.catalog_new_count, 7)
        self.assertGreaterEqual(wizard.catalog_error_count, 3)
        wizard.action_next()
        self.assertEqual(wizard.current_step, "golive")

    # ------------------------------------------------------------------
    # 6. Go live
    # ------------------------------------------------------------------
    def test_golive_writes_settings_and_queues_first_sync_once(self):
        wizard = self._connected_wizard()
        other = self.create_user("mg_wizard_alert", MANAGER_GROUP)
        wizard.write({
            "auto_sync_enabled": True,
            "auto_sync_frequency": "daily",
            "auto_sync_hour": 3,
            "auto_sync_customers": False,
            "run_syncs_in_background": True,
            "stock_push_enabled": True,
            "stock_qty_basis": "on_hand",
            "alert_user_ids": [(6, 0, other.ids)],
        })
        wizard._goto("golive")
        self.assertTrue(wizard.issue_line_ids, "steps not done yet are listed as known issues")
        fix = wizard.issue_line_ids.filtered(lambda l: l.step == "catalog")[:1]
        self.assertTrue(fix)
        fix.action_fix()
        self.assertEqual(wizard.current_step, "catalog", "Fix jumps to the step")
        wizard._goto("golive")

        wizard.action_run_first_sync()
        wizard.action_run_first_sync()  # a second click reuses the queued jobs
        self.assertEqual(wizard.golive_status, "ok", wizard.golive_message)
        self.assertTrue(wizard.first_sync_queued)
        instance = self.instance
        self.assertTrue(instance.auto_sync_enabled)
        self.assertEqual(instance.auto_sync_frequency, "daily")
        self.assertEqual(instance.auto_sync_hour, 3)
        self.assertFalse(instance.auto_sync_customers)
        self.assertTrue(instance.stock_push_enabled)
        self.assertEqual(instance.stock_qty_basis, "on_hand")
        self.assertEqual(instance.alert_user_ids, other)
        self.assertTrue(instance.auto_sync_cron_id)
        self.assertTrue(instance.order_workflow_ids, "the default order workflows are kept")
        Job = self.env["magento.job"]
        for entity in ("categories", "products", "customers", "orders"):
            self.assertEqual(Job.search_count([("identity_key", "=", "sync-%s-%s" % (instance.id, entity))]), 1,
                             entity)
        self.assertIn("already queued", wizard.golive_message.lower())
        self.assertEqual(self.http_attempts, [], "queuing makes no Magento call")
        action = wizard.action_open_console()
        self.assertEqual(action["tag"], "sdlc_magento_dashboard")

    def test_golive_invalid_scope_is_shown_on_the_step(self):
        wizard = self._connected_wizard()
        wizard.write({"auto_sync_enabled": True, "auto_sync_products": False, "auto_sync_customers": False,
                      "auto_sync_categories": False, "auto_sync_orders": False})
        with mute_logger("odoo.models"):
            wizard.action_apply_golive()
        self.assertEqual(wizard.golive_status, "error")
        self.assertIn("Auto Sync scope", wizard.golive_message)
        self.assertFalse(self.instance.auto_sync_enabled)

    # ------------------------------------------------------------------
    # Resume, access, entry points
    # ------------------------------------------------------------------
    def test_resume_on_existing_instance(self):
        self.instance.write({"store_code": "default", "store_id": 1, "website_id": 1})
        self.env["magento.payment.map"].create({"instance_id": self.instance.id, "magento_method": "checkmo"})
        wizard = self._connected_wizard()
        self.assertEqual(wizard.instance_id, self.instance)
        self.assertEqual(wizard.base_url, self.instance.base_url)
        self.assertEqual(wizard.auth_method, "bearer")
        self.assertEqual(wizard.access_token, "test-integration-token")
        for step in ("url", "credentials", "stores", "mappings"):
            self.assertEqual(wizard["%s_status" % step], "ok", step)
        self.assertEqual(wizard.current_step, "catalog")

        # active_id from the instance form works as well, and changing a value updates
        # the same instance instead of creating a new one.
        defaults = self.Wizard.with_context(active_model="magento.instance", active_id=self.instance.id) \
            .default_get(["instance_id", "url_input"])
        self.assertEqual(defaults["instance_id"], self.instance.id)
        self._http(_magento_ok)
        count = self.env["magento.instance"].search_count([])
        wizard.write({"access_token": "rotated-token"})
        wizard.action_validate_credentials()
        self.assertEqual(wizard.credentials_status, "ok", wizard.credentials_message)
        self.assertEqual(self.env["magento.instance"].search_count([]), count)
        self.assertEqual(self.instance.sudo().access_token, "rotated-token")

    def test_non_manager_cannot_open_the_wizard(self):
        Wizard = self.Wizard.with_user(self.magento_user)
        with self.assertRaises(AccessError), mute_logger("odoo.models", "odoo.addons.base.models.ir_model"):
            Wizard.default_get(["url_input"])
        with self.assertRaises(AccessError), mute_logger("odoo.models", "odoo.addons.base.models.ir_model"):
            Wizard.create({"url_input": SHOP})
        wizard = self._wizard()
        with self.assertRaises(AccessError), mute_logger("odoo.models", "odoo.addons.base.models.ir_model"):
            wizard.with_user(self.magento_user).action_check_url()

        def nav(user):
            groups = self.env["magento.dashboard"].with_user(user).get_nav_links()
            return {link["xml_id"] for group in groups for link in group["links"]}

        self.assertNotIn(WIZARD_ACTION, nav(self.magento_user))
        self.assertIn(WIZARD_ACTION, nav(self.manager), "the Console links to the wizard")
        # A manager can open it from the menu.
        self.Wizard.with_user(self.manager).create({"url_input": SHOP})
