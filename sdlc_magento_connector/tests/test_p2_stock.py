"""Roadmap 2.6: stock push (basis, locations, buffer, kits, backorders, MSI sources),
event-driven debounced delta push and the nightly reconcile."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import MagentoTestCommon, make_response
from .test_bulk import _sync_ok


@tagged("post_install", "-at_install", "magento")
class TestStockPushP2(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        cls.shelf = cls.env["stock.location"].create({
            "name": "Shelf", "usage": "internal", "location_id": cls.stock_location.id,
        })
        cls.other_location = cls.env["stock.location"].create({
            "name": "Second store", "usage": "internal",
            "location_id": cls.warehouse.view_location_id.id,
        })
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.Dirty = cls.env["magento.stock.dirty"]
        cls.Job = cls.env["magento.job"]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _storable(self, sku, qty=0.0, location=None):
        product = self.create_product(sku, is_storable=True)
        if qty:
            self.env["stock.quant"]._update_available_quantity(product, location or self.stock_location, qty)
        return product

    def _mapped(self, sku, qty=0.0, magento_id=None, location=None, **map_vals):
        product = self._storable(sku, qty, location=location)
        product_map = self.create_map(product, magento_id or abs(hash(sku)) % 10 ** 7, **map_vals)
        return product, product_map

    def _push(self, maps=None):
        """Run the full push with a successful synchronous bulk; return the sent items."""
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        self.instance._magento_push_stock_bulk()
        return bulk.call_args.args[3] if bulk.call_args else []

    @staticmethod
    def _qty_by_sku(items):
        return {it["sourceItems"][0]["sku"]: it["sourceItems"][0]["quantity"] for it in items}

    def _dirty(self, instance=None):
        instance = instance or self.instance
        self.env.flush_all()
        self.env.cr.execute("SELECT product_id FROM magento_stock_dirty WHERE instance_id = %s ORDER BY id",
                            [instance.id])
        return [row[0] for row in self.env.cr.fetchall()]

    def _new_transaction(self):
        """Forget the per-transaction dedup of stock marks (as a commit does)."""
        for key in list(self.env.cr.precommit.data):
            if key.startswith("sdlc_magento_connector.stock_"):
                self.env.cr.precommit.data.pop(key)

    def _delta_jobs(self, instance=None):
        instance = instance or self.instance
        return self.Job.search([
            ("identity_key", "like", "stock-delta-%s" % instance.id),
            ("state", "in", ("pending", "started")),
        ])

    def _receive(self, product, qty, user=None):
        picking = self.env["stock.picking"].with_user(user or self.env.user).create({
            "picking_type_id": self.warehouse.in_type_id.id,
            "location_id": self.supplier_location.id,
            "location_dest_id": self.stock_location.id,
            "move_ids": [Command.create({
                "name": product.display_name,
                "product_id": product.id,
                "product_uom_qty": qty,
                "product_uom": product.uom_id.id,
                "location_id": self.supplier_location.id,
                "location_dest_id": self.stock_location.id,
            })],
        })
        picking.action_confirm()
        picking.move_ids.quantity = qty
        picking.move_ids.picked = True
        picking.button_validate()
        self.assertEqual(picking.state, "done")
        return picking

    # ------------------------------------------------------------------
    # Quantity basis, locations, buffer
    # ------------------------------------------------------------------
    def test_defaults_are_safe_for_upgrades(self):
        self.assertFalse(self.instance.stock_push_enabled, "No automatic writes to Magento after an upgrade")
        self.assertEqual(self.instance.stock_qty_basis, "free")
        self.assertEqual(self.instance.stock_safety_buffer, 0.0)
        cron = self.env.ref("sdlc_magento_connector.ir_cron_magento_stock_reconcile")
        self.assertFalse(cron.active, "The nightly reconcile is inactive by default")

    def test_quantity_basis(self):
        product, _m = self._mapped("BASIS-1", qty=10.0)
        self.env["stock.quant"]._update_reserved_quantity(product, self.stock_location, 3.0)
        out = self.env["stock.move"].create({
            "name": product.display_name, "product_id": product.id, "product_uom_qty": 4.0, "product_uom": product.uom_id.id,
            "location_id": self.stock_location.id, "location_dest_id": self.customer_location.id,
        })
        out._action_confirm()
        expected = {"free": 7.0, "on_hand": 10.0, "forecast": 6.0}
        for basis, qty in expected.items():
            self.instance.stock_qty_basis = basis
            self.assertEqual(self._qty_by_sku(self._push()), {"BASIS-1": qty}, basis)

    def test_multi_location_sum_and_children(self):
        product, _m = self._mapped("LOC-1", qty=4.0)
        quants = self.env["stock.quant"]
        quants._update_available_quantity(product, self.shelf, 2.0)
        quants._update_available_quantity(product, self.other_location, 30.0)
        self.assertEqual(self._qty_by_sku(self._push()), {"LOC-1": 6.0}, "Default: stock location + children")
        self.instance.stock_location_ids = self.stock_location | self.other_location
        self.assertEqual(self._qty_by_sku(self._push()), {"LOC-1": 36.0}, "Locations are summed")
        self.instance.stock_location_ids = self.shelf | self.stock_location
        self.assertEqual(self._qty_by_sku(self._push()), {"LOC-1": 6.0}, "Nested locations are not double counted")
        with self.assertRaises(ValidationError):
            self.instance.stock_location_ids = self.customer_location

    def test_safety_buffer_and_clamp(self):
        self._mapped("BUF-1", qty=7.0)
        product_neg, _m = self._mapped("BUF-NEG")
        self.env["stock.quant"]._update_available_quantity(product_neg, self.stock_location, -3.0)
        items = self._push()
        self.assertEqual(self._qty_by_sku(items), {"BUF-1": 7.0, "BUF-NEG": 0.0}, "Negative stock is clamped at 0")
        self.instance.stock_safety_buffer = 5.0
        items = self._push()
        self.assertEqual(self._qty_by_sku(items), {"BUF-1": 2.0, "BUF-NEG": 0.0})
        self.instance.stock_safety_buffer = 50.0
        items = self._push()
        self.assertEqual(items[0]["sourceItems"][0], {
            "sku": "BUF-1", "source_code": "default", "quantity": 0.0, "status": 0})
        with self.assertRaises(ValidationError):
            self.instance.stock_safety_buffer = -1.0

    # ------------------------------------------------------------------
    # Kits
    # ------------------------------------------------------------------
    def test_kit_quantity_from_components(self):
        kit = self.create_product("KIT-1")  # a kit is not storable itself
        self.create_map(kit, 4001)
        comp_a = self._storable("KIT-A", qty=10.0)
        comp_b = self._storable("KIT-B", qty=7.0)
        Instance = type(self.env["magento.instance"])
        self.startPatcher(patch.object(
            Instance, "_magento_kit_components",
            lambda inst, products: {kit.id: [(comp_a, 2.0), (comp_b, 3.0)]} if kit in products else {},
        ))
        # min(floor(10 / 2), floor(7 / 3)) = 2
        self.assertEqual(self._qty_by_sku(self._push()), {"KIT-1": 2.0})
        self.instance.stock_safety_buffer = 1.0
        self.assertEqual(self._qty_by_sku(self._push()), {"KIT-1": 1.0})

    def test_kit_components_without_mrp(self):
        product = self._storable("NOKIT")
        if "mrp.bom" in self.env:
            self.skipTest("mrp is installed: covered by test_kit_with_mrp")
        self.assertEqual(self.instance._magento_kit_components(product), {})

    def test_kit_with_mrp(self):
        if "mrp.bom" not in self.env:
            self.skipTest("mrp is not installed in this database")
        kit = self.create_product("MRP-KIT", is_storable=False)
        self.create_map(kit, 4101)
        comp_a = self._storable("MRP-A", qty=9.0)
        comp_b = self._storable("MRP-B", qty=20.0)
        self.env["mrp.bom"].create({
            "product_tmpl_id": kit.product_tmpl_id.id, "product_qty": 1.0, "type": "phantom",
            "bom_line_ids": [Command.create({"product_id": comp_a.id, "product_qty": 2.0}),
                             Command.create({"product_id": comp_b.id, "product_qty": 4.0})],
        })
        self.assertEqual(self._qty_by_sku(self._push()), {"MRP-KIT": 4.0})

    # ------------------------------------------------------------------
    # Backorders, payload shapes, MSI sources
    # ------------------------------------------------------------------
    def test_backorders_keep_product_in_stock(self):
        _p, product_map = self._mapped("BO-1", magento_backorders="1")
        self._mapped("BO-0", magento_backorders="0")
        self._mapped("BO-NONE")
        items = self._push()
        status = {it["sourceItems"][0]["sku"]: it["sourceItems"][0]["status"] for it in items}
        self.assertEqual(status, {"BO-1": 1, "BO-0": 0, "BO-NONE": 0})
        self.assertEqual(self._qty_by_sku(items)["BO-1"], 0.0, "The quantity is still pushed")

        self.instance.stock_push_mode = "legacy"
        items = self._push()
        by_sku = {it["productSku"]: it["stockItem"] for it in items}
        self.assertEqual(by_sku["BO-1"], {"qty": 0.0, "is_in_stock": True})
        self.assertEqual(by_sku["BO-0"], {"qty": 0.0, "is_in_stock": False})

        values = product_map._apply_magento_data({
            "id": 77, "sku": "BO-1",
            "extension_attributes": {"stock_item": {"item_id": 5, "qty": 0, "backorders": 2}},
        })
        self.assertEqual(values["magento_backorders"], "2")

    def test_msi_and_legacy_payload_shapes(self):
        self._mapped("SHAPE-1", qty=3.0, magento_stock_item_id="91")
        self.instance.msi_source_code = "eu"
        self.assertEqual(self._push(), [
            {"sourceItems": [{"sku": "SHAPE-1", "source_code": "eu", "quantity": 3.0, "status": 1}]},
        ])
        bulk = self._api_mocks["async_bulk"]
        self.assertEqual(bulk.call_args.args[1:3], ("post", "inventory/source-items"))

        self.instance.stock_push_mode = "legacy"
        self.assertEqual(self._push(), [
            {"productSku": "SHAPE-1", "itemId": 91, "stockItem": {"qty": 3.0, "is_in_stock": True}},
        ])
        self.assertEqual(bulk.call_args.args[1:3], ("put", "products/byProductSku/stockItems/byItemId"))

    def test_msi_source_mapping_one_request_per_sku(self):
        product, _m = self._mapped("SRC-1", qty=5.0)
        self.env["stock.quant"]._update_available_quantity(product, self.other_location, 8.0)
        self.env["magento.stock.source.map"].create([
            {"instance_id": self.instance.id, "source_code": "main", "location_ids": [Command.set(self.stock_location.ids)]},
            {"instance_id": self.instance.id, "source_code": "store2", "sequence": 20,
             "location_ids": [Command.set(self.other_location.ids)]},
        ])
        self.assertEqual(self._push(), [{"sourceItems": [
            {"sku": "SRC-1", "source_code": "main", "quantity": 5.0, "status": 1},
            {"sku": "SRC-1", "source_code": "store2", "quantity": 8.0, "status": 1},
        ]}])
        self.assertEqual(_m.last_pushed_qty, 13.0)
        # Legacy has one stock: the source mapping is ignored.
        self.instance.stock_push_mode = "legacy"
        self.assertEqual(self._push()[0]["stockItem"]["qty"], 5.0)
        with self.assertRaises(ValidationError):
            self.env["magento.stock.source.map"].create({
                "instance_id": self.instance.id, "source_code": "bad code",
                "location_ids": [Command.set(self.stock_location.ids)]})

    def test_last_push_recorded_and_failures_in_error_center(self):
        _p, ok_map = self._mapped("OK-1", qty=4.0)
        _p, bad_map = self._mapped("BAD-1", qty=2.0)

        def responder(prepared):
            sku = json.loads(prepared.body)["sourceItems"][0]["sku"]
            if sku == "BAD-1":
                return make_response(400, {"message": 'Source "%1" does not exist.', "parameters": ["default"]},
                                     url=prepared.url)
            return make_response(200, [], url=prepared.url)

        self.startPatcher(patch("requests.sessions.Session.send",
                                lambda session, prepared, **kw: responder(prepared)))
        result = self.instance._magento_push_stock_bulk()
        self.assertEqual((result["complete"], result["failed"]), (1, 1))
        self.assertEqual(ok_map.last_pushed_qty, 4.0)
        self.assertTrue(ok_map.last_stock_push_at)
        self.assertFalse(bad_map.last_stock_push_at, "A failed item is not stamped as pushed")
        issue = self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("entity", "=", "stock"), ("magento_ref", "=", "BAD-1")])
        self.assertEqual((issue.state, issue.severity), ("open", "permanent"))
        self.assertIn('Source "default" does not exist.', issue.message)

    # ------------------------------------------------------------------
    # Event-driven, debounced delta push
    # ------------------------------------------------------------------
    def test_validated_picking_marks_dirty_and_enqueues_one_job(self):
        self.instance.stock_push_enabled = True
        p1, _m1 = self._mapped("EV-1")
        p2, _m2 = self._mapped("EV-2")
        unmapped = self._storable("EV-UNMAPPED")
        stock_user = self.create_user("stock_only", "stock.group_stock_user")
        before = fields.Datetime.now()

        self._receive(p1, 5.0, user=stock_user)
        self.assertEqual(set(self._dirty()), {p1.id})
        job = self._delta_jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual((job.state, job.method_name, job.channel, job.identity_key, job.res_ids),
                         ("pending", "_magento_push_stock_delta", "stock-%s" % self.instance.id,
                          "stock-delta-%s" % self.instance.id, [self.instance.id]))
        self.assertGreaterEqual(job.eta, before + timedelta(seconds=59))
        self.assertLessEqual(job.eta, fields.Datetime.now() + timedelta(seconds=61))
        self.assertFalse(self.http_attempts, "Nothing is sent to Magento while validating")

        self._receive(p2, 2.0)
        self._receive(unmapped, 1.0)
        self.assertEqual(self._delta_jobs(), job, "Later moves reuse the pending job")
        self.assertEqual(set(self._dirty()), {p1.id, p2.id})

        # The job runs as superuser (the stock user has no Magento rights) and pushes both.
        bulk = self.patch_api("async_bulk", side_effect=_sync_ok)
        job.sudo()._run_job()
        self.assertEqual(job.state, "done", job.last_error)
        self.assertEqual(self._qty_by_sku(bulk.call_args.args[3]), {"EV-1": 5.0, "EV-2": 2.0})
        self.assertEqual(self._dirty(), [])

    def test_no_marks_when_disabled_or_not_mapped(self):
        product, _m = self._mapped("OFF-1")
        self._receive(product, 3.0)
        self.assertEqual(self._dirty(), [], "Automatic stock push is off by default")
        self.assertFalse(self._delta_jobs())
        # Reservations only matter for the free basis.
        self.instance.write({"stock_push_enabled": True, "stock_qty_basis": "on_hand"})
        self.env["stock.quant"]._update_reserved_quantity(product, self.stock_location, 1.0)
        self.assertEqual(self._dirty(), [])
        self.instance.stock_qty_basis = "free"
        self._new_transaction()
        self.env["stock.quant"]._update_reserved_quantity(product, self.stock_location, 1.0)
        self.assertEqual(self._dirty(), [product.id])
        # Non-internal locations are ignored.
        other, _m2 = self._mapped("OFF-2")
        self.env["stock.quant"]._update_available_quantity(other, self.customer_location, 1.0)
        self.assertNotIn(other.id, self._dirty())

    def test_forecast_basis_marks_on_confirm(self):
        product, _m = self._mapped("FC-1", qty=10.0)
        self.instance.write({"stock_push_enabled": True, "stock_qty_basis": "forecast"})
        move = self.env["stock.move"].create({
            "name": product.display_name, "product_id": product.id, "product_uom_qty": 4.0, "product_uom": product.uom_id.id,
            "location_id": self.stock_location.id, "location_dest_id": self.customer_location.id,
        })
        move._action_confirm()
        self.assertEqual(self._dirty(), [product.id])
        self.assertEqual(len(self._delta_jobs()), 1)

    def test_delta_pushes_only_dirty_and_clears_flags(self):
        self.instance.stock_push_enabled = True
        p1, m1 = self._mapped("DL-1", qty=3.0)
        _p2, m2 = self._mapped("DL-2", qty=9.0)
        p3, _m3 = self._mapped("DL-3", qty=1.0)
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM magento_stock_dirty")
        self._new_transaction()
        self.Dirty._magento_mark(p1.ids)
        self.assertEqual(self._dirty(), [p1.id])

        def push_and_change(api, method, route, items, store_code=None):
            # A change committed while the job pushes must survive the cleanup.
            self.env.cr.execute(
                "INSERT INTO magento_stock_dirty (instance_id, product_id, date) VALUES (%s, %s, now())",
                [self.instance.id, p3.id])
            return _sync_ok(api, method, route, items, store_code)

        bulk = self.patch_api("async_bulk", side_effect=push_and_change)
        result = self.instance._magento_push_stock_delta()
        self.assertEqual(bulk.call_count, 1)
        self.assertEqual(self._qty_by_sku(bulk.call_args.args[3]), {"DL-1": 3.0}, "Only the dirty SKU")
        self.assertEqual((result["items"], result["products"]), (1, 1))
        self.assertEqual(self._dirty(), [p3.id], "Rows read are deleted, newer rows are kept")
        self.assertEqual(m1.last_pushed_qty, 3.0)
        self.assertFalse(m2.last_stock_push_at)
        (m1 | _m3).invalidate_recordset(["stock_push_pending"])
        self.assertFalse(m1.stock_push_pending)
        self.assertTrue(_m3.stock_push_pending)
        self.assertEqual(self.env["magento.product.map"].search([("stock_push_pending", "=", True)]), _m3)
        self.assertNotIn(_m3, self.env["magento.product.map"].search([("stock_push_pending", "=", False)]))

        bulk.side_effect = _sync_ok
        self.instance._magento_push_stock_delta()
        self.assertEqual(self._qty_by_sku(bulk.call_args.args[3]), {"DL-3": 1.0})
        bulk.reset_mock()
        self.assertEqual(self.instance._magento_push_stock_delta(), {"items": 0, "products": 0})
        bulk.assert_not_called()

    def test_delta_job_started_gets_follow_up(self):
        self.instance.stock_push_enabled = True
        first = self.instance._magento_enqueue_stock_delta()
        self.assertEqual(self.instance._magento_enqueue_stock_delta(), first)
        first.sudo().write({"state": "started"})
        follow_up = self.instance._magento_enqueue_stock_delta()
        self.assertNotEqual(follow_up, first)
        self.assertEqual(follow_up.identity_key, "stock-delta-%s-next" % self.instance.id)
        self.assertEqual(follow_up.channel, first.channel)
        self.assertEqual(self.instance._magento_enqueue_stock_delta(), follow_up)

    def test_full_push_clears_dirty_rows(self):
        self.instance.stock_push_enabled = True
        product, _m = self._mapped("FULL-1", qty=2.0)
        self._new_transaction()
        self.Dirty._magento_mark(product.ids)
        self.assertEqual(self._dirty(), [product.id])
        self._push()
        self.assertEqual(self._dirty(), [])

    def test_product_payload_leaves_stock_to_stock_push(self):
        attribute_set = self.env["magento.attribute.set"].with_context(magento_skip_push=True).create({
            "instance_id": self.instance.id, "magento_id": "4", "name": "Default"})
        _p, product_map = self._mapped("PAY-1", quantity=12.0, stock_status="in_stock",
                                       attribute_set_id=attribute_set.id)
        stock_item = product_map._build_magento_payload()["product"].get("extension_attributes", {})
        self.assertIn("stock_item", stock_item, "Unchanged while automatic stock push is off")
        self.instance.stock_push_enabled = True
        payload = product_map._build_magento_payload()
        self.assertNotIn("stock_item", payload["product"].get("extension_attributes") or {})

    # ------------------------------------------------------------------
    # Nightly reconcile
    # ------------------------------------------------------------------
    def test_nightly_reconcile_respects_stock_push_enabled(self):
        other = self.create_instance("Second Magento")
        self.instance.stock_push_enabled = True
        jobs = self.env["magento.instance"]._cron_magento_stock_reconcile()
        self.assertEqual(jobs.instance_id, self.instance)
        self.assertEqual((jobs.method_name, jobs.identity_key, jobs.channel), (
            "_magento_push_stock_bulk", "stock-push-%s" % self.instance.id, "stock-%s" % self.instance.id))
        self.assertFalse(self.Job.search([("identity_key", "=", "stock-push-%s" % other.id)]))
        # Deduplicated with a pending manual push.
        self.assertEqual(self.env["magento.instance"]._cron_magento_stock_reconcile(), jobs)
        cron = self.env.ref("sdlc_magento_connector.ir_cron_magento_stock_reconcile")
        self.assertEqual(cron.code.strip(), "model._cron_magento_stock_reconcile()")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "days"))

    def test_instance_stock_button_and_counts(self):
        self.instance.stock_push_enabled = True
        product, product_map = self._mapped("BTN-1")
        self.create_map(self.create_product("BTN-CONF"), 5501, type_id="configurable")
        self._new_transaction()
        self.Dirty._magento_mark(product.ids)
        self.instance.invalidate_recordset(["stock_map_count", "stock_dirty_count"])
        self.assertEqual((self.instance.stock_map_count, self.instance.stock_dirty_count), (1, 1))
        action = self.instance.action_view_stock_maps()
        self.assertEqual(self.env["magento.product.map"].search(action["domain"]), product_map)
