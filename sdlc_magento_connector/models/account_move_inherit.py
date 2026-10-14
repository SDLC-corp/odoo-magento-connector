import logging
from collections import defaultdict

import requests
from markupsafe import Markup, escape

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)

REFUND_MODES = [("offline", "Offline"), ("online", "Online")]

# Magento payment methods that can never refund online (no gateway transaction).
OFFLINE_PAYMENT_METHODS = frozenset({
    "checkmo", "banktransfer", "cashondelivery", "purchaseorder", "free", "companycredit",
})


class AccountMove(models.Model):
    _inherit = "account.move"

    magento_sale_order_id = fields.Many2one(
        "sale.order",
        string="Magento Order",
        compute="_compute_magento_sale_order",
        store=True,
    )
    magento_sale_order_name = fields.Char(
        string="Order #",
        compute="_compute_magento_sale_order",
        store=True,
    )
    magento_sale_order_date = fields.Date(
        string="Order Date",
        compute="_compute_magento_sale_order",
        store=True,
    )
    magento_sale_order_created_at = fields.Datetime(
        string="Order Date/Time",
        compute="_compute_magento_sale_order",
        store=True,
    )
    magento_instance_id = fields.Many2one(
        "magento.instance",
        string="Magento Instance",
        related="magento_sale_order_id.magento_instance_id",
        store=True,
        index=True,
    )
    magento_invoice_id = fields.Char(
        string="Magento Invoice ID",
        readonly=True,
        copy=False,
        index=True,
    )
    magento_credit_memo_id = fields.Char(
        string="Magento Credit Memo ID",
        readonly=True,
        copy=False,
        index=True,
    )
    magento_credit_memo_created_at = fields.Datetime(
        string="Credit Memo Created",
        readonly=True,
        copy=False,
    )
    magento_credit_memo_display = fields.Char(
    string="Credit Memo",
    compute="_compute_magento_credit_memo_display",
    store=True,
    index=True,
    )
    magento_refund_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("open", "Open"),
            ("partial", "Partially Refunded"),
            ("refunded", "Refunded"),
        ],
        string="Refund Status",
        compute="_compute_magento_refund_state",
        store=True,
        readonly=True,
    )
    magento_refunded_amount = fields.Monetary(
    string="Refunded",
    currency_field="currency_id",
    compute="_compute_magento_refunded_amount",
    store=True,
    )

    # Store Magento credit memo amounts so Odoo UI matches Magento even if taxes/config differ.
    magento_credit_memo_grand_total = fields.Monetary(
        string="Magento Credit Memo Grand Total",
        currency_field="currency_id",
        readonly=True,
        copy=False,
    )
    magento_credit_memo_tax_amount = fields.Monetary(
        string="Magento Credit Memo Tax",
        currency_field="currency_id",
        readonly=True,
        copy=False,
    )
    magento_credit_memo_subtotal = fields.Monetary(
        string="Magento Credit Memo Subtotal",
        currency_field="currency_id",
        readonly=True,
        copy=False,
    )

    # --- Phase 3.2: returns lifecycle ---
    magento_refund_mode = fields.Selection(
        REFUND_MODES, string="Magento Refund", readonly=True, copy=False,
        help="Offline: /V1/order/{id}/refund (money returned outside Magento). Online: "
             "/V1/invoice/{id}/refund through the payment gateway.",
    )
    magento_restock = fields.Boolean(
        string="Returned to Stock", readonly=True, copy=False,
        help="Sent to Magento with return_to_stock_items (credit note of an Odoo return), or "
             "an imported Magento credit memo that restocked the items.",
    )
    magento_restock_pending = fields.Json(
        readonly=True, copy=False,
        help="{sale line id: qty} still to receive back for an imported credit memo.",
    )
    magento_restock_waiting = fields.Boolean(
        string="Restock Waiting for Delivery", readonly=True, copy=False, index="btree_not_null",
    )
    magento_return_reason_id = fields.Many2one(
        "magento.return.reason", string="Magento Return Reason", copy=False, ondelete="set null",
    )
    magento_return_picking_ids = fields.One2many(
        "stock.picking", "magento_refund_move_id", string="Return Receipts", readonly=True,
    )

    _magento_invoice_uniq = models.Constraint(
        "unique(magento_instance_id, magento_invoice_id)",
        "This Magento invoice is already linked to another journal entry of the same instance.",
    )
    _magento_credit_memo_uniq = models.Constraint(
        "unique(magento_instance_id, magento_credit_memo_id)",
        "This Magento credit memo is already linked to another journal entry of the same instance.",
    )

    @api.depends(
        "invoice_line_ids.sale_line_ids.order_id",
        "invoice_line_ids.sale_line_ids.order_id.magento_order_id",
        "invoice_line_ids.sale_line_ids.order_id.magento_instance_id",
        "invoice_line_ids.sale_line_ids.order_id.magento_increment_id",
        "invoice_line_ids.sale_line_ids.order_id.name",
        "invoice_line_ids.sale_line_ids.order_id.date_order",
        "invoice_line_ids.sale_line_ids.order_id.magento_created_at",
        "reversed_entry_id",
        "reversed_entry_id.invoice_origin",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.magento_order_id",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.magento_instance_id",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.magento_increment_id",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.name",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.date_order",
        "reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.magento_created_at",
        "invoice_origin",
    )
    def _compute_magento_sale_order(self):
        SaleOrder = self.env["sale.order"]
        for move in self:
            order = False
            orders = move.invoice_line_ids.mapped("sale_line_ids.order_id")
            orders = orders.filtered(lambda o: o.magento_order_id or o.magento_instance_id)
            if orders:
                order = orders[:1]

            # When a credit note is created via reversal, Odoo doesn't always keep
            # the sale_line_ids/invoice_origin on the refund move; fall back to the
            # reversed invoice to recover the Magento sale order link.
            if not order and move.reversed_entry_id:
                reversed_move = move.reversed_entry_id
                reversed_orders = reversed_move.invoice_line_ids.mapped("sale_line_ids.order_id")
                reversed_orders = reversed_orders.filtered(
                    lambda o: o.magento_order_id or o.magento_instance_id
                )
                if reversed_orders:
                    order = reversed_orders[:1]
                elif reversed_move.invoice_origin:
                    origins = [
                        o.strip()
                        for o in reversed_move.invoice_origin.split(",")
                        if o.strip()
                    ]
                    if origins:
                        order = SaleOrder.search([
                            ("magento_order_id", "!=", False),
                            "|",
                            ("name", "in", origins),
                            ("magento_increment_id", "in", origins),
                        ], limit=1)

            if not order and move.invoice_origin:
                origins = [o.strip() for o in move.invoice_origin.split(",") if o.strip()]
                if origins:
                    order = SaleOrder.search([
                        ("magento_order_id", "!=", False),
                        "|",
                        ("name", "in", origins),
                        ("magento_increment_id", "in", origins),
                    ], limit=1)

            move.magento_sale_order_id = order or False
            move.magento_sale_order_name = (
                (order.magento_increment_id or order.name) if order else ""
            )
            order_dt = order.magento_created_at or order.date_order if order else False
            move.magento_sale_order_created_at = order_dt or False
            move.magento_sale_order_date = (
                fields.Date.to_date(order_dt) if order_dt else False
            )

    @api.depends("move_type", "state", "payment_state", "magento_credit_memo_id")
    def _compute_magento_refund_state(self):
        for move in self:
            if move.move_type != "out_refund":
                move.magento_refund_state = False
                continue
            if move.state != "posted":
                move.magento_refund_state = "draft"
            elif move.magento_credit_memo_id:
                move.magento_refund_state = "refunded"
            elif move.payment_state in ("paid", "in_payment"):
                move.magento_refund_state = "refunded"
            elif move.payment_state == "partial":
                move.magento_refund_state = "partial"
            else:
                move.magento_refund_state = "open"

    @api.depends(
        "amount_total",
        "magento_credit_memo_id",
        "magento_credit_memo_grand_total",
        "magento_credit_memo_tax_amount",
    )
    def _compute_magento_refunded_amount(self):
        for move in self:
            # Prefer Magento totals (when available) so UI matches Magento.
            if move.magento_credit_memo_id and move.magento_credit_memo_grand_total:
                # User expectation: "Refunded" excluding tax (Magento grid often shows excl. tax).
                base = float(move.magento_credit_memo_grand_total or 0.0)
                tax = float(move.magento_credit_memo_tax_amount or 0.0)
                move.magento_refunded_amount = abs(base - tax) if tax else abs(base)
            else:
                move.magento_refunded_amount = abs(move.amount_total or 0.0)

    @api.depends("ref", "name", "magento_credit_memo_id", "state", "move_type")
    def _compute_magento_credit_memo_display(self):
        for move in self:
            ref = (move.ref or "").strip()
            if ref:
                move.magento_credit_memo_display = ref
                continue

            credit_id = (move.magento_credit_memo_id or "").strip()
            if credit_id:
                move.magento_credit_memo_display = (
                    credit_id.zfill(9) if credit_id.isdigit() else credit_id
                )
                continue

            # Fallback so unsynced credit notes are still identifiable in the list view.
            if move.name and move.name != "/":
                move.magento_credit_memo_display = move.name
            else:
                move.magento_credit_memo_display = str(move.id or "")

    def _magento_prepare_invoice_payload(self):
        self.ensure_one()
        order = self.magento_sale_order_id
        if not order or not order.magento_order_id:
            return None

        item_qtys = {}
        for line in self.invoice_line_ids:
            sale_lines = line.sale_line_ids.filtered(lambda l: l.order_id == order)
            for sol in sale_lines:
                if not sol.magento_item_id:
                    continue
                qty = line.quantity or sol.qty_invoiced or sol.product_uom_qty or 0.0
                if qty <= 0:
                    continue
                item_id = sol._magento_push_item_id()
                item_qtys.setdefault(item_id, 0.0)
                item_qtys[item_id] += qty

        if not item_qtys:
            for line in self.invoice_line_ids.filtered(lambda l: l.product_id):
                matches = order.order_line.filtered(
                    lambda sol: sol.magento_item_id
                    and (
                        sol.product_id == line.product_id
                        or (
                            sol.magento_sku
                            and line.product_id.default_code
                            and sol.magento_sku == line.product_id.default_code
                        )
                    )
                )
                if len(matches) == 1:
                    sol = matches[0]
                    qty = line.quantity or sol.qty_invoiced or sol.product_uom_qty or 0.0
                    if qty > 0:
                        item_id = sol._magento_push_item_id()
                        item_qtys.setdefault(item_id, 0.0)
                        item_qtys[item_id] += qty

        if not item_qtys:
            return None

        items = []
        for item_id, qty in item_qtys.items():
            try:
                order_item_id = int(item_id)
            except (TypeError, ValueError):
                raise UserError(_("Magento order item ID is missing or invalid."))
            items.append({
                "order_item_id": order_item_id,
                "qty": qty,
            })

        return {
            "items": items,
            "capture": False,
            "notify": False,
            "appendComment": False,
        }

    # ------------------------------------------------------------------
    # Odoo -> Magento credit memo (3.2)
    # ------------------------------------------------------------------
    def _magento_line_price_difference(self, line, sol, qty):
        """Signed tax-included amount by which a refunded product line differs from the
        order price (Magento refunds items at the order price: the difference is an
        adjustment)."""
        currency = self.currency_id
        line_unit = line.price_unit * (1.0 - (line.discount or 0.0) / 100.0)
        sol_unit = sol.price_unit * (1.0 - (sol.discount or 0.0) / 100.0)
        if float_compare(line_unit, sol_unit, precision_digits=max(currency.decimal_places, 2) + 2) == 0:
            return 0.0
        base = line_unit * qty
        ratio = (line.price_total / base) if base else 1.0
        return currency.round((line_unit - sol_unit) * qty * ratio)

    def _magento_credit_memo_lines(self):
        """Classify the credit note lines for the Magento credit memo.

        * product lines of Magento order items -> ``items`` (qty per push item id); a
          price lower/higher than the order price adds an adjustment;
        * shipping lines -> ``shipping`` (tax included when the instance says so);
        * item / shipping discount and FPT lines -> ignored (Magento recomputes them);
        * the instance refund adjustment / return fee products and any other free line
          -> signed ``adjustments``.
        """
        self.ensure_one()
        order = self.magento_sale_order_id
        instance = order.magento_instance_id
        currency = self.currency_id
        shipping_product = instance.shipping_product_id
        adjustment_product = instance.refund_adjustment_product_id
        fee_product = instance.return_fee_product_id
        items = defaultdict(float)
        item_lines = {}
        shipping = 0.0
        adjustments = []
        for line in self.invoice_line_ids.filtered(lambda l: l.display_type == "product"):
            sols = line.sale_line_ids.filtered(lambda l: l.order_id == order)
            item_sol = sols.filtered("magento_item_id")[:1]
            if not item_sol and not sols and line.product_id and line.product_id not in (
                    shipping_product | adjustment_product | fee_product):
                matches = order.order_line.filtered(
                    lambda sol: sol.magento_item_id
                    and (
                        sol.product_id == line.product_id
                        or (
                            sol.magento_sku
                            and line.product_id.default_code
                            and sol.magento_sku == line.product_id.default_code
                        )
                    )
                )
                if len(matches) == 1:
                    item_sol = matches
            if item_sol:
                qty = line.quantity or item_sol.qty_invoiced or item_sol.product_uom_qty or 0.0
                if qty <= 0:
                    continue
                key = item_sol._magento_push_item_id()
                items[key] += qty
                item_lines[key] = item_sol
                difference = self._magento_line_price_difference(line, item_sol, qty)
                if not currency.is_zero(difference):
                    adjustments.append(difference)
                continue
            role = sols[:1].magento_line_role if sols else False
            is_delivery = bool(sols and "is_delivery" in sols._fields and sols[:1].is_delivery)
            if role == "shipping" or is_delivery or (shipping_product and line.product_id == shipping_product):
                shipping += line.price_total if instance.shipping_includes_tax else line.price_subtotal
                continue
            if role in ("discount", "fpt"):
                continue
            if adjustment_product and line.product_id == adjustment_product:
                adjustments.append(abs(line.price_total))
            elif fee_product and line.product_id == fee_product:
                adjustments.append(-abs(line.price_total))
            elif not currency.is_zero(line.price_total):
                adjustments.append(line.price_total)
        return {
            "items": dict(items),
            "item_lines": item_lines,
            "shipping": currency.round(shipping),
            "positive": currency.round(sum(a for a in adjustments if a > 0)),
            "negative": currency.round(-sum(a for a in adjustments if a < 0)),
        }

    def _magento_refund_return_pickings(self, item_lines):
        """Done Odoo returns (incoming pickings of the order) this credit note restocks, and
        the Magento order item ids to send as ``return_to_stock_items``."""
        self.ensure_one()
        order = self.magento_sale_order_id
        Picking = self.env["stock.picking"]
        pickings = self.magento_return_picking_ids.filtered(lambda p: p.state == "done")
        if not pickings and order:
            pickings = Picking.search([
                ("sale_id", "=", order.id),
                ("state", "=", "done"),
                ("picking_type_code", "=", "incoming"),
                ("magento_refund_move_id", "=", False),
            ]).filtered(lambda p: p.return_id or p.location_id.usage == "customer")
        by_line = {sol: key for key, sol in item_lines.items()}
        restock_ids = []
        used = Picking
        for picking in pickings.sorted("id"):
            keys = {
                by_line[move.sale_line_id]
                for move in picking.move_ids
                if move.state == "done" and move.sale_line_id in by_line
            }
            if not keys:
                continue
            used |= picking
            for key in keys:
                try:
                    item_id = int(key)
                except (TypeError, ValueError):
                    continue
                if item_id not in restock_ids:
                    restock_ids.append(item_id)
        return used, restock_ids

    def _magento_refund_invoice_id(self):
        """Magento invoice id refunded by this credit note (online refunds)."""
        self.ensure_one()
        if self.reversed_entry_id.magento_invoice_id:
            return self.reversed_entry_id.magento_invoice_id
        order = self.magento_sale_order_id
        invoices = order._magento_get_customer_invoices().filtered("magento_invoice_id").sorted("id")
        return invoices[-1:].magento_invoice_id or False

    def _magento_refund_route(self):
        """(mode, Magento invoice id, why offline) for this credit note."""
        self.ensure_one()
        order = self.magento_sale_order_id
        instance = order.magento_instance_id
        method = (order.magento_payment_method or "").strip()
        mapping = self.env["magento.payment.map"].sudo().search([
            ("instance_id", "=", instance.id), ("magento_method", "=", method),
        ], limit=1) if method else self.env["magento.payment.map"]
        mode = mapping.refund_mode or "instance"
        if mode == "offline":
            return "offline", False, _("payment method %s is set to offline refunds", method)
        if mode == "instance":
            if not instance.magento_refund_online:
                return "offline", False, ""
            if not method or method in OFFLINE_PAYMENT_METHODS:
                return "offline", False, _(
                    "payment method %s cannot refund online", method or _("(unknown)"))
        invoice_id = self._magento_refund_invoice_id()
        if not invoice_id:
            return "offline", False, _("no Magento invoice is linked to the order")
        return "online", invoice_id, ""

    def _magento_prepare_credit_memo_payload(self):
        """Payload of POST /V1/order/{id}/refund (and /V1/invoice/{id}/refund): refunded
        items, shipping refund, adjustments, return-to-stock items and the reason."""
        self.ensure_one()
        order = self.magento_sale_order_id
        if not order or not order.magento_order_id:
            return None
        instance = order.magento_instance_id
        data = self._magento_credit_memo_lines()
        if not data["items"]:
            return None
        items = []
        for item_id, qty in data["items"].items():
            try:
                order_item_id = int(item_id)
            except (TypeError, ValueError):
                raise UserError(_("Magento order item ID is missing or invalid."))
            items.append({"order_item_id": order_item_id, "qty": qty})
        pickings, restock_ids = self._magento_refund_return_pickings(data["item_lines"])
        arguments = {
            # Always explicit: without it Magento refunds the whole remaining shipping.
            "shipping_amount": data["shipping"],
            "adjustment_positive": data["positive"],
            "adjustment_negative": data["negative"],
        }
        if restock_ids:
            arguments["extension_attributes"] = {"return_to_stock_items": restock_ids}
        reason = self.magento_return_reason_id or pickings.magento_return_reason_id[:1]
        payload = {
            "items": items,
            "notify": bool(instance.magento_refund_notify),
            "appendComment": bool(reason),
            "arguments": arguments,
        }
        if reason:
            payload["comment"] = {
                "comment": _("Return reason: %s", reason.name),
                "is_visible_on_front": 0,
            }
        return payload

    def _magento_create_invoice(self):
        self.ensure_one()
        if self.env.context.get("skip_magento_sync"):
            return
        if self.move_type != "out_invoice":
            return
        if self.magento_invoice_id:
            return
        order = self.magento_sale_order_id
        if not order or not order.magento_order_id:
            return
        instance = order.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required to create a Magento invoice."))
        if order.magento_status == "complete" or order.magento_invoice_state == "invoiced":
            try:
                order._magento_sync_invoices(MagentoAPI(instance.sudo()))
            except (UserError, requests.exceptions.RequestException) as exc:
                self.message_post(body=_("Magento invoice sync failed: %s") % exc)
            self.message_post(
                body=_("Magento order is already complete/invoiced. Skipped creating Magento invoice.")
            )
            return
        payload = self._magento_prepare_invoice_payload()
        if not payload:
            raise UserError(
                _("No Magento order items found on this invoice. "
                  "Ensure Magento order lines exist before pushing the invoice.")
            )
        invoice_id, _invoice_data = order._magento_push_document("invoice", self, payload)
        if not invoice_id:
            raise UserError(_("Magento did not return an invoice ID for order %s.") % order.name)
        self.with_context(skip_magento_sync=True).write({"magento_invoice_id": invoice_id})
        try:
            order._magento_refresh_from_magento(MagentoAPI(instance.sudo()), order.magento_order_id)
        except (UserError, requests.exceptions.RequestException) as exc:
            _logger.warning(
                "Magento order %s refresh after invoice push failed: %s", order.name, exc
            )

    def _magento_create_credit_memo(self):
        self.ensure_one()
        if self.env.context.get("skip_magento_sync"):
            return
        if self.move_type != "out_refund":
            return
        if self.magento_credit_memo_id:
            return
        order = self.magento_sale_order_id
        if not order or not order.magento_order_id:
            return
        instance = order.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required to create a Magento credit memo."))
        payload = self._magento_prepare_credit_memo_payload()
        if not payload:
            raise UserError(
                _("No Magento order items found on this refund. "
                  "Ensure Magento order lines exist before pushing the credit memo.")
            )
        mode, invoice_id, why_offline = self._magento_refund_route()
        kind = "credit_memo"
        if mode == "online":
            kind = "credit_memo_online"
            payload = dict(payload, isOnline=True, _magento_invoice_id=invoice_id)
        credit_memo_id, credit_data = order._magento_push_document(
            kind, self, payload, amount=self.amount_total, currency=self.currency_id
        )
        if not credit_memo_id:
            raise UserError(_("Magento did not return a credit memo ID for order %s.") % order.name)
        pickings, restock_ids = self._magento_refund_return_pickings(
            self._magento_credit_memo_lines()["item_lines"])
        reason = self.magento_return_reason_id or pickings.magento_return_reason_id[:1]
        self.with_context(skip_magento_sync=True).write({
            "magento_credit_memo_id": credit_memo_id,
            "magento_refund_mode": mode,
            "magento_restock": bool(restock_ids),
            "magento_return_reason_id": reason.id or False,
        })
        if pickings:
            pickings.with_context(skip_magento_sync=True).write({"magento_refund_move_id": self.id})
        if not credit_data:
            try:
                credit_data = MagentoAPI(instance.sudo()).get_credit_memo(credit_memo_id)
            except requests.exceptions.RequestException as exc:
                _logger.warning(
                    "Magento credit memo %s could not be read back: %s", credit_memo_id, exc
                )
                credit_data = {}
        if credit_data:
            if credit_data.get("created_at"):
                self.magento_credit_memo_created_at = fields.Datetime.to_datetime(
                    credit_data.get("created_at")
                )
            if credit_data.get("increment_id") and not self.ref:
                # Store the Magento document number as Odoo's external reference.
                self.ref = credit_data.get("increment_id")
            # Persist Magento amounts so tax configuration differences don't affect the display.
            order._magento_update_credit_memo_amounts(self, credit_data)
        if not self.magento_credit_memo_created_at:
            self.magento_credit_memo_created_at = fields.Datetime.now()
        # Force recompute stored values so list view updates immediately
        self._compute_magento_credit_memo_display()
        self._compute_magento_refunded_amount()
        self._compute_magento_refund_state()
        self._magento_post_credit_memo_notes(payload, mode, invoice_id, why_offline, pickings,
                                             restock_ids, reason,
                                             (credit_data or {}).get("increment_id") or credit_memo_id)

    def _magento_post_credit_memo_notes(self, payload, mode, invoice_id, why_offline, pickings,
                                        restock_ids, reason, memo):
        """Chatter trace of a pushed credit memo on the credit note, the order and returns."""
        self.ensure_one()
        order = self.magento_sale_order_id
        arguments = payload.get("arguments") or {}
        if mode == "online":
            route = _("online refund through the payment gateway (Magento invoice %s)", invoice_id)
        else:
            route = _("offline refund")
            if why_offline:
                route = "%s (%s)" % (route, why_offline)
        details = [
            _("Refund: %s", route),
            _("Items: %s", ", ".join(
                "#%s × %s" % (item["order_item_id"], item["qty"]) for item in payload["items"])),
            _("Shipping refunded: %s", arguments.get("shipping_amount", 0.0)),
        ]
        if arguments.get("adjustment_positive"):
            details.append(_("Adjustment refund: %s", arguments["adjustment_positive"]))
        if arguments.get("adjustment_negative"):
            details.append(_("Return fee: %s", arguments["adjustment_negative"]))
        if restock_ids:
            details.append(_("Returned to stock: items %s (%s)", ", ".join(
                "#%s" % i for i in restock_ids), ", ".join(pickings.mapped("name"))))
        else:
            details.append(_("Returned to stock: no (refund only)"))
        if reason:
            details.append(_("Reason: %s", reason.display_name))
        lines = Markup("<br/>").join(escape(d) for d in details)
        self.message_post(body=Markup("%s<br/>%s") % (
            _("Magento credit memo %s created.", memo), lines))
        order.message_post(body=Markup("%s %s<br/>%s") % (
            _("Magento credit memo %s created from", memo), self._get_html_link(), lines))
        for picking in pickings:
            picking.message_post(body=Markup("%s %s") % (
                _("Returned to stock in Magento with credit memo %s:", memo), self._get_html_link()))

    def action_open_magento_credit_memo(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Credit Memo",
            "res_model": "account.move",
            "view_mode": "form",
            "res_id": self.id,
            "target": "current",
        }

    def action_post(self):
        res = super().action_post()
        if self.env.context.get("skip_magento_sync"):
            return res
        for move in self:
            try:
                move._magento_create_invoice()
            except UserError as exc:
                move.message_post(body=_("Magento invoice not created: %s") % exc)
            except Exception as exc:
                move.message_post(body=_("Magento invoice not created due to error: %s") % exc)
            try:
                move._magento_create_credit_memo()
            except UserError as exc:
                move.message_post(body=_("Magento credit memo not created: %s") % exc)
            except Exception as exc:
                move.message_post(body=_("Magento credit memo not created due to error: %s") % exc)
        return res

    def action_open_magento_invoice(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Invoice",
            "res_model": "account.move",
            "view_mode": "form",
            "res_id": self.id,
            "target": "current",
        }

    def _invoice_paid_hook(self):
        res = super()._invoice_paid_hook()
        if self.env.context.get("skip_magento_sync"):
            return res
        for move in self.filtered(lambda m: m.move_type == "out_invoice" and m.payment_state == "paid"):
            order = move.magento_sale_order_id
            if not order or not order.magento_order_id:
                continue
            instance = order.magento_instance_id
            if not instance or not instance.update_magento_status_on_paid:
                continue
            status_code = (instance.magento_paid_status or "").strip()
            if not status_code:
                continue
            # PUT /V1/orders/{id} only edits addresses; the status goes through a comment.
            try:
                order._magento_push_status(
                    status_code,
                    _("Invoice %s paid in Odoo.") % move.name,
                )
            except (UserError, requests.exceptions.RequestException) as exc:
                move.message_post(
                    body=_("Failed to update Magento order status on payment: %s") % exc
                )
        return res
