"""Phase 1.4: paginated searchCriteria, updated_at watermarks, single-record resync."""
import http.client
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MagentoTestCommon, build_item, build_order, build_product, make_response

SYNC_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_instance"
API_LOGGER = "odoo.addons.sdlc_magento_connector.services.magento_api"


def _dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


@tagged("post_install", "-at_install", "magento")
class TestIterSearch(MagentoTestCommon):
    """MagentoAPI.iter_search: pagination, stop conditions and searchCriteria params."""

    def _paged_request(self, pages, total_count=None):
        """Fake ``_request``: page N of ``pages``; past the end, Magento echoes the last page."""
        calls = []

        def _request(api, method, endpoint, *, params=None, payload=None):
            calls.append(dict(params or {}))
            page = int(params["searchCriteria[currentPage]"])
            items = pages[min(page, len(pages)) - 1]
            body = {"items": items}
            if total_count is not None:
                body["total_count"] = total_count
            return make_response(200, body)

        self.patch_api("_request", side_effect=_request)
        return calls

    def test_three_pages_then_echoed_last_page(self):
        pages = [
            [{"entity_id": 1}, {"entity_id": 2}],
            [{"entity_id": 3}, {"entity_id": 4}],
            [{"entity_id": 5}, {"entity_id": 6}],
        ]
        calls = self._paged_request(pages)  # no total_count: only the echo ends it
        stats = {}
        items = list(MagentoAPI(self.instance).iter_search(
            "/rest/V1/orders", page_size=2, stats=stats))
        self.assertEqual([i["entity_id"] for i in items], [1, 2, 3, 4, 5, 6])
        self.assertEqual([c["searchCriteria[currentPage]"] for c in calls], [1, 2, 3, 4],
                         "Page 4 echoes page 3 and must end the listing")
        self.assertEqual(stats["pages"], 4)
        self.assertEqual(stats["items"], 6)

    def test_stops_at_total_count_and_on_short_page(self):
        pages = [[{"entity_id": 1}, {"entity_id": 2}], [{"entity_id": 3}, {"entity_id": 4}],
                 [{"entity_id": 5}, {"entity_id": 6}]]
        calls = self._paged_request(pages, total_count=6)
        items = list(MagentoAPI(self.instance).iter_search("/rest/V1/orders", page_size=2))
        self.assertEqual(len(items), 6)
        self.assertEqual(len(calls), 3)

        calls = self._paged_request([[{"entity_id": 1}, {"entity_id": 2}], [{"entity_id": 3}]])
        items = list(MagentoAPI(self.instance).iter_search("/rest/V1/orders", page_size=2))
        self.assertEqual(len(items), 3)
        self.assertEqual(len(calls), 2, "A short page is the last page")

    def test_partial_overlap_is_deduplicated(self):
        # A record moving between pages must not be yielded twice.
        pages = [[{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 3}], [{"id": 4}]]
        self._paged_request(pages)
        items = list(MagentoAPI(self.instance).iter_search(
            "/rest/V1/customers/search", page_size=2, id_key="id"))
        self.assertEqual([i["id"] for i in items], [1, 2, 3, 4])

    def test_page_size_is_clamped(self):
        self.assertEqual(MagentoAPI.clamp_page_size(1000), 300)
        self.assertEqual(MagentoAPI.clamp_page_size(-5), 1)
        self.assertEqual(MagentoAPI.clamp_page_size(None), 100)
        calls = self._paged_request([[]])
        list(MagentoAPI(self.instance).iter_search("/rest/V1/orders", page_size=5000))
        self.assertEqual(calls[0]["searchCriteria[pageSize]"], 300)

    def test_search_criteria_params_on_the_wire(self):
        sent = []

        def _send(session, prepared, **kwargs):
            sent.append(prepared)
            return make_response(200, {"items": [], "total_count": 0}, url=prepared.url)

        self.startPatcher(patch.object(requests.sessions.Session, "send", _send))
        api = MagentoAPI(self.instance)
        self.assertEqual(api.get_orders(_dt("2026-10-01 09:50:00"), page_size=50), [])
        self.assertEqual(len(sent), 1)
        url = urlsplit(sent[0].url)
        self.assertEqual(url.path, "/rest/V1/orders")
        params = dict(parse_qsl(url.query))
        self.assertEqual(params["searchCriteria[filter_groups][0][filters][0][field]"], "updated_at")
        self.assertEqual(params["searchCriteria[filter_groups][0][filters][0][value]"], "2026-10-01 09:50:00")
        self.assertEqual(params["searchCriteria[filter_groups][0][filters][0][condition_type]"], "gteq")
        self.assertEqual(params["searchCriteria[sortOrders][0][field]"], "updated_at")
        self.assertEqual(params["searchCriteria[sortOrders][0][direction]"], "ASC")
        self.assertEqual(params["searchCriteria[sortOrders][1][field]"], "entity_id")
        self.assertEqual(params["searchCriteria[sortOrders][1][direction]"], "ASC")
        self.assertEqual(params["searchCriteria[pageSize]"], "50")
        self.assertEqual(params["searchCriteria[currentPage]"], "1")
        self.assertNotIn("created_at", sent[0].url, "Never a creation-date cut-off")

        # Products page through the store-scoped endpoint, without a filter on a full read.
        sent.clear()
        api.get_products()
        params = dict(parse_qsl(urlsplit(sent[0].url).query))
        self.assertIn("/V1/products", sent[0].url)
        self.assertNotIn("searchCriteria[filter_groups][0][filters][0][field]", params)
        self.assertEqual(params["searchCriteria[pageSize]"], "100")


