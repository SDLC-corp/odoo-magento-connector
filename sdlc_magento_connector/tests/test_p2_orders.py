"""Phase 2 order fidelity (2.4) and auto-workflow (2.5).

Fixtures follow the JSON of a real Magento 2.4.9 store (GET /V1/orders/{id},
/V1/invoices, /V1/creditmemos, /V1/shipments): orders 000000001 (processing, invoiced,
paid: 29.00 + 8.25% tax 2.39 + shipping 5.00 = 36.39) and 000000002 (closed: invoiced,
paid, fully refunded: 32.00 + 2.64 + 5.00 = 39.64).
"""
from odoo import Command
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import MagentoTestCommon, build_address, once

SYNC_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_instance"

RATE_MI = {"code": "US-MI-*-Rate 1", "title": "US-MI-*-Rate 1", "percent": 8.25}


def r2(value):
    return round(value + 1e-9, 2)


def mg_item(item_id, sku, price, qty=1.0, tax_percent=8.25, discount=0.0, discount_percent=0.0,
            product_type="simple", product_id=None, name=None, tax_amount=None, price_incl=None,
            qty_shipped=0.0, qty_invoiced=0.0, qty_refunded=0.0, weee_row=0.0):
    row = r2(price * qty)
    if tax_amount is None:
        tax_amount = r2((row - discount) * tax_percent / 100.0)
    row_incl = r2(row + tax_amount) if price_incl is None else r2(price_incl * qty)
    return {
        "item_id": item_id,
        "order_id": 1,
        "sku": sku,
        "name": name or sku,
        "product_id": product_id or item_id + 1000,
        "product_type": product_type,
        "price": price,
        "base_price": price,
        "original_price": price,
        "price_incl_tax": price_incl if price_incl is not None else r2(row_incl / qty),
        "qty_ordered": qty,
        "qty_invoiced": qty_invoiced,
        "qty_shipped": qty_shipped,
        "qty_refunded": qty_refunded,
        "qty_canceled": 0,
        "row_total": row,
        "row_total_incl_tax": row_incl,
        "tax_percent": tax_percent,
        "tax_amount": tax_amount,
        "discount_amount": discount,
        "discount_percent": discount_percent,
        "discount_tax_compensation_amount": 0,
        "weee_tax_applied": "[]",
        "weee_tax_applied_row_amount": weee_row,
        "weee_tax_applied_amount": weee_row / qty if qty else 0,
        "extension_attributes": {"itemized_taxes": []},
    }


def mg_order(entity_id, items, *, state="processing", status=None, shipping=5.0, shipping_tax=0.0,
             paid=True, invoiced=True, refunded=0.0, method="checkmo", coupon=None,
             discount_description=None, applied_taxes=None, item_applied_taxes=None,
             grand_total=None, shipping_discount=0.0):
    subtotal = r2(sum(i["row_total"] for i in items))
    tax = r2(sum(i["tax_amount"] for i in items) + shipping_tax)
    discount = r2(sum(i["discount_amount"] for i in items) + shipping_discount)
    weee = r2(sum(i["weee_tax_applied_row_amount"] for i in items))
    grand = r2(subtotal - discount + tax + shipping + weee) if grand_total is None else grand_total
    for item in items:
        item["order_id"] = entity_id
    address = build_address(parent_id=entity_id)
    if applied_taxes is None:
        applied_taxes = [dict(RATE_MI, amount=tax, base_amount=tax)] if tax else []
    if item_applied_taxes is None:
        item_applied_taxes = [{"type": "product", "applied_taxes": applied_taxes}] if tax else []
    order = {
        "entity_id": entity_id,
        "increment_id": "%09d" % entity_id,
        "state": state,
        "status": status or state,
        "created_at": "2026-10-12 08:35:37",
        "updated_at": "2026-10-12 08:35:38",
        "customer_id": 1,
        "customer_is_guest": 0,
        "customer_email": "roni_cost@example.com",
        "customer_firstname": "Veronica",
        "customer_lastname": "Costello",
        "order_currency_code": "USD",
        "base_currency_code": "USD",
        "subtotal": subtotal,
        "subtotal_incl_tax": r2(subtotal + tax - shipping_tax),
        "discount_amount": -discount,
        "tax_amount": tax,
        "shipping_amount": shipping,
        "shipping_incl_tax": r2(shipping + shipping_tax),
        "shipping_tax_amount": shipping_tax,
        "shipping_discount_amount": shipping_discount,
        "shipping_description": "Flat Rate - Fixed",
        "discount_tax_compensation_amount": 0,
        "grand_total": grand,
        "base_grand_total": grand,
        "total_invoiced": grand if invoiced else 0,
        "total_paid": grand if paid else 0,
        "total_refunded": refunded,
        "total_due": 0 if paid else grand,
        "items": items,
        "billing_address": address,
        "payment": {"method": method, "amount_ordered": grand,
                    "additional_information": ["Check / Money order"]},
        "extension_attributes": {
            "shipping_assignments": [{
                "shipping": {"address": dict(address, address_type="shipping"),
                             "method": "flatrate_flatrate",
                             "total": {"shipping_amount": shipping}},
                "items": items,
            }],
            "applied_taxes": applied_taxes,
            "item_applied_taxes": item_applied_taxes,
        },
    }
    if coupon:
        order["coupon_code"] = coupon
        order["discount_description"] = discount_description or coupon
    return order


