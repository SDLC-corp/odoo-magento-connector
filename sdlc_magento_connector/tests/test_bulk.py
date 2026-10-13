"""Roadmap 1.9: bulk/async transport, bulk operation polling, bulk stock push and
"Apply to Existing" through the bulk API."""
import json
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MagentoTestCommon, make_response

_API_LOGGER = "odoo.addons.sdlc_magento_connector.services.magento_api"


def _sync_ok(api, method, route, items, store_code=None):
    """``async_bulk`` stand-in: every item synchronously complete, one chunk."""
    items = list(items)
    return [{
        "bulk_uuid": False,
        "request_items": [{"id": i, "status": 1, "result_message": ""} for i in range(len(items))],
        "errors": False,
        "items": items,
        "sync": True,
    }]


class _BulkCase(MagentoTestCommon):

    def record_http(self, responder):
        """Answer every HTTP request with ``responder(prepared)``; return the sent requests."""
        sent = []

        def _send(session, prepared, **kwargs):
            sent.append(prepared)
            return responder(prepared)

        self.startPatcher(patch.object(requests.sessions.Session, "send", _send))
        return sent

    def issue(self, entity, ref, state="open"):
        return self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("entity", "=", entity),
            ("magento_ref", "=", ref), ("state", "=", state),
        ])


