"""Phase 3.3: real-time push receiver (signed webhooks from the SDLC_OdooConnector module)."""
import json
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import SUPERUSER_ID, fields
from odoo import http as odoo_http
from odoo.http import router as odoo_http_router  # Odoo 20: db_filter lives here
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.controllers import webhook as webhook_controller
from odoo.addons.sdlc_magento_connector.models import magento_webhook
from odoo.addons.sdlc_magento_connector.models.magento_webhook import sign_body

from .common import MagentoTestCommon, build_product

USER_GROUP = "sdlc_magento_connector.group_magento_user"
MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
WEBHOOK_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_webhook"


def _event(entity, entity_id=None, event="saved", **extra):
    vals = {"id": uuid.uuid4().hex, "entity": entity, "event": event,
            "occurred_at": "2026-10-13T10:00:00Z"}
    if entity_id is not None:
        vals["entity_id"] = str(entity_id)
    vals.update(extra)
    return vals


@tagged("post_install", "-at_install", "magento")
class TestWebhookReceiver(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance.write({"webhook_enabled": True})
        cls.secret = cls.instance.sudo().webhook_secret
        cls.key = cls.instance.webhook_uuid
        cls.Instance = cls.env["magento.instance"]
        cls.Job = cls.env["magento.job"].sudo()
        cls.Event = cls.env["magento.webhook.event"].sudo()

    # ------------------------------------------------------------------
    def _request(self, events, *, secret=None, timestamp=None, delivery=None, key=None,
                 body_timestamp=None):
        timestamp = int(time.time()) if timestamp is None else timestamp
        delivery = delivery or uuid.uuid4().hex
        body = json.dumps({
            "delivery_id": delivery,
            "timestamp": timestamp if body_timestamp is None else body_timestamp,
            "events": events,
        }).encode()
        headers = {
            "X-SDLC-Signature": sign_body(self.secret if secret is None else secret, body),
            "X-SDLC-Timestamp": str(timestamp),
            "X-SDLC-Event-Id": delivery,
        }
        return body, headers, key or self.key

    def _post(self, events, **kwargs):
        body, headers, key = self._request(events, **kwargs)
        with mute_logger(WEBHOOK_LOGGER):
            return self.Instance._magento_webhook_handle(key, body, headers)

    def _rt_jobs(self):
        return self.Job.search([("identity_key", "=like", "rt-%s-%%" % self.instance.id)])

    # ------------------------------------------------------------------
    def test_keys_generated_unique_and_url(self):
        other = self.create_instance("Second Magento")
        self.assertRegex(self.key, r"^[0-9a-f]{32}$")
        self.assertNotEqual(other.webhook_uuid, self.key)
        self.assertNotEqual(other.sudo().webhook_secret, self.secret)
        self.assertGreaterEqual(len(self.secret), 32)
        self.instance.webhook_base_url = "https://erp.example.test/"
        self.assertEqual(self.instance.webhook_url,
                         "https://erp.example.test/magento/webhook/%s" % self.key)
        # A copied instance never shares the keys.
        self.assertFalse(self.instance.copy_data()[0].get("webhook_uuid"))

    def test_valid_signature_enqueues_deduplicated_jobs(self):
        status, data = self._post([
            _event("order", 101, increment_id="000000101"),
            _event("invoice", 31, order_id=101),
            _event("shipment", 41, order_id=101),
            _event("product", 12, sku="24-MB04"),
            _event("customer", 7),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "ok", "accepted": 5, "duplicates": 0, "ignored": 0, "jobs": 3})
        jobs = self._rt_jobs()
        self.assertEqual(len(jobs), 3, "invoice and shipment join the order's job")
        by_key = {job.identity_key: job for job in jobs}
        order_job = by_key["rt-%s-order-101" % self.instance.id]
        self.assertEqual(order_job.method_name, "_magento_resync_one")
        self.assertEqual(order_job.args, ["order", "101"])
        self.assertEqual(order_job.res_ids, [self.instance.id])
        self.assertEqual(order_job.channel, "magento.%s.orders" % self.instance.id)
        self.assertEqual(order_job.user_id.id, SUPERUSER_ID, "runs as the technical user")
        self.assertEqual(order_job.priority, magento_webhook.RT_JOB_PRIORITY)
        self.assertEqual(by_key["rt-%s-product-24-MB04" % self.instance.id].args, ["product", "24-MB04"])
        self.assertEqual(by_key["rt-%s-customer-7" % self.instance.id].args, ["customer", "7"])
        events = self.Event.search([("instance_id", "=", self.instance.id)])
        self.assertEqual(len(events), 5)
        self.assertEqual(set(events.mapped("state")), {"queued"})
        self.assertEqual(events.filtered(lambda e: e.entity == "invoice").job_id, order_job)
        self.assertTrue(self.instance.last_webhook_at)
        self.assertEqual(self.instance.webhook_health, "healthy")
        self.assertEqual(self.instance.webhook_event_count, 5)

    def test_response_never_carries_data(self):
        status, data = self._post([_event("customer", 9)])
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"status", "accepted", "duplicates", "ignored", "jobs"})
        self.assertTrue(all(isinstance(v, (int, str)) for v in data.values()))

    def test_bad_signature_401(self):
        status, data = self._post([_event("order", 102)], secret="not-the-secret")
        self.assertEqual(status, 401)
        self.assertEqual(data, {"error": "invalid signature"})
        self.assertFalse(self._rt_jobs())
        rejected = self.Event.search([("instance_id", "=", self.instance.id), ("state", "=", "rejected")])
        self.assertEqual(len(rejected), 1)
        self.assertIn("401", rejected.note)
        self.assertEqual(self.instance.webhook_health, "rejecting")
        # Throttled: a flood of bad requests stores one row a minute.
        for _i in range(5):
            self._post([_event("order", 102)], secret="nope")
        self.assertEqual(self.Event.search_count([("state", "=", "rejected")]), 1)
        # Missing signature, tampered body.
        body, headers, key = self._request([_event("order", 103)])
        headers.pop("X-SDLC-Signature")
        with mute_logger(WEBHOOK_LOGGER):
            self.assertEqual(self.Instance._magento_webhook_handle(key, body, headers)[0], 401)
            body, headers, key = self._request([_event("order", 103)])
            tampered = body.replace(b"103", b"104")
            self.assertEqual(self.Instance._magento_webhook_handle(key, tampered, headers)[0], 401)
        self.assertFalse(self._rt_jobs())

    def test_old_timestamp_rejected(self):
        old = int(time.time()) - magento_webhook.MAX_SKEW_SECONDS - 60
        status, data = self._post([_event("order", 105)], timestamp=old)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "stale timestamp")
        future = int(time.time()) + magento_webhook.MAX_SKEW_SECONDS + 60
        self.assertEqual(self._post([_event("order", 105)], timestamp=future)[0], 401)
        # Within the window: accepted.
        self.assertEqual(self._post([_event("order", 105)], timestamp=int(time.time()) - 120)[0], 200)

    def test_signed_body_must_repeat_headers(self):
        """The signature covers the body; a header timestamp that differs from the signed
        one (a replay with a fresh header) is rejected."""
        status, data = self._post([_event("order", 106)], body_timestamp=int(time.time()) - 3600)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "headers do not match the signed body")
        self.assertFalse(self._rt_jobs())

    def test_replayed_event_id_ignored(self):
        events = [_event("order", 107), _event("customer", 8)]
        body, headers, key = self._request(events)
        status, first = self.Instance._magento_webhook_handle(key, body, headers)
        self.assertEqual((status, first["accepted"]), (200, 2))
        jobs = self._rt_jobs()
        jobs.write({"state": "done"})  # even once processed, a replay queues nothing
        status, second = self.Instance._magento_webhook_handle(key, body, headers)
        self.assertEqual(status, 200)
        self.assertEqual((second["accepted"], second["duplicates"]), (0, 2))
        self.assertEqual(self._rt_jobs(), jobs)
        # Same event id inside one batch, and in a different delivery.
        again = dict(events[0])
        status, third = self._post([again, again])
        self.assertEqual((third["accepted"], third["duplicates"]), (0, 2))
        self.assertEqual(self.Event.search_count([("instance_id", "=", self.instance.id)]), 2)

    def test_unknown_uuid_404(self):
        for key in (uuid.uuid4().hex, "not-a-uuid", "", self.key.upper() + "0"):
            body, headers, _key = self._request([_event("order", 108)])
            status, data = self.Instance._magento_webhook_handle(key, body, headers)
            self.assertEqual((status, data), (404, {"error": "not found"}), key)
        self.assertFalse(self._rt_jobs())
        self.assertFalse(self.Event.search([]))

    def test_disabled_instance_404(self):
        self.instance.webhook_enabled = False
        self.assertEqual(self._post([_event("order", 109)])[0], 404)
        self.instance.webhook_enabled = True
        self.instance.active = False
        self.assertEqual(self._post([_event("order", 109)])[0], 404)
        self.assertFalse(self._rt_jobs())
        self.assertFalse(self.Event.search([]), "nothing is stored for a disabled instance")

    def test_burst_of_50_events_for_one_order_gives_one_job(self):
        events = [_event("order", 555, event="saved") for _i in range(50)]
        status, data = self._post(events[:20])
        self.assertEqual(status, 200)
        status, data = self._post(events[20:])  # a second delivery joins the same job
        self.assertEqual(status, 200)
        jobs = self._rt_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.identity_key, "rt-%s-order-555" % self.instance.id)
        self.assertEqual(self.Event.search_count([("job_id", "=", jobs.id)]), 50)

    def test_event_while_job_runs_queues_one_follow_up(self):
        self._post([_event("order", 556)])
        running = self._rt_jobs()
        running.write({"state": "started", "date_started": fields.Datetime.now()})
        for _i in range(3):
            self._post([_event("order", 556)])
        jobs = self._rt_jobs()
        self.assertEqual(len(jobs), 2)
        follow_up = jobs - running
        self.assertEqual(follow_up.identity_key, "rt-%s-order-556-next" % self.instance.id)
        self.assertEqual(follow_up.state, "pending")

    def test_stock_event_refreshes_sku_stock(self):
        product = self.create_product("WH-STOCK-1")
        product_map = self.create_map(product, 77, quantity=1.0, stock_status="out_of_stock")
        status, data = self._post([_event("stock", 77, sku="WH-STOCK-1"),
                                   _event("stock", 77, sku="WH-STOCK-1")])
        self.assertEqual((status, data["jobs"]), (200, 1))
        job = self._rt_jobs()
        self.assertEqual(job.method_name, "_magento_webhook_refresh_stock")
        self.assertEqual(job.args, ["WH-STOCK-1"])
        self.assertEqual(job.channel, "stock-%s" % self.instance.id)
        self.assertEqual(job.identity_key, "rt-%s-stock-WH-STOCK-1" % self.instance.id)
        fetch = self.patch_api("_request_with_prefix_fallback",
                               return_value={"qty": 7, "is_in_stock": True, "backorders": 0})
        self.assertTrue(job._run_job())
        self.assertEqual(job.state, "done", job.last_error)
        endpoint = fetch.call_args.args[2]("/rest/V1")
        self.assertEqual(endpoint, "/rest/V1/stockItems/WH-STOCK-1")
        self.assertEqual(product_map.quantity, 7.0)
        self.assertEqual(product_map.stock_status, "in_stock")
        # An unmapped SKU makes no Magento call.
        fetch.reset_mock()
        result = self.instance._magento_webhook_refresh_stock("NOT-MAPPED")
        self.assertIn("not mapped", result)
        fetch.assert_not_called()

    def test_product_event_job_updates_odoo_product(self):
        """The queued job re-fetches the product through the REST API (payload untrusted)."""
        self._post([_event("product", 12, sku="RT-SKU", name="Injected Name")])
        job = self._rt_jobs()
        get_product = self.patch_api("get_product", return_value=build_product(12, "RT-SKU", "Fresh Name"))
        self.patch_api("get_product_media", return_value={})
        self.assertTrue(job._run_job(), job.last_error)
        self.assertEqual(get_product.call_args.args[1], "RT-SKU")
        product_map = self.env["magento.product.map"].search(
            [("instance_id", "=", self.instance.id), ("sku", "=", "RT-SKU")])
        self.assertEqual(product_map.name, "Fresh Name")

    def test_ignored_events(self):
        status, data = self._post([
            _event("product", 13, event="deleted", sku="GONE"),
            _event("category", 3),
            _event("order"),  # no id
            {"entity": "order", "entity_id": "1"},  # no event id: cannot be deduplicated
            "garbage",
        ])
        self.assertEqual(status, 200)
        self.assertEqual((data["accepted"], data["ignored"], data["jobs"]), (0, 5, 0))
        self.assertFalse(self._rt_jobs())
        events = self.Event.search([("instance_id", "=", self.instance.id)])
        self.assertEqual(set(events.mapped("state")), {"ignored"})
        self.assertEqual(len(events), 3)

    def test_malformed_requests(self):
        body = b"not json"
        headers = {"X-SDLC-Signature": sign_body(self.secret, body),
                   "X-SDLC-Timestamp": str(int(time.time()))}
        self.assertEqual(self.Instance._magento_webhook_handle(self.key, body, headers)[0], 400)
        status, _data = self._post({"not": "a list"})
        self.assertEqual(status, 400)
        too_many = [_event("customer", i) for i in range(magento_webhook.MAX_EVENTS + 1)]
        self.assertEqual(self._post(too_many)[0], 413)
        big = b"x" * (magento_webhook.MAX_BODY_BYTES + 1)
        self.assertEqual(self.Instance._magento_webhook_handle(self.key, big, {})[0], 413)

    def test_secret_not_readable_by_non_managers(self):
        user = self.create_user("wh_user", USER_GROUP)
        manager = self.create_user("wh_manager", MANAGER_GROUP)
        as_user = self.instance.with_user(user)
        self.assertNotIn("webhook_secret", as_user.fields_get())
        with self.assertRaises(AccessError):
            as_user.read(["webhook_secret"])
        with self.assertRaises(AccessError):
            as_user.action_webhook_regenerate_secret()
        with self.assertRaises(AccessError):
            as_user.action_webhook_regenerate_uuid()
        # The URL and status are visible to users; the secret is not.
        self.assertTrue(as_user.webhook_url)
        self.assertEqual(self.instance.with_user(manager).webhook_secret, self.secret)
        self.instance.with_user(manager).action_webhook_regenerate_secret()
        new_secret = self.instance.sudo().webhook_secret
        self.assertNotEqual(new_secret, self.secret)
        # The old secret is now rejected.
        self.assertEqual(self._post([_event("order", 110)])[0], 401)
        self.assertEqual(self._post([_event("order", 110)], secret=new_secret)[0], 200)

    def test_controller_direct_call(self):
        body, headers, key = self._request([_event("order", 111)])
        responses = []
        fake_request = SimpleNamespace(
            db=self.env.cr.dbname,
            env=self.env,
            httprequest=SimpleNamespace(content_length=len(body), headers=headers,
                                        get_data=lambda cache=True: body),
            make_json_response=lambda data, status=200: responses.append((status, data)) or (status, data),
        )
        controller = webhook_controller.MagentoWebhookController()
        endpoint = controller.magento_webhook.original_endpoint
        with patch.object(webhook_controller, "request", fake_request):
            status, data = endpoint(controller, key)
            self.assertEqual(status, 200)
            self.assertEqual(data["accepted"], 1)
            fake_request.httprequest.content_length = magento_webhook.MAX_BODY_BYTES + 1
            self.assertEqual(endpoint(controller, key)[0], 413)
            fake_request.httprequest.content_length = len(body)
            fake_request.db = None
            self.assertEqual(endpoint(controller, key)[0], 404)
        self.assertEqual(len(self._rt_jobs()), 1)
        routing = controller.magento_webhook.original_routing
        self.assertEqual(routing["auth"], "none")
        self.assertFalse(routing["csrf"])
        self.assertFalse(routing["readonly"], "the receiver writes: never a read-only cursor")
        self.assertEqual(routing["methods"], ["POST"])

    def test_reduced_polling_while_healthy(self):
        self.instance.write({"webhook_reduces_polling": True, "webhook_poll_interval_hours": 6})
        # No push yet: polls normally.
        self.assertTrue(self.instance._magento_webhook_poll_due())
        self._post([_event("customer", 1)])
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.webhook_health, "healthy")
        self.assertTrue(self.instance._magento_webhook_poll_due(), "never polled yet")
        with patch.object(type(self.instance), "_magento_enqueue_scheduled_sync", autospec=True) as enqueue, \
                patch.object(type(self.instance), "_magento_sync_in_background", return_value=True):
            self.instance._run_scheduled_sync("cron_auto_scope")
            self.assertEqual(enqueue.call_count, 1)
            self.assertTrue(self.instance.webhook_last_poll_at)
            self.instance._run_scheduled_sync("cron_auto_scope")
            self.assertEqual(enqueue.call_count, 1, "skipped: pushes healthy, last poll recent")
            self.instance.webhook_last_poll_at = fields.Datetime.now() - timedelta(hours=7)
            self.instance._run_scheduled_sync("cron_auto_scope")
            self.assertEqual(enqueue.call_count, 2)
            # Without the option, every run polls.
            self.instance.webhook_reduces_polling = False
            self.instance._run_scheduled_sync("cron_auto_scope")
            self.assertEqual(enqueue.call_count, 3)

    def test_cleanup_removes_old_events(self):
        self._post([_event("customer", 2), _event("customer", 3)])
        events = self.Event.search([("instance_id", "=", self.instance.id)])
        old = events[:1]
        self.env.cr.execute("UPDATE magento_webhook_event SET received_at = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(days=magento_webhook.EVENT_RETENTION_DAYS + 1),
                             old.id))
        self.env.invalidate_all()
        totals = self.env["magento.instance"]._cron_cleanup_logs()
        self.assertEqual(totals["webhook_events"], 1)
        self.assertFalse(old.exists())
        self.assertEqual(len(events.exists()), 1)

    def test_signature_helper_matches_magento_format(self):
        """Same value as the PHP module: 'sha256=' . hash_hmac('sha256', $body, $secret)."""
        self.assertEqual(
            sign_body("secret", b'{"events":[]}'),
            "sha256=a642b59553c93e227ec0f2f38910fbf71231a2197c00899833c00478cec86f34",
        )

    def test_creditmemo_return_to_stock_flags_reach_the_import(self):
        """Magento's REST API does not expose which refunded items went back to stock; the
        companion module pushes the order item ids and the credit memo import receives them."""
        status, data = self._post([
            _event("creditmemo", 5, order_id=900, return_to_stock_items=[11, "12", "x", -1, 11]),
            _event("creditmemo", 6, order_id=900, return_to_stock_items=[]),
        ])
        self.assertEqual((status, data["jobs"]), (200, 1))
        event = self.Event.search([("magento_id", "=", "5")])
        self.assertEqual(event.return_to_stock_items, "11,12")
        self.assertEqual(self._rt_jobs().args, ["order", "900"])
        self.assertEqual(self.instance._magento_webhook_restock_flags(["900"]), {"5": [11, 12]})

        partner = self.env["res.partner"].create({"name": "WH Return Customer"})
        order = self.env["sale.order"].with_context(**self.no_push_ctx()).create({
            "partner_id": partner.id, "magento_instance_id": self.instance.id,
            "magento_order_id": "900"})
        memos = [
            {"entity_id": 5, "items": [{"order_item_id": 11, "qty": 1}]},
            {"entity_id": 6, "items": [{"order_item_id": 13, "qty": 1}]},
            {"entity_id": 7, "items": [{"order_item_id": 14}],
             "extension_attributes": {"return_to_stock_items": [14]}},
        ]
        self.patch_api("get_credit_memos", return_value=memos)
        seen = []
        with patch.object(type(order), "_magento_create_credit_memo_from_magento", autospec=True,
                          side_effect=lambda so, data: seen.append(data) or False):
            order._magento_sync_credit_memos()
        self.assertEqual([memo.get("return_to_stock_items") for memo in seen], [[11, 12], None, None])
        self.assertEqual(seen[2]["extension_attributes"], {"return_to_stock_items": [14]})

    def test_magento_api_untouched_by_receiver(self):
        """Receiving a push makes no HTTP call; only the job re-fetches."""
        self._post([_event("order", 112), _event("product", 1, sku="X")])
        self.assertEqual(self.http_attempts, [])