def mg_document(entity_id, order, *, kind="invoice", state=2):
    """A Magento invoice / credit memo covering the whole order."""
    items = []
    for item in order["items"]:
        items.append({
            "entity_id": entity_id * 10 + len(items),
            "order_item_id": item["item_id"],
            "sku": item["sku"],
            "name": item["name"],
            "qty": item["qty_ordered"],
            "price": item["price"],
            "price_incl_tax": item["price_incl_tax"],
            "row_total": item["row_total"],
            "row_total_incl_tax": item["row_total_incl_tax"],
            "tax_amount": item["tax_amount"],
            "discount_amount": item["discount_amount"],
            "weee_tax_applied_row_amount": item["weee_tax_applied_row_amount"],
        })
    doc = {
        "entity_id": entity_id,
        "increment_id": "%09d" % entity_id,
        "order_id": order["entity_id"],
        "state": state,
        "created_at": "2026-10-12 08:35:38",
        "subtotal": order["subtotal"],
        "discount_amount": order["discount_amount"],
        "tax_amount": order["tax_amount"],
        "shipping_amount": order["shipping_amount"],
        "shipping_incl_tax": order["shipping_incl_tax"],
        "shipping_tax_amount": order["shipping_tax_amount"],
        "grand_total": order["grand_total"],
        "base_grand_total": order["grand_total"],
        "items": items,
    }
    if kind == "credit_memo":
        doc.update({"adjustment_positive": 0, "adjustment_negative": 0})
    return doc


def mg_shipment(entity_id, order):
    return {
        "entity_id": entity_id,
        "increment_id": "%09d" % entity_id,
        "order_id": order["entity_id"],
        "created_at": "2026-10-12 08:35:38",
        "items": [{"order_item_id": i["item_id"], "qty": i["qty_ordered"], "sku": i["sku"]}
                  for i in order["items"]],
    }


def live_order_1():
    return mg_order(1, [mg_item(1, "WS03-XS-Red", 29.0, product_type="configurable",
                                product_id=1428, name="Iris Workout Top", qty_shipped=1,
                                qty_invoiced=1)])


def live_order_2():
    return mg_order(2, [mg_item(2, "WS08-XS-Blue", 32.0, product_type="configurable",
                                product_id=1492, name="Minerva LumaTech V-Tee", qty_shipped=1,
                                qty_invoiced=1, qty_refunded=1)],
                    state="closed", refunded=39.64)