@tagged("post_install", "-at_install", "magento")
class TestBulkApi(_BulkCase):

    def test_async_bulk_url_chunking_and_payload(self):
        self.instance.write({"use_async_bulk": True, "bulk_chunk_size": 2})
        counter = iter(range(100))
        sent = self.record_http(lambda p: make_response(202, {
            "bulk_uuid": "uuid-%s" % next(counter),
            "request_items": [{"id": i, "data_hash": None, "status": "accepted"}
                              for i in range(len(json.loads(p.body)))],
            "errors": False,
        }, url=p.url))
        items = [{"sourceItems": [{"sku": "S%s" % i, "source_code": "default", "quantity": i, "status": 1}]}
                 for i in range(5)]

        results = MagentoAPI(self.instance).async_bulk("POST", "inventory/source-items", items)

        self.assertEqual(len(sent), 3, "5 items in chunks of 2 = 3 bulk requests")
        for prepared in sent:
            self.assertEqual(prepared.method, "POST")
            self.assertEqual(urlsplit(prepared.url).path, "/rest/all/async/bulk/V1/inventory/source-items")
            self.assertTrue(prepared.headers.get("Authorization"))
        self.assertEqual([json.loads(p.body) for p in sent], [items[0:2], items[2:4], items[4:5]])
        self.assertEqual([r["bulk_uuid"] for r in results], ["uuid-0", "uuid-1", "uuid-2"])
        self.assertEqual([len(r["request_items"]) for r in results], [2, 2, 1])
        self.assertEqual(results[2]["items"], items[4:5])
        self.assertFalse(any(r["sync"] or r["errors"] for r in results))

        sent.clear()
        MagentoAPI(self.instance).async_bulk("put", "/V1/products/bySku", [{"product": {"sku": "A"}}],
                                             store_code="fr")
        self.assertEqual(sent[0].method, "PUT")
        self.assertEqual(urlsplit(sent[0].url).path, "/rest/fr/async/bulk/V1/products/bySku")

    def test_async_bulk_default_chunk_size_is_20(self):
        self.instance.write({"use_async_bulk": True})
        self.assertEqual(self.instance.bulk_chunk_size, 20)
        sent = self.record_http(lambda p: make_response(202, {"bulk_uuid": "u", "request_items": []}, url=p.url))
        MagentoAPI(self.instance).async_bulk("post", "products", [{"product": {"sku": str(i)}} for i in range(45)])
        self.assertEqual([len(json.loads(p.body)) for p in sent], [20, 20, 5])

    def test_async_bulk_rejects_get(self):
        with self.assertRaises(ValueError):
            MagentoAPI(self.instance).async_bulk("get", "products", [{}])

    def test_async_bulk_auth_error_stops_remaining_chunks(self):
        self.instance.write({"use_async_bulk": True, "bulk_chunk_size": 1})
        sent = self.record_http(lambda p: make_response(401, {"message": "The consumer isn't authorized"},
                                                        url=p.url))
        results = MagentoAPI(self.instance).async_bulk("post", "products", [{"a": 1}, {"a": 2}, {"a": 3}])
        self.assertEqual(len(sent), 1, "After a 401 the other chunks are not sent")
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertFalse(result["bulk_uuid"])
            self.assertIsInstance(result["error"], requests.exceptions.HTTPError)

    def test_sync_fallback_sends_items_one_by_one(self):
        self.assertFalse(self.instance.use_async_bulk, "Async bulk is off by default")

        def responder(prepared):
            path = urlsplit(prepared.url).path
            if path.endswith("/products/BAD"):
                return make_response(400, {"message": 'The "%1" attribute value is invalid.',
                                           "parameters": ["color"]}, url=prepared.url)
            return make_response(200, json.loads(prepared.body)["product"], url=prepared.url)

        sent = self.record_http(responder)
        items = [{"product": {"sku": "A B", "name": "x"}}, {"product": {"sku": "BAD"}}]
        results = MagentoAPI(self.instance).async_bulk("put", "products/bySku", items)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["sync"])
        self.assertFalse(result["bulk_uuid"])
        self.assertTrue(result["errors"])
        self.assertEqual([r["status"] for r in result["request_items"]], [1, 3])
        self.assertEqual(result["request_items"][1]["result_message"],
                         'HTTP 400: The "color" attribute value is invalid.')
        self.assertFalse([p for p in sent if "/async/bulk/" in p.url], "No bulk endpoint in sync mode")
        self.assertEqual(sent[0].method, "PUT")
        self.assertEqual(urlsplit(sent[0].url).path, "/rest/all/V1/products/A%20B")
        self.assertEqual(json.loads(sent[0].body), items[0])

    def test_sync_fallback_classifies_errors_and_stops_on_auth(self):
        def responder(prepared):
            path = urlsplit(prepared.url).path
            if path.endswith("/BUSY"):
                return make_response(503, {"message": "Service Unavailable"}, url=prepared.url)
            if path.endswith("/DENIED"):
                return make_response(401, {"message": "Unauthorized"}, url=prepared.url)
            return make_response(200, {}, url=prepared.url)

        sent = self.record_http(responder)
        items = [{"product": {"sku": s}} for s in ("OK", "BUSY", "DENIED", "NEVER")]
        with mute_logger(_API_LOGGER):
            result = MagentoAPI(self.instance).async_bulk("put", "products/bySku", items)[0]
        self.assertEqual([r["status"] for r in result["request_items"]], [1, 2, 3, 3])
        self.assertIn("HTTP 401", result["request_items"][3]["result_message"])
        self.assertFalse([p for p in sent if p.url.endswith("/NEVER")], "Nothing is sent after a 401")

    def test_sync_fallback_legacy_stock_url(self):
        sent = self.record_http(lambda p: make_response(200, 1, url=p.url))
        item = {"productSku": "SKU/1", "itemId": 77, "stockItem": {"qty": 3.0, "is_in_stock": True}}
        result = MagentoAPI(self.instance).async_bulk("put", "products/byProductSku/stockItems/byItemId", [item])[0]
        self.assertEqual(result["request_items"][0]["status"], 1)
        self.assertEqual(urlsplit(sent[0].url).path, "/rest/all/V1/products/SKU%2F1/stockItems/77")

    def test_bulk_status_endpoints(self):
        sent = self.record_http(lambda p: make_response(200, {"operations_list": []}, url=p.url))
        api = MagentoAPI(self.instance)
        api.get_bulk_status("abc-1")
        api.get_bulk_detailed_status("abc-1")
        self.assertEqual([(p.method, urlsplit(p.url).path) for p in sent], [
            ("GET", "/rest/V1/bulk/abc-1/status"),
            ("GET", "/rest/V1/bulk/abc-1/detailed-status"),
        ])


