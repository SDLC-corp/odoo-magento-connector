"""Phase 3.1: totals explanation engine ("Why does this differ?").

Fixtures follow the JSON of a real Magento 2.4.9 store (GET /V1/orders/{id}). Order
000000001: 29.00 + 8.25% tax 2.39 + flat rate shipping 5.00 = 36.39. The rounding case is
three items at 9.99 taxed 8.25% by Magento's "Row Total" method (0.82 per row, 2.46) while
Odoo rounds globally (2.47).

Every test checks the invariant: sum(components) + unexplained remainder == difference,
to the cent.
"""
from datetime import date

import requests

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import MagentoTestCommon, build_address, once

EXPLAINER_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_totals_explainer"
RATE_MI = {"code": "US-MI-*-Rate 1", "title": "US-MI-*-Rate 1", "percent": 8.25}


def r2(value):
    return round(value + 1e-9, 2)


def cents(value):
    return int(round(value * 100))


def mg_item(item_id, sku, price, qty=1.0, tax_percent=8.25, discount=0.0, tax_amount=None,
            price_incl=None, row_total=None, weee_row=0.0, dtc=0.0, product_id=None, name=None):
    row = r2(price * qty) if row_total is None else row_total
    if tax_amount is None:
        tax_amount = r2((row - discount) * tax_percent / 100.0)
    row_incl = r2(price_incl * qty) if price_incl is not None else r2(row + tax_amount + dtc)
    return {
        "item_id": item_id,
        "order_id": 1,
        "sku": sku,
        "name": name or sku,
        "product_id": product_id or item_id + 1000,
        "product_type": "simple",
        "price": price,
        "base_price": price,
        "original_price": price_incl if price_incl is not None else price,
        "price_incl_tax": price_incl if price_incl is not None else r2(row_incl / qty),
        "qty_ordered": qty,
        "qty_invoiced": 0,
        "qty_shipped": 0,
        "qty_refunded": 0,
        "qty_canceled": 0,
        "row_total": row,
        "row_total_incl_tax": row_incl,
        "tax_percent": tax_percent,
        "tax_amount": tax_amount,
        "discount_amount": discount,
        "discount_percent": 0,
        "discount_tax_compensation_amount": dtc,
        "weee_tax_applied": "[]",
        "weee_tax_applied_row_amount": weee_row,
        "weee_tax_applied_amount": weee_row / qty if qty else 0,
    }


def mg_order(entity_id, items, *, shipping=5.0, shipping_tax=0.0, grand_total=None,
             state="new", status="pending", extra=None):
    subtotal = r2(sum(i["row_total"] for i in items))
    tax = r2(sum(i["tax_amount"] for i in items) + shipping_tax)
    discount = r2(sum(i["discount_amount"] for i in items))
    weee = r2(sum(i["weee_tax_applied_row_amount"] for i in items))
    dtc = r2(sum(i["discount_tax_compensation_amount"] for i in items))
    grand = r2(subtotal - discount + tax + dtc + shipping + weee) if grand_total is None else grand_total
    for item in items:
        item["order_id"] = entity_id
    address = build_address(parent_id=entity_id)
    applied = [dict(RATE_MI, amount=tax, base_amount=tax)] if tax else []
    item_applied = []
    if any(i["tax_amount"] for i in items):
        item_applied.append({"type": "product", "applied_taxes": [dict(RATE_MI)]})
    if shipping_tax:
        item_applied.append({"type": "shipping", "applied_taxes": [dict(RATE_MI)]})
    order = {
        "entity_id": entity_id,
        "increment_id": "%09d" % entity_id,
        "state": state,
        "status": status,
        "created_at": "2026-10-12 08:35:37",
        "updated_at": "2026-10-12 08:35:38",
        "customer_id": 1,
        "customer_is_guest": 0,
        "customer_email": "roni_cost@example.com",
        "customer_firstname": "Veronica",
        "customer_lastname": "Costello",
        "order_currency_code": "USD",
        "base_currency_code": "USD",
        "store_id": 1,
        "subtotal": subtotal,
        "subtotal_incl_tax": r2(sum(i["row_total_incl_tax"] for i in items)),
        "discount_amount": -discount,
        "tax_amount": tax,
        "shipping_amount": shipping,
        "shipping_incl_tax": r2(shipping + shipping_tax),
        "shipping_tax_amount": shipping_tax,
        "shipping_discount_amount": 0,
        "shipping_description": "Flat Rate - Fixed",
        "discount_tax_compensation_amount": dtc,
        "shipping_discount_tax_compensation_amount": 0,
        "grand_total": grand,
        "base_grand_total": grand,
        "total_invoiced": 0,
        "total_paid": 0,
        "total_refunded": 0,
        "total_due": grand,
        "items": items,
        "billing_address": address,
        "payment": {"method": "checkmo", "amount_ordered": grand,
                    "additional_information": ["Check / Money order"]},
        "extension_attributes": {
            "shipping_assignments": [{
                "shipping": {"address": dict(address, address_type="shipping"),
                             "method": "flatrate_flatrate",
                             "total": {"shipping_amount": shipping}},
                "items": items,
            }],
            "applied_taxes": applied,
            "item_applied_taxes": item_applied,
        },
    }
    order.update(extra or {})
    return order