@tagged("post_install", "-at_install", "magento")
class TestP2Orders(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        Product = cls.env["product.product"].with_context(**ctx)
        Map = cls.env["magento.product.map"].with_context(**ctx)
        cls.products = {}
        for sku, magento_id, price in (("WS03-XS-Red", 1428, 29.0), ("WS08-XS-Blue", 1492, 32.0),
                                       ("24-MB01", 1, 100.0), ("24-MB02", 2, 33.33)):
            product = Product.create({"name": sku, "default_code": sku, "type": "consu",
                                      "list_price": price})
            Map.create({"instance_id": cls.instance.id, "magento_id": str(magento_id), "sku": sku,
                        "name": sku, "odoo_product_id": product.id})
            cls.products[sku] = product
        cls.env.company.tax_calculation_rounding_method = "round_globally"

    def setUp(self):
        super().setUp()
        self.docs = {"invoices": {}, "credit_memos": {}, "shipments": {}}

        def lister(kind):
            def _list(api, order_id=None, **kwargs):
                return list(self.docs[kind].get(str(order_id), []))
            return _list

        self.patch_api("get_invoices", side_effect=lister("invoices"))
        self.patch_api("get_credit_memos", side_effect=lister("credit_memos"))
        self.patch_api("get_shipments", side_effect=lister("shipments"))
        self.push_mocks = {
            name: self.patch_api(name, return_value=None)
            for name in ("create_invoice", "create_credit_memo", "create_shipment",
                         "add_order_comment")
        }

    # ------------------------------------------------------------------ helpers
    def add_docs(self, order, invoice=True, credit_memo=False, shipment=False):
        key = str(order["entity_id"])
        if invoice:
            self.docs["invoices"][key] = [mg_document(order["entity_id"], order)]
        if credit_memo:
            self.docs["credit_memos"][key] = [mg_document(order["entity_id"], order, kind="credit_memo")]
        if shipment:
            self.docs["shipments"][key] = [mg_shipment(order["entity_id"], order)]

    def sync(self, *orders):
        self.patch_api("get_orders", side_effect=once(list(orders)))
        counts = self.instance.sync_orders()
        self.env.invalidate_all()
        return counts

    def lines(self, order, role):
        return order.order_line.filtered(lambda l: l.magento_line_role == role)

    def payments_of(self, move):
        return self.env["account.payment"].search([("reconciled_invoice_ids", "in", move.ids)])

    def assertNoPush(self):
        for name, mock in self.push_mocks.items():
            self.assertFalse(mock.called, "%s must not be called for imported documents" % name)
        self.assertFalse(self.http_attempts)

    # ------------------------------------------------------------------ 2.4 totals
    def test_live_order_1_processing_paid(self):
        data = live_order_1()
        self.add_docs(data, shipment=True)
        counts = self.sync(data)
        self.assertEqual(counts["errors"], 0, counts["error_messages"])
        order = self.magento_orders(1)
        self.assertEqual(order.amount_untaxed, 34.0)
        self.assertEqual(order.amount_tax, 2.39)
        self.assertEqual(order.amount_total, 36.39)
        self.assertFalse(order.magento_total_differs)
        product_line = self.lines(order, "product")
        self.assertEqual(product_line.product_id, self.products["WS03-XS-Red"])
        self.assertEqual(product_line.tax_id.mapped("amount"), [8.25])
        self.assertIn("US-MI-*-Rate 1", product_line.tax_id.name)
        shipping = self.lines(order, "shipping")
        self.assertEqual(shipping.price_unit, 5.0)
        self.assertFalse(shipping.tax_id, "Magento did not tax shipping")
        self.assertEqual(shipping.name, "Flat Rate - Fixed")
        self.assertEqual(shipping.product_id, self.instance.shipping_product_id)
        # Workflow "Processing and paid": confirmed, invoiced (the Magento invoice), paid.
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.date_order.strftime("%Y-%m-%d %H:%M:%S"), "2026-10-12 08:35:37")
        self.assertEqual(order.magento_workflow_id.name, "Processing and paid")
        invoice = order.invoice_ids
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.magento_invoice_id, "1")
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_total, 36.39)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertEqual(order.invoice_status, "invoiced")
        # Processing rule does not validate deliveries.
        self.assertTrue(order.picking_ids)
        self.assertNotIn("done", order.picking_ids.mapped("state"))
        self.assertNoPush()

    def test_live_order_2_closed_refunded(self):
        data = live_order_2()
        self.add_docs(data, credit_memo=True, shipment=True)
        counts = self.sync(data)
        self.assertEqual(counts["errors"], 0, counts["error_messages"])
        order = self.magento_orders(2)
        self.assertEqual(order.amount_total, 39.64)
        self.assertEqual(order.amount_tax, 2.64)
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.magento_refund_state, "refunded")
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
        refund = order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")
        self.assertEqual(len(invoice), 1)
        self.assertEqual(len(refund), 1)
        self.assertEqual(invoice.amount_total, 39.64)
        self.assertEqual(refund.amount_total, 39.64)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertIn(refund.payment_state, ("paid", "in_payment"))
        self.assertEqual(refund.magento_credit_memo_id, "2")
        # Closed rule validates the delivery Magento shipped (no shipment pushed back).
        done = order.picking_ids.filtered(lambda p: p.state == "done")
        self.assertTrue(done)
        self.assertEqual(done.magento_shipment_id, "2")
        self.assertNoPush()

    def test_reconciliation_has_no_mismatch(self):
        orders = (live_order_1(), live_order_2())
        self.add_docs(orders[0])
        self.add_docs(orders[1], credit_memo=True)
        self.sync(*orders)
        for data in orders:
            order = self.magento_orders(data["entity_id"])
            self.assertEqual(order.currency_id.compare_amounts(order.amount_total, data["grand_total"]), 0)
            self.assertFalse(order.magento_total_differs)

    def test_discount_coupon_exact_line_discount(self):
        item = mg_item(11, "24-MB01", 120.0, discount=12.0, discount_percent=10.0)
        data = mg_order(11, [item], state="new", status="pending", paid=False, invoiced=False,
                        coupon="SAVE10", discount_description="10% off")
        self.assertEqual(data["grand_total"], 121.91)
        self.sync(data)
        order = self.magento_orders(11)
        self.assertEqual(order.magento_coupon_code, "SAVE10")
        self.assertEqual(order.magento_discount_description, "10% off")
        line = self.lines(order, "product")
        self.assertEqual(line.discount, 10.0)
        self.assertFalse(self.lines(order, "discount"), "An exact percentage stays a line discount")
        self.assertEqual(order.amount_tax, 8.91)
        self.assertEqual(order.amount_total, 121.91)
        self.assertFalse(order.magento_total_differs)
        self.assertEqual(order.state, "draft", "Pending orders stay quotations")

    def test_discount_fixed_amount_discount_line(self):
        item = mg_item(12, "24-MB02", 33.33, qty=2.0, discount=7.0)
        data = mg_order(12, [item], state="new", status="pending", paid=False, invoiced=False,
                        coupon="MINUS7")
        self.assertEqual(data["grand_total"], 69.58)
        self.sync(data)
        order = self.magento_orders(12)
        product_line = self.lines(order, "product")
        discount = self.lines(order, "discount")
        self.assertEqual(product_line.discount, 0.0)
        self.assertEqual(len(discount), 1)
        self.assertEqual(discount.price_unit, -7.0)
        self.assertEqual(discount.tax_id, product_line.tax_id, "Tax applies after discount")
        self.assertIn("MINUS7", discount.name)
        self.assertEqual(order.amount_tax, 4.92)
        self.assertEqual(order.amount_total, 69.58)
        self.assertFalse(order.magento_total_differs)

    def test_prices_including_tax(self):
        self.instance.prices_include_tax = True
        item = mg_item(13, "WS03-XS-Red", 29.0, price_incl=31.39, tax_amount=2.39)
        data = mg_order(13, [item], state="new", status="pending", paid=False, invoiced=False)
        self.assertEqual(data["grand_total"], 36.39)
        self.sync(data)
        order = self.magento_orders(13)
        line = self.lines(order, "product")
        self.assertEqual(line.price_unit, 31.39)
        self.assertTrue(line.tax_id.price_include)
        self.assertEqual(order.amount_untaxed, 34.0)
        self.assertEqual(order.amount_tax, 2.39)
        self.assertEqual(order.amount_total, 36.39)

    def test_multi_rate_tax(self):
        state = {"code": "US-MI-State", "title": "Michigan", "percent": 6.0, "amount": 6.0}
        county = {"code": "US-MI-County", "title": "County", "percent": 2.0, "amount": 2.0}
        item = mg_item(14, "24-MB01", 100.0, tax_percent=8.0)
        data = mg_order(14, [item], state="new", status="pending", paid=False, invoiced=False,
                        shipping=0.0, applied_taxes=[state, county],
                        item_applied_taxes=[{"type": "product", "item_id": 14,
                                             "applied_taxes": [state, county]}])
        self.sync(data)
        order = self.magento_orders(14)
        line = self.lines(order, "product")
        self.assertEqual(sorted(line.tax_id.mapped("amount")), [2.0, 6.0])
        self.assertFalse(self.lines(order, "shipping"), "No shipping line for free shipping")
        self.assertEqual(order.amount_tax, 8.0)
        self.assertEqual(order.amount_total, 108.0)

    def test_zero_tax_line_has_no_tax(self):
        item = mg_item(15, "24-MB01", 100.0, tax_percent=0.0)
        data = mg_order(15, [item], state="new", status="pending", paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(15)
        self.assertFalse(self.lines(order, "product").tax_id,
                         "Product default taxes must not apply to an untaxed Magento line")
        self.assertEqual(order.amount_total, 105.0)

    def test_auto_created_taxes_are_reused(self):
        self.sync(live_order_1(), live_order_2())
        taxes = (self.magento_orders(1) | self.magento_orders(2)).order_line.tax_id
        self.assertEqual(len(taxes), 1)
        maps = self.env["magento.tax.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(len(maps), 1)
        self.assertEqual(maps.tax_id, taxes)
        self.assertTrue(maps.auto_created)
        self.assertEqual(maps.magento_code, "US-MI-*-Rate 1")
        self.assertEqual(self.env["account.tax"].search_count([("name", "=", taxes.name)]), 1)

    @mute_logger(SYNC_LOGGER)
    def test_unmapped_tax_without_auto_create_fails_clearly(self):
        self.instance.auto_create_taxes = False
        counts = self.sync(live_order_1())
        self.assertEqual(counts["errors"], 1)
        self.assertIn("not mapped", " ".join(counts["error_messages"]))
        self.assertFalse(self.magento_orders(1))
        mapping = self.env["magento.tax.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(len(mapping), 1)
        self.assertFalse(mapping.tax_id)
        issue = self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("entity", "=", "order"),
            ("magento_ref", "=", "1"), ("state", "=", "open"),
        ])
        self.assertTrue(issue)
        # Mapping the rate fixes the next import.
        tax = self.env["account.tax"].create({
            "name": "MI 8.25", "amount": 8.25, "type_tax_use": "sale",
            "price_include": False,
        })
        mapping.tax_id = tax
        self.sync(live_order_1())
        order = self.magento_orders(1)
        self.assertEqual(order.order_line.tax_id, tax)
        self.assertEqual(order.amount_total, 36.39)

    def test_fpt_line(self):
        item = mg_item(16, "24-MB01", 100.0, tax_percent=0.0, weee_row=3.5)
        data = mg_order(16, [item], state="new", status="pending", paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(16)
        fpt = self.lines(order, "fpt")
        self.assertEqual(fpt.price_unit, 3.5)
        self.assertEqual(order.amount_total, data["grand_total"])

    # ------------------------------------------------------------------ payments
    def test_payment_map_journal(self):
        journal = self.env["account.journal"].create({
            "name": "PayPal", "code": "MGPP", "type": "bank", "company_id": self.company.id,
        })
        self.env["magento.payment.map"].create({
            "instance_id": self.instance.id, "magento_method": "paypal_express",
            "journal_id": journal.id,
        })
        data = mg_order(21, [mg_item(21, "WS03-XS-Red", 29.0)], method="paypal_express")
        self.add_docs(data)
        self.sync(data)
        order = self.magento_orders(21)
        self.assertEqual(order.magento_payment_method, "paypal_express")
        payments = self.payments_of(order.invoice_ids)
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments.journal_id, journal)
        self.assertEqual(payments.amount, 36.39)

    def test_unmapped_payment_method_is_listed_and_uses_default(self):
        self.add_docs(live_order_1())
        self.sync(live_order_1())
        mapping = self.env["magento.payment.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(mapping.magento_method, "checkmo")
        self.assertFalse(mapping.journal_id)
        payments = self.payments_of(self.magento_orders(1).invoice_ids)
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments.journal_id.type, "bank")
        carrier = self.env["magento.carrier.map"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(carrier.magento_code, "flatrate_flatrate")

    # ------------------------------------------------------------------ 2.5 workflow
    def test_workflow_pending_stays_quotation(self):
        data = mg_order(31, [mg_item(31, "WS03-XS-Red", 29.0)], state="new", status="pending",
                        paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(31)
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.invoice_ids)
        self.assertEqual(order.amount_total, 36.39)

    def test_workflow_complete_validates_delivery(self):
        data = mg_order(32, [mg_item(32, "WS03-XS-Red", 29.0, qty_shipped=1, qty_invoiced=1)],
                        state="complete")
        self.add_docs(data, shipment=True)
        self.sync(data)
        order = self.magento_orders(32)
        self.assertEqual(order.state, "sale")
        self.assertIn(order.invoice_ids.payment_state, ("paid", "in_payment"))
        self.assertEqual(order.picking_ids.mapped("state"), ["done"])
        self.assertEqual(order.delivery_status, "full")
        self.assertNoPush()

    def test_workflow_canceled(self):
        data = mg_order(33, [mg_item(33, "WS03-XS-Red", 29.0)], state="canceled",
                        paid=False, invoiced=False)
        self.sync(data)
        self.assertEqual(self.magento_orders(33).state, "cancel")

    def test_workflow_invoice_created_when_magento_has_none(self):
        workflow = self.env["magento.order.workflow"].create({
            "name": "Pending checkmo", "instance_id": self.instance.id, "sequence": 1,
            "magento_states": "new", "payment_methods": "checkmo", "confirm_order": True,
            "create_invoice": True, "post_invoice": True, "lock_order": True,
        })
        data = mg_order(34, [mg_item(34, "WS03-XS-Red", 29.0)], state="new", status="pending",
                        paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(34)
        self.assertEqual(order.magento_workflow_id, workflow)
        self.assertEqual(order.state, "done")  # Odoo 16: a locked order is done
        self.assertEqual(order.invoice_ids.state, "posted")
        self.assertEqual(order.invoice_ids.amount_total, 36.39)
        self.assertFalse(order.invoice_ids.magento_invoice_id)
        self.assertNoPush()

    @mute_logger(SYNC_LOGGER)
    def test_failing_step_stops_workflow_and_opens_issue(self):
        data = live_order_1()
        self.add_docs(data)
        self.patch_api("get_invoices", side_effect=RuntimeError("invoice listing down"))
        counts = self.sync(data)
        order = self.magento_orders(1)
        self.assertTrue(order, "The imported order is kept")
        self.assertEqual(order.state, "sale", "Steps before the failure are kept")
        self.assertFalse(order.invoice_ids)
        self.assertEqual(counts["errors"], 1)
        issue = self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("magento_ref", "=", "1"), ("state", "=", "open"),
        ])
        self.assertIn("invoice listing down", issue.message)

    # ------------------------------------------------------------------ idempotency
    def test_resync_is_idempotent(self):
        data = live_order_2()
        self.add_docs(data, credit_memo=True, shipment=True)
        self.sync(data)
        order = self.magento_orders(2)
        lines = order.order_line
        moves = order.invoice_ids
        payments = self.payments_of(moves)
        self.sync(live_order_2())
        self.sync(live_order_2())
        order = self.magento_orders(2)
        self.assertEqual(len(order), 1)
        self.assertEqual(order.order_line, lines)
        self.assertEqual(order.invoice_ids, moves)
        self.assertEqual(self.payments_of(moves), payments)
        self.assertEqual(order.amount_total, 39.64)
        self.assertNoPush()

    def test_draft_resync_updates_lines_in_place(self):
        data = mg_order(41, [mg_item(41, "WS03-XS-Red", 29.0)], state="new", status="pending",
                        paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(41)
        shipping = self.lines(order, "shipping")
        data = mg_order(41, [mg_item(41, "WS03-XS-Red", 29.0, qty=2.0)], state="new",
                        status="pending", paid=False, invoiced=False, shipping=10.0)
        self.sync(data)
        order = self.magento_orders(41)
        self.assertEqual(self.lines(order, "shipping"), shipping, "Same line, updated in place")
        self.assertEqual(shipping.price_unit, 10.0)
        self.assertEqual(self.lines(order, "product").product_uom_qty, 2.0)
        self.assertEqual(order.amount_total, data["grand_total"])
        self.assertEqual(len(order.order_line), 2)

    def test_imported_invoice_not_pushed_on_repost(self):
        data = live_order_1()
        self.add_docs(data)
        self.sync(data)
        invoice = self.magento_orders(1).invoice_ids
        invoice.button_draft()
        invoice.action_post()
        self.assertNoPush()

    # ------------------------------------------------------------------ difference flag
    def test_total_difference_is_flagged_not_plugged(self):
        data = mg_order(51, [mg_item(51, "WS03-XS-Red", 29.0)], state="new", status="pending",
                        paid=False, invoiced=False, grand_total=36.44)
        self.sync(data)
        order = self.magento_orders(51)
        self.assertTrue(order.magento_total_differs)
        self.assertEqual(order.magento_total_difference, 0.05)
        self.assertEqual(order.amount_total, 36.39, "No plug line")
        self.assertEqual(set(order.order_line.mapped("magento_line_role")), {"product", "shipping"})
        notes = order.message_ids.filtered(lambda m: "differs from the Magento grand total" in (m.body or ""))
        self.assertEqual(len(notes), 1)
        found = self.env["sale.order"].search([("magento_total_differs", "=", True)])
        self.assertIn(order, found)
        # Same difference on re-sync: no second note. Fixed in Magento: flag cleared.
        self.sync(data)
        notes = order.message_ids.filtered(lambda m: "differs from the Magento grand total" in (m.body or ""))
        self.assertEqual(len(notes), 1)
        data["grand_total"] = 36.39
        self.sync(data)
        self.assertFalse(order.magento_total_differs)
        self.assertEqual(order.magento_total_difference, 0.0)

    def test_default_workflows_created(self):
        names = self.instance.order_workflow_ids.mapped("name")
        self.assertEqual(len(names), 5)
        self.assertTrue(self.instance.order_workflows_initialized)
        # The data hook does not duplicate them.
        self.env["magento.instance"]._magento_init_default_order_workflows()
        self.assertEqual(len(self.instance.order_workflow_ids), 5)

    def test_tax_rounding_option(self):
        self.env.company.tax_calculation_rounding_method = "round_per_line"
        self.instance.tax_rounding_method = "round_globally"
        self.assertTrue(self.instance.tax_rounding_differs)
        self.instance.action_apply_tax_rounding_to_company()
        self.assertEqual(self.env.company.tax_calculation_rounding_method, "round_globally")
        self.assertFalse(self.instance.tax_rounding_differs)

    def test_odoo_line_untouched_on_draft_resync(self):
        data = mg_order(61, [mg_item(61, "WS03-XS-Red", 29.0)], state="new", status="pending",
                        paid=False, invoiced=False)
        self.sync(data)
        order = self.magento_orders(61)
        order.write({"order_line": [Command.create({
            "product_id": self.products["24-MB01"].id, "product_uom_qty": 1, "price_unit": 1.0,
        })]})
        self.sync(data)
        self.assertIn(self.products["24-MB01"], order.order_line.product_id,
                      "Lines added in Odoo are not Magento lines and are kept")