@tagged("post_install", "-at_install", "magento")
class TestBulkOperation(_BulkCase):

    def _entries(self, skus, entity="product"):
        return [{"payload": {"product": {"sku": sku}}, "entity": entity, "ref": sku} for sku in skus]

    def _submit_async(self, skus, uuid="uuid-1"):
        self.patch_api("async_bulk", return_value=[{
            "bulk_uuid": uuid,
            "request_items": [{"id": i, "status": "accepted"} for i in range(len(skus))],
            "errors": False,
            "items": [{"product": {"sku": s}} for s in skus],
            "sync": False,
        }])
        return self.env["magento.bulk.operation"]._submit(
            self.instance, "product_update", "put", "products/bySku", self._entries(skus))

    def test_poll_maps_statuses_to_issues(self):
        Issue = self.env["magento.sync.issue"]
        Issue._record_failure(self.instance, "product", "S1", "old failure", severity="transient")
        op = self._submit_async(["S1", "S2", "S3", "S4", "S5"])
        self.assertEqual((op.state, op.transport, op.bulk_uuid, op.open_count), ("accepted", "async", "uuid-1", 5))

        detailed = self.patch_api("get_bulk_detailed_status", return_value={"operations_list": [
            {"id": 11, "status": 1, "result_message": "Service execution success"},
            {"id": 12, "status": 2, "result_message": "Lock wait timeout"},
            {"id": 13, "status": 3, "result_message": "The product SKU is invalid", "error_code": 0},
            {"id": 14, "status": 4, "result_message": None},
            {"id": 15, "status": 5, "result_message": "Rejected: duplicate"},
        ]})
        self.env["magento.bulk.operation"]._cron_poll_bulk_operations()

        self.assertEqual(detailed.call_args.args[1], "uuid-1")
        self.assertEqual(op.poll_attempts, 1)
        self.assertTrue(op.last_polled)
        self.assertEqual(op.state, "accepted", "An open item keeps the operation polling")
        self.assertEqual((op.complete_count, op.retriable_count, op.failed_count, op.open_count,
                          op.rejected_count), (1, 1, 1, 1, 1))
        self.assertFalse(self.issue("product", "S1"), "Status 1 resolves the open issue")
        self.assertTrue(self.issue("product", "S1", state="resolved"))
        s2 = self.issue("product", "S2")
        self.assertEqual(s2.severity, "transient")
        self.assertIn("Lock wait timeout", s2.message)
        s3 = self.issue("product", "S3")
        self.assertEqual(s3.severity, "permanent")
        self.assertIn("The product SKU is invalid", s3.message)
        self.assertEqual(json.loads(s3.payload), {"product": {"sku": "S3"}})
        self.assertFalse(self.issue("product", "S4"), "Status 4 is still open: no issue yet")
        self.assertEqual(self.issue("product", "S5").severity, "permanent")

        # Next poll: the open item completes; finished items are not recorded twice.
        detailed.return_value = {"operations_list": [
            {"id": 11, "status": 1}, {"id": 12, "status": 2, "result_message": "Lock wait timeout"},
            {"id": 13, "status": 3, "result_message": "The product SKU is invalid"},
            {"id": 14, "status": 1}, {"id": 15, "status": 5, "result_message": "Rejected: duplicate"},
        ]}
        self.env["magento.bulk.operation"]._cron_poll_bulk_operations()
        self.assertEqual(op.state, "partial")
        self.assertTrue(op.date_done)
        self.assertEqual(self.issue("product", "S2").occurrences, 1)
        self.assertEqual(op.poll_attempts, 2)

        # Finished operations are not polled any more.
        detailed.reset_mock()
        self.env["magento.bulk.operation"]._cron_poll_bulk_operations()
        detailed.assert_not_called()

    def test_poll_matches_operation_key_and_finishes_done(self):
        op = self._submit_async(["K1", "K2"])
        self.patch_api("get_bulk_detailed_status", return_value={"operations_list": [
            {"id": 900, "operation_key": 1, "status": 1},
            {"id": 901, "operation_key": 0, "status": 3, "result_message": "bad K1"},
        ]})
        op._poll()
        self.assertEqual(op.state, "partial")
        self.assertIn("bad K1", self.issue("product", "K1").message)
        self.assertFalse(self.issue("product", "K2"))

        op2 = self._submit_async(["D1"], uuid="uuid-2")
        self.patch_api("get_bulk_detailed_status", return_value={"operations_list": [{"id": 1, "status": 1}]})
        op2._poll()
        self.assertEqual(op2.state, "done")

    def test_gives_up_after_max_polls(self):
        self.env["ir.config_parameter"].sudo().set_param("sdlc_magento_connector.bulk_max_polls", "3")
        op = self._submit_async(["G1", "G2"], uuid="uuid-stuck")
        self.patch_api("get_bulk_detailed_status", return_value={"operations_list": [
            {"id": 1, "status": 4}, {"id": 2, "status": 4},
        ]})
        Bulk = self.env["magento.bulk.operation"]
        Bulk._cron_poll_bulk_operations()
        Bulk._cron_poll_bulk_operations()
        self.assertEqual(op.state, "accepted")
        Bulk._cron_poll_bulk_operations()
        self.assertEqual(op.state, "failed")
        self.assertEqual(op.poll_attempts, 3)
        self.assertIn("async.operations.all", op.message)
        issue = self.issue("other", "bulk-uuid-stuck")
        self.assertTrue(issue)
        self.assertEqual(issue.severity, "permanent")

    def test_poll_http_error_counts_as_attempt(self):
        self.env["ir.config_parameter"].sudo().set_param("sdlc_magento_connector.bulk_max_polls", "2")
        op = self._submit_async(["E1"], uuid="uuid-err")
        self.patch_api("get_bulk_detailed_status",
                       side_effect=requests.exceptions.ConnectionError("Connection refused"))
        op._poll()
        self.assertEqual((op.state, op.poll_attempts), ("accepted", 1))
        self.assertIn("Connection refused", op.message)
        op._poll()
        self.assertEqual(op.state, "failed")

    def test_submit_sync_results_recorded_immediately(self):
        self.patch_api("async_bulk", return_value=[{
            "bulk_uuid": False,
            "request_items": [{"id": 0, "status": 1, "result_message": ""},
                              {"id": 1, "status": 3, "result_message": "HTTP 400: invalid"}],
            "errors": True,
            "items": [{"product": {"sku": "Y1"}}, {"product": {"sku": "Y2"}}],
            "sync": True,
        }])
        op = self.env["magento.bulk.operation"]._submit(
            self.instance, "product_update", "put", "products/bySku", self._entries(["Y1", "Y2"]))
        self.assertEqual((op.transport, op.state, op.complete_count, op.failed_count), ("sync", "partial", 1, 1))
        self.assertEqual(self.issue("product", "Y2").message, "HTTP 400: invalid")
        self.assertFalse(self.issue("product", "Y1"))

    def test_submit_refused_chunk_records_failures(self):
        error = requests.exceptions.ConnectionError("Connection refused")
        self.patch_api("async_bulk", return_value=[{
            "bulk_uuid": False, "request_items": [], "errors": True,
            "items": [{"product": {"sku": "R1"}}], "sync": False, "error": error,
        }])
        op = self.env["magento.bulk.operation"]._submit(
            self.instance, "product_update", "put", "products/bySku", self._entries(["R1"]))
        self.assertEqual(op.state, "failed")
        self.assertEqual(self.issue("product", "R1").severity, "transient")

    def test_user_reads_manager_writes(self):
        op = self._submit_async(["U1"])
        user = self.create_user("bulk_user", "sdlc_magento_connector.group_magento_user")
        self.assertEqual(op.with_user(user).read(["state"])[0]["state"], "accepted")
        with self.assertRaises(AccessError):
            op.with_user(user).unlink()
        with self.assertRaises(AccessError):
            op.with_user(user).action_poll_now()


