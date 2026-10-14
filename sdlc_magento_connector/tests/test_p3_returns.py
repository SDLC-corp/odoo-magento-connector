"""Roadmap 3.2: full returns lifecycle.

Odoo -> Magento: credit memo payload (items, shipping, adjustments, return_to_stock_items),
offline (/V1/order/{id}/refund) vs online (/V1/invoice/{id}/refund) refunds.
Magento -> Odoo: restock vs refund only, return location rules, adjustment lines on the
instance products, reasons, chatter and the stock push mark. Both directions idempotent.
"""
from odoo import Command
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.models.magento_returns import _create_invoice_refund
from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MagentoTestCommon

ORDER_ID = "7001"
BAG_ITEM = 11


def mg_credit_memo(entity_id, items, *, shipping=0.0, positive=0.0, negative=0.0,
                   restock_items=None, comments=None, transaction_id=None, state=2):
    """Magento credit memo as returned by GET /V1/creditmemo/{id} (items: (id, qty, price))."""
    rows = []
    subtotal = 0.0
    for item_id, qty, price in items:
        subtotal += qty * price
        rows.append({
            "entity_id": entity_id * 10 + len(rows),
            "order_item_id": item_id,
            "sku": "24-MB01",
            "qty": qty,
            "price": price,
            "row_total": qty * price,
            "tax_amount": 0,
            "discount_amount": 0,
            "weee_tax_applied_row_amount": 0,
        })
    doc = {
        "entity_id": entity_id,
        "increment_id": "%09d" % entity_id,
        "order_id": int(ORDER_ID),
        "state": state,
        "created_at": "2026-10-12 09:00:00",
        "subtotal": subtotal,
        "discount_amount": 0,
        "tax_amount": 0,
        "shipping_amount": shipping,
        "shipping_incl_tax": shipping,
        "adjustment_positive": positive,
        "adjustment_negative": negative,
        "grand_total": subtotal + shipping + positive - negative,
        "base_grand_total": subtotal + shipping + positive - negative,
        "items": rows,
    }
    if restock_items is not None:
        doc["extension_attributes"] = {"return_to_stock_items": restock_items}
    if comments:
        doc["comments"] = [{"comment": text} for text in comments]
    if transaction_id:
        doc["transaction_id"] = transaction_id
    return doc