@tagged("post_install", "-at_install", "magento")
class TestPostRetrySafety(MagentoTestCommon):
    """A POST is retried only when the request certainly never reached Magento."""

    def _send_raising(self, exc):
        sent = []

        def _send(session, prepared, **kwargs):
            sent.append(prepared.method)
            raise exc

        self.startPatcher(patch.object(requests.sessions.Session, "send", _send))
        return sent

    def test_post_not_retried_after_connection_aborted(self):
        aborted = requests.exceptions.ConnectionError(
            ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed")))
        sent = self._send_raising(aborted)
        with self.assertRaises(requests.exceptions.ConnectionError):
            MagentoAPI(self.instance).create_invoice(5, {"capture": True})
        self.assertEqual(sent, ["POST"], "A POST that may have been applied must not be re-sent")

    def test_post_retried_when_connection_never_opened(self):
        refused = requests.exceptions.ConnectionError(MaxRetryError(
            None, "/rest/V1/order/5/invoice", NewConnectionError(None, "Connection refused")))
        sent = self._send_raising(refused)
        with mute_logger(API_LOGGER), self.assertRaises(requests.exceptions.ConnectionError):
            MagentoAPI(self.instance).create_invoice(5, {"capture": True})
        self.assertEqual(len(sent), MagentoAPI.MAX_RETRIES + 1)

    def test_post_retried_on_connect_timeout(self):
        sent = self._send_raising(requests.exceptions.ConnectTimeout("connect timed out"))
        with mute_logger(API_LOGGER), self.assertRaises(requests.exceptions.ConnectTimeout):
            MagentoAPI(self.instance).create_shipment(5, {})
        self.assertEqual(len(sent), MagentoAPI.MAX_RETRIES + 1)

    def test_get_still_retried_after_connection_aborted(self):
        aborted = requests.exceptions.ConnectionError(
            ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed")))
        sent = self._send_raising(aborted)
        with mute_logger(API_LOGGER), self.assertRaises(requests.exceptions.ConnectionError):
            MagentoAPI(self.instance).get_order(5)
        self.assertEqual(len(sent), MagentoAPI.MAX_RETRIES + 1)


@tagged("post_install", "-at_install", "magento")
class TestWatermarks(MagentoTestCommon):
    """updated_at watermarks: advance on success, held back by failures, overlap, full sync."""

    def _patch_catalog(self, products, broken_skus=()):
        by_sku = {p["sku"]: p for p in products}
        calls = []

        def _get_products(api, updated_from=None, **kwargs):
            calls.append({"updated_from": updated_from, **kwargs})
            return list(products)

        def _get_product(api, sku, *args, **kwargs):
            if sku in broken_skus:
                raise RuntimeError("Simulated processing failure for %s" % sku)
            return by_sku[sku]

        self.patch_api("get_products", side_effect=_get_products)
        self.patch_api("get_product", side_effect=_get_product)
        self.patch_api("get_product_media", return_value={})
        return calls

    @staticmethod
    def _product(magento_id, sku, updated_at):
        product = build_product(magento_id, sku)
        product["updated_at"] = updated_at
        return product

    def test_watermark_advances_to_max_success(self):
        self.assertFalse(self.instance.products_sync_watermark)
        calls = self._patch_catalog([
            self._product(1, "WM-A", "2026-10-01 10:00:00"),
            self._product(2, "WM-B", "2026-10-01 12:00:00"),
            self._product(3, "WM-C", "2026-10-01 11:00:00"),
        ])
        counts = self.instance.sync_products()
        self.assertEqual(counts["errors"], 0)
        self.assertIsNone(calls[0]["updated_from"], "No watermark yet: full fetch")
        self.assertEqual(calls[0]["page_size"], 100)
        self.assertEqual(self.instance.products_sync_watermark, _dt("2026-10-01 12:00:00"))
        self.assertEqual(counts["watermark_after"], "2026-10-01 12:00:00")

    def test_overlap_is_applied(self):
        self.instance.write({
            "orders_sync_watermark": _dt("2026-10-01 12:00:00"),
            "incremental_overlap_minutes": 30,
            "sync_page_size": 250,
        })
        calls = []

        def _get_orders(api, updated_from=None, **kwargs):
            calls.append({"updated_from": updated_from, **kwargs})
            return []

        self.patch_api("get_orders", side_effect=_get_orders)
        self.instance.sync_orders()
        self.assertEqual(calls[0]["updated_from"], _dt("2026-10-01 11:30:00"))
        self.assertEqual(calls[0]["page_size"], 250)
        self.assertEqual(self.instance.orders_sync_watermark, _dt("2026-10-01 12:00:00"),
                         "Nothing processed: the watermark stays")

    def test_failed_record_holds_watermark_and_is_refetched(self):
        catalog = [
            self._product(1, "WM-OK1", "2026-10-01 10:00:00"),
            self._product(2, "WM-BROKEN", "2026-10-01 11:00:00"),
            self._product(3, "WM-OK2", "2026-10-01 12:00:00"),
        ]
        self._patch_catalog(catalog, broken_skus={"WM-BROKEN"})
        with mute_logger(SYNC_LOGGER):
            counts = self.instance.sync_products()
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(self.instance.products_sync_watermark, _dt("2026-10-01 10:59:59"),
                         "The watermark stops just before the oldest failed record")

        # Next run: the failed record is inside the fetch window and now succeeds.
        calls = self._patch_catalog(catalog)
        counts = self.instance.sync_products()
        self.assertEqual(calls[0]["updated_from"], _dt("2026-10-01 10:49:59"))
        self.assertLessEqual(calls[0]["updated_from"], _dt("2026-10-01 11:00:00"))
        self.assertEqual(counts["errors"], 0)
        self.assertTrue(self.env["magento.product.map"].search(
            [("instance_id", "=", self.instance.id), ("sku", "=", "WM-BROKEN")]))
        self.assertEqual(self.instance.products_sync_watermark, _dt("2026-10-01 12:00:00"))

    def test_full_sync_context_ignores_watermark(self):
        self.instance.write({"customers_sync_watermark": _dt("2026-10-01 12:00:00")})
        calls = []

        def _get_customers(api, updated_from=None, **kwargs):
            calls.append(updated_from)
            return [{"id": 77, "email": "wm@example.com", "firstname": "W", "lastname": "M",
                     "updated_at": "2026-10-02 08:00:00"}]

        self.patch_api("get_customers", side_effect=_get_customers)
        self.instance.with_context(magento_full_sync=True).sync_customers()
        self.assertEqual(calls, [None])
        self.assertEqual(self.instance.customers_sync_watermark, _dt("2026-10-02 08:00:00"))

    def test_reset_watermarks(self):
        self.instance.write({
            "orders_sync_watermark": _dt("2026-10-01 12:00:00"),
            "products_sync_watermark": _dt("2026-10-01 12:00:00"),
            "customers_sync_watermark": _dt("2026-10-01 12:00:00"),
        })
        self.instance.action_reset_watermarks()
        self.assertFalse(self.instance.orders_sync_watermark)
        self.assertFalse(self.instance.products_sync_watermark)
        self.assertFalse(self.instance.customers_sync_watermark)

    def test_report_notes_paging_and_watermark(self):
        self._patch_catalog([self._product(1, "WM-R", "2026-10-01 10:00:00")])
        self.instance.action_sync_products()
        report = self.env["magento.sync.report"].search(
            [("instance_id", "=", self.instance.id)], order="id desc", limit=1)
        self.assertIn("pages fetched", report.notes)
        self.assertIn("records seen: 1", report.notes)
        self.assertIn("watermark none -> 2026-10-01 10:00:00", report.notes)

    def test_page_size_constraint(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.instance.write({"sync_page_size": 301})


@tagged("post_install", "-at_install", "magento")
class TestResyncOne(MagentoTestCommon):
    """_magento_resync_one and the _magento_on_record_result hook."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env["product.product"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.env["magento.product.map"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True).create({
                "instance_id": cls.instance.id, "magento_id": "1", "sku": "24-MB01",
                "name": cls.bag.name, "odoo_product_id": cls.bag.id,
            })

    def setUp(self):
        super().setUp()
        self.patch_order_side_calls()
        self.hook = self.startPatcher(patch.object(
            type(self.env["magento.instance"]), "_magento_on_record_result", autospec=True))

    def test_resync_one_order(self):
        data = build_order(5001, [build_item(50011, "24-MB01", qty=1.0, price=34.0, product_id=1)])
        get_order = self.patch_api("get_order", return_value=data)
        counts = self.instance._magento_resync_one("order", "5001")
        self.assertEqual(get_order.call_args.args[1], "5001")
        self.assertEqual(counts["created"], 1)
        order = self.magento_orders(5001)
        self.assertEqual(len(order), 1)
        self.hook.assert_called_once()
        args, kwargs = self.hook.call_args
        self.assertEqual(args[1:4], ("order", "5001", True))
        self.assertEqual(kwargs["record"], order)
        # Idempotent: a second resync updates the same order.
        counts = self.instance._magento_resync_one("order", 5001)
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(len(self.magento_orders(5001)), 1)

    def test_resync_one_product_by_sku(self):
        data = build_product(9, "RS-SKU", "Resync Bag")
        get_product = self.patch_api("get_product", return_value=data)
        self.patch_api("get_product_media", return_value={})
        counts = self.instance._magento_resync_one("product", "RS-SKU")
        self.assertEqual(get_product.call_args.args[1], "RS-SKU")
        self.assertEqual(counts["created"], 1)
        mapping = self.env["magento.product.map"].search(
            [("instance_id", "=", self.instance.id), ("sku", "=", "RS-SKU")])
        self.assertEqual(mapping.magento_id, "9")
        args, kwargs = self.hook.call_args
        self.assertEqual(args[1:4], ("product", "RS-SKU", True))
        self.assertEqual(kwargs["record"], mapping)

    def test_resync_one_raises_on_failure(self):
        self.patch_api("get_product", side_effect=requests.exceptions.HTTPError("404 not found"))
        with mute_logger(SYNC_LOGGER), self.assertRaises(requests.exceptions.HTTPError):
            self.instance._magento_resync_one("product", "MISSING")
        args, kwargs = self.hook.call_args
        self.assertEqual(args[1:4], ("product", "MISSING", False))

        self.patch_api("get_order", return_value=build_order(
            5002, [build_item(50021, "24-MB01", product_id=1)]))
        self.startPatcher(patch.object(
            type(self.env["magento.instance"]), "_sync_order_record", autospec=True,
            side_effect=RuntimeError("boom")))
        with mute_logger(SYNC_LOGGER), self.assertRaises(RuntimeError):
            self.instance._magento_resync_one("order", "5002")
        args, kwargs = self.hook.call_args
        self.assertEqual(args[1:4], ("order", "5002", False))
        self.assertIsInstance(kwargs["exc"], RuntimeError)

    def test_sync_loop_reports_each_record_to_hook(self):
        self.patch_api("get_customers", return_value=[
            {"id": 31, "email": "a@example.com", "firstname": "A", "lastname": "A",
             "updated_at": "2026-10-01 10:00:00"},
        ])
        self.instance.sync_customers()
        args, kwargs = self.hook.call_args
        self.assertEqual(args[1:4], ("customer", "31", True))
        self.assertTrue(kwargs["record"])