@tagged("post_install", "-at_install", "magento")
class TestBulkStockPush(_BulkCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.other_location = cls.env["stock.location"].create({
            "name": "Other internal", "usage": "internal",
            "location_id": cls.warehouse.view_location_id.id,
        })

    def _storable(self, sku, qty=0.0, other_qty=0.0):
        product = self.create_product(sku, is_storable=True)
        quants = self.env["stock.quant"]
        if qty:
            quants._update_available_quantity(product, self.stock_location, qty)
        if other_qty:
            quants._update_available_quantity(product, self.other_location, other_qty)
        return product

    def test_default_location_is_company_warehouse_stock(self):
        self.assertEqual(self.instance.stock_location_id, self.stock_location)
        self.assertEqual(self.instance.msi_source_code, "default")
        self.assertEqual(self.instance.stock_push_mode, "msi")

    def test_msi_push_quantities_from_location(self):
        p1 = self._storable("MSI-1", qty=7.0, other_qty=100.0)
        p2 = self._storable("MSI-2")
        consumable = self.create_product("MSI-CONSU")
        config = self._storable("MSI-CONF", qty=4.0)
        m1 = self.create_map(p1, 501)
        self.create_map(p2, 502)
        self.create_map(consumable, 503)
        self.create_map(config, 504, type_id="configurable")
        self.instance.write({"msi_source_code": "eu_wh"})
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)

        result = self.instance._magento_push_stock_bulk()

        api, method, route, items = bulk.call_args.args
        self.assertEqual((method, route), ("post", "inventory/source-items"))
        self.assertEqual(items, [
            {"sourceItems": [{"sku": "MSI-1", "source_code": "eu_wh", "quantity": 7.0, "status": 1}]},
            {"sourceItems": [{"sku": "MSI-2", "source_code": "eu_wh", "quantity": 0.0, "status": 0}]},
        ], "Free qty at the instance location only; storable simple products only")
        self.assertEqual(result["items"], 2)
        op = self.env["magento.bulk.operation"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual((op.operation, op.state, op.complete_count), ("stock_update", "done", 2))
        self.assertEqual(json.loads(op.items_json)[0]["res_id"], m1.id)

        # Another location: its quantity is pushed instead.
        self.instance.write({"stock_location_id": self.other_location.id})
        self.instance._magento_push_stock_bulk()
        self.assertEqual(bulk.call_args.args[3][0]["sourceItems"][0]["quantity"], 100.0)

    def test_reserved_quantity_is_not_pushed(self):
        product = self._storable("RES-1", qty=10.0)
        self.env["stock.quant"]._update_reserved_quantity(product, self.stock_location, 3.0)
        self.create_map(product, 601)
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        self.instance._magento_push_stock_bulk()
        self.assertEqual(bulk.call_args.args[3][0]["sourceItems"][0]["quantity"], 7.0)

    def test_legacy_push_payload(self):
        p1 = self._storable("LEG-1", qty=2.0)
        p2 = self._storable("LEG-2")
        self.create_map(p1, 701, magento_stock_item_id="77")
        self.create_map(p2, 702)
        self.instance.write({"stock_push_mode": "legacy"})
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        self.instance._magento_push_stock_bulk()
        _api, method, route, items = bulk.call_args.args
        self.assertEqual((method, route), ("put", "products/byProductSku/stockItems/byItemId"))
        self.assertEqual(items, [
            {"productSku": "LEG-1", "itemId": 77, "stockItem": {"qty": 2.0, "is_in_stock": True}},
            {"productSku": "LEG-2", "itemId": 1, "stockItem": {"qty": 0.0, "is_in_stock": False}},
        ])

    def test_stock_item_id_stored_on_import(self):
        product = self._storable("IMP-1")
        product_map = self.create_map(product, 801)
        values = product_map._apply_magento_data({
            "id": 801, "sku": "IMP-1", "extension_attributes": {"stock_item": {"item_id": 4242, "qty": 1}},
        })
        self.assertEqual(values["magento_stock_item_id"], "4242")

    def test_failed_stock_items_land_in_error_center(self):
        product = self._storable("ERR-1", qty=1.0)
        self.create_map(product, 901)
        sent = self.record_http(lambda p: make_response(
            400, {"message": 'Source "%1" does not exist.', "parameters": ["default"]}, url=p.url))
        self.instance._magento_push_stock_bulk()
        self.assertTrue(sent)
        self.assertTrue(all("/V1/inventory/source-items" in p.url for p in sent))
        issue = self.issue("stock", "ERR-1")
        self.assertEqual(issue.severity, "permanent")
        self.assertIn('Source "default" does not exist.', issue.message)

    def test_push_is_enqueued_with_dedup(self):
        Job = self.env["magento.job"]
        self.instance.action_push_stock_bulk()
        self.instance.action_push_stock_bulk()
        jobs = Job.search([("identity_key", "=", "stock-push-%s" % self.instance.id)])
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs.method_name, jobs.res_ids, jobs.instance_id), (
            "_magento_push_stock_bulk", [self.instance.id], self.instance))

        product = self._storable("JOB-1", qty=3.0)
        self.create_map(product, 1001)
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        jobs._run_job()
        self.assertEqual(jobs.state, "done")
        self.assertEqual(bulk.call_count, 1)

    def test_push_requires_manager(self):
        user = self.create_user("stock_user", "sdlc_magento_connector.group_magento_user")
        with self.assertRaises(AccessError):
            self.instance.with_user(user).action_push_stock_bulk()