@tagged("post_install", "-at_install", "magento")
class TestP3Returns(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.instance.write({"stock_push_enabled": True})
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        Location = cls.env["stock.location"]
        cls.returns_location = Location.create({
            "name": "Returns", "usage": "internal", "location_id": cls.stock_location.id})
        cls.damaged_location = Location.create({
            "name": "Damaged", "usage": "internal", "location_id": cls.warehouse.view_location_id.id})
        cls.bags = cls.env["product.category"].create({"name": "Bags"})
        Product = cls.env["product.product"].with_context(**ctx)
        cls.bag = Product.create({
            "name": "Joust Duffle Bag", "default_code": "24-MB01", "detailed_type": "product", "list_price": 34.0, "categ_id": cls.bags.id,
        })
        cls.env["magento.product.map"].with_context(**ctx).create({
            "instance_id": cls.instance.id, "magento_id": "1", "sku": "24-MB01",
            "name": "Joust Duffle Bag", "odoo_product_id": cls.bag.id, "type_id": "simple",
        })
        cls.goodwill = Product.create({"name": "Goodwill", "type": "service", "list_price": 0.0})
        cls.partner = cls.env["res.partner"].create({
            "name": "Agent RT", "email": "agent-rt-test@example.com"})
        shipping_product = cls.instance._magento_service_product("shipping")
        cls.order = cls.env["sale.order"].with_context(**ctx).create({
            "partner_id": cls.partner.id,
            "magento_instance_id": cls.instance.id,
            "magento_order_id": ORDER_ID,
            "magento_increment_id": "000007001",
            "magento_status": "complete",
            "magento_payment_method": "checkmo",
            "warehouse_id": cls.warehouse.id,
            "order_line": [
                Command.create({
                    "product_id": cls.bag.id, "product_uom_qty": 2.0, "price_unit": 34.0,
                    "tax_id": [Command.clear()], "magento_item_id": str(BAG_ITEM),
                    "magento_line_role": "product", "magento_line_key": str(BAG_ITEM),
                }),
                Command.create({
                    "product_id": shipping_product.id, "name": "Flat Rate - Fixed",
                    "product_uom_qty": 1.0, "price_unit": 10.0, "tax_id": [Command.clear()],
                    "magento_line_role": "shipping", "magento_line_key": "shipping",
                }),
            ],
        }).with_env(cls.env)
        cls.bag_line = cls.order.order_line.filtered(lambda l: l.product_id == cls.bag)
        cls.shipping_line = cls.order.order_line - cls.bag_line
        cls.order.with_context(**ctx).action_confirm()
        cls.delivery = cls.order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        for move in cls.delivery.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        cls.delivery.with_context(**ctx)._action_done()
        cls.Reason = cls.env["magento.return.reason"]
        cls.Rule = cls.env["magento.return.location.rule"]

    def setUp(self):
        super().setUp()
        self.patch_api("get_credit_memos", return_value=[])
        self.patch_api("get_credit_memo", side_effect=lambda api, cid: {
            "entity_id": cid, "increment_id": "%09d" % int(cid)})
        self.create_offline = self.patch_api("create_credit_memo", return_value=901)
        self.create_online = self.patch_api("create_invoice_refund", return_value=902)

    # ------------------------------------------------------------------ helpers
    def _refund(self, lines, post=True):
        """Credit note of the order; ``lines``: (sale line, qty) or a line vals dict."""
        commands = []
        for line in lines:
            if isinstance(line, dict):
                commands.append(Command.create(line))
            else:
                sale_line, qty = line
                commands.append(Command.create(sale_line._prepare_invoice_line(quantity=qty)))
        move = self.env["account.move"].with_context(
            skip_magento_sync=True, default_move_type="out_refund").create({
                "move_type": "out_refund",
                "partner_id": self.partner.id,
                "invoice_origin": self.order.name,
                "invoice_line_ids": commands,
            }).with_env(self.env)
        self.assertEqual(move.magento_sale_order_id, self.order)
        if post:
            move.action_post()
        return move

    def _invoice(self, magento_invoice_id):
        move = self.env["account.move"].with_context(
            skip_magento_sync=True, default_move_type="out_invoice").create({
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "invoice_origin": self.order.name,
                "invoice_line_ids": [Command.create(self.bag_line._prepare_invoice_line(quantity=2.0))],
            })
        move.with_context(skip_magento_sync=True).action_post()
        move.with_context(skip_magento_sync=True).write({"magento_invoice_id": magento_invoice_id})
        return move.with_env(self.env)

    def _odoo_return(self, qty=1.0, reason=None):
        """Odoo return (stock.return.picking) of ``qty`` bags, validated."""
        wizard = self.env["stock.return.picking"].with_context(
            active_id=self.delivery.id, active_ids=self.delivery.ids, active_model="stock.picking",
        ).create({"picking_id": self.delivery.id})
        wizard.product_return_moves.write({"quantity": qty})
        if reason:
            wizard.magento_return_reason_id = reason
        picking = self.env["stock.picking"].browse(wizard.create_returns()["res_id"])
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking._action_done()
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.picking_type_code, "incoming")
        return picking

    def _payload(self, mock):
        self.assertEqual(mock.call_count, 1, "exactly one POST expected")
        api, order_id, payload = mock.call_args.args
        self.assertEqual(order_id, ORDER_ID)
        return payload

    def _dirty_products(self):
        self.env.flush_all()
        self.env.cr.execute("SELECT product_id FROM magento_stock_dirty WHERE instance_id = %s",
                            [self.instance.id])
        return {row[0] for row in self.env.cr.fetchall()}

    def _forget_stock_marks(self):
        self.env.cr.execute("DELETE FROM magento_stock_dirty")
        for key in list(self.env.cr.precommit.data):
            if key.startswith("sdlc_magento_connector.stock_"):
                self.env.cr.precommit.data.pop(key)

    def _messages(self, record):
        return " ".join(record.message_ids.mapped("body"))

    # ------------------------------------------------------------------ Odoo -> Magento
    def test_offline_refund_payload_items_shipping_adjustments(self):
        fee = self.instance._magento_return_line_vals("return_fee", -2.0)
        extra = self.instance._magento_return_line_vals("refund_adjustment", 3.0)
        goodwill = {"product_id": self.goodwill.id, "name": "Goodwill", "quantity": 1.0,
                    "price_unit": 1.5, "tax_ids": [Command.clear()]}
        move = self._refund([(self.bag_line, 1.0), (self.shipping_line, 1.0), fee, extra, goodwill])
        self.assertEqual(move.amount_total, 34.0 + 10.0 - 2.0 + 3.0 + 1.5)
        payload = self._payload(self.create_offline)
        self.assertFalse(self.create_online.called)
        self.assertEqual(payload["items"], [{"order_item_id": BAG_ITEM, "qty": 1.0}])
        self.assertEqual(payload["arguments"]["shipping_amount"], 10.0)
        self.assertEqual(payload["arguments"]["adjustment_positive"], 4.5)
        self.assertEqual(payload["arguments"]["adjustment_negative"], 2.0)
        self.assertNotIn("extension_attributes", payload["arguments"])
        self.assertNotIn("isOnline", payload)
        self.assertEqual(move.magento_credit_memo_id, "901")
        self.assertEqual(move.magento_refund_mode, "offline")
        self.assertFalse(move.magento_restock)
        self.assertIn("offline refund", self._messages(move))
        self.assertIn("901", self._messages(self.order) + self._messages(move))

    def test_offline_refund_without_shipping_sends_zero_shipping(self):
        """Without shipping line the shipping refund is 0, never Magento's default (all)."""
        self._refund([(self.bag_line, 1.0)])
        payload = self._payload(self.create_offline)
        self.assertEqual(payload["arguments"]["shipping_amount"], 0.0)
        self.assertEqual(payload["arguments"]["adjustment_positive"], 0.0)
        self.assertEqual(payload["arguments"]["adjustment_negative"], 0.0)

    def test_lower_refund_price_becomes_negative_adjustment(self):
        vals = self.bag_line._prepare_invoice_line(quantity=1.0)
        vals["price_unit"] = 30.0
        self._refund([vals])
        payload = self._payload(self.create_offline)
        self.assertEqual(payload["items"], [{"order_item_id": BAG_ITEM, "qty": 1.0}])
        self.assertEqual(payload["arguments"]["adjustment_negative"], 4.0)

    def test_online_refund_uses_invoice_endpoint(self):
        self.instance.magento_refund_online = True
        self.order.magento_payment_method = "braintree"
        self._invoice("501")
        move = self._refund([(self.bag_line, 1.0)])
        self.assertFalse(self.create_offline.called, "online refunds never use /order/{id}/refund")
        payload = self._payload(self.create_online)
        self.assertIs(payload["isOnline"], True)
        self.assertEqual(payload["_magento_invoice_id"], "501")
        self.assertEqual(move.magento_credit_memo_id, "902")
        self.assertEqual(move.magento_refund_mode, "online")
        self.assertIn("501", self._messages(move))

    def test_invoice_refund_api_call(self):
        post = self.patch_api("_post", return_value=55)
        # The real method (create_invoice_refund is mocked in setUp).
        result = _create_invoice_refund(
            MagentoAPI(self.instance.sudo()), ORDER_ID, {"items": [{"order_item_id": 1, "qty": 1}], "isOnline": True,
                       "_magento_invoice_id": "501"})
        self.assertEqual(result, 55)
        _api, endpoint, body = post.call_args.args
        self.assertEqual(endpoint, "/rest/V1/invoice/501/refund")
        self.assertNotIn("_magento_invoice_id", body)
        self.assertIs(body["isOnline"], True)

    def test_refund_route_by_instance_and_payment_method(self):
        self._invoice("501")
        move = self._refund([(self.bag_line, 1.0)], post=False)
        self.assertEqual(move._magento_refund_route()[0], "offline", "instance option off")
        self.instance.magento_refund_online = True
        mode, _invoice, why = move._magento_refund_route()
        self.assertEqual(mode, "offline", "check/money order cannot refund online")
        self.assertIn("checkmo", why)
        Map = self.env["magento.payment.map"]
        checkmo = Map.create({"instance_id": self.instance.id, "magento_method": "checkmo",
                              "refund_mode": "online"})
        self.assertEqual(move._magento_refund_route()[:2], ("online", "501"), "explicit mapping wins")
        checkmo.refund_mode = "offline"
        self.order.magento_payment_method = "braintree"
        self.assertEqual(move._magento_refund_route()[0], "online")
        Map.create({"instance_id": self.instance.id, "magento_method": "braintree",
                    "refund_mode": "offline"})
        self.assertEqual(move._magento_refund_route()[0], "offline")

    def test_return_picking_credit_note_sends_return_to_stock_items(self):
        damaged = self.Reason.create({"code": "DMG", "name": "Damaged", "instance_id": self.instance.id})
        returned = self._odoo_return(qty=1.0, reason=damaged)
        self.assertEqual(returned.magento_return_reason_id, damaged)
        self.assertEqual(self.bag_line.qty_delivered, 1.0)
        move = self._refund([(self.bag_line, 1.0)])
        payload = self._payload(self.create_offline)
        self.assertEqual(payload["arguments"]["extension_attributes"],
                         {"return_to_stock_items": [BAG_ITEM]})
        self.assertTrue(payload["appendComment"])
        self.assertIn("Damaged", payload["comment"]["comment"])
        self.assertTrue(move.magento_restock)
        self.assertEqual(move.magento_return_picking_ids, returned)
        self.assertEqual(move.magento_return_reason_id, damaged)
        self.assertIn("901", self._messages(returned))
        # The next credit note does not reuse a return already sent.
        self.create_offline.reset_mock()
        self._refund([(self.bag_line, 1.0)])
        payload = self._payload(self.create_offline)
        self.assertNotIn("extension_attributes", payload["arguments"])

    # ------------------------------------------------------------------ Magento -> Odoo
    def test_import_restock_credit_memo_creates_receipt_into_rule_location(self):
        self.Rule.create({"instance_id": self.instance.id, "categ_id": self.bags.id,
                          "location_id": self.returns_location.id})
        self._forget_stock_marks()
        doc = mg_credit_memo(31, [(BAG_ITEM, 1.0, 34.0)], restock_items=[BAG_ITEM])
        move = self.order._magento_create_credit_memo_from_magento(doc)
        self.assertEqual(move.move_type, "out_refund")
        self.assertTrue(move.magento_restock)
        self.assertEqual(move.magento_refund_mode, "offline")
        receipt = move.magento_return_picking_ids
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt.state, "done")
        self.assertEqual(receipt.picking_type_code, "incoming")
        self.assertEqual(receipt.location_dest_id, self.returns_location)
        self.assertEqual(receipt.sale_id, self.order)
        self.assertEqual(receipt.return_id, self.delivery)
        self.assertEqual(receipt.move_ids.quantity, 1.0)
        self.assertEqual(self.bag.with_context(location=self.returns_location.id).qty_available, 1.0)
        self.assertEqual(self.bag_line.qty_delivered, 1.0, "the return counts against the delivery")
        # Stock push: the validated receipt marked the product dirty (delta job queued).
        self.assertIn(self.bag.id, self._dirty_products())
        # Chatter on the order, the credit note and the receipt.
        self.assertIn("Returned to stock", self._messages(self.order))
        self.assertIn("Returned to stock", self._messages(move))
        self.assertIn("000000031", self._messages(receipt))

    def test_import_no_restock_credit_memo_creates_no_picking(self):
        pickings = self.order.picking_ids
        doc = mg_credit_memo(32, [(BAG_ITEM, 1.0, 34.0)])
        move = self.order._magento_create_credit_memo_from_magento(doc)
        self.assertFalse(move.magento_restock)
        self.assertFalse(move.magento_return_picking_ids)
        self.assertEqual(self.order.picking_ids, pickings)
        self.assertIn("refund only", self._messages(move))

    def test_import_restock_policy_and_reason(self):
        damaged = self.Reason.create({"code": "DMG", "name": "Damaged", "restock": False})
        Reason = self.Reason
        self.assertEqual(Reason._magento_match_text(self.instance, "Customer says: damaged box"), damaged)
        self.assertFalse(Reason._magento_match_text(self.instance, "undamaged"))
        self.instance.magento_import_restock = "always"
        move = self.order._magento_create_credit_memo_from_magento(
            mg_credit_memo(33, [(BAG_ITEM, 1.0, 34.0)], comments=["Damaged in transit"]))
        self.assertEqual(move.magento_return_reason_id, damaged)
        self.assertFalse(move.magento_return_picking_ids, "the reason blocks the restock")
        move = self.order._magento_create_credit_memo_from_magento(
            mg_credit_memo(34, [(BAG_ITEM, 1.0, 34.0)], transaction_id="ch_123"))
        self.assertEqual(move.magento_refund_mode, "online")
        self.assertEqual(len(move.magento_return_picking_ids), 1, "policy 'always' restocks")
        self.instance.magento_import_restock = "never"
        move = self.order._magento_create_credit_memo_from_magento(
            mg_credit_memo(35, [(BAG_ITEM, 1.0, 34.0)], restock_items=[BAG_ITEM]))
        self.assertFalse(move.magento_return_picking_ids)

    def test_import_adjustments_become_lines_on_instance_products(self):
        other_income = self.env["account.account"].create({
            "name": "Magento return fees", "code": "RTFEE9", "account_type": "income_other"})
        self.instance.return_fee_account_id = other_income
        doc = mg_credit_memo(36, [(BAG_ITEM, 1.0, 34.0)], shipping=10.0, positive=3.0, negative=2.0)
        move = self.order._magento_create_credit_memo_from_magento(doc)
        fee = move.invoice_line_ids.filtered(lambda l: l.product_id == self.instance.return_fee_product_id)
        extra = move.invoice_line_ids.filtered(
            lambda l: l.product_id == self.instance.refund_adjustment_product_id)
        self.assertEqual(fee.price_unit, -2.0)
        self.assertEqual(fee.account_id, other_income)
        self.assertEqual(extra.price_unit, 3.0)
        self.assertEqual(self.instance.return_fee_product_id.default_code, "MAGENTO-RETURN-FEE")
        self.assertEqual(self.instance.refund_adjustment_product_id.default_code, "MAGENTO-REFUND-ADJ")
        self.assertFalse(move.invoice_line_ids.filtered(
            lambda l: l.product_id == self.instance.discount_product_id))
        self.assertEqual(move.amount_total, doc["grand_total"])
        self.assertEqual(move.amount_total, 34.0 + 10.0 + 3.0 - 2.0)
        self.assertIn("Return fee", self._messages(move))

    def test_import_waits_for_the_delivery(self):
        """A restock imported before Odoo delivered waits, then runs on delivery."""
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        order = self.order.copy({"magento_order_id": "7002", "magento_instance_id": self.instance.id})
        order.order_line.filtered(lambda l: l.product_id == self.bag).magento_item_id = "21"
        order.with_context(**ctx).action_confirm()
        doc = mg_credit_memo(37, [(21, 1.0, 34.0)], restock_items=[21])
        move = order._magento_create_credit_memo_from_magento(doc)
        self.assertTrue(move.magento_restock_waiting)
        self.assertFalse(move.magento_return_picking_ids)
        delivery = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        for stock_move in delivery.move_ids:
            stock_move.quantity = stock_move.product_uom_qty
            stock_move.picked = True
        delivery.with_context(**ctx)._action_done()
        self.assertFalse(move.magento_restock_waiting)
        self.assertEqual(move.magento_return_picking_ids.state, "done")

    # ------------------------------------------------------------------ idempotency
    def test_import_twice_is_idempotent(self):
        doc = mg_credit_memo(38, [(BAG_ITEM, 1.0, 34.0)], restock_items=[BAG_ITEM])
        move = self.order._magento_create_credit_memo_from_magento(doc)
        again = self.order._magento_create_credit_memo_from_magento(dict(doc))
        self.assertEqual(again, move)
        self.assertEqual(len(move.magento_return_picking_ids), 1)
        self.assertEqual(self.env["account.move"].search_count(
            [("magento_credit_memo_id", "=", "38"), ("magento_instance_id", "=", self.instance.id)]), 1)

    def test_push_twice_and_reimport_is_idempotent(self):
        returned = self._odoo_return(qty=1.0)
        move = self._refund([(self.bag_line, 1.0)])
        self.assertEqual(self.create_offline.call_count, 1)
        move._magento_create_credit_memo()
        move.button_draft()
        move.action_post()
        self.assertEqual(self.create_offline.call_count, 1, "a linked credit note is never re-posted")
        # The credit memo Odoo created comes back with the next order sync: reused as is,
        # no second credit note and no Magento-side return receipt.
        pickings = self.order.picking_ids
        doc = mg_credit_memo(901, [(BAG_ITEM, 1.0, 34.0)], restock_items=[BAG_ITEM])
        self.assertEqual(self.order._magento_create_credit_memo_from_magento(doc), move)
        self.assertEqual(self.order.picking_ids, pickings)
        self.assertEqual(move.magento_return_picking_ids, returned)

    def test_push_links_matching_magento_credit_memo_without_post(self):
        self._odoo_return(qty=1.0)
        existing = mg_credit_memo(950, [(BAG_ITEM, 1.0, 34.0)])
        self.patch_api("get_credit_memos", return_value=[existing])
        move = self._refund([(self.bag_line, 1.0)])
        self.assertFalse(self.create_offline.called)
        self.assertEqual(move.magento_credit_memo_id, "950")
        self.assertTrue(move.magento_restock)

    # ------------------------------------------------------------------ location rules
    def test_return_location_rule_priority(self):
        damaged = self.Reason.create({"code": "DMG", "name": "Damaged"})
        other_wh = self.env["stock.warehouse"].create({"name": "East", "code": "EST"})
        product = self.bag
        location = self.instance._magento_return_location
        self.assertEqual(location(self.warehouse, product), self.stock_location, "fallback")
        self.Rule.create({"instance_id": self.instance.id, "sequence": 30,
                          "warehouse_id": self.warehouse.id, "location_id": self.returns_location.id})
        self.Rule.create({"instance_id": self.instance.id, "sequence": 20, "categ_id": self.bags.id,
                          "warehouse_id": other_wh.id, "location_id": other_wh.lot_stock_id.id})
        reason_rule = self.Rule.create({"instance_id": self.instance.id, "sequence": 10,
                                        "reason_id": damaged.id,
                                        "location_id": self.damaged_location.id})
        self.assertEqual(location(self.warehouse, product, damaged), self.damaged_location)
        self.assertEqual(location(self.warehouse, product), self.returns_location)
        self.assertEqual(location(other_wh, product), other_wh.lot_stock_id)
        self.assertEqual(location(other_wh, self.goodwill), other_wh.lot_stock_id, "fallback")
        reason_rule.sequence = 40
        self.assertEqual(location(self.warehouse, product, damaged), self.returns_location)
        reason_rule.sequence = 5
        reason_rule.active = False
        self.assertEqual(location(self.warehouse, product, damaged), self.returns_location,
                         "archived rules are ignored")
