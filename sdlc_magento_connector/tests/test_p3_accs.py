"""Roadmap 3.9: Adobe Commerce as a Cloud Service (ACCS).

Simulated HTTP only (the local store is Magento Open Source, not ACCS), built from Adobe's
documented shapes: the IMS token response ``{"access_token", "token_type", "expires_in"}``
of https://ims-na1.adobelogin.com/ims/token/v3 and the tenant URLs
``https://<server>.api.commerce.adobe.com/<tenant>/V1/...`` with the ``Store`` header.
"""
import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.services import magento_auth
from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI, MagentoCapabilityError

from .common import MAGENTO_BASE_URL, MagentoTestCommon, make_response

ACCS_BASE = "https://na1-sandbox.api.commerce.adobe.com"
TENANT = "AbCYab34cdEfGHiJ27123"
MANAGER = "sdlc_magento_connector.group_magento_manager"
USER = "sdlc_magento_connector.group_magento_user"


class FakeTransport:
    """Stands in for ``requests.Session.send``: answers registered routes in order (the
    last answer repeats) and records every request. An unexpected request fails the test."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def add(self, method, pattern, *answers):
        self.routes.append((method.upper(), re.compile(pattern), list(answers)))
        return self

    def __call__(self, *args, **kwargs):
        request = args[-1]  # patched on the class: called as send(prepared_request, **kw)
        self.requests.append(request)
        for method, pattern, answers in self.routes:
            if request.method == method and pattern.search(request.url):
                status, payload = answers.pop(0) if len(answers) > 1 else answers[0]
                return make_response(status, payload, url=request.url)
        raise AssertionError("Unexpected HTTP %s %s" % (request.method, request.url))

    def sent(self, method=None, pattern=None):
        regex = re.compile(pattern) if pattern else None
        return [r for r in self.requests
                if (method is None or r.method == method.upper()) and (regex is None or regex.search(r.url))]


def ims_answer(token, expires_in=86399):
    return 200, {"access_token": token, "token_type": "bearer", "expires_in": expires_in}


@tagged("post_install", "-at_install", "magento")
class TestAccs(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.accs = cls.create_instance(
            "ACCS store", base_url=ACCS_BASE, platform="accs", accs_tenant_id=TENANT,
            auth_method="ims", ims_client_id="client-123", ims_client_secret="secret-456",
        )

    def setUp(self):
        super().setUp()
        self.http = FakeTransport()
        self.startPatcher(patch.object(requests.sessions.Session, "send", self.http))

    def ims_calls(self):
        return self.http.sent("POST", r"ims-na1\.adobelogin\.com/ims/token/v3")

    # ------------------------------------------------------------------
    # IMS token
    # ------------------------------------------------------------------
    def test_ims_token_fetched_cached_and_refreshed_near_expiry(self):
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-1"), ims_answer("tok-2"))
        self.http.add("GET", r"/V1/store/websites$", (200, [{"id": 1, "code": "base"}]))
        api_client = MagentoAPI(self.accs)

        self.assertEqual(api_client.get_websites(), [{"id": 1, "code": "base"}])
        self.assertEqual(len(self.ims_calls()), 1)
        body = parse_qs(self.ims_calls()[0].body)
        self.assertEqual(body["grant_type"], ["client_credentials"])
        self.assertEqual(body["client_id"], ["client-123"])
        self.assertEqual(body["client_secret"], ["secret-456"])
        self.assertEqual(body["scope"], ["openid,AdobeID,commerce.accs"])
        get = self.http.sent("GET")[0]
        self.assertEqual(get.url, "%s/%s/V1/store/websites" % (ACCS_BASE, TENANT))
        self.assertEqual(get.headers["Authorization"], "Bearer tok-1")
        accs = self.accs.sudo()
        self.assertEqual(accs.ims_access_token, "tok-1")
        expected_expiry = fields.Datetime.now() + timedelta(seconds=86399)
        self.assertLess(abs((accs.ims_token_expires_at - expected_expiry).total_seconds()), 120)

        # Cached: a second call (even through a new client) does not fetch again.
        MagentoAPI(self.accs).get_websites()
        self.assertEqual(len(self.ims_calls()), 1)

        # Close to expiry (inside the refresh margin): refreshed before use.
        accs.write({"ims_token_expires_at": fields.Datetime.now() + timedelta(seconds=60)})
        MagentoAPI(self.accs).get_websites()
        self.assertEqual(len(self.ims_calls()), 2)
        self.assertEqual(self.http.sent("GET")[-1].headers["Authorization"], "Bearer tok-2")
        self.assertEqual(accs.ims_access_token, "tok-2")

    def test_401_forces_exactly_one_refresh_and_retry(self):
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-old"), ims_answer("tok-new"))
        self.http.add("GET", r"/V1/store/websites$",
                      (401, {"message": "The consumer isn't authorized to access %resources."}),
                      (200, []))
        MagentoAPI(self.accs).get_websites()
        gets = self.http.sent("GET")
        self.assertEqual([g.headers["Authorization"] for g in gets], ["Bearer tok-old", "Bearer tok-new"])
        self.assertEqual(len(self.ims_calls()), 2)

    def test_persistent_401_is_raised_after_one_refresh(self):
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-a"), ims_answer("tok-b"), ims_answer("tok-c"))
        self.http.add("GET", r"/V1/store/websites$", (401, {"message": "Unauthorized"}))
        with self.assertRaises(requests.exceptions.HTTPError):
            MagentoAPI(self.accs).get_websites()
        self.assertEqual(len(self.http.sent("GET")), 2)
        self.assertEqual(len(self.ims_calls()), 2)

    def test_ims_refusal_is_a_clear_error(self):
        self.http.add("POST", r"/ims/token/v3$",
                      (400, {"error": "invalid_client", "error_description": "invalid client_id parameter"}))
        with self.assertRaisesRegex(UserError, "invalid client_id parameter"):
            MagentoAPI(self.accs).get_websites()
        self.assertFalse(self.http.sent("GET"))

    def test_fetch_ims_token_helper(self):
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-x", expires_in=3600))
        token = magento_auth.fetch_ims_token("id", "secret", now=1000.0)
        self.assertEqual(token["access_token"], "tok-x")
        self.assertEqual(token["expires_at"], 4600.0)
        self.assertTrue(magento_auth.ims_token_is_fresh(4600.0, now=4000.0))
        self.assertFalse(magento_auth.ims_token_is_fresh(4600.0, now=4400.0))  # inside the 300 s margin
        with self.assertRaises(magento_auth.ImsTokenError):
            magento_auth.fetch_ims_token("", "secret")

    def test_changing_the_credential_drops_the_cached_token(self):
        accs = self.accs.sudo()
        accs.write({"ims_access_token": "cached", "ims_token_expires_at": fields.Datetime.now() + timedelta(hours=5)})
        accs.write({"ims_client_secret": "rotated"})
        self.assertFalse(accs.ims_access_token)
        self.assertFalse(accs.ims_token_expires_at)

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------
    def test_accs_urls(self):
        api_client = MagentoAPI(self.accs)
        prefix = "/" + TENANT
        self.assertEqual(api_client._accs_endpoint("/rest/V1/orders"), (prefix + "/V1/orders", None))
        self.assertEqual(api_client._accs_endpoint("/rest/default/V1/products/A%20B"),
                         (prefix + "/V1/products/A%20B", {"Store": "default"}))
        self.assertEqual(api_client._accs_endpoint("/rest/all/V1/products"),
                         (prefix + "/V1/products", {"Store": "all"}))
        self.assertEqual(api_client._accs_endpoint("/rest/all/async/bulk/V1/products/bySku"),
                         (prefix + "/V1/async/bulk/products/bySku", {"Store": "all"}))
        self.assertEqual(api_client._accs_endpoint("/rest/V1/bulk/abc/detailed-status"),
                         (prefix + "/V1/bulk/abc/detailed-status", None))
        # A base URL that already ends with the tenant does not get it twice.
        self.accs.write({"base_url": "%s/%s" % (ACCS_BASE, TENANT)})
        self.assertEqual(MagentoAPI(self.accs)._accs_endpoint("/rest/V1/orders"), ("/V1/orders", None))

    def test_store_scoped_and_bulk_requests_on_the_wire(self):
        self.accs.write({"use_async_bulk": True})
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-1"))
        self.http.add("GET", r"/V1/products/SKU-1$", (200, {"sku": "SKU-1"}))
        self.http.add("PUT", r"/V1/async/bulk/products/bySku$",
                      (202, {"bulk_uuid": "u-1", "request_items": [{"id": 0, "status": "accepted"}],
                             "errors": False}))
        api_client = MagentoAPI(self.accs)
        self.assertEqual(api_client.get_product_for_store("SKU-1", "fr"), {"sku": "SKU-1"})
        get = self.http.sent("GET")[0]
        self.assertEqual(get.url, "%s/%s/V1/products/SKU-1" % (ACCS_BASE, TENANT))
        self.assertEqual(get.headers["Store"], "fr")

        result = api_client.async_bulk("put", "products/bySku", [{"product": {"sku": "SKU-1"}}])
        self.assertEqual(result[0]["bulk_uuid"], "u-1")
        bulk = self.http.sent("PUT")[0]
        self.assertEqual(bulk.url, "%s/%s/V1/async/bulk/products/bySku" % (ACCS_BASE, TENANT))
        self.assertEqual(bulk.headers["Store"], "all")
        self.assertNotIn("/rest", bulk.url)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------
    def test_unsupported_features_are_blocked_with_a_clear_message(self):
        api_client = MagentoAPI(self.accs)
        with self.assertRaisesRegex(MagentoCapabilityError, "Not available on Adobe Commerce as a Cloud Service"):
            api_client.create_guest_cart()
        with self.assertRaises(MagentoCapabilityError) as ctx:
            api_client._request("get", "/rest/V1/customers/me")
        self.assertEqual(ctx.exception.capability, "customer_self")
        self.accs.write({"use_async_bulk": True})
        with self.assertRaisesRegex(MagentoCapabilityError, "async/bulk form of POST /V1/customers"):
            api_client.async_bulk("post", "customers", [{"customer": {"email": "a@example.com"}}])
        self.assertFalse(self.http.requests, "a blocked feature must not reach the network")

        available, message = self.accs._magento_capability("guest_cart")
        self.assertFalse(available)
        self.assertIn("not available on Adobe Commerce as a Cloud Service", message)
        self.assertTrue(self.accs._magento_capability("admin_rest")[0])
        self.assertIn("Not available on ACCS", self.accs.accs_capability_html)

        partner = self.env["res.partner"].create({"name": "ACCS buyer"})
        order = self.env["sale.order"].create({"partner_id": partner.id, "magento_instance_id": self.accs.id})
        with self.assertRaisesRegex(UserError, "Export Odoo orders to Magento is not available"):
            order.action_create_magento_order()
        self.assertFalse(self.http.requests)

    def test_diagnose_access_understands_accs(self):
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-1"))
        self.http.add("GET", r"/%s/V1/" % TENANT, (200, {"items": [], "total_count": 0}))
        self.accs.action_diagnose_access()
        self.assertEqual(self.accs.state, "connected")
        html = str(self.accs.access_diagnostics)
        self.assertIn("Not on this platform", html)
        self.assertIn("guest cart", html)
        self.assertTrue(all("/rest/" not in r.url for r in self.http.requests))
        self.assertTrue(self.http.sent("GET", r"/%s/V1/orders" % TENANT))

    def test_diagnose_access_reports_an_ims_failure(self):
        self.http.add("POST", r"/ims/token/v3$", (401, {"error": "invalid_client"}))
        self.accs.action_diagnose_access()
        self.assertEqual(self.accs.state, "error")
        self.assertIn("invalid_client", self.accs.last_connection_message)
        self.assertFalse(self.http.sent("GET"))

    def test_setup_wizard_points_accs_hosts_to_the_instance_form(self):
        status, message, _url, _ssl = self.env["magento.setup.wizard"]._probe_store(ACCS_BASE, True)
        self.assertEqual(status, "error")
        self.assertIn("Adobe Commerce as a Cloud Service", message)
        self.assertFalse(self.http.requests)

    # ------------------------------------------------------------------
    # Configuration rules and secrets
    # ------------------------------------------------------------------
    def test_platform_and_auth_must_match(self):
        with self.assertRaisesRegex(ValidationError, "only accepts Adobe IMS"):
            self.create_instance("ACCS oauth", platform="accs", accs_tenant_id="T1", auth_method="bearer")
        with self.assertRaisesRegex(ValidationError, "tenant ID"):
            self.create_instance("ACCS no tenant", platform="accs", auth_method="ims",
                                 ims_client_id="a", ims_client_secret="b")
        with self.assertRaisesRegex(ValidationError, "for Adobe Commerce as a Cloud Service"):
            self.create_instance("PaaS ims", auth_method="ims", ims_client_id="a", ims_client_secret="b")
        with self.assertRaisesRegex(ValidationError, "IMS Client Secret"):
            self.create_instance("ACCS no secret", platform="accs", accs_tenant_id="T1", auth_method="ims",
                                 ims_client_id="a")
        # An IMS instance needs no integration token: the required field gets a marker.
        inst = self.create_instance("ACCS no token", platform="accs", accs_tenant_id="T1", auth_method="ims",
                                    ims_client_id="a", ims_client_secret="b", access_token=False)
        self.assertEqual(inst.sudo().access_token, "adobe-ims")

    def test_ims_secrets_are_manager_only(self):
        for name in ("ims_client_id", "ims_client_secret", "ims_org_id", "ims_access_token",
                     "ims_token_expires_at", "ims_scopes", "ims_token_url"):
            self.assertEqual(self.accs._fields[name].groups, MANAGER, name)
        user = self.create_user("mg_accs_user", USER)
        with self.assertRaises(AccessError):
            self.accs.with_user(user).read(["ims_client_secret"])
        with self.assertRaises(AccessError):
            self.accs.with_user(user).action_ims_fetch_token()
        # A Magento user's sync still authenticates: the token is read with sudo.
        self.http.add("POST", r"/ims/token/v3$", ims_answer("tok-user"))
        self.http.add("GET", r"/V1/store/websites$", (200, []))
        MagentoAPI(self.accs.with_user(user)).get_websites()
        self.assertEqual(self.http.sent("GET")[0].headers["Authorization"], "Bearer tok-user")

    def test_default_platform_behaviour_is_unchanged(self):
        self.assertEqual(self.instance.platform, "open_source_paas")
        self.http.add("GET", r"/rest/V1/store/websites$", (200, []))
        self.http.add("GET", r"/rest/default/V1/products/SKU-1$", (200, {"sku": "SKU-1"}))
        api_client = MagentoAPI(self.instance)
        self.assertFalse(api_client.is_accs)
        api_client.get_websites()
        api_client.get_product_for_store("SKU-1", "default")
        urls = [r.url for r in self.http.requests]
        self.assertEqual(urls, ["%s/rest/V1/store/websites" % MAGENTO_BASE_URL,
                                "%s/rest/default/V1/products/SKU-1" % MAGENTO_BASE_URL])
        self.assertTrue(all("Store" not in r.headers for r in self.http.requests))
        self.assertEqual(self.http.requests[0].headers["Authorization"], "Bearer test-integration-token")
        self.assertFalse(self.ims_calls())
        self.assertEqual(self.instance._magento_ims_token(), "")
        self.assertTrue(self.instance._magento_capability("guest_cart")[0])