def live_order_1(**kwargs):
    return mg_order(1, [mg_item(1, "WS03-XS-Red", 29.0, product_id=1428, name="Iris Workout Top")],
                    **kwargs)


@tagged("post_install", "-at_install", "magento")
class TestP3Totals(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        Product = cls.env["product.product"].with_context(**ctx)
        Map = cls.env["magento.product.map"].with_context(**ctx)
        cls.products = {}
        for sku, magento_id, price in (("WS03-XS-Red", 1428, 29.0), ("24-MB01", 1, 100.0),
                                       ("24-MB02", 2, 33.33), ("24-WG01", 10, 9.99),
                                       ("24-WG02", 11, 9.99), ("24-WG03", 12, 9.99),
                                       ("24-UG01", 20, 1.99)):
            product = Product.create({"name": sku, "default_code": sku, "type": "consu",
                                      "list_price": price, "taxes_id": [Command.clear()]})
            Map.create({"instance_id": cls.instance.id, "magento_id": str(magento_id), "sku": sku,
                        "name": sku, "odoo_product_id": product.id})
            cls.products[sku] = product
        cls.env.company.tax_calculation_rounding_method = "round_globally"
        cls.Explanation = cls.env["magento.totals.explanation"]

    def setUp(self):
        super().setUp()
        self.patch_order_side_calls()
        for name in ("create_invoice", "create_credit_memo", "create_shipment", "add_order_comment"):
            self.patch_api(name, return_value=None)
        self.payloads = {}

        def _get_order(api, order_id):
            return self.payloads[str(order_id)]

        self.get_order = self.patch_api("get_order", side_effect=_get_order)

    # ------------------------------------------------------------------ helpers
    def sync(self, *orders):
        for data in orders:
            self.payloads[str(data["entity_id"])] = data
        self.patch_api("get_orders", side_effect=once(list(orders)))
        counts = self.instance.sync_orders()
        self.assertEqual(counts["errors"], 0, counts.get("error_messages"))
        self.env.invalidate_all()
        return [self.magento_orders(data["entity_id"]) for data in orders]

    def explain(self, order):
        action = order.action_magento_explain_totals()
        self.assertEqual(action["res_model"], "magento.totals.explanation")
        explanation = self.Explanation.browse(action["res_id"])
        self.assertBalanced(explanation)
        return explanation

    def assertBalanced(self, explanation):
        """The invariant: components + remainder == difference, exactly, in cents."""
        self.assertEqual(
            sum(cents(line.amount) for line in explanation.line_ids), cents(explanation.difference),
            "Components and remainder must add up to the difference",
        )
        self.assertEqual(cents(explanation.difference),
                         cents(explanation.magento_grand_total - explanation.odoo_total))
        unexplained = explanation.line_ids.filtered(lambda l: l.category == "unexplained")
        self.assertEqual(len(unexplained), 1, "The remainder is always shown")
        self.assertEqual(cents(unexplained.amount), cents(explanation.unexplained_amount))

    def components(self, explanation, category=None):
        lines = explanation.line_ids.filtered(lambda l: l.category != "unexplained")
        if category:
            lines = lines.filtered(lambda l: l.category == category)
        return lines

    def lines(self, order, role):
        return order.order_line.filtered(lambda l: l.magento_line_role == role)

    # ------------------------------------------------------------------ zero difference
    def test_matching_order_has_zero_difference(self):
        order, = self.sync(live_order_1())
        self.assertEqual(order.amount_total, 36.39)
        self.assertFalse(order.magento_total_differs)
        explanation = self.explain(order)
        self.assertTrue(explanation.matches)
        self.assertEqual(explanation.difference, 0.0)
        self.assertEqual(explanation.calibration_gap, 0.0, "The model reproduces Odoo's total")
        self.assertFalse(self.components(explanation))
        self.assertEqual(explanation.unexplained_amount, 0.0)
        self.assertEqual(explanation.state, "explained")
        self.assertEqual(explanation.source, "api")
        self.assertEqual(explanation.magento_grand_total, 36.39)
        self.assertEqual(explanation.magento_increment_id, "000000001")
        self.assertIn("36.39", explanation.summary_html)
        # Read-only: one GET, nothing written to Magento.
        self.assertEqual(self.get_order.call_count, 1)
        self.assertFalse(self.http_attempts)
        # Smart button and re-explaining keep a single explanation per order.
        self.assertEqual(order.magento_explanation_id, explanation)
        self.assertEqual(order.magento_explanation_state, "explained")
        again = self.explain(order)
        self.assertEqual(again, explanation)
        self.assertEqual(order.action_view_magento_explanation()["res_id"], explanation.id)

    # ------------------------------------------------------------------ categories
    def test_rounding_per_line_vs_global(self):
        items = [mg_item(n, sku, 9.99, product_id=pid) for n, sku, pid in (
            (21, "24-WG01", 10), (22, "24-WG02", 11), (23, "24-WG03", 12))]
        data = mg_order(21, items)
        self.assertEqual(data["tax_amount"], 2.46, "Magento 'Row Total': 0.82 per row")
        self.assertEqual(data["grand_total"], 37.43)
        order, = self.sync(data)
        self.assertEqual(order.amount_tax, 2.47, "Odoo rounds globally")
        self.assertTrue(order.magento_total_differs)
        explanation = self.explain(order)
        self.assertEqual(explanation.difference, -0.01)
        rounding = self.components(explanation, "rounding")
        self.assertEqual(len(rounding), 1)
        self.assertEqual(rounding.amount, -0.01)
        self.assertIn("per line 2.46", rounding.evidence)
        self.assertIn("globally 2.47", rounding.evidence)
        self.assertEqual(len(self.components(explanation)), 1)
        self.assertEqual(explanation.state, "explained")
        # The instance mirror says "globally": the fix opens the instance settings.
        self.assertEqual(rounding.fix_type, "open_instance")
        action = rounding.action_apply_fix()
        self.assertEqual((action["res_model"], action["res_id"]), ("magento.instance", self.instance.id))

        # Instance mirrors Magento (per line) but the company still rounds globally:
        # the fix is the existing "Apply to Company" action.
        self.instance.tax_rounding_method = "round_per_line"
        rounding = self.components(self.explain(order), "rounding")
        self.assertEqual(rounding.fix_type, "apply_rounding")
        user = self.create_user("mg_p3_operator", "sdlc_magento_connector.group_magento_user")
        with self.assertRaises(AccessError):
            rounding.with_user(user).action_apply_fix()
        rounding.action_apply_fix()
        self.assertEqual(self.env.company.tax_calculation_rounding_method, "round_per_line")

    def test_tax_included_prices(self):
        # Magento catalog prices include tax: 3 x 1.99 incl. = 5.97 (tax 0.45, excl. 5.52; unit
        # price excl. 1.8383). Imported tax-excluded: 3 x 1.8383 = 5.51 + 0.45 = 5.96.
        item = mg_item(31, "24-UG01", 1.8383, qty=3, row_total=5.52, tax_amount=0.45,
                       price_incl=1.99, product_id=20)
        data = mg_order(31, [item])
        self.assertEqual(data["grand_total"], 10.97)
        self.assertFalse(self.instance.prices_include_tax)
        order, = self.sync(data)
        self.assertEqual(order.amount_total, 10.96, "Tax-excluded import of a tax-included catalog")
        explanation = self.explain(order)
        mode = self.components(explanation, "tax_mode")
        self.assertEqual(len(mode), 1)
        self.assertEqual(mode.amount, 0.01)
        self.assertIn("1.8383", mode.evidence)
        self.assertEqual(mode.fix_type, "open_instance")
        self.assertIn("Catalog Prices Include Tax", mode.fix_label)
        self.assertEqual(mode.action_apply_fix()["res_id"], self.instance.id)
        self.assertEqual(explanation.unexplained_amount, 0.0)

    def test_tax_rate_wrong_mapping(self):
        wrong = self.env["account.tax"].create({
            "name": "Sales tax 8%", "amount": 8.0, "amount_type": "percent", "type_tax_use": "sale",
        })
        mapping = self.env["magento.tax.map"].create({
            "instance_id": self.instance.id, "magento_code": RATE_MI["code"],
            "magento_title": RATE_MI["title"], "percent": 8.25, "tax_id": wrong.id,
        })
        order, = self.sync(live_order_1())
        self.assertEqual(order.amount_total, 36.32)
        explanation = self.explain(order)
        rate = self.components(explanation, "tax_rate")
        self.assertEqual(len(rate), 1)
        self.assertEqual(rate.amount, 0.07)
        self.assertIn("8.25%", rate.evidence)
        self.assertIn("Sales tax 8%", rate.evidence)
        self.assertEqual(rate.sale_line_id, self.lines(order, "product"))
        action = rate.action_apply_fix()
        self.assertEqual(action["res_model"], "magento.tax.map")
        self.assertEqual(action["domain"], [("id", "in", [mapping.id])])
        self.assertEqual(explanation.state, "explained")

    def test_discount_tax_compensation(self):
        # Tax-included catalog, 10.00 off a 100.00 (incl.) item: Magento books a discount tax
        # compensation of 0.76; the connector instance still says "tax-excluded prices".
        item = mg_item(41, "24-MB01", 92.3787, row_total=92.38, tax_amount=6.86, price_incl=100.0,
                       discount=10.0, dtc=0.76, product_id=1)
        data = mg_order(41, [item], shipping=0.0)
        self.assertEqual(data["grand_total"], 90.0)
        order, = self.sync(data)
        self.assertEqual(order.amount_total, 89.18)
        explanation = self.explain(order)
        self.assertEqual(explanation.difference, 0.82)
        dtc = self.components(explanation, "discount_tax")
        self.assertEqual(len(dtc), 1)
        self.assertIn("0.76", dtc.evidence)
        self.assertEqual(dtc.fix_type, "open_instance")
        # Two causes: the product price mode and the tax on the discount.
        self.assertTrue(self.components(explanation, "tax_mode"))
        self.assertEqual(cents(sum(self.components(explanation).mapped("amount"))), 82)
        self.assertEqual(explanation.unexplained_amount, 0.0)

    def test_fpt_not_imported(self):
        item = mg_item(51, "24-MB01", 100.0, tax_percent=0.0, weee_row=3.5, product_id=1)
        data = mg_order(51, [item])
        self.assertEqual(data["grand_total"], 108.5)
        order, = self.sync(data)
        self.lines(order, "fpt").unlink()
        explanation = self.explain(order)
        fpt = self.components(explanation, "fpt")
        self.assertEqual(len(fpt), 1)
        self.assertEqual(fpt.amount, 3.5)
        self.assertEqual(fpt.fix_type, "open_order")
        action = fpt.action_apply_fix()
        self.assertEqual((action["res_model"], action["res_id"]), ("sale.order", order.id))

    def test_shipping_amount_and_tax(self):
        data = live_order_1(shipping=10.0, shipping_tax=0.83)
        self.assertEqual(data["grand_total"], 42.22)
        order, = self.sync(data)
        self.assertEqual(order.amount_total, 42.22)
        shipping = self.lines(order, "shipping")
        self.assertTrue(shipping.tax_ids)
        shipping.write({"tax_ids": [Command.clear()], "price_unit": 9.0})
        explanation = self.explain(order)
        found = self.components(explanation, "shipping").sorted("sequence")
        self.assertEqual(found.mapped("name"), ["Shipping amount", "Shipping tax"])
        self.assertEqual(found.mapped("amount"), [1.0, 0.83])
        self.assertEqual(explanation.unexplained_amount, 0.0)

    def test_missing_extra_and_edited_lines(self):
        data = mg_order(61, [mg_item(61, "WS03-XS-Red", 29.0, product_id=1428),
                             mg_item(62, "24-MB01", 100.0, product_id=1)])
        self.assertEqual(data["grand_total"], 144.64)
        order, = self.sync(data)
        self.assertEqual(order.amount_total, 144.64)
        product_lines = self.lines(order, "product")
        kept = product_lines.filtered(lambda l: l.magento_item_id == "61")
        (product_lines - kept).unlink()
        kept.price_unit = 31.0
        order.write({"order_line": [Command.create({
            "product_id": self.products["24-MB02"].id, "product_uom_qty": 1, "price_unit": 1.0,
            "tax_ids": [Command.clear()],
        })]})
        explanation = self.explain(order)
        missing = self.components(explanation, "missing_line")
        extra = self.components(explanation, "extra_line")
        edited = self.components(explanation, "edited_line")
        self.assertEqual((len(missing), len(extra), len(edited)), (1, 1, 1))
        self.assertEqual(extra.amount, -1.0)
        self.assertEqual(edited.amount, -2.17)
        self.assertEqual(missing.amount, 108.25)
        self.assertEqual(missing.magento_item_id, "62")
        self.assertEqual(explanation.state, "explained")

    def test_discount_line_removed(self):
        data = mg_order(71, [mg_item(71, "24-MB02", 33.33, qty=2.0, discount=7.0, product_id=2)])
        self.assertEqual(data["grand_total"], 69.58)
        order, = self.sync(data)
        self.lines(order, "discount").unlink()
        explanation = self.explain(order)
        discount = self.components(explanation, "discount")
        self.assertEqual(len(discount), 1)
        self.assertEqual(discount.amount, -7.58)
        self.assertEqual(explanation.unexplained_amount, 0.0)

    def test_magento_only_deduction(self):
        data = live_order_1(grand_total=26.39, extra={"gift_cards_amount": 10.0})
        order, = self.sync(data)
        explanation = self.explain(order)
        deduction = self.components(explanation, "adjustment")
        self.assertEqual(deduction.amount, -10.0)
        self.assertEqual(explanation.state, "explained")

    def test_currency_rounding_is_negligible(self):
        order, = self.sync(live_order_1(grand_total=36.396))
        explanation = self.explain(order)
        self.assertEqual(explanation.difference, 0.01)
        rounding = self.components(explanation, "currency_rounding")
        self.assertEqual(rounding.amount, 0.01)
        self.assertTrue(rounding.negligible)
        self.assertEqual(explanation.unexplained_amount, 0.0)
        self.assertEqual(explanation.state, "explained")

    def test_unexplained_remainder_is_never_hidden(self):
        order, = self.sync(live_order_1(grand_total=36.44))
        self.assertTrue(order.magento_total_differs)
        explanation = self.explain(order)
        self.assertEqual(explanation.difference, 0.05)
        self.assertFalse(self.components(explanation))
        self.assertEqual(explanation.unexplained_amount, 0.05)
        self.assertEqual(explanation.state, "unexplained")

    def test_multiple_causes_combined(self):
        wrong = self.env["account.tax"].create({
            "name": "Sales tax 8%", "amount": 8.0, "amount_type": "percent", "type_tax_use": "sale",
        })
        self.env["magento.tax.map"].create({
            "instance_id": self.instance.id, "magento_code": RATE_MI["code"], "percent": 8.25,
            "tax_id": wrong.id,
        })
        order, = self.sync(live_order_1())
        self.lines(order, "shipping").price_unit = 4.0
        order.write({"order_line": [Command.create({
            "product_id": self.products["24-MB01"].id, "product_uom_qty": 1, "price_unit": 1.0,
            "tax_ids": [Command.clear()],
        })]})
        self.assertEqual(order.amount_total, 36.32)
        explanation = self.explain(order)
        self.assertEqual(explanation.difference, 0.07)
        by_category = {line.category: line.amount for line in self.components(explanation)}
        self.assertEqual(by_category, {"extra_line": -1.0, "tax_rate": 0.07, "shipping": 1.0})
        self.assertEqual(explanation.unexplained_amount, 0.0)
        self.assertEqual(explanation.state, "explained")
        self.assertEqual(explanation.component_count, 3)

    def test_partially_explained(self):
        order, = self.sync(live_order_1(grand_total=36.44))
        self.lines(order, "shipping").price_unit = 4.0
        explanation = self.explain(order)
        self.assertEqual(self.components(explanation, "shipping").amount, 1.0)
        self.assertEqual(explanation.unexplained_amount, 0.05)
        self.assertEqual(explanation.state, "partially")

    # ------------------------------------------------------------------ payload source
    @mute_logger(EXPLAINER_LOGGER)
    def test_stored_payload_when_magento_unreachable(self):
        order, = self.sync(live_order_1(grand_total=36.44))
        self.patch_api("get_order", side_effect=requests.exceptions.ConnectionError("reset"))
        with self.assertRaises(UserError):
            order.action_magento_explain_totals()
        self.patch_api("get_order", side_effect=lambda api, order_id: self.payloads[str(order_id)])
        self.explain(order)
        self.patch_api("get_order", side_effect=requests.exceptions.ConnectionError("reset"))
        explanation = self.explain(order)
        self.assertEqual(explanation.source, "stored")
        self.assertEqual(explanation.unexplained_amount, 0.05)

    def test_not_a_magento_order(self):
        partner = self.env["res.partner"].create({"name": "Walk-in"})
        order = self.env["sale.order"].create({"partner_id": partner.id})
        with self.assertRaises(UserError):
            self.Explanation._explain_orders(order)

    # ------------------------------------------------------------------ reconciliation
    def test_explain_all_on_reconciliation_run(self):
        good = live_order_1()
        bad = mg_order(81, [mg_item(81, "WS03-XS-Red", 29.0, product_id=1428)], grand_total=36.44)
        items = [mg_item(n, sku, 9.99, product_id=pid) for n, sku, pid in (
            (91, "24-WG01", 10), (92, "24-WG02", 11), (93, "24-WG03", 12))]
        rounded = mg_order(91, items)
        orders = self.sync(good, bad, rounded)
        summaries = [{
            "entity_id": data["entity_id"], "increment_id": data["increment_id"],
            "created_at": data["created_at"], "grand_total": data["grand_total"],
            "order_currency_code": "USD", "store_id": 1, "status": data["status"],
        } for data in (good, bad, rounded)]
        self.patch_api("iter_search", side_effect=lambda api, endpoint, **kw: iter(list(summaries)))
        run = self.env["magento.reconciliation"].create({
            "instance_id": self.instance.id, "date_from": date(2026, 10, 12), "date_to": date(2026, 10, 12),
        })
        run.action_run()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.mismatch_count, 2)
        self.assertEqual(run.explanation_count, 0)
        action = run.action_explain_all()
        self.assertEqual(action["res_model"], "magento.totals.explanation")
        explanations = self.Explanation.search(action["domain"])
        self.assertEqual(explanations.sale_order_id, orders[1] | orders[2])
        for explanation in explanations:
            self.assertBalanced(explanation)
        self.env.invalidate_all()
        self.assertEqual(run.explanation_count, 2)
        mismatches = run.finding_ids.filtered(lambda f: f.kind == "mismatch")
        states = {f.sale_order_id: f.explanation_state for f in mismatches}
        self.assertEqual(states, {orders[1]: "unexplained", orders[2]: "explained"})
        finding = mismatches.filtered(lambda f: f.sale_order_id == orders[2])
        self.assertEqual(finding.explanation_unexplained, 0.0)
        single = finding.action_explain()
        self.assertEqual(single["res_id"], finding.explanation_id.id)
        self.assertEqual(run.action_view_explanations()["res_model"], "magento.totals.explanation")

    @mute_logger(EXPLAINER_LOGGER)
    def test_explain_all_survives_a_failing_order(self):
        order_ok, order_ko = self.sync(live_order_1(grand_total=36.44),
                                       mg_order(82, [mg_item(82, "24-MB01", 100.0, product_id=1)],
                                                grand_total=1.0))
        del self.payloads["82"]

        def _get_order(api, order_id):
            if str(order_id) not in self.payloads:
                raise requests.exceptions.ConnectionError("Magento is down")
            return self.payloads[str(order_id)]

        self.patch_api("get_order", side_effect=_get_order)
        run = self.env["magento.reconciliation"].create({
            "instance_id": self.instance.id, "date_from": date(2026, 10, 12), "date_to": date(2026, 10, 12),
            "state": "done",
        })
        self.env["magento.reconciliation.order"].create([{
            "reconciliation_id": run.id, "kind": "mismatch", "magento_order_id": order.magento_order_id,
            "sale_order_id": order.id, "magento_total": 1.0, "odoo_total": order.amount_total,
        } for order in (order_ok, order_ko)])
        action = run.action_explain_all()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("1 order(s) explained, 1 failed", action["params"]["message"])
        self.assertEqual(self.Explanation.search([]).sale_order_id, order_ok)
