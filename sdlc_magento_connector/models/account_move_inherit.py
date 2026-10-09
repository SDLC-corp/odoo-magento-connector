import logging

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)


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

    def _magento_prepare_credit_memo_payload(self):
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
            "notify": False,
            "appendComment": False,
        }

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
        api = MagentoAPI(instance.sudo())
        try:
            invoice_id = api.create_invoice(order.magento_order_id, payload)
        except requests.exceptions.RequestException as exc:
            order._magento_raise_http_error(exc)
        self.magento_invoice_id = str(invoice_id)
        try:
            order._magento_refresh_from_magento(api, order.magento_order_id)
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
        api = MagentoAPI(instance.sudo())
        try:
            credit_memo_id = api.create_credit_memo(order.magento_order_id, payload)
        except requests.exceptions.RequestException as exc:
            order._magento_raise_http_error(exc)
        self.magento_credit_memo_id = str(credit_memo_id)
        try:
            credit_data = api.get_credit_memo(credit_memo_id)
        except requests.exceptions.RequestException as exc:
            _logger.warning("Magento credit memo %s could not be read back: %s", credit_memo_id, exc)
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