@tagged("post_install", "-at_install", "magento")
class TestWebhookRoute(HttpCase):
    """The real route through Odoo's HTTP stack: auth none, no CSRF, JSON answers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["magento.instance"].create({
            "name": "Route Magento", "base_url": "https://magento.example.test",
            "access_token": "route-token", "webhook_enabled": True,
        })

    def setUp(self):
        super().setUp()
        dbname = self.env.cr.dbname
        # The server's dbfilter may name another database: accept the test one.
        self.patch(odoo_http_router, "db_filter", lambda dbs, host=None: [db for db in dbs if db == dbname])
        self.headers_db = {"X-Odoo-Database": dbname}

    def _post(self, key, body, headers):
        with mute_logger(WEBHOOK_LOGGER):
            return self.url_open("/magento/webhook/%s" % key, data=body,
                                 headers=dict(self.headers_db, **headers))

    def test_route(self):
        key, secret = self.instance.webhook_uuid, self.instance.sudo().webhook_secret
        timestamp = int(time.time())
        body = json.dumps({"delivery_id": "d1", "timestamp": timestamp,
                           "events": [_event("order", 9001)]}).encode()
        headers = {"Content-Type": "application/json", "X-SDLC-Timestamp": str(timestamp),
                   "X-SDLC-Event-Id": "d1", "X-SDLC-Signature": sign_body(secret, body)}
        response = self._post(key, body, headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accepted"], 1)
        self.assertTrue(self.env["magento.job"].sudo().search(
            [("identity_key", "=", "rt-%s-order-9001" % self.instance.id)]))
        bad = dict(headers, **{"X-SDLC-Signature": sign_body("wrong", body)})
        response = self._post(key, body, bad)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "invalid signature"})
        response = self._post(uuid.uuid4().hex, body, headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "not found"})
