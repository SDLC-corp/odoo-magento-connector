"""Phase 1.5: OAuth 1.0a request signing, Bearer back-compat and access diagnostics."""
import base64
import json
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.services import magento_auth
from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MAGENTO_BASE_URL, MagentoTestCommon, make_response
from odoo.addons.sdlc_magento_connector.models import sql_compat

USER_GROUP = "sdlc_magento_connector.group_magento_user"
MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"

OAUTH_CREDENTIALS = {
    "auth_method": "oauth1",
    "consumer_key": "ck-123",
    "consumer_secret": "cs-456",
    "access_token": "at-789",
    "access_token_secret": "ats-000",
}


def _magento_error(status, message, parameters=None, url=MAGENTO_BASE_URL + "/rest/V1/x"):
    payload = {"message": message}
    if parameters is not None:
        payload["parameters"] = parameters
    return make_response(status, payload, url=url)


class _SendRecorder:
    """Session.send replacement: records prepared requests, answers from ``responder``."""

    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def __call__(self, session, prepared, **kwargs):
        self.requests.append(prepared)
        response = self.responder(prepared)
        if isinstance(response, Exception):
            raise response
        response.request = prepared
        response.url = prepared.url
        return response


@tagged("post_install", "-at_install", "magento")
class TestOAuthSigner(MagentoTestCommon):
    """Pure signer: RFC 5849 vectors and output format."""

    RFC_ARGS = (
        "GET", "http://photos.example.net/photos", {"file": "vacation.jpg", "size": "original"},
        "dpf43f3p2l4k3l03", "kd94hf93k423kf44", "nnch734d00sl2jdk", "pfkkdhi9sl3r4s00",
    )

    def test_rfc5849_known_vector_hmac_sha1(self):
        # RFC 5849 section 1.2 (no oauth_version in the example).
        oauth = magento_auth.oauth_parameters(
            *self.RFC_ARGS, signature_method="HMAC-SHA1", nonce="chapoH", timestamp=137131202, version=None)
        self.assertEqual(oauth["oauth_signature"], "MdpQcU8iPSUjWoN/UDMsK2sui9I=")
        # Same request in the OAuth Core 1.0 appendix A.5 (with oauth_version=1.0).
        oauth = magento_auth.oauth_parameters(
            *self.RFC_ARGS, signature_method="HMAC-SHA1", nonce="kllo9940pd9333jh", timestamp=1191242096)
        self.assertEqual(oauth["oauth_signature"], "tR3+Ty81lMeYAr/Fid0kMTYa/WM=")

    def test_rfc5849_parameter_normalization(self):
        # RFC 5849 section 3.4.1.3.2 example (body params passed explicitly).
        url = "http://example.com/request?b5=%3D%253D&a3=a&c%40=&a2=r%20b"
        oauth = {
            "oauth_consumer_key": "9djdj82h48djs9d2", "oauth_token": "kkk9d7dh3k39sjv7",
            "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": "137131201",
            "oauth_nonce": "7d8f3e4a", "realm": "Example",
        }
        base = magento_auth.signature_base_string("POST", url, [("c2", ""), ("a3", "2 q")], oauth)
        expected_params = (
            "a2=r%20b&a3=2%20q&a3=a&b5=%3D%253D&c%40=&c2=&oauth_consumer_key=9djdj82h48djs9d2"
            "&oauth_nonce=7d8f3e4a&oauth_signature_method=HMAC-SHA1&oauth_timestamp=137131201"
            "&oauth_token=kkk9d7dh3k39sjv7"
        )
        self.assertEqual(base, "POST&http%3A%2F%2Fexample.com%2Frequest&" + magento_auth.percent_encode(expected_params))

    def test_base_url_normalization(self):
        self.assertEqual(magento_auth.normalize_base_url("HTTPS://Shop.Example.COM:443/rest/V1/x?a=1"),
                         "https://shop.example.com/rest/V1/x")
        self.assertEqual(magento_auth.normalize_base_url("http://shop.example.com:8080/x"),
                         "http://shop.example.com:8080/x")

    def test_hmac_sha256_header_format(self):
        header = magento_auth.sign(*self.RFC_ARGS)
        self.assertTrue(header.startswith("OAuth "))
        parsed = magento_auth.parse_authorization_header(header)
        self.assertEqual(parsed["oauth_signature_method"], "HMAC-SHA256")
        self.assertEqual(parsed["oauth_version"], "1.0")
        self.assertEqual(parsed["oauth_consumer_key"], "dpf43f3p2l4k3l03")
        self.assertEqual(parsed["oauth_token"], "nnch734d00sl2jdk")
        self.assertTrue(parsed["oauth_nonce"] and parsed["oauth_timestamp"].isdigit())
        self.assertEqual(len(base64.b64decode(parsed["oauth_signature"])), 32)
        sha1 = magento_auth.parse_authorization_header(magento_auth.sign(*self.RFC_ARGS, signature_method="HMAC-SHA1"))
        self.assertEqual(len(base64.b64decode(sha1["oauth_signature"])), 20)
        # Fresh nonce on each call.
        self.assertNotEqual(parsed["oauth_nonce"], magento_auth.parse_authorization_header(
            magento_auth.sign(*self.RFC_ARGS))["oauth_nonce"])