@tagged("post_install", "-at_install", "magento")
class TestApplyToExistingBulk(_BulkCase):

    def _product_mapping(self):
        field = self.env["magento.available.field"].create({
            "name": "Custom -> meta_title", "code": "custom_attributes.meta_title",
            "instance_id": self.instance.id, "mapping_type": "product",
        })
        odoo_field = self.env["ir.model.fields"]._get("magento.product.map", "name")
        return self.env["magento.field.mapping"].create({
            "mapping_type": "product", "instance_id": self.instance.id,
            "odoo_field": odoo_field.id, "magento_field_id": field.id,
        })

    def test_apply_to_existing_uses_bulk(self):
        mapping = self._product_mapping()
        self.create_map(self.create_product("AP-1", name="Alpha"), 1101)
        self.create_map(self.create_product("AP-2", name="Beta"), 1102)
        self.create_map(self.create_product("AP-CONF"), 1103, type_id="configurable")
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        update = self.patch_api("update_product")

        action = mapping.action_apply_to_existing()

        update.assert_not_called()
        self.assertEqual(bulk.call_count, 1, "One bulk call for all records")
        _api, method, route, items = bulk.call_args.args
        self.assertEqual((method, route), ("put", "products/bySku"))
        self.assertEqual(items, [
            {"product": {"custom_attributes": [{"attribute_code": "meta_title", "value": "Alpha"}],
                         "sku": "AP-1"}},
            {"product": {"custom_attributes": [{"attribute_code": "meta_title", "value": "Beta"}],
                         "sku": "AP-2"}},
        ])
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn("Pushed: 2", action["params"]["message"])
        self.assertIn("Skipped: 1", action["params"]["message"])
        op = self.env["magento.bulk.operation"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual((op.operation, op.state), ("product_update", "done"))

    def test_apply_to_existing_async_queues(self):
        mapping = self._product_mapping()
        self.create_map(self.create_product("AS-1"), 1201)
        self.instance.write({"use_async_bulk": True})
        sent = self.record_http(lambda p: make_response(202, {
            "bulk_uuid": "uuid-map", "errors": False,
            "request_items": [{"id": 0, "status": "accepted"}],
        }, url=p.url))
        action = mapping.action_apply_to_existing()
        self.assertEqual(len(sent), 1)
        self.assertEqual(urlsplit(sent[0].url).path, "/rest/all/async/bulk/V1/products/bySku")
        self.assertIn("Queued: 1", action["params"]["message"])
        op = self.env["magento.bulk.operation"].search([("bulk_uuid", "=", "uuid-map")])
        self.assertEqual(op.state, "accepted")

    def test_apply_to_existing_sync_failure_counted(self):
        mapping = self._product_mapping()
        self.create_map(self.create_product("SF-1"), 1301)
        self.record_http(lambda p: make_response(400, {"message": "Invalid attribute"}, url=p.url))
        action = mapping.action_apply_to_existing()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("Failed: 1", action["params"]["message"])
        self.assertIn("Invalid attribute", self.issue("product", "SF-1").message)
