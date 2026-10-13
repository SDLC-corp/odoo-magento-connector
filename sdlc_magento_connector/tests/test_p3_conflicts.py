"""Phase 3.7: conflict detection for orders changed in Magento after Odoo processed them.

Mocked: the Magento order JSON (shape of GET /V1/orders) is fed to the order import
(``instance.sync_orders``), like a scheduled sync or a webhook-triggered resync would.
"""
from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import MagentoTestCommon, build_address, build_item, build_order

NO_PUSH = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)


def history(entity_id, status, created_at, comment=""):
    return {"entity_id": entity_id, "parent_id": 1, "status": status, "created_at": created_at,
            "comment": comment, "entity_name": "order", "is_customer_notified": 0}


@tagged("post_install", "-at_install", "magento")
class TestP3Conflicts(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env["product.product"].with_context(**NO_PUSH)
        Map = cls.env["magento.product.map"].with_context(**NO_PUSH)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.pack = Product.create({"name": "Strive Shoulder Pack", "default_code": "24-MB04",
                                   "type": "consu", "list_price": 32.0})
        cls.bottle = Product.create({"name": "Water Bottle", "default_code": "24-UG06",
                                     "type": "consu", "list_price": 7.0})
        for product, magento_id in ((cls.bag, 1), (cls.pack, 4), (cls.bottle, 44)):
            Map.create({"instance_id": cls.instance.id, "magento_id": str(magento_id),
                        "sku": product.default_code, "name": product.name,
                        "odoo_product_id": product.id})
        cls.Conflict = cls.env["magento.order.conflict"]

    def setUp(self):
        super().setUp()
        self.patch_order_side_calls()

    # ------------------------------------------------------------------ helpers
    def _data(self, entity_id, bag_qty=1.0, bag_price=34.0, pack_qty=2.0, bottle=False,
              pack=True, status="pending", state="new", histories=(), shipping=None,
              email="cf-test@example.com", method="checkmo", updated_at="2026-10-01 10:00:00"):
        items = [build_item(entity_id * 10 + 1, "24-MB01", qty=bag_qty, price=bag_price, product_id=1)]
        if pack:
            items.append(build_item(entity_id * 10 + 2, "24-MB04", qty=pack_qty, price=32.0, product_id=4))
        if bottle:
            items.append(build_item(entity_id * 10 + 3, "24-UG06", qty=1.0, price=7.0, product_id=44))
        data = build_order(entity_id, items, status=status, state=state, email=email,
                           shipping=shipping)
        data["updated_at"] = updated_at
        data["payment"]["method"] = method
        data["status_histories"] = list(histories)
        return data

    def _sync(self, data):
        counts = self.import_orders([data])
        self.env.invalidate_all()
        self.assertEqual(counts["errors"], 0, counts.get("error_messages"))
        return self.magento_orders(data["entity_id"])

    def _confirmed(self, entity_id, **kwargs):
        order = self._sync(self._data(entity_id, **kwargs))
        order.with_context(**NO_PUSH).action_confirm()
        self.assertEqual(order.state, "sale")
        return order

    def _deliver(self, order):
        picking = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.with_context(skip_magento_sync=True)._action_done()
        # Linked to a Magento shipment: nothing to push on the next sync.
        picking.with_context(skip_magento_sync=True).write({"magento_shipment_id": "9%s" % order.id})
        self.assertEqual(picking.state, "done")
        return picking

    def _rows(self, order, ctype=None, state="open"):
        domain = [("order_id", "=", order.id)]
        if ctype:
            domain.append(("conflict_type", "=", ctype))
        if state:
            domain.append(("state", "=", state))
        return self.Conflict.search(domain)

    def _line(self, order, product):
        return order.order_line.filtered(lambda l: l.product_id == product)

    def assertFlag(self, order):
        order.invalidate_recordset()
        expected = bool(self._rows(order))
        self.assertEqual(order.magento_sync_conflict, expected,
                         "magento_sync_conflict must mean 'has open conflicts'")
        self.assertEqual(order.magento_conflict_count, len(self._rows(order)))
        found = self.env["sale.order"].search([("magento_sync_conflict", "=", True), ("id", "=", order.id)])
        self.assertEqual(bool(found), expected)

    # ------------------------------------------------------------------ snapshot
    def test_snapshot_stored_on_first_processing(self):
        order = self._sync(self._data(3001))
        snapshot = order.magento_import_snapshot
        self.assertEqual(set(snapshot["items"]), {"30011", "30012"})
        self.assertEqual(snapshot["items"]["30011"]["sku"], "24-MB01")
        self.assertEqual(snapshot["items"]["30012"]["qty"], 2.0)
        self.assertEqual(snapshot["items"]["30011"]["price"], 34.0)
        self.assertEqual(snapshot["totals"]["grand_total"], 98.0)
        self.assertEqual(snapshot["status"], "pending")
        self.assertEqual(snapshot["payment_method"], "checkmo")
        self.assertEqual(snapshot["customer"]["email"], "cf-test@example.com")
        self.assertEqual(snapshot["updated_at"], "2026-10-01 10:00:00")
        self.assertTrue(snapshot["shipping_address_hash"])
        self.assertEqual(set(snapshot["odoo_lines"]), {"30011", "30012"})
        self.assertFalse(self._rows(order, state=None), "A first import is not a conflict")
        self.assertFlag(order)

    # ------------------------------------------------------------------ quotations
    def test_quotation_changes_are_applied_and_recorded_resolved_auto(self):
        order = self._sync(self._data(3002))
        new_ship = build_address(street=("1 New Street",), city="Detroit", postcode="48201",
                                 address_type="shipping", parent_id=3002)
        order = self._sync(self._data(3002, bag_qty=3.0, bag_price=30.0, bottle=True,
                                      shipping=new_ship, method="banktransfer",
                                      updated_at="2026-10-02 10:00:00"))
        self.assertEqual(order.state, "draft")
        self.assertEqual(self._line(order, self.bag).product_uom_qty, 3.0, "Magento wins on a quotation")
        self.assertEqual(self._line(order, self.bag).price_unit, 30.0)
        self.assertTrue(self._line(order, self.bottle))
        self.assertEqual(order.partner_shipping_id.street, "1 New Street")
        self.assertEqual(order.partner_shipping_id.type, "delivery")
        auto = self._rows(order, state="resolved_auto")
        self.assertEqual(set(auto.mapped("conflict_type")),
                         {"items_changed", "qty_changed", "price_changed", "totals_changed",
                          "address_changed", "payment_changed"})
        self.assertTrue(all(r.severity == "info" and not r.resolved_by for r in auto))
        self.assertFalse(self._rows(order))
        self.assertEqual(order.magento_import_snapshot["items"]["30021"]["qty"], 3.0,
                         "The snapshot follows Magento on a quotation")
        # Same data again: nothing new is recorded.
        self._sync(self._data(3002, bag_qty=3.0, bag_price=30.0, bottle=True, shipping=new_ship,
                              method="banktransfer", updated_at="2026-10-02 10:00:00"))
        self.assertEqual(len(self._rows(order, state=None)), len(auto))
        self.assertFlag(order)

    def test_quotation_line_added_in_odoo_is_edited_in_odoo(self):
        order = self._sync(self._data(3003))
        order.write({"order_line": [Command.create({
            "product_id": self.bottle.id, "product_uom_qty": 1, "price_unit": 7.0})]})
        order = self._sync(self._data(3003))
        self.assertIn(self.bottle, order.order_line.product_id, "Odoo lines are kept")
        row = self._rows(order, "edited_in_odoo")
        self.assertEqual(len(row), 1)
        self.assertEqual(row.severity, "warn")
        self.assertFalse(row.can_accept, "Informational: Keep Odoo only")
        self.assertIn("added in Odoo", row.summary)
        self.assertFlag(order)
        # Removed again in Odoo: back in sync, resolved automatically.
        self._line(order, self.bottle).unlink()
        self._sync(self._data(3003))
        self.assertEqual(row.state, "resolved_auto")
        self.assertFlag(order)

    # ------------------------------------------------------------------ confirmed: accept / keep
    def test_confirmed_qty_change_accept_rewrites_lines(self):
        order = self._confirmed(3010)
        order = self._sync(self._data(3010, bag_qty=3.0))
        self.assertEqual(self._line(order, self.bag).product_uom_qty, 1.0, "Lines are never overwritten")
        qty = self._rows(order, "qty_changed")
        self.assertEqual(len(qty), 1)
        self.assertEqual((qty.severity, qty.can_accept), ("warn", True))
        self.assertEqual(qty.diff["rows"][0]["before"], "1")
        self.assertEqual(qty.diff["rows"][0]["after"], "3")
        self.assertIn("Magento (now)", qty.diff_html)
        self.assertTrue(self._rows(order, "totals_changed"))
        self.assertTrue(order.magento_conflict_note)
        self.assertFlag(order)

        order.action_lock()
        qty.action_accept()
        self.assertEqual(self._line(order, self.bag).product_uom_qty, 3.0)
        self.assertTrue(order.locked, "Locked again after the rewrite")
        self.assertEqual(qty.state, "accepted")
        self.assertEqual(qty.resolved_by, self.env.user)
        self.assertEqual(self._rows(order, "totals_changed", state="accepted").state, "accepted",
                         "One rewrite resolves every line conflict of the order")
        self.assertFalse(order.magento_conflict_note)
        self.assertTrue(order.message_ids.filtered(lambda m: "Magento version applied" in (m.body or "")))
        self.assertEqual(order.magento_import_snapshot["items"]["30101"]["qty"], 3.0)
        self.assertFlag(order)
        # The same Magento data again: in sync, nothing re-opens.
        self._sync(self._data(3010, bag_qty=3.0))
        self.assertFalse(self._rows(order))
        self.assertFlag(order)

    def test_confirmed_keep_odoo_acknowledges_this_magento_version(self):
        order = self._confirmed(3011)
        self._sync(self._data(3011, bag_qty=3.0))
        rows = self._rows(order)
        self.assertEqual(set(rows.mapped("conflict_type")), {"qty_changed", "totals_changed"})
        rows.action_keep()
        self.assertEqual(set(rows.mapped("state")), {"kept"})
        self.assertEqual(self._line(order, self.bag).product_uom_qty, 1.0)
        self.assertFlag(order)
        self._sync(self._data(3011, bag_qty=3.0))
        self.assertFalse(self._rows(order), "A kept Magento version never re-opens")
        # A newer Magento change does.
        self._sync(self._data(3011, bag_qty=4.0))
        self.assertEqual(len(self._rows(order, "qty_changed")), 1)
        self.assertFlag(order)

    def test_items_added_and_removed_in_magento(self):
        order = self._confirmed(3012)
        order = self._sync(self._data(3012, pack=False, bottle=True))
        row = self._rows(order, "items_changed")
        self.assertEqual(len(row), 1)
        labels = " ".join(r["label"] for r in row.diff["rows"])
        self.assertIn("24-UG06 (added in Magento)", labels)
        self.assertIn("24-MB04 (removed in Magento)", labels)
        row.action_accept()
        self.assertEqual(self._line(order, self.bottle).product_uom_qty, 1.0)
        self.assertEqual(self._line(order, self.pack).product_uom_qty, 0.0,
                         "A confirmed line is never deleted: quantity 0")
        self.assertEqual(row.state, "accepted")
        self.assertFlag(order)

    def test_price_changed_detected(self):
        order = self._confirmed(3013)
        self._sync(self._data(3013, bag_price=29.0))
        row = self._rows(order, "price_changed")
        self.assertEqual(len(row), 1)
        self.assertEqual(row.diff["rows"][0]["after"], "29.00")
        self.assertEqual(row.diff["rows"][0]["odoo"], "34.00")

    # ------------------------------------------------------------------ delivered / invoiced
    def test_delivered_order_conflict_is_critical_keep_only(self):
        order = self._confirmed(3020)
        self._deliver(order)
        self._sync(self._data(3020, bag_qty=3.0))
        row = self._rows(order, "qty_changed")
        self.assertEqual((row.severity, row.can_accept), ("crit", False))
        self.assertIn("credit", row.guidance)
        with self.assertRaises(UserError):
            row.action_accept()
        self.assertEqual(row.state, "open")
        row.action_keep()
        self.assertEqual(row.state, "kept")
        self.assertEqual(self._line(order, self.bag).product_uom_qty, 1.0)

    # ------------------------------------------------------------------ address
    def test_address_change_before_delivery_apply_new_address(self):
        order = self._confirmed(3030)
        picking = order.picking_ids
        old_delivery = order.partner_shipping_id
        new_ship = build_address(street=("77 Harbour Road",), city="Detroit", postcode="48201",
                                 address_type="shipping", parent_id=3030)
        self._sync(self._data(3030, shipping=new_ship))
        row = self._rows(order, "address_changed")
        self.assertEqual(len(row), 1)
        self.assertTrue(row.can_accept)
        self.assertEqual(order.partner_shipping_id, old_delivery, "Not applied without a decision")
        self.assertIn("77 Harbour Road", str(row.diff))
        row.action_accept()
        self.assertEqual(order.partner_shipping_id.street, "77 Harbour Road")
        self.assertEqual(order.partner_shipping_id.parent_id, order.partner_id)
        self.assertEqual(order.partner_shipping_id.type, "delivery")
        self.assertEqual(picking.partner_id, order.partner_shipping_id, "Open delivery follows")
        self.assertEqual(row.state, "accepted")
        self._sync(self._data(3030, shipping=new_ship))
        self.assertFalse(self._rows(order), "Applied address: in sync")
        self.assertFlag(order)

    def test_address_change_after_delivery_is_keep_only(self):
        order = self._confirmed(3031)
        self._deliver(order)
        new_ship = build_address(street=("5 Late Lane",), address_type="shipping", parent_id=3031)
        self._sync(self._data(3031, shipping=new_ship))
        row = self._rows(order, "address_changed")
        self.assertEqual((row.severity, row.can_accept), ("crit", False))

    # ------------------------------------------------------------------ customer / payment
    def test_customer_and_payment_changed(self):
        order = self._confirmed(3040)
        self._sync(self._data(3040, email="someone-else@example.com", method="banktransfer"))
        customer = self._rows(order, "customer_changed")
        payment = self._rows(order, "payment_changed")
        self.assertEqual(len(customer), 1)
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.diff["rows"][0]["after"], "banktransfer")
        payment.action_accept()
        self.assertEqual(order.magento_import_snapshot["payment_method"], "banktransfer")
        customer.action_keep()
        self.assertFalse(self._rows(order))
        self._sync(self._data(3040, email="someone-else@example.com", method="banktransfer"))
        self.assertFalse(self._rows(order))
        self.assertFlag(order)

    # ------------------------------------------------------------------ status history
    def test_rapid_status_changes_are_all_recorded_in_order(self):
        order = self._sync(self._data(3050))
        events = order.magento_status_event_ids.sorted("sequence")
        self.assertEqual(events.mapped("to_status"), ["pending"])
        self.assertEqual(events.source, "import")
        histories = [
            history(501, "holded", "2026-10-02 10:00:01", "On hold"),
            history(502, "processing", "2026-10-02 10:00:01", "Released"),
            history(503, "processing", "2026-10-02 10:00:02", "Just a comment"),
            history(504, "holded", "2026-10-02 10:00:03"),
            history(505, "processing", "2026-10-02 10:00:04"),
        ]
        # Magento lists the history newest first; the final status is all a sync sees.
        data = self._data(3050, status="processing", state="processing",
                          histories=list(reversed(histories)), updated_at="2026-10-02 10:00:04")
        self._sync(data)
        events = order.magento_status_event_ids.sorted("sequence")
        transitions = events.filtered("is_transition")
        self.assertEqual([(e.from_status or "", e.to_status) for e in transitions], [
            ("", "pending"), ("pending", "holded"), ("holded", "processing"),
            ("processing", "holded"), ("holded", "processing"),
        ], "Every transition is kept, in order, never only the last one")
        self.assertEqual(len(events), 6, "Comment-only history entries are kept too")
        self.assertFalse(events.filtered("is_regression"))
        self._sync(data)
        self.assertEqual(len(order.magento_status_event_ids), 6, "A re-sync records nothing twice")
        self.assertFalse(self._rows(order))

    def test_status_regression_opens_and_resolves(self):
        order = self._confirmed(3051)
        self._sync(self._data(3051, status="complete", state="complete",
                              histories=[history(511, "complete", "2026-10-02 09:00:00")]))
        self.assertFalse(self._rows(order))
        self._sync(self._data(3051, status="processing", state="processing", histories=[
            history(511, "complete", "2026-10-02 09:00:00"),
            history(512, "processing", "2026-10-02 09:30:00", "Reopened")]))
        row = self._rows(order, "status_regressed")
        self.assertEqual(len(row), 1)
        self.assertEqual(row.diff["rows"][0]["before"], "complete")
        self.assertEqual(row.diff["rows"][0]["after"], "processing")
        regression = order.magento_status_event_ids.filtered("is_regression")
        self.assertEqual(regression.mapped("to_status"), ["processing"])
        self.assertFlag(order)
        self._sync(self._data(3051, status="complete", state="complete", histories=[
            history(511, "complete", "2026-10-02 09:00:00"),
            history(512, "processing", "2026-10-02 09:30:00"),
            history(513, "complete", "2026-10-02 10:00:00")]))
        self.assertEqual(row.state, "resolved_auto")
        self.assertFlag(order)

    def test_status_changed_by_odoo_action_is_recorded(self):
        order = self._confirmed(3052)
        order._magento_store_remote_state({"state": "holded", "status": "holded"})
        event = order.magento_status_event_ids.sorted("sequence")[-1:]
        self.assertEqual((event.from_status, event.to_status, event.source),
                         ("pending", "holded", "odoo"))
        self._sync(self._data(3052, status="holded", state="holded"))
        self.assertEqual(len(order.magento_status_event_ids), 2, "The sync does not repeat it")

    # ------------------------------------------------------------------ cancel after fulfilment
    def test_canceled_after_fulfilment(self):
        order = self._confirmed(3060)
        self._deliver(order)
        self._sync(self._data(3060, status="canceled", state="canceled"))
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.magento_cancel_conflict)
        row = self._rows(order, "canceled_after_fulfilment")
        self.assertEqual((row.severity, row.can_accept), ("crit", False))
        self.assertIn("return", row.guidance)
        with self.assertRaises(UserError):
            row.action_accept()
        self._sync(self._data(3060, status="canceled", state="canceled"))
        self.assertEqual(len(self._rows(order, "canceled_after_fulfilment", state=None)), 1)
        self.assertFlag(order)

    # ------------------------------------------------------------------ edited in Odoo
    def test_confirmed_line_edited_in_odoo(self):
        order = self._confirmed(3070)
        self._line(order, self.bag).product_uom_qty = 5.0
        self._sync(self._data(3070))
        row = self._rows(order, "edited_in_odoo")
        self.assertEqual((row.severity, row.can_accept), ("warn", False))
        self.assertFalse(self._rows(order, "qty_changed"), "Magento did not change: not a Magento conflict")
        self._line(order, self.bag).product_uom_qty = 1.0
        self._sync(self._data(3070))
        self.assertEqual(row.state, "resolved_auto")
        self.assertFlag(order)

    def test_same_change_on_both_sides_is_in_sync(self):
        order = self._confirmed(3071)
        self._line(order, self.bag).product_uom_qty = 3.0
        self._sync(self._data(3071, bag_qty=3.0))
        self.assertFalse(self._rows(order, "qty_changed"))
        self.assertFalse(self._rows(order, "edited_in_odoo"))

    # ------------------------------------------------------------------ resync back in sync
    def test_resync_back_in_sync_resolves_automatically(self):
        order = self._confirmed(3080)
        self._sync(self._data(3080, bag_qty=3.0))
        rows = self._rows(order)
        self.assertTrue(rows)
        self.assertTrue(order.magento_sync_conflict)
        self._sync(self._data(3080, bag_qty=1.0))
        self.assertEqual(set(rows.mapped("state")), {"resolved_auto"})
        self.assertFalse(any(rows.mapped("resolved_by")))
        self.assertFalse(order.magento_conflict_note)
        self.assertFlag(order)

    def test_no_duplicate_open_rows_per_type(self):
        order = self._confirmed(3081)
        for qty in (3.0, 3.0, 4.0, 5.0):
            self._sync(self._data(3081, bag_qty=qty))
        rows = self._rows(order, "qty_changed")
        self.assertEqual(len(rows), 1, "One open row per (order, type), refreshed in place")
        self.assertEqual(rows.diff["rows"][0]["after"], "5")
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.Conflict.create({"order_id": order.id, "conflict_type": "qty_changed"})
            self.env.flush_all()

    # ------------------------------------------------------------------ bulk
    def test_bulk_accept_and_keep(self):
        first = self._confirmed(3090)
        second = self._confirmed(3091)
        delivered = self._confirmed(3092)
        self._deliver(delivered)
        for entity_id in (3090, 3091, 3092):
            self._sync(self._data(entity_id, bag_qty=2.0))
        rows = self.Conflict.search([("order_id", "in", (first | second | delivered).ids),
                                     ("state", "=", "open")])
        self.assertEqual(len(rows), 6)
        result = rows.action_accept()
        self.assertEqual(result["params"]["type"], "warning", "Rows that cannot be applied are skipped")
        self.assertEqual(self._line(first, self.bag).product_uom_qty, 2.0)
        self.assertEqual(self._line(second, self.bag).product_uom_qty, 2.0)
        self.assertEqual(self._line(delivered, self.bag).product_uom_qty, 1.0)
        self.assertFalse(self._rows(first) | self._rows(second))
        left = self._rows(delivered)
        self.assertEqual(len(left), 2)
        left.action_keep()
        self.assertFalse(self._rows(delivered))
        for order in (first, second, delivered):
            self.assertFlag(order)

    # ------------------------------------------------------------------ legacy / API compat
    def test_order_without_snapshot_uses_odoo_lines_as_baseline(self):
        order = self._confirmed(3100)
        order.sudo().magento_import_snapshot = False
        self._sync(self._data(3100, bag_qty=3.0))
        self.assertTrue(order.magento_import_snapshot.get("legacy"))
        self.assertEqual(len(self._rows(order, "qty_changed")), 1)
        self.assertTrue(order.magento_sync_conflict)

    def test_set_conflict_helper_keeps_flag_meaning(self):
        order = self._confirmed(3101)
        order._magento_set_conflict(["24-MB01: quantity 3 in Magento, 1 in Odoo."])
        self.assertTrue(order.magento_sync_conflict)
        self.assertEqual(len(self._rows(order, "items_changed")), 1)
        self.assertFlag(order)