@tagged("post_install", "-at_install", "magento")
class TestOAuthApi(MagentoTestCommon):
    """MagentoAPI signs what requests really sends."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oauth_instance = cls.create_instance("OAuth Magento", **OAUTH_CREDENTIALS)

    def _record(self, responder):
        recorder = _SendRecorder(responder)
        self.startPatcher(patch.object(
            requests.sessions.Session, "send",
            lambda session, prepared, **kwargs: recorder(session, prepared, **kwargs)))
        return recorder

    def _assert_validly_signed(self, prepared, secret="cs-456", token_secret="ats-000", method="HMAC-SHA256"):
        """Recompute the signature the way Magento does: from the URL and query received."""
        header = prepared.headers["Authorization"]
        self.assertTrue(header.startswith("OAuth "), header)
        parsed = magento_auth.parse_authorization_header(header)
        oauth = {k: v for k, v in parsed.items() if k != "oauth_signature"}
        base = magento_auth.signature_base_string(prepared.method, prepared.url, None, oauth)
        self.assertEqual(parsed["oauth_signature"],
                         magento_auth.hmac_signature(base, secret, token_secret, method))
        return parsed, base

    def test_oauth_query_params_signed_and_json_body_excluded(self):
        recorder = self._record(lambda prepared: make_response(200, {"items": []}))
        api = MagentoAPI(self.oauth_instance)
        api._get("/rest/V1/products", params={"searchCriteria[pageSize]": 1, "q": "blue shirt"})
        prepared = recorder.requests[0]
        self.assertIn("%20", prepared.url, "spaces are sent as %20 so Magento decodes what was signed")
        self.assertNotIn("+", urlsplit(prepared.url).query)
        parsed, base = self._assert_validly_signed(prepared)
        self.assertEqual(parsed["oauth_consumer_key"], "ck-123")
        self.assertEqual(parsed["oauth_token"], "at-789")
        self.assertIn(magento_auth.percent_encode("searchCriteria%5BpageSize%5D=1"), base)
        self.assertIn(magento_auth.percent_encode("q=blue%20shirt"), base)

        api._post("/rest/V1/orders/5/comments", {"statusHistory": {"comment": "hello"}})
        prepared = recorder.requests[1]
        self.assertEqual(json.loads(prepared.body), {"statusHistory": {"comment": "hello"}})
        parsed, base = self._assert_validly_signed(prepared)
        self.assertNotIn("statusHistory", base)

    def test_each_retry_and_fallback_is_resigned(self):
        statuses = iter([503, 503, 200])
        recorder = self._record(lambda prepared: make_response(next(statuses), {}))
        with mute_logger("odoo.addons.sdlc_magento_connector.services.magento_api"):
            MagentoAPI(self.oauth_instance).get_websites()
        self.assertEqual(len(recorder.requests), 3)
        nonces = set()
        for prepared in recorder.requests:
            parsed, _base = self._assert_validly_signed(prepared)
            nonces.add(parsed["oauth_nonce"])
        self.assertEqual(len(nonces), 3, "every attempt needs a fresh nonce")

        # Store-prefix fallback: each URL is signed for itself.
        recorder.requests.clear()
        self.oauth_instance.store_code = "fr"
        responses = iter([make_response(404, {"message": "no"}), make_response(200, {"items": []})])
        recorder.responder = lambda prepared: next(responses)
        api = MagentoAPI(self.oauth_instance)
        api._request_with_prefix_fallback("get", lambda prefix: f"{prefix}/orders", params={"searchCriteria": ""})
        self.assertEqual([urlsplit(p.url).path for p in recorder.requests], ["/rest/fr/V1/orders", "/rest/V1/orders"])
        for prepared in recorder.requests:
            self._assert_validly_signed(prepared)

    def test_sha1_signature_method(self):
        self.oauth_instance.oauth_signature_method = "sha1"
        recorder = self._record(lambda prepared: make_response(200, []))
        MagentoAPI(self.oauth_instance).get_websites()
        parsed, _base = self._assert_validly_signed(recorder.requests[0], method="HMAC-SHA1")
        self.assertEqual(parsed["oauth_signature_method"], "HMAC-SHA1")

    def test_bearer_still_works(self):
        self.assertEqual(self.instance.auth_method, "bearer", "token-only creates stay on Bearer")
        recorder = self._record(lambda prepared: make_response(200, {"items": []}))
        MagentoAPI(self.instance)._get("/rest/V1/orders", params={"searchCriteria": ""})
        prepared = recorder.requests[0]
        self.assertEqual(prepared.headers["Authorization"], "Bearer test-integration-token")
        self.assertTrue(prepared.url.endswith("/rest/V1/orders?searchCriteria="))

    def test_existing_instances_default_to_bearer_after_upgrade(self):
        Instance = self.env["magento.instance"]
        cr = self.env.cr
        # Upgrade from a version without the column: _auto_init keeps old rows on Bearer.
        sql_compat.execute(cr, "ALTER TABLE magento_instance DROP COLUMN auth_method")
        Instance._keep_existing_instances_on_bearer()  # the step _auto_init runs first
        self.env.invalidate_all()
        self.assertEqual(self.oauth_instance.auth_method, "bearer")
        self.assertEqual(self.instance.auth_method, "bearer")
        # A NULL left behind is repaired on update (init) and read as Bearer meanwhile.
        sql_compat.execute(cr, "UPDATE magento_instance SET auth_method = NULL WHERE id = %s", [self.instance.id])
        self.env.invalidate_all()
        self.assertFalse(self.instance.auth_method)
        recorder = self._record(lambda prepared: make_response(200, []))
        MagentoAPI(self.instance).get_websites()
        self.assertEqual(recorder.requests[0].headers["Authorization"], "Bearer test-integration-token")
        Instance.init()
        self.env.invalidate_all()
        self.assertEqual(self.instance.auth_method, "bearer")

    def test_new_instances_default_to_oauth(self):
        self.assertEqual(self.env["magento.instance"].default_get(["auth_method"])["auth_method"], "oauth1")
        vals = dict(OAUTH_CREDENTIALS)
        vals.pop("auth_method")
        instance = self.create_instance("Form-like create", **vals)
        self.assertEqual(instance.auth_method, "oauth1")

    def test_oauth_requires_all_four_credentials(self):
        for missing in ("consumer_key", "consumer_secret", "access_token_secret"):
            vals = dict(OAUTH_CREDENTIALS, **{missing: False})
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.create_instance("Incomplete %s" % missing, **vals)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.oauth_instance.write({"consumer_secret": "  "})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.instance.write({"auth_method": "oauth1"})
        self.instance.write(dict(OAUTH_CREDENTIALS, access_token="tok"))
        self.assertEqual(self.instance.auth_method, "oauth1")


@tagged("post_install", "-at_install", "magento")
class TestAccessDiagnostics(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oauth_instance = cls.create_instance("OAuth Magento", **OAUTH_CREDENTIALS)
        cls.magento_user = cls.create_user("mg_user_oauth", USER_GROUP)

    def _record(self, responder):
        recorder = _SendRecorder(responder)
        self.startPatcher(patch.object(
            requests.sessions.Session, "send",
            lambda session, prepared, **kwargs: recorder(session, prepared, **kwargs)))
        return recorder

    def test_diagnostics_missing_resources_and_msi(self):
        def responder(prepared):
            path = urlsplit(prepared.url).path
            if path.endswith("/products"):
                return _magento_error(403, "The consumer isn't authorized to access %resources.",
                                      {"resources": "Magento_Catalog::products"})
            if path.endswith("/orders"):
                return _magento_error(401, "The consumer isn't authorized to access %resources.",
                                      {"resources": "Magento_Sales::actions_view"})
            if path.endswith("/inventory/sources"):
                return _magento_error(404, "Request does not match any route.")
            return make_response(200, {"items": []})

        recorder = self._record(responder)
        self.oauth_instance.action_diagnose_access()
        self.assertEqual(len(recorder.requests), 9, "one request per probe, no retries")
        html = str(self.oauth_instance.access_diagnostics)
        self.assertIn("Magento_Catalog::products", html)
        self.assertIn("Magento_Sales::actions_view", html, "the resource Magento names wins")
        self.assertEqual(html.count("text-bg-danger"), 2)
        self.assertEqual(html.count("text-bg-warning"), 1)
        self.assertIn("Not installed", html)
        self.assertEqual(html.count("text-bg-success"), 6)
        self.assertNotIn("2.4.4", html, "OAuth instances never get the Bearer hint")
        self.assertEqual(self.oauth_instance.state, "error")
        self.assertIn("Magento_Catalog::products", self.oauth_instance.last_connection_message)
        self.assertIn("Magento_Sales::actions_view", self.oauth_instance.last_connection_message)
        self.assertNotIn("InventoryApi", self.oauth_instance.last_connection_message)

    def test_diagnostics_all_granted_and_msi_absent_is_connected(self):
        def responder(prepared):
            if urlsplit(prepared.url).path.endswith("/inventory/sources"):
                return _magento_error(404, "Request does not match any route.")
            return make_response(200, {"items": []})

        self._record(responder)
        self.oauth_instance.action_diagnose_access()
        self.assertEqual(self.oauth_instance.state, "connected")
        self.assertIn("Not installed", str(self.oauth_instance.access_diagnostics))

    def test_diagnostics_bearer_244_hint(self):
        self._record(lambda prepared: _magento_error(
            401, "The consumer isn't authorized to access %resources.", {"resources": "Magento_Backend::store"}))
        self.instance.action_diagnose_access()
        html = str(self.instance.access_diagnostics)
        self.assertIn("2.4.4", html)
        self.assertIn("OAuth 1.0a", html)
        self.assertEqual(self.instance.state, "error")
        self.assertIn("2.4.4", self.instance.last_connection_message)

    def test_diagnostics_unreachable(self):
        self.oauth_instance.action_diagnose_access()  # common.py blocks every HTTP send
        self.assertEqual(len(self.http_attempts), 1, "an unreachable host stops after one attempt")
        self.assertEqual(self.oauth_instance.state, "error")
        self.assertIn("connection failed", self.oauth_instance.last_connection_message)
        self.assertIn("alert-danger", str(self.oauth_instance.access_diagnostics))

    def test_user_cannot_read_secrets_or_diagnose(self):
        instance = self.oauth_instance.with_user(self.magento_user)
        instance.read(["name", "auth_method", "access_diagnostics"])
        for field_name in ("consumer_key", "consumer_secret", "access_token_secret", "access_token",
                           "oauth_signature_method"):
            with self.assertRaises(AccessError, msg=field_name):
                instance.read([field_name])
        with self.assertRaises(AccessError), mute_logger("odoo.models"):
            instance.action_diagnose_access()
        # A sync started by a Magento user still signs (secrets read with sudo).
        recorder = self._record(lambda prepared: make_response(200, []))
        MagentoAPI(instance).get_websites()
        self.assertTrue(recorder.requests[0].headers["Authorization"].startswith("OAuth "))
