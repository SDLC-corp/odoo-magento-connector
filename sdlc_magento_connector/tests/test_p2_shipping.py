"""Phase 2.7: shipments with tracking (both ways, partial, MSI source) and the order
lifecycle (cancel / hold / release hold, both ways). Every Magento call is mocked."""
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.models.stock_picking_inherit import split_tracking_numbers

from .common import MagentoTestCommon, build_item, build_order, once

NO_PUSH = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)


def _shipment(entity_id, order_id, items, tracks=(), created_at="2026-10-12 09:00:00"):
    return {
        "entity_id": entity_id,
        "increment_id": "%09d" % (100000000 + entity_id),
        "order_id": order_id,
        "created_at": created_at,
        "items": [{"order_item_id": item_id, "qty": qty} for item_id, qty in items],
        "tracks": [
            {"entity_id": entity_id * 10 + i, "track_number": number, "title": "Flat Rate",
             "carrier_code": "custom"}
            for i, number in enumerate(tracks)
        ],
    }


@tagged("post_install", "-at_install", "magento")
class TestP2Shipping(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env["product.product"].with_context(**NO_PUSH)
        Map = cls.env["magento.product.map"].with_context(**NO_PUSH)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.hoodie = Product.create({"name": "Chaz Kangeroo Hoodie-XS-Black",
                                     "default_code": "MH01-XS-Black", "type": "consu",
                                     "list_price": 52.0})
        for product, magento_id in ((cls.bag, 1), (cls.hoodie, 2)):
            Map.create({"instance_id": cls.instance.id, "magento_id": str(magento_id),
                        "sku": product.default_code, "name": product.name,
                        "odoo_product_id": product.id})
        cls.partner = cls.env["res.partner"].create({"name": "Agent T Test",
                                                     "email": "agent-t-test@example.com"})
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1)

    def setUp(self):
        super().setUp()
        self.magento_shipments = []   # what GET /V1/shipments?order_id= returns
        self.next_shipment_id = [501]

        def list_shipments(api, order_id=None, **kwargs):
            return [dict(s) for s in self.magento_shipments if str(s["order_id"]) == str(order_id)]

        def read_shipment(api, shipment_id):
            for shipment in self.magento_shipments:
                if str(shipment["entity_id"]) == str(shipment_id):
                    return dict(shipment)
            return {}

        def post_shipment(api, order_id, payload):
            entity_id = self.next_shipment_id[0]
            self.next_shipment_id[0] += 1
            self.magento_shipments.append(_shipment(
                entity_id, int(order_id),
                [(i["order_item_id"], i["qty"]) for i in payload["items"]],
                tracks=[t["track_number"] for t in payload.get("tracks") or []],
            ))
            return entity_id

        self.get_shipments = self.patch_api("get_shipments", side_effect=list_shipments)
        self.get_shipment = self.patch_api("get_shipment", side_effect=read_shipment)
        self.create_shipment = self.patch_api("create_shipment", side_effect=post_shipment)
        self.post = self.patch_api("_post", return_value=True)
        for name in ("get_invoices", "get_credit_memos", "get_order_transactions"):
            self.patch_api(name, return_value=[])

    # ------------------------------------------------------------------ helpers
    def _order(self, magento_id="7001", qty_bag=1.0, qty_hoodie=2.0, confirm=True, **vals):
        lines = [Command.create({"product_id": self.bag.id, "product_uom_qty": qty_bag,
                                 "price_unit": 34.0, "tax_id": [Command.clear()]})]
        if qty_hoodie:
            lines.append(Command.create({"product_id": self.hoodie.id, "product_uom_qty": qty_hoodie,
                                         "price_unit": 52.0, "tax_id": [Command.clear()]}))
        order = self.env["sale.order"].with_context(**NO_PUSH).create(dict({
            "partner_id": self.partner.id,
            "magento_instance_id": self.instance.id,
            "magento_order_id": magento_id,
            "magento_increment_id": "0000%s" % magento_id,
            "magento_state": "processing",
            "magento_status": "processing",
            "magento_shipping_method": "flatrate_flatrate",
            "magento_shipping_description": "Flat Rate - Fixed",
            "order_line": lines,
        }, **vals))
        bag_line = order.order_line.filtered(lambda l: l.product_id == self.bag)
        bag_line.magento_item_id = "11"
        hoodie_line = order.order_line.filtered(lambda l: l.product_id == self.hoodie)
        # Configurable: child 22 of parent 21 (shipped through the parent).
        hoodie_line.write({"magento_item_id": "22", "magento_parent_item_id": "21"})
        if confirm:
            order.with_context(**NO_PUSH).action_confirm()
        return order.with_env(self.env)

    def _validate(self, picking, qtys=None, tracking=None):
        """Validate ``picking`` (Odoo creates the backorder of a partial delivery)."""
        if tracking:
            picking.with_context(skip_magento_sync=True).magento_tracking_ref = tracking
        for move in picking.move_ids:
            qty = (qtys or {}).get(move.product_id, move.product_uom_qty)
            move.quantity = qty
            move.picked = True
        picking._action_done()
        self.assertEqual(picking.state, "done")
        return picking

    def _delivery(self, order):
        return order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing" and p.state != "done")

    def _payload(self, call=-1):
        return self.create_shipment.call_args_list[call].args[2]

    def _track_posts(self):
        return [c.args[2]["entity"] for c in self.post.call_args_list
                if c.args[1] == "/rest/V1/shipment/track"]

    def _order_posts(self):
        return [c.args[1] for c in self.post.call_args_list if c.args[1].startswith("/rest/V1/orders/")]

    # ------------------------------------------------------------------ helpers (unit)
    def test_split_tracking_numbers(self):
        self.assertEqual(split_tracking_numbers("A1, b2;A1", " b 2 ", None, "C3\nD4"),
                         ["A1", "b2", "C3", "D4"])

    # ------------------------------------------------------------------ export
    def test_full_shipment_with_tracks_and_msi_source(self):
        order = self._order()
        picking = self._validate(self._delivery(order), tracking="TRK-1, TRK-2")
        self.assertEqual(self.create_shipment.call_count, 1)
        payload = self._payload()
        self.assertEqual(sorted((i["order_item_id"], i["qty"]) for i in payload["items"]),
                         [(11, 1.0), (21, 2.0)], "configurable child shipped through its parent")
        self.assertEqual(payload["tracks"], [
            {"track_number": "TRK-1", "title": "Flat Rate - Fixed", "carrier_code": "custom"},
            {"track_number": "TRK-2", "title": "Flat Rate - Fixed", "carrier_code": "custom"},
        ])
        self.assertEqual(payload["arguments"], {"extension_attributes": {"source_code": "default"}})
        self.assertFalse(payload["notify"])
        self.assertEqual(picking.magento_shipment_id, "501")
        self.assertEqual(picking.magento_shipment_increment_id, "100000501")
        self.assertEqual(picking.magento_source_code, "default")
        self.assertEqual(picking.magento_tracking_ref, "TRK-1, TRK-2")
        self.assertFalse(self._track_posts(), "tracks travel with the shipment")

    def test_partial_shipment_then_backorder(self):
        order = self._order(qty_bag=2.0, qty_hoodie=2.0)
        first = self._validate(self._delivery(order), qtys={self.bag: 1.0, self.hoodie: 0.0},
                               tracking="PART-1")
        payload = self._payload()
        self.assertEqual([(i["order_item_id"], i["qty"]) for i in payload["items"]], [(11, 1.0)],
                         "only the shipped quantity is sent")
        self.assertEqual([t["track_number"] for t in payload["tracks"]], ["PART-1"])
        backorder = self._delivery(order)
        self.assertEqual(len(backorder), 1)
        self.assertEqual(backorder.backorder_id, first)
        second = self._validate(backorder, tracking="PART-2")
        self.assertEqual(self.create_shipment.call_count, 2)
        payload = self._payload()
        self.assertEqual(sorted((i["order_item_id"], i["qty"]) for i in payload["items"]),
                         [(11, 1.0), (21, 2.0)])
        self.assertEqual([t["track_number"] for t in payload["tracks"]], ["PART-2"])
        self.assertEqual((first.magento_shipment_id, second.magento_shipment_id), ("501", "502"))

    def test_msi_source_from_location_map_and_legacy_mode(self):
        self.env["magento.stock.source.map"].create({
            "instance_id": self.instance.id, "source_code": "wh_east",
            "location_ids": [Command.set(self.warehouse.lot_stock_id.ids)],
        })
        order = self._order(qty_hoodie=0)
        picking = self._validate(self._delivery(order))
        self.assertEqual(self._payload()["arguments"]["extension_attributes"]["source_code"], "wh_east")
        self.assertEqual(picking.magento_source_code, "wh_east")
        self.assertNotIn("tracks", self._payload())

        self.instance.stock_push_mode = "legacy"
        order = self._order(magento_id="7002", qty_hoodie=0)
        self._validate(self._delivery(order))
        self.assertNotIn("arguments", self._payload(), "no MSI source outside MSI mode")

    def test_track_carrier_from_carrier_map(self):
        mapping = self.env["magento.carrier.map"].create({
            "instance_id": self.instance.id, "magento_code": "ups_GND", "name": "UPS Ground"})
        order = self._order(magento_shipping_method="ups_GND")
        picking = self._delivery(order)
        self.assertEqual(picking._magento_track_carrier(), ("ups", "UPS Ground"))
        # Reverse map from the transfer's delivery carrier (stock_delivery's carrier_id).
        self.env.cr.execute("UPDATE magento_carrier_map SET carrier_ref = %s WHERE id = %s",
                            ("delivery.carrier,77", mapping.id))
        self.env.invalidate_all()
        carrier = SimpleNamespace(_name="delivery.carrier", id=77, name="My UPS", delivery_type="fixed")
        Picking = type(picking)
        with patch.object(Picking, "_magento_odoo_carrier", lambda self: carrier):
            self.assertEqual(picking._magento_track_carrier(), ("ups", "UPS Ground"))
            mapping.magento_code = "flatrate_flatrate"
            self.assertEqual(picking._magento_track_carrier(), ("custom", "UPS Ground"),
                             "carriers Magento cannot track are sent as custom")
            other = SimpleNamespace(_name="delivery.carrier", id=78, name="Courier X",
                                    delivery_type="fixed")
        with patch.object(Picking, "_magento_odoo_carrier", lambda self: other):
            self.assertEqual(picking._magento_track_carrier(), ("custom", "Courier X"),
                             "unmapped carrier: custom code, carrier name")

    def test_tracking_added_after_shipment(self):
        order = self._order(qty_hoodie=0)
        picking = self._validate(self._delivery(order))
        self.assertNotIn("tracks", self._payload())
        self.assertEqual(picking.magento_shipment_id, "501")

        picking.magento_tracking_ref = "LATE-1"
        self.assertEqual(self._track_posts(), [{
            "order_id": 7001, "parent_id": 501, "track_number": "LATE-1",
            "title": "Flat Rate - Fixed", "carrier_code": "custom",
        }])
        # Magento now has LATE-1: only the new number is posted.
        self.magento_shipments[0]["tracks"] = [{"track_number": "LATE-1"}]
        picking.magento_tracking_ref = "LATE-1, LATE-2"
        self.assertEqual([t["track_number"] for t in self._track_posts()], ["LATE-1", "LATE-2"])
        self.magento_shipments[0]["tracks"].append({"track_number": "LATE-2"})
        action = picking.action_push_magento_tracking()
        self.assertEqual(len(self._track_posts()), 2, "nothing missing: nothing posted")
        self.assertEqual(action["params"]["type"], "info")
        self.assertEqual(self.create_shipment.call_count, 1)

    def test_tracking_push_failure_never_blocks_save(self):
        order = self._order(qty_hoodie=0)
        picking = self._validate(self._delivery(order))
        self.patch_api("_post", side_effect=UserError("boom"))
        picking.magento_tracking_ref = "FAIL-1"
        self.assertEqual(picking.magento_tracking_ref, "FAIL-1")
        self.assertIn("Magento tracking sync failed", picking.message_ids[0].body)

    def test_export_idempotent(self):
        order = self._order(qty_hoodie=0)
        # An earlier attempt created the shipment in Magento, without the track.
        self.magento_shipments.append(_shipment(601, 7001, [(11, 1.0)]))
        picking = self._validate(self._delivery(order), tracking="IDEM-1")
        self.assertFalse(self.create_shipment.called, "matching shipment linked, not re-posted")
        self.assertEqual(picking.magento_shipment_id, "601")
        self.assertEqual([t["track_number"] for t in self._track_posts()], ["IDEM-1"],
                         "the missing track is added to the linked shipment")
        # Pushing again is a no-op.
        self.assertFalse(picking._magento_create_shipment())
        self.assertEqual(self.create_shipment.call_count, 0)

    # ------------------------------------------------------------------ import
    def test_import_partial_shipment_with_backorder_and_tracks(self):
        order = self._order(qty_bag=2.0, qty_hoodie=0)
        self.magento_shipments.append(_shipment(701, 7001, [(11, 1.0)], tracks=["IMP-1"]))
        order._magento_sync_shipments()
        done = order.picking_ids.filtered(lambda p: p.state == "done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done.magento_shipment_id, "701")
        self.assertEqual(done.move_ids.quantity, 1.0)
        self.assertEqual(done.magento_tracking_ref, "IMP-1")
        if "carrier_tracking_ref" in done._fields:
            self.assertEqual(done.carrier_tracking_ref, "IMP-1")
        backorder = self._delivery(order)
        self.assertEqual(len(backorder), 1, "the rest waits in a backorder")
        self.assertEqual(backorder.move_ids.product_uom_qty, 1.0)
        self.assertFalse(self.create_shipment.called, "imported shipments are never pushed back")

        # Idempotent: a second sync changes nothing.
        order._magento_sync_shipments()
        self.assertEqual(len(order.picking_ids), 2)
        self.assertNotEqual(backorder.state, "done")

        # The rest ships in Magento: the backorder is validated.
        self.magento_shipments.append(_shipment(702, 7001, [(11, 1.0)], tracks=["IMP-2"]))
        order._magento_sync_shipments()
        self.assertEqual(backorder.state, "done")
        self.assertEqual(backorder.magento_shipment_id, "702")
        self.assertEqual(backorder.magento_tracking_ref, "IMP-2")
        self.assertEqual(order.delivery_status, "full")
        self.assertFalse(self.create_shipment.called)
        self.assertFalse(self._track_posts())

    def test_missing_stock_delivery(self):
        Picking = type(self.env["stock.picking"])
        if "carrier_tracking_ref" not in self.env["stock.picking"]._fields:
            self.assertEqual(self.env["stock.picking"]._magento_delivery_fields(), set())
        with patch.object(Picking, "_magento_delivery_fields", lambda self: set()):
            order = self._order(qty_hoodie=0)
            picking = self._delivery(order)
            self.assertFalse(picking._magento_odoo_carrier())
            self._validate(picking, tracking="NODLV-1")
            self.assertEqual([t["track_number"] for t in self._payload()["tracks"]], ["NODLV-1"])
            # Import still works without the Delivery fields.
            order2 = self._order(magento_id="7003", qty_bag=2.0, qty_hoodie=0)
            self.magento_shipments.append(_shipment(703, 7003, [(11, 2.0)], tracks=["NODLV-2"]))
            order2._magento_sync_shipments()
            done = order2.picking_ids.filtered(lambda p: p.state == "done")
            self.assertEqual(done.magento_tracking_ref, "NODLV-2")

    # ------------------------------------------------------------------ Odoo -> Magento lifecycle
    def _remote(self, *states, invoiced=0.0):
        """get_order answers ``states`` in turn (the last one repeats)."""
        answers = [{"entity_id": 7001, "state": s, "status": s if s != "new" else "pending",
                    "total_invoiced": invoiced} for s in states]

        def _get(api, order_id):
            return dict(answers.pop(0) if len(answers) > 1 else answers[0])

        return self.patch_api("get_order", side_effect=_get)

    def test_cancel_allowed(self):
        order = self._order(confirm=False)
        self._remote("processing", "canceled")
        order.action_cancel()
        self.assertEqual(order.state, "cancel")
        self.assertEqual(self._order_posts(), ["/rest/V1/orders/7001/cancel"])
        self.assertEqual(order.magento_state, "canceled")
        self.assertIn("cancelled from Odoo", order.message_ids[0].body)

    def test_cancel_already_canceled_in_magento(self):
        order = self._order(confirm=False)
        self._remote("canceled")
        order.action_cancel()
        self.assertEqual(order.state, "cancel")
        self.assertFalse(self._order_posts(), "idempotent: nothing posted")

    def test_cancel_refused(self):
        order = self._order(confirm=False)
        for states, invoiced, reason in ((("processing",), 10.0, "already invoiced"),
                                         (("complete",), 0.0, "can no longer be cancelled"),
                                         (("holded",), 0.0, "Release the hold")):
            self._remote(*states, invoiced=invoiced)
            with self.assertRaisesRegex(UserError, reason):
                order.action_cancel()
            self.assertNotEqual(order.state, "cancel")
        self.assertFalse(self._order_posts())

    def test_magento_refuses_cancel(self):
        order = self._order(confirm=False)
        self._remote("processing")
        self.patch_api("_post", return_value=False)
        with self.assertRaisesRegex(UserError, "Magento refused"):
            order.action_cancel()

    def test_hold_and_release(self):
        order = self._order()
        self._remote("processing", "holded")
        order.action_magento_hold()
        self.assertEqual(self._order_posts(), ["/rest/V1/orders/7001/hold"])
        self.assertTrue(order.magento_on_hold)
        self.assertIn("put on hold", order.message_ids[0].body)
        # Already on hold: nothing posted.
        self._remote("holded")
        order.action_magento_hold()
        self.assertEqual(len(self._order_posts()), 1)
        self.assertIn("Already on hold", order.message_ids[0].body)

        self._remote("holded", "processing")
        order.action_magento_unhold()
        self.assertEqual(self._order_posts()[-1], "/rest/V1/orders/7001/unhold")
        self.assertFalse(order.magento_on_hold)
        self._remote("processing")
        order.action_magento_unhold()
        self.assertEqual(len(self._order_posts()), 2, "not on hold: nothing posted")

        self._remote("complete")
        with self.assertRaisesRegex(UserError, "cannot be put on hold"):
            order.action_magento_hold()

    def test_cancel_by_workflow_never_calls_magento(self):
        order = self._order(confirm=False)
        order._magento_wf_cancel()
        self.assertEqual(order.state, "cancel")
        self.assertFalse(self.post.called)

    # ------------------------------------------------------------------ Magento -> Odoo lifecycle
    def _sync(self, data):
        self.patch_api("get_orders", side_effect=once([data]))
        counts = self.instance.sync_orders()
        self.env.invalidate_all()
        self.assertEqual(counts["errors"], 0, counts.get("error_messages"))
        return self.magento_orders(data["entity_id"])

    def _mg(self, entity_id, state, status=None):
        data = build_order(entity_id, [build_item(entity_id * 10 + 1, "24-MB01", qty=1.0, price=34.0,
                                                  product_id=1)],
                           state=state, status=status or state, email="agent-t-test@example.com")
        data["shipping_description"] = "Flat Rate - Fixed"
        return data

    def test_magento_cancel_reflected_when_safe(self):
        order = self._sync(self._mg(801, "processing"))
        self.assertEqual(order.state, "draft")
        order = self._sync(self._mg(801, "canceled"))
        self.assertEqual(order.state, "cancel")
        self.assertFalse(order.magento_cancel_conflict)
        self.assertFalse(self._order_posts(), "a Magento cancellation is never sent back")

    def test_magento_cancel_conflict_when_delivered(self):
        order = self._sync(self._mg(802, "processing"))
        order.with_context(**NO_PUSH).action_confirm()
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.with_context(skip_magento_sync=True)._action_done()
        order = self._sync(self._mg(802, "canceled"))
        self.assertEqual(order.state, "sale", "a delivered order is not cancelled silently")
        self.assertTrue(order.magento_cancel_conflict)
        notes = order.message_ids.filtered(lambda m: "not cancelled in Odoo" in (m.body or ""))
        self.assertEqual(len(notes), 1)
        # A re-sync does not repeat the note.
        self._sync(self._mg(802, "canceled"))
        notes = order.message_ids.filtered(lambda m: "not cancelled in Odoo" in (m.body or ""))
        self.assertEqual(len(notes), 1)
        self.assertFalse(self.create_shipment.called, "no shipment pushed for a canceled order")
        self.assertFalse(self._order_posts())

    def test_magento_hold_reflected(self):
        order = self._sync(self._mg(803, "holded", status="holded"))
        self.assertTrue(order.magento_on_hold)
        self.assertNotEqual(order.state, "cancel")
        order = self._sync(self._mg(803, "processing"))
        self.assertFalse(order.magento_on_hold)
        self.assertFalse(self._order_posts())
