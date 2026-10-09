import logging
import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    is_magento_order = fields.Boolean(
        string="Is Magento Order",
        compute="_compute_is_magento_order",
        store=False,
    )
    magento_order_id = fields.Char("Magento Order ID", readonly=True, copy=False)
    magento_sync_conflict = fields.Boolean(
        "Magento Conflict", copy=False, readonly=True,
        help="Set when Magento changed an order that is no longer a draft in Odoo; the lines were not overwritten.",
    )
    magento_conflict_note = fields.Text(copy=False, readonly=True)
    magento_increment_id = fields.Char("Magento Increment ID", readonly=True, copy=False)
    magento_status = fields.Char("Magento Status", readonly=True, copy=False)
    magento_currency = fields.Char("Magento Currency", readonly=True, copy=False)
    magento_customer_email = fields.Char("Magento Customer Email", copy=False)
    magento_instance_id = fields.Many2one(
        "magento.instance",
        string="Magento Instance",
        copy=False,
    )
    magento_created_at = fields.Datetime("Magento Created At", readonly=True, copy=False)
    magento_updated_at = fields.Datetime("Magento Updated At", readonly=True, copy=False)
    magento_total_invoiced = fields.Monetary(
        string="Magento Invoiced",
        currency_field="currency_id",
        readonly=True,
        copy=False,
    )
    magento_total_paid = fields.Monetary(
        string="Magento Paid",
        currency_field="currency_id",
        readonly=True,
        copy=False,
    )
    magento_invoice_state = fields.Selection(
        [
            ("not_invoiced", "Not Invoiced"),
            ("partial", "Partially Invoiced"),
            ("invoiced", "Invoiced"),
        ],
        string="Magento Invoice Status",
        readonly=True,
        copy=False,
    )
    magento_payment_state = fields.Selection(
        [
            ("not_paid", "Not Paid"),
            ("partial", "Partially Paid"),
            ("paid", "Paid"),
        ],
        string="Magento Payment Status",
        readonly=True,
        copy=False,
    )

    @api.depends("magento_order_id", "magento_instance_id")
    def _compute_is_magento_order(self):
        for order in self:
            order.is_magento_order = bool(
                order.magento_order_id
                or order.magento_instance_id
            )

    @api.depends("order_line.invoice_status", "state", "magento_invoice_state")
    def _compute_invoice_status(self):
        super()._compute_invoice_status()
        for order in self:
            if not (order.magento_order_id or order.magento_instance_id):
                continue
            if order.magento_invoice_state in ("partial", "invoiced"):
                order.invoice_status = "invoiced"

    # -------------------------
    # MAGENTO DATA MAPPING
    # -------------------------
    @api.model
    def _magento_extract_billing_address(self, data):
        billing = data.get("billing_address") or {}
        if billing:
            return billing
        addresses = data.get("addresses") or []
        if addresses:
            return addresses[0]
        return {}

    @api.model
    def _magento_resolve_customer(self, data):
        billing = self._magento_extract_billing_address(data)
        email = data.get("customer_email") or billing.get("email") or ""
        firstname = data.get("customer_firstname") or billing.get("firstname") or ""
        lastname = data.get("customer_lastname") or billing.get("lastname") or ""
        name = " ".join([p for p in [firstname, lastname] if p]).strip() or email or ""

        partner_model = self.env["res.partner"].sudo()
        partner = False
        if email:
            partner = partner_model.search([("email", "=", email)], limit=1)
        if not partner and name:
            partner = partner_model.search([("name", "=", name)], limit=1)
        if partner:
            return partner, email or partner.email or ""

        if not name:
            name = "Magento Guest"

        vals = {
            "name": name,
            "email": email or False,
        }
        phone = billing.get("telephone") or ""
        if phone:
            vals["phone"] = phone
        street = billing.get("street") or []
        if street:
            vals["street"] = street[0] if len(street) > 0 else ""
            vals["street2"] = street[1] if len(street) > 1 else ""
        city = billing.get("city") or ""
        if city:
            vals["city"] = city
        postcode = billing.get("postcode") or ""
        if postcode:
            vals["zip"] = postcode
        country_code = billing.get("country_id") or ""
        if country_code:
            country = self.env["res.country"].sudo().search(
                [("code", "=", country_code)], limit=1
            )
            if country:
                vals["country_id"] = country.id
        region = billing.get("region") or ""
        if region and "state_id" in partner_model._fields and vals.get("country_id"):
            state = self.env["res.country.state"].sudo().search(
                [("name", "=", region), ("country_id", "=", vals.get("country_id"))],
                limit=1,
            )
            if state:
                vals["state_id"] = state.id
        partner = partner_model.create(vals)
        return partner, email or partner.email or ""

    @api.model
    def _magento_prepare_order_vals(self, data, instance):
        partner, email = self._magento_resolve_customer(data)
        magento_id = str(data.get("entity_id") or data.get("id") or "")
        currency_code = data.get("order_currency_code") or data.get("base_currency_code") or ""
        total_invoiced = data.get("total_invoiced")
        if total_invoiced is None:
            total_invoiced = data.get("base_total_invoiced")
        try:
            total_invoiced = float(total_invoiced or 0.0)
        except (TypeError, ValueError):
            total_invoiced = 0.0
        total_paid = data.get("total_paid")
        if total_paid is None:
            total_paid = data.get("base_total_paid")
        try:
            total_paid = float(total_paid or 0.0)
        except (TypeError, ValueError):
            total_paid = 0.0
        grand_total = data.get("grand_total")
        if grand_total is None:
            grand_total = data.get("base_grand_total")
        try:
            grand_total = float(grand_total or 0.0)
        except (TypeError, ValueError):
            grand_total = 0.0
        if total_invoiced <= 0:
            invoice_state = "not_invoiced"
        elif grand_total and total_invoiced + 0.0001 < grand_total:
            invoice_state = "partial"
        else:
            invoice_state = "invoiced"
        if total_paid <= 0:
            payment_state = "not_paid"
        elif grand_total and total_paid + 0.0001 < grand_total:
            payment_state = "partial"
        else:
            payment_state = "paid"
        vals = {
            "partner_id": partner.id,
            "partner_invoice_id": partner.id,
            "partner_shipping_id": partner.id,
            "magento_instance_id": instance.id if instance else False,
            "magento_order_id": magento_id,
            "magento_increment_id": data.get("increment_id") or "",
            "magento_status": data.get("status") or "",
            "magento_currency": currency_code,
            "magento_customer_email": email,
            "magento_total_invoiced": total_invoiced,
            "magento_invoice_state": invoice_state,
            "magento_total_paid": total_paid,
            "magento_payment_state": payment_state,
        }
        if data.get("created_at"):
            vals["date_order"] = data.get("created_at")
            vals["magento_created_at"] = data.get("created_at")
        if data.get("updated_at"):
            vals["magento_updated_at"] = data.get("updated_at")
        if currency_code:
            currency = self.env["res.currency"].search([("name", "=", currency_code)], limit=1)
            if currency:
                vals["currency_id"] = currency.id
        return vals

    @api.model
    def _magento_prepare_order_line_vals(self, item, instance):
        sku = item.get("sku") or ""
        product = False
        product_map = False
        map_model = self.env["magento.product.map"].sudo()
        magento_product_id = item.get("product_id")

        if instance:
            if magento_product_id:
                product_map = map_model.search([
                    ("instance_id", "=", instance.id),
                    ("magento_id", "=", str(magento_product_id)),
                ], limit=1)
            if not product_map and sku:
                product_map = map_model.search([
                    ("instance_id", "=", instance.id),
                    ("sku", "=", sku),
                ], limit=1)
        elif sku:
            product_map = map_model.search([("sku", "=", sku)], limit=1)

        if product_map and product_map.odoo_product_id:
            product = product_map.odoo_product_id
        elif sku:
            product = self.env["product.product"].sudo().search(
                [("default_code", "=", sku)], limit=1
            )

        qty = float(item.get("qty_ordered") or item.get("qty") or 0.0)
        price = float(item.get("price") or 0.0)
        vals = {
            "product_id": product.id if product else False,
            "name": item.get("name") or (product.display_name if product else sku or "Magento Item"),
            "product_uom_qty": qty or 1.0,
            "price_unit": price,
            "magento_item_id": str(item.get("item_id") or ""),
            "magento_product_id": product_map.id if product_map else False,
            "magento_sku": sku,
        }
        if product and "product_uom" in self.env["sale.order.line"]._fields:
            vals["product_uom"] = product.uom_id.id
        elif "product_uom" in self.env["sale.order.line"]._fields:
            try:
                vals["product_uom"] = self.env.ref("uom.product_uom_unit").id
            except Exception:
                pass
        return vals

    @api.model
    def _magento_compute_invoice_summary(self, order_data, invoices):
        grand_total = order_data.get("grand_total")
        if grand_total is None:
            grand_total = order_data.get("base_grand_total")
        try:
            grand_total = float(grand_total or 0.0)
        except (TypeError, ValueError):
            grand_total = 0.0

        total_invoiced = 0.0
        invoices = invoices or []
        for inv in invoices:
            amount = inv.get("grand_total")
            if amount is None:
                amount = inv.get("base_grand_total")
            try:
                amount = float(amount or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            total_invoiced += amount

        if total_invoiced <= 0 and invoices:
            invoice_state = "partial"
        elif total_invoiced <= 0:
            invoice_state = "not_invoiced"
        elif grand_total and total_invoiced + 0.0001 < grand_total:
            invoice_state = "partial"
        else:
            invoice_state = "invoiced"
        return total_invoiced, invoice_state

    def _magento_sync_order_lines(self, data):
        self.ensure_one()
        items = data.get("items") or []
        if not items:
            return
        if self.state not in ("draft", "sent") and hasattr(self, "action_unlock"):
            try:
                self.action_unlock()
            except Exception:
                pass
        self.order_line.unlink()
        line_vals = [(0, 0, self._magento_prepare_order_line_vals(item, self.magento_instance_id)) for item in items]
        if line_vals:
            self.write({"order_line": line_vals})

    def _magento_create_invoice_from_magento(self, invoice_data):
        self.ensure_one()
        invoice_id = invoice_data.get("entity_id") or invoice_data.get("id")
        if not invoice_id:
            return False

        existing_move = self.env["account.move"].search(
            [("magento_invoice_id", "=", str(invoice_id))], limit=1
        )
        if existing_move:
            # If the invoice was created by a cron/admin user, Sales users won't see it due to
            # Sale's record rule (invoice_user_id must be the current user or False).
            if (
                existing_move.invoice_user_id
                and existing_move.create_uid
                and existing_move.invoice_user_id.id == existing_move.create_uid.id
            ):
                existing_move.with_context(skip_magento_sync=True).write({"invoice_user_id": False})
            return existing_move

        candidates = self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("magento_sale_order_id", "=", self.id),
            ("magento_invoice_id", "=", False),
        ])
        if candidates:
            total = invoice_data.get("grand_total")
            if total is None:
                total = invoice_data.get("base_grand_total")
            try:
                total = float(total or 0.0)
            except (TypeError, ValueError):
                total = 0.0
            match = False
            if len(candidates) == 1:
                match = candidates[0]
            elif total:
                for candidate in candidates:
                    currency = candidate.currency_id or self.currency_id
                    if currency and currency.compare_amounts(candidate.amount_total, total) == 0:
                        match = candidate
                        break
            if match:
                match.magento_invoice_id = str(invoice_id)
                invoice_date = invoice_data.get("created_at")
                if invoice_date and not match.invoice_date:
                    invoice_date = fields.Datetime.to_datetime(invoice_date)
                    match.invoice_date = fields.Date.to_date(invoice_date) if invoice_date else False
                invoice_ref = invoice_data.get("increment_id") or ""
                if invoice_ref and not match.ref:
                    match.ref = invoice_ref
                if (
                    match.invoice_user_id
                    and match.create_uid
                    and match.invoice_user_id.id == match.create_uid.id
                ):
                    match.with_context(skip_magento_sync=True).write({"invoice_user_id": False})
                return match

        items = invoice_data.get("items") or []
        line_vals = []
        missing_items = []
        for item in items:
            order_item_id = item.get("order_item_id") or item.get("item_id")
            if not order_item_id:
                continue
            qty = float(item.get("qty") or item.get("qty_invoiced") or item.get("qty_ordered") or 0.0)
            if qty <= 0:
                continue
            order_line = self.order_line.filtered(
                lambda l: l.magento_item_id == str(order_item_id)
            )[:1]
            if order_line:
                line_vals.append((0, 0, order_line._prepare_invoice_line(quantity=qty)))
            else:
                missing_items.append(order_item_id)

        if not line_vals:
            raise UserError(
                _("No Magento invoice lines could be mapped to Odoo sale order lines. "
                  "Please sync the Magento order lines first.")
            )

        if missing_items:
            _logger.warning(
                "Magento invoice %s for order %s has unmapped items: %s",
                invoice_id,
                self.name,
                ",".join([str(x) for x in missing_items]),
            )

        invoice_date = invoice_data.get("created_at")
        if invoice_date:
            invoice_date = fields.Datetime.to_datetime(invoice_date)
            invoice_date = fields.Date.to_date(invoice_date) if invoice_date else False
        invoice_ref = invoice_data.get("increment_id") or ""

        vals = {
            "move_type": "out_invoice",
            "partner_id": (self.partner_invoice_id or self.partner_id).id,
            "invoice_origin": self.name,
            "invoice_date": invoice_date,
            # Sales app users are restricted by Sale's "Personal Invoices" record rule
            # (invoice_user_id = current user OR invoice_user_id is False). Magento sync
            # typically runs via cron/admin; keep it unassigned so sales users can see it.
            "invoice_user_id": False,
            "currency_id": self.currency_id.id,
            "company_id": self.company_id.id,
            "invoice_line_ids": line_vals,
            "ref": invoice_ref,
        }

        move = self.env["account.move"].with_context(
            default_move_type="out_invoice",
            skip_magento_sync=True,
        ).create(vals)
        move.magento_invoice_id = str(invoice_id)

        try:
            move.with_context(skip_magento_sync=True).action_post()
        except Exception as exc:
            _logger.warning(
                "Magento invoice %s imported as draft (post failed): %s",
                invoice_id,
                exc,
            )
        return move

    def _magento_sync_invoices(self, api=None):
        self.ensure_one()
        if not self.magento_order_id:
            return
        instance = self.magento_instance_id
        if not instance:
            return
        api = api or MagentoAPI(instance)
        try:
            invoices = api.get_invoices(order_id=self.magento_order_id)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        if not invoices:
            return
        for invoice in invoices:
            invoice_id = invoice.get("entity_id") or invoice.get("id")
            if not invoice_id:
                continue
            if self.env["account.move"].search(
                [("magento_invoice_id", "=", str(invoice_id))], limit=1
            ):
                continue
            try:
                invoice_data = api.get_invoice(invoice_id)
            except requests.exceptions.HTTPError:
                invoice_data = invoice
            self._magento_create_invoice_from_magento(invoice_data)

    def _magento_create_credit_memo_from_magento(self, credit_data):
        self.ensure_one()
        credit_id = credit_data.get("entity_id") or credit_data.get("id")
        if not credit_id:
            return False

        existing_move = self.env["account.move"].search(
            [("magento_credit_memo_id", "=", str(credit_id))], limit=1
        )
        if existing_move:
            if (
                existing_move.invoice_user_id
                and existing_move.create_uid
                and existing_move.invoice_user_id.id == existing_move.create_uid.id
            ):
                existing_move.with_context(skip_magento_sync=True).write({"invoice_user_id": False})
            # Backfill Magento totals (and keep them up to date) so amounts match Magento UI.
            self._magento_update_credit_memo_amounts(existing_move, credit_data)
            return existing_move

        items = credit_data.get("items") or []
        line_vals = []
        missing_items = []
        for item in items:
            order_item_id = item.get("order_item_id") or item.get("item_id")
            if not order_item_id:
                continue
            qty = float(item.get("qty") or item.get("qty_refunded") or item.get("qty_ordered") or 0.0)
            if qty <= 0:
                continue
            order_line = self.order_line.filtered(
                lambda l: l.magento_item_id == str(order_item_id)
            )[:1]
            if order_line:
                line_vals.append((0, 0, order_line._prepare_invoice_line(quantity=qty)))
            else:
                missing_items.append(order_item_id)

        if not line_vals:
            raise UserError(
                _("No Magento credit memo lines could be mapped to Odoo sale order lines. "
                  "Please sync the Magento order lines first.")
            )

        if missing_items:
            _logger.warning(
                "Magento credit memo %s for order %s has unmapped items: %s",
                credit_id,
                self.name,
                ",".join([str(x) for x in missing_items]),
            )

        credit_date = credit_data.get("created_at")
        if credit_date:
            credit_date = fields.Datetime.to_datetime(credit_date)
            credit_date = fields.Date.to_date(credit_date) if credit_date else False
        credit_ref = credit_data.get("increment_id") or ""

        vals = {
            "move_type": "out_refund",
            "partner_id": (self.partner_invoice_id or self.partner_id).id,
            "invoice_origin": self.name,
            "invoice_date": credit_date,
            # Keep it unassigned so sales users can see Magento credit notes regardless
            # of which user ran the sync job.
            "invoice_user_id": False,
            "currency_id": self.currency_id.id,
            "company_id": self.company_id.id,
            "invoice_line_ids": line_vals,
            "ref": credit_ref,
        }

        move = self.env["account.move"].with_context(
            default_move_type="out_refund",
            skip_magento_sync=True,
        ).create(vals)
        move.magento_credit_memo_id = str(credit_id)
        if credit_data.get("created_at"):
            move.magento_credit_memo_created_at = fields.Datetime.to_datetime(
                credit_data.get("created_at")
            )
        self._magento_update_credit_memo_amounts(move, credit_data)

        try:
            move.with_context(skip_magento_sync=True).action_post()
        except Exception as exc:
            _logger.warning(
                "Magento credit memo %s imported as draft (post failed): %s",
                credit_id,
                exc,
            )
        return move

    def _magento_update_credit_memo_amounts(self, move, credit_data):
        """Persist Magento credit memo totals on account.move for display consistency."""
        if not move or not credit_data:
            return

        def _to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        vals = {
            "magento_credit_memo_grand_total": _to_float(
                credit_data.get("grand_total") or credit_data.get("base_grand_total")
            ),
            "magento_credit_memo_tax_amount": _to_float(
                credit_data.get("tax_amount") or credit_data.get("base_tax_amount")
            ),
            "magento_credit_memo_subtotal": _to_float(
                credit_data.get("subtotal") or credit_data.get("base_subtotal")
            ),
        }
        move.with_context(skip_magento_sync=True).write(vals)

    def _magento_sync_credit_memos(self, api=None):
        self.ensure_one()
        if not self.magento_order_id:
            return
        instance = self.magento_instance_id
        if not instance:
            return
        api = api or MagentoAPI(instance)
        try:
            credit_memos = api.get_credit_memos(order_id=self.magento_order_id)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        if not credit_memos:
            return
        for memo in credit_memos:
            credit_id = memo.get("entity_id") or memo.get("id")
            if not credit_id:
                continue
            try:
                credit_data = api.get_credit_memo(credit_id)
            except requests.exceptions.HTTPError:
                credit_data = memo
            existing = self.env["account.move"].search(
                [("magento_credit_memo_id", "=", str(credit_id))], limit=1
            )
            if existing:
                # Update totals even for existing moves so UI matches Magento.
                self._magento_update_credit_memo_amounts(existing, credit_data)
                if (
                    existing.invoice_user_id
                    and existing.create_uid
                    and existing.invoice_user_id.id == existing.create_uid.id
                ):
                    existing.with_context(skip_magento_sync=True).write({"invoice_user_id": False})
                continue
            self._magento_create_credit_memo_from_magento(credit_data)

    def _magento_apply_payments_from_magento(self):
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance or not instance.auto_register_magento_payment:
            return
        if self.magento_payment_state not in ("partial", "paid"):
            return

        invoices = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        if not invoices:
            return

        total_paid = float(self.magento_total_paid or 0.0)
        if total_paid <= 0:
            return

        already_paid = sum(
            (inv.amount_total - inv.amount_residual) for inv in invoices
        )
        if self.currency_id and self.currency_id.compare_amounts(already_paid, total_paid) >= 0:
            return

        remaining = total_paid - already_paid
        journal, method = instance._get_magento_payment_config(self.company_id)
        if not journal or not method:
            self.message_post(
                body=_(
                    "Magento shows this order as paid, but no payment journal/method is configured "
                    "to register the payment in Odoo."
                )
            )
            return

        for invoice in invoices.sorted(key=lambda m: m.invoice_date or m.date or m.id):
            if remaining <= 0:
                break
            if invoice.amount_residual <= 0:
                continue
            amount = min(invoice.amount_residual, remaining)
            if amount <= 0:
                continue
            wizard = self.env["account.payment.register"].with_context(
                active_model="account.move",
                active_ids=invoice.ids,
                dont_redirect_to_payments=True,
                skip_magento_sync=True,
            ).create({
                "journal_id": journal.id,
                "payment_method_line_id": method.id,
                "amount": amount,
            })
            try:
                wizard.action_create_payments()
            except Exception as exc:
                self.message_post(
                    body=_("Failed to register Magento payment on invoice %s: %s") % (invoice.name, exc)
                )
                break
            remaining -= amount

    def _magento_get_shipment_item_quantities(self, shipment_data):
        item_qty = {}
        for item in shipment_data.get("items") or []:
            order_item_id = item.get("order_item_id") or item.get("item_id")
            if not order_item_id:
                continue
            qty = item.get("qty")
            if qty is None:
                qty = item.get("qty_shipped")
            try:
                qty = float(qty or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            key = str(order_item_id)
            item_qty[key] = item_qty.get(key, 0.0) + qty
        return item_qty

    def _magento_find_picking_for_shipment(self):
        self.ensure_one()
        pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )
        if not pickings:
            return False
        done_candidates = pickings.filtered(lambda p: p.state == "done" and not p.magento_shipment_id)
        if done_candidates:
            return done_candidates.sorted(key=lambda p: p.date_done or p.scheduled_date or p.id)[0]
        open_candidates = pickings.filtered(lambda p: p.state != "done" and not p.magento_shipment_id)
        if open_candidates:
            return open_candidates.sorted(key=lambda p: p.scheduled_date or p.date or p.id)[0]
        return False

    def _magento_apply_shipment_quantities(self, picking, shipment_data):
        self.ensure_one()
        item_qty = self._magento_get_shipment_item_quantities(shipment_data)
        if not item_qty:
            return False
        for move in picking.move_ids:
            if move.state == "cancel":
                continue
            sale_line = move.sale_line_id if "sale_line_id" in move._fields else False
            if not sale_line or not sale_line.magento_item_id:
                continue
            qty = item_qty.get(str(sale_line.magento_item_id))
            if qty is None:
                continue
            if sale_line.product_uom and move.product_uom and sale_line.product_uom != move.product_uom:
                qty = sale_line.product_uom._compute_quantity(
                    qty, move.product_uom, rounding_method="HALF-UP"
                )
            move.quantity = qty
            if qty:
                move.picked = True
        return True

    def _magento_apply_shipment(self, shipment_data):
        self.ensure_one()
        shipment_id = shipment_data.get("entity_id") or shipment_data.get("id")
        if not shipment_id:
            return False
        shipment_id = str(shipment_id)
        if self.env["stock.picking"].search(
            [("magento_shipment_id", "=", shipment_id)], limit=1
        ):
            return False

        picking = self._magento_find_picking_for_shipment()
        if not picking:
            _logger.warning(
                "No picking found to link Magento shipment %s for order %s.",
                shipment_id,
                self.name,
            )
            return False

        if picking.state != "done":
            applied = self._magento_apply_shipment_quantities(picking, shipment_data)
            if applied:
                try:
                    picking.with_context(
                        skip_magento_sync=True,
                        skip_sanity_check=True,
                    )._action_done()
                except Exception as exc:
                    _logger.warning(
                        "Failed to validate picking %s for Magento shipment %s: %s",
                        picking.name,
                        shipment_id,
                        exc,
                    )

        picking.write({
            "magento_shipment_id": shipment_id,
            "magento_shipment_increment_id": shipment_data.get("increment_id") or "",
        })
        return picking

    def _magento_sync_shipments(self, api=None):
        self.ensure_one()
        if not self.magento_order_id or not self.magento_instance_id:
            return
        api = api or MagentoAPI(self.magento_instance_id)
        try:
            shipments = api.get_shipments(order_id=self.magento_order_id)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        if not shipments:
            return

        for shipment in shipments:
            shipment_id = shipment.get("entity_id") or shipment.get("id")
            if not shipment_id:
                continue
            if self.env["stock.picking"].search(
                [("magento_shipment_id", "=", str(shipment_id))], limit=1
            ):
                continue
            try:
                shipment_data = api.get_shipment(shipment_id)
            except requests.exceptions.HTTPError:
                shipment_data = shipment
            self._magento_apply_shipment(shipment_data)

    def _magento_push_shipments(self):
        for order in self:
            for picking in order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.state == "done"
                and not p.magento_shipment_id
            ):
                try:
                    picking._magento_create_shipment()
                except UserError as exc:
                    order.message_post(
                        body=_("Magento shipment sync failed for picking %s: %s")
                        % (picking.name, exc)
                    )
                except Exception as exc:
                    _logger.exception(
                        "Magento shipment sync failed for picking %s: %s",
                        picking.name,
                        exc,
                    )

    # -------------------------
    # MAGENTO API HELPERS
    # -------------------------
    def _magento_http_error_message(self, exc):
        response = exc.response
        if response is None:
            return _("Magento API error: %s") % exc
        try:
            payload = response.json()
            message = payload.get("message") or response.text
        except Exception:
            message = response.text
        status = response.status_code
        return _("Magento API error (%s): %s") % (status, message)

    def _magento_raise_http_error(self, exc):
        raise UserError(self._magento_http_error_message(exc)) from exc

    def _magento_split_customer_name(self, partner):
        name = (partner.name or "").strip() if partner else ""
        if not name:
            return "", ""
        parts = name.split()
        firstname = parts[0]
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
        return firstname, lastname

    def _magento_split_shipping_method(self, code):
        if not code:
            return "", ""
        if "_" not in code:
            raise UserError(
                _("Default Shipping Method Code must be in 'carrier_method' format (e.g. flatrate_flatrate).")
            )
        carrier, method = code.split("_", 1)
        return carrier, method

    def _magento_extract_line_sku_hints(self, line):
        hints = []
        texts = [
            line.magento_sku or "",
            line.name or "",
            line.display_name or "",
            line.product_id.display_name if line.product_id else "",
        ]
        for text in texts:
            value = str(text or "").strip()
            if not value:
                continue
            if " - " in value:
                prefix = value.split(" - ", 1)[0].strip()
                if prefix and " " not in prefix:
                    hints.append(prefix)
            if "[" in value and "]" in value:
                left = value.find("[")
                right = value.find("]", left + 1)
                if right > left:
                    inside = value[left + 1:right].strip()
                    if inside and " " not in inside:
                        hints.append(inside)
        cleaned = []
        seen = set()
        for hint in hints:
            token = hint.lower()
            if token in seen:
                continue
            seen.add(token)
            cleaned.append(hint)
        return cleaned

    def _magento_resolve_line_product_map(self, line):
        map_model = self.env["magento.product.map"].sudo()
        instance = self.magento_instance_id or line.order_id.magento_instance_id
        instance_domain = [("instance_id", "=", instance.id)] if instance else []

        if line.magento_product_id:
            if not instance or line.magento_product_id.instance_id.id == instance.id:
                return line.magento_product_id

        if line.product_id:
            mapping = map_model.search(
                instance_domain + [("odoo_product_id", "=", line.product_id.id)],
                limit=1,
            )
            if mapping:
                return mapping

            for sku in [line.magento_sku, line.product_id.default_code, *self._magento_extract_line_sku_hints(line)]:
                value = str(sku or "").strip()
                if not value:
                    continue
                mapping = map_model.search(
                    instance_domain + [("sku", "=", value)],
                    limit=1,
                )
                if mapping:
                    return mapping

            template_maps = map_model.search(
                instance_domain + [("odoo_product_id.product_tmpl_id", "=", line.product_id.product_tmpl_id.id)]
            )
            if len(template_maps) == 1:
                return template_maps

        for sku in [line.magento_sku, line.product_id.default_code if line.product_id else "", *self._magento_extract_line_sku_hints(line)]:
            value = str(sku or "").strip()
            if not value:
                continue
            mapping = map_model.search(
                instance_domain + [("sku", "=", value)],
                limit=1,
            )
            if mapping:
                return mapping
        return False

    def _magento_get_line_sku_candidates(self, line):
        mapping = self._magento_resolve_line_product_map(line)
        if mapping:
            if not line.magento_product_id or line.magento_product_id.id != mapping.id:
                line.magento_product_id = mapping.id
            if mapping.sku and line.magento_sku != mapping.sku:
                line.magento_sku = mapping.sku

        candidates = [
            mapping.sku if mapping else "",
            line.magento_product_id.sku if line.magento_product_id else "",
            line.magento_sku or "",
            *self._magento_extract_line_sku_hints(line),
            line.product_id.default_code if line.product_id else "",
        ]
        cleaned = []
        seen = set()
        for sku in candidates:
            value = str(sku or "").strip()
            if not value:
                continue
            token = value.lower()
            if token in seen:
                continue
            seen.add(token)
            cleaned.append(value)
        return cleaned

    def _magento_build_cart_address(self, partner, api=None, include_email=False):
        if not partner:
            raise UserError(_("Customer is required to create a Magento order."))
        firstname, lastname = self._magento_split_customer_name(partner)
        if not firstname:
            firstname = partner.name or ""
        if not lastname:
            lastname = "Customer"
        missing = []
        if not partner.street:
            missing.append("street")
        if not partner.city:
            missing.append("city")
        if not partner.zip:
            missing.append("zip")
        if not partner.country_id:
            missing.append("country")
        if not partner.phone:
            missing.append("phone")
        if include_email and not partner.email and not self.magento_customer_email:
            missing.append("email")
        if missing:
            raise UserError(
                _("Customer address incomplete. Required: %s") % ", ".join(missing)
            )
        street = [partner.street]
        if partner.street2:
            street.append(partner.street2)
        country_code = partner.country_id.code or ""
        address = {
            "firstname": firstname or partner.name or "",
            "lastname": lastname or "",
            "street": street,
            "city": partner.city or "",
            "postcode": partner.zip or "",
            "country_id": country_code,
            "telephone": partner.phone or "",
        }
        if include_email:
            address["email"] = partner.email or self.magento_customer_email or ""
        if partner.state_id:
            address["region"] = partner.state_id.name or ""
            address["region_code"] = partner.state_id.code or ""
        if api and country_code:
            try:
                country_data = api.get_country(country_code) or {}
            except requests.exceptions.HTTPError:
                country_data = {}
            regions = country_data.get("available_regions") or []
            if regions and not partner.state_id:
                raise UserError(
                    _("State/Region is required for country %s.") % country_code
                )
            if regions and partner.state_id:
                state_code = (partner.state_id.code or "").upper()
                state_name = (partner.state_id.name or "").lower()
                region_id = None
                for region in regions:
                    code = (region.get("code") or "").upper()
                    name = (region.get("name") or "").lower()
                    if state_code and code == state_code:
                        region_id = region.get("id")
                        break
                    if state_name and name == state_name:
                        region_id = region.get("id")
                        break
                if region_id is not None:
                    address["region_id"] = region_id
        if partner.company_name:
            address["company"] = partner.company_name
        return address

    def _magento_resolve_order_instance(self):
        self.ensure_one()
        if self.magento_instance_id:
            return self.magento_instance_id

        map_model = self.env["magento.product.map"].sudo()
        resolved_instance_ids = set()

        for line in self.order_line:
            if line.magento_product_id and line.magento_product_id.instance_id:
                resolved_instance_ids.add(line.magento_product_id.instance_id.id)
                continue

            if line.product_id:
                maps = map_model.search([("odoo_product_id", "=", line.product_id.id)])
                resolved_instance_ids.update(maps.mapped("instance_id").ids)

            for sku in [line.magento_sku, line.product_id.default_code if line.product_id else ""]:
                value = str(sku or "").strip()
                if not value:
                    continue
                maps = map_model.search([("sku", "=", value)])
                resolved_instance_ids.update(maps.mapped("instance_id").ids)

        if len(resolved_instance_ids) == 1:
            instance = self.env["magento.instance"].browse(next(iter(resolved_instance_ids)))
            self.magento_instance_id = instance.id
            return instance

        if len(resolved_instance_ids) > 1:
            raise UserError(
                _(
                    "Multiple Magento instances match the order lines. "
                    "Please set Magento Instance on the order before creating in Magento."
                )
            )

        active_instances = self.env["magento.instance"].search([("active", "=", True)])
        if len(active_instances) == 1:
            self.magento_instance_id = active_instances.id
            return active_instances
        return False

    def _magento_is_not_saleable_error(self, exc):
        status = exc.response.status_code if exc.response is not None else 0
        if status != 400:
            return False
        message = (self._magento_http_error_message(exc) or "").lower()
        saleable_markers = (
            "not available",
            "not saleable",
            "not salable",
            "out of stock",
            "requested qty is not available",
            "requested quantity is not available",
        )
        return any(marker in message for marker in saleable_markers)

    def _magento_should_try_next_sku(self, exc):
        status = exc.response.status_code if exc.response is not None else 0
        message = (self._magento_http_error_message(exc) or "").lower()
        if self._magento_is_not_saleable_error(exc):
            return False
        retry_markers = (
            "doesn't exist",
            "does not exist",
            "requested doesn't exist",
            "no such entity",
            "product that was requested doesn't exist",
        )
        if status == 404:
            return True
        if status == 400 and any(marker in message for marker in retry_markers):
            return True
        return False

    def _magento_create_order_via_cart(self, api):
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required."))
        payment_method = instance.default_payment_method or ""
        if not payment_method:
            raise UserError(
                _("Set a Default Payment Method Code on the Magento Instance before creating orders.")
            )
        shipping_method = instance.default_shipping_method or ""
        if not shipping_method:
            raise UserError(
                _("Set a Default Shipping Method Code on the Magento Instance before creating orders.")
            )
        carrier_code, method_code = self._magento_split_shipping_method(shipping_method)
        if not carrier_code or not method_code:
            raise UserError(
                _("Default Shipping Method Code must be in 'carrier_method' format (e.g. flatrate_flatrate).")
            )

        partner = self.partner_id
        if not partner:
            raise UserError(_("Customer is required to create a Magento order."))
        email = partner.email or self.magento_customer_email or ""
        if not email:
            raise UserError(_("Customer email is required to create a Magento order."))

        cart_id = api.create_guest_cart()
        for line in self.order_line:
            sku_candidates = self._magento_get_line_sku_candidates(line)
            if not sku_candidates:
                raise UserError(_("Each order line must have a SKU to create a Magento order."))
            qty = float(line.product_uom_qty or 0.0)
            if qty <= 0:
                raise UserError(_("Each order line must have a quantity greater than 0."))
            add_error = None
            availability_error = None
            added = False
            attempted_skus = []
            saleable_autofix_attempts = set()
            idx = 0
            while idx < len(sku_candidates):
                sku = sku_candidates[idx]
                idx += 1
                attempted_skus.append(sku)
                try:
                    api.add_guest_cart_item(cart_id, sku, qty)
                    if line.magento_sku != sku:
                        line.magento_sku = sku
                    if line.magento_product_id and line.magento_product_id.sku != sku:
                        line.magento_product_id.with_context(
                            skip_magento_sync=True,
                            skip_odoo_sync=True,
                        ).write({"sku": sku})
                    added = True
                    break
                except requests.exceptions.HTTPError as exc:
                    add_error = exc
                    if self._magento_is_not_saleable_error(exc):
                        sku_token = str(sku or "").strip().lower()
                        if sku_token and sku_token not in saleable_autofix_attempts:
                            saleable_autofix_attempts.add(sku_token)
                            try:
                                if api.make_product_saleable_for_cart(sku, qty=qty):
                                    _logger.info(
                                        "Magento saleable auto-fix applied for SKU '%s' (order=%s). Retrying line add.",
                                        sku,
                                        self.name,
                                    )
                                    idx -= 1
                                    continue
                            except requests.exceptions.HTTPError:
                                pass
                        availability_error = exc
                        break
                    if self._magento_should_try_next_sku(exc):
                        status = exc.response.status_code if exc.response is not None else 0
                        # If SKU was renamed in Magento but mapping keeps old SKU,
                        # fetch product by Magento ID and retry with fresh SKU.
                        if (
                            status == 404
                            and line.magento_product_id
                            and line.magento_product_id.magento_id
                        ):
                            try:
                                remote_product = api.get_product_by_id(line.magento_product_id.magento_id)
                            except requests.exceptions.HTTPError:
                                remote_product = {}
                            fresh_sku = str((remote_product or {}).get("sku") or "").strip()
                            if fresh_sku:
                                existing_tokens = {str(s or "").strip().lower() for s in sku_candidates}
                                if fresh_sku.lower() not in existing_tokens:
                                    sku_candidates.append(fresh_sku)
                        continue
                    break
            if added:
                continue
            final_error = availability_error or add_error
            if final_error:
                error_message = self._magento_http_error_message(final_error)
                if availability_error:
                    raise UserError(
                        _(
                            "Magento product is not saleable for order line '%(line)s'. "
                            "Tried SKU(s): %(skus)s. %(error)s "
                            "Check product status, website assignment, and salable stock in Magento."
                        )
                        % {
                            "line": line.display_name or line.name or _("(no name)"),
                            "skus": ", ".join(attempted_skus or sku_candidates),
                            "error": error_message,
                        }
                    )
                if self._magento_should_try_next_sku(final_error):
                    raise UserError(
                        _(
                            "Magento product is not available for order line '%(line)s'. "
                            "Tried SKU(s): %(skus)s. %(error)s"
                        )
                        % {
                            "line": line.display_name or line.name or _("(no name)"),
                            "skus": ", ".join(attempted_skus or sku_candidates),
                            "error": error_message,
                        }
                    )
                self._magento_raise_http_error(final_error)

        shipping_partner = self.partner_shipping_id or partner
        billing_partner = self.partner_invoice_id or partner
        shipping_address = self._magento_build_cart_address(
            shipping_partner, api=api, include_email=True
        )
        billing_address = self._magento_build_cart_address(
            billing_partner, api=api, include_email=True
        )
        ship_payload = {
            "addressInformation": {
                "shipping_address": shipping_address,
                "billing_address": billing_address,
                "shipping_method_code": method_code,
                "shipping_carrier_code": carrier_code,
            }
        }
        api.set_guest_shipping_information(cart_id, ship_payload)

        pay_payload = {
            "email": email,
            "paymentMethod": {
                "method": payment_method,
            },
            "billing_address": billing_address,
        }
        order_id = api.set_guest_payment_information(cart_id, pay_payload)
        return api.get_order(order_id)

    def _magento_build_update_payload(self):
        self.ensure_one()
        customer_email = self.magento_customer_email or (self.partner_id.email if self.partner_id else "") or ""
        payload = {}
        if self.magento_status:
            payload["status"] = self.magento_status
        if customer_email:
            payload["customer_email"] = customer_email
        if self.magento_currency:
            payload["order_currency_code"] = self.magento_currency
        return {"entity": payload} if payload else None

    def _magento_drop_unsupported_field(self, payload, field_name):
        entity = (payload or {}).get("entity") or {}
        if not entity or not field_name:
            return False
        target = field_name.replace("_", "").lower()
        for key in list(entity.keys()):
            if key.replace("_", "").lower() == target:
                entity.pop(key, None)
                return True
        return False

    def _magento_update_order(self, api, payload):
        self.ensure_one()
        if not payload or not payload.get("entity"):
            return None
        attempts = 0
        fallback_fields = ["customer_email", "order_currency_code", "status"]
        while attempts < 3:
            attempts += 1
            try:
                return api.update_order(self.magento_order_id, payload)
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code == 400:
                    message = ""
                    params = {}
                    try:
                        data = response.json()
                        message = data.get("message") or response.text
                        params = data.get("parameters") or {}
                    except Exception:
                        message = response.text
                    if "is not supported" in (message or ""):
                        field_name = params.get("fieldName") if isinstance(params, dict) else ""
                        dropped = False
                        if field_name:
                            dropped = self._magento_drop_unsupported_field(payload, field_name or "")
                        if not dropped:
                            for key in list(fallback_fields):
                                if self._magento_drop_unsupported_field(payload, key):
                                    fallback_fields.remove(key)
                                    dropped = True
                                    break
                        if dropped and payload.get("entity"):
                            _logger.warning(
                                "Magento order update retry without unsupported field '%s' (id=%s).",
                                field_name or "unknown",
                                self.magento_order_id,
                            )
                            continue
                        _logger.warning(
                            "Magento order update skipped due to unsupported field '%s' (id=%s).",
                            field_name or "unknown",
                            self.magento_order_id,
                        )
                        return None
                self._magento_raise_http_error(exc)
        return None

    def _magento_refresh_from_magento(self, api, magento_id):
        self.ensure_one()
        if not magento_id:
            return
        try:
            data = api.get_order(magento_id)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        values = self._magento_prepare_order_vals(data, self.magento_instance_id)
        self.write(values)
        self._magento_sync_order_lines(data)

    def _magento_rainbow_man_action(self, message):
        return {
            "type": "ir.actions.act_window_close",
            "effect": {
                "fadeout": "slow",
                "message": message,
                "type": "rainbow_man",
            }
        }

    # -------------------------
    # BUTTON ACTIONS
    # -------------------------
    def action_create_magento_order(self):
        self.ensure_one()
        instance = self._magento_resolve_order_instance()
        if not instance:
            raise UserError(
                _(
                    "Please configure Magento Instance on the order, "
                    "or keep only one active Magento instance."
                )
            )
        if not self.magento_instance_id:
            self.magento_instance_id = instance.id
        api = MagentoAPI(instance)
        try:
            response = self._magento_create_order_via_cart(api)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        if isinstance(response, dict):
            values = self._magento_prepare_order_vals(response, instance)
            self.write(values)
            self._magento_sync_order_lines(response)
            if not response.get("increment_id") or not response.get("items"):
                self._magento_refresh_from_magento(api, values.get("magento_order_id"))
        return self._magento_rainbow_man_action(_("Order created in Magento."))

    def action_update_magento_order(self):
        self.ensure_one()
        if not self.magento_order_id:
            raise UserError(_("Magento Order ID is required to update an order in Magento."))
        instance = self.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required."))
        api = MagentoAPI(instance)
        payload = self._magento_build_update_payload()
        if not payload:
            return self._magento_rainbow_man_action(_("No order changes to update in Magento."))
        try:
            response = self._magento_update_order(api, payload)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        if isinstance(response, dict):
            values = self._magento_prepare_order_vals(response, instance)
            self.write(values)
            if not response.get("increment_id") or not response.get("items"):
                self._magento_refresh_from_magento(api, self.magento_order_id)
        return self._magento_rainbow_man_action(_("Order updated in Magento."))

    def action_pull_magento_order(self):
        self.ensure_one()
        if not self.magento_order_id:
            raise UserError(_("Magento Order ID is required to pull order from Magento."))
        instance = self.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required."))
        api = MagentoAPI(instance)
        try:
            data = api.get_order(self.magento_order_id)
        except requests.exceptions.HTTPError as exc:
            self._magento_raise_http_error(exc)
        values = self._magento_prepare_order_vals(data, instance)
        if values.get("magento_invoice_state") == "not_invoiced":
            try:
                invoices = api.get_invoices(order_id=self.magento_order_id)
            except requests.exceptions.HTTPError:
                invoices = []
            if invoices:
                total_invoiced, invoice_state = self._magento_compute_invoice_summary(
                    data, invoices
                )
                if invoice_state != "not_invoiced":
                    values["magento_total_invoiced"] = total_invoiced
                    values["magento_invoice_state"] = invoice_state
        self.write(values)
        self._magento_sync_order_lines(data)
        if self.magento_invoice_state in ("partial", "invoiced"):
            self._magento_sync_invoices(api)
        if self.magento_payment_state in ("partial", "paid"):
            self._magento_apply_payments_from_magento()
        self._magento_sync_credit_memos(api)
        self._magento_sync_shipments(api)
        return self._magento_rainbow_man_action(_("Order pulled from Magento."))


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    magento_item_id = fields.Char("Magento Item ID", readonly=True, copy=False)
    magento_product_id = fields.Many2one(
        "magento.product.map",
        string="Magento Product",
        ondelete="set null",
    )
    magento_sku = fields.Char("Magento SKU")

    @api.onchange("magento_product_id")
    def _onchange_magento_product_id(self):
        for line in self:
            magento_ctx = bool(
                line.env.context.get("from_magento_order_menu")
                or line.env.context.get("default_magento_instance_id")
            )
            product_map = line.magento_product_id
            if not product_map:
                continue
            line.magento_sku = product_map.sku or ""
            line.product_id = product_map.odoo_product_id.id if product_map.odoo_product_id else False
            line.name = product_map.name or product_map.sku or ""
            if line.order_id and (line.order_id.is_magento_order or line.order_id.magento_instance_id or magento_ctx):
                if line.order_id and not line.order_id.magento_instance_id and product_map.instance_id:
                    line.order_id.magento_instance_id = product_map.instance_id
                line.price_unit = product_map.price or (line.product_id.lst_price if line.product_id else 0.0)
            if not line.product_uom_qty:
                line.product_uom_qty = 1.0

    @api.onchange("product_id")
    def _onchange_product_id(self):
        res = super()._onchange_product_id()
        for line in self:
            if not line.product_id:
                continue
            magento_ctx = bool(
                line.env.context.get("from_magento_order_menu")
                or line.env.context.get("default_magento_instance_id")
            )
            if not line.product_uom_qty:
                line.product_uom_qty = 1.0
            if not line.magento_sku:
                line.magento_sku = line.product_id.default_code or ""
            product_map = line.magento_product_id
            if not product_map:
                domain = [("odoo_product_id", "=", line.product_id.id)]
                if line.order_id and line.order_id.magento_instance_id:
                    domain.append(("instance_id", "=", line.order_id.magento_instance_id.id))
                product_map = self.env["magento.product.map"].search(domain, limit=1)
                if product_map:
                    line.magento_product_id = product_map.id
            if product_map and product_map.sku:
                line.magento_sku = product_map.sku
            if line.order_id and (line.order_id.is_magento_order or line.order_id.magento_instance_id or magento_ctx):
                if line.order_id and not line.order_id.magento_instance_id and product_map and product_map.instance_id:
                    line.order_id.magento_instance_id = product_map.instance_id
                if product_map and product_map.price:
                    line.price_unit = product_map.price
                elif not line.price_unit:
                    line.price_unit = line.product_id.lst_price or 0.0
        return res
