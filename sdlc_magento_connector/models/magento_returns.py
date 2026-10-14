"""Full returns lifecycle (roadmap 3.2).

Odoo -> Magento (credit note posted, see ``account_move_inherit``)
    The Magento credit memo carries the refunded items, the shipping refund, the
    positive / negative adjustment computed from the credit note lines and, when the
    credit note follows an Odoo return (incoming picking of the order), the
    ``return_to_stock_items``. It is posted offline (``/V1/order/{id}/refund``) or online
    through the gateway (``/V1/invoice/{id}/refund``) according to the instance and the
    payment method mapping.

Magento -> Odoo (credit memo import)
    * Adjustments become lines on the instance "Refund adjustment" / "Return fee"
      products (and accounts when set).
    * Restock vs refund only: when the Magento credit memo flags items as returned to
      stock (``extension_attributes.return_to_stock_items`` or item ``back_to_stock``;
      Magento itself does not keep the flag on the stored credit memo, so the instance
      policy can also say "always") a receipt from the customer is created and validated
      for those quantities. Its destination is the first matching return location rule
      (warehouse / product category / reason), else the warehouse stock location.
      Validating it updates the quants, which marks the products for the stock push.
      Quantities not delivered in Odoo yet wait for the delivery.
    * The reason comes from the credit memo comment (matched on the reason codes/names).

Chatter notes are posted on the order, the credit note and the return picking.
"""
import json
import logging
import re
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero

from ..services.magento_api import MagentoAPI
from .account_move_inherit import REFUND_MODES
from .magento_order_mapping import PUSH_SKIP_CTX
from .sql_compat import Constraint

_logger = logging.getLogger(__name__)

# kind -> (instance product field, instance account field, product name, default_code)
RETURN_PRODUCTS = {
    "refund_adjustment": ("refund_adjustment_product_id", "refund_adjustment_account_id",
                          "Magento Refund Adjustment", "MAGENTO-REFUND-ADJ"),
    "return_fee": ("return_fee_product_id", "return_fee_account_id",
                   "Magento Return Fee", "MAGENTO-RETURN-FEE"),
}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _create_invoice_refund(api, order_id, payload):
    """POST /V1/invoice/{invoiceId}/refund (online refund through the gateway).

    Same call shape as ``create_credit_memo`` (``order_id``, ``payload``) so the
    idempotent ``_magento_push_document`` can use it; the Magento invoice id travels in
    the private ``_magento_invoice_id`` payload key, removed before sending.
    """
    body = dict(payload)
    invoice_id = body.pop("_magento_invoice_id", None)
    if not invoice_id:
        raise UserError(_("An online refund needs the Magento invoice id."))
    return api._post(f"/rest/V1/invoice/{invoice_id}/refund", body)


if not hasattr(MagentoAPI, "create_invoice_refund"):
    MagentoAPI.create_invoice_refund = _create_invoice_refund


class MagentoReturnReason(models.Model):
    _name = "magento.return.reason"
    _description = "Magento Return Reason"
    _order = "sequence, name, id"

    sequence = fields.Integer(default=10)
    code = fields.Char(required=True, help="Short code, also matched in Magento credit memo comments.")
    name = fields.Char(required=True, translate=True)
    instance_id = fields.Many2one(
        "magento.instance", string="Instance", index=True, ondelete="cascade",
        help="Empty: the reason is available for every instance.",
    )
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    restock = fields.Boolean(
        string="Return to Stock", default=True,
        help="Off: a Magento credit memo with this reason never creates a return receipt "
             "(e.g. damaged goods that are written off).",
    )
    active = fields.Boolean(default=True)

    _magento_return_reason_code_uniq = Constraint(
        "unique(instance_id, code)",
        "This return reason code already exists for this instance.",
    )

    @api.depends("code", "name")
    def _compute_display_name(self):
        for reason in self:
            if reason.code and reason.name and reason.code != reason.name:
                reason.display_name = "[%s] %s" % (reason.code, reason.name)
            else:
                reason.display_name = reason.name or reason.code or ""

    @api.model
    def _magento_reasons_for(self, instance):
        return self.sudo().search([
            "|", ("instance_id", "=", False), ("instance_id", "=", instance.id),
        ])

    @api.model
    def _magento_match_text(self, instance, text):
        """First reason (instance-specific first, then by sequence) whose code or name
        appears as a whole word/phrase in ``text`` (a Magento credit memo comment)."""
        text = (text or "").strip()
        if not text or not instance:
            return self.browse()
        reasons = self._magento_reasons_for(instance).sorted(
            lambda r: (0 if r.instance_id else 1, r.sequence, r.id))
        lowered = text.lower()
        for reason in reasons:
            for needle in (reason.code, reason.name):
                needle = (needle or "").strip().lower()
                if needle and re.search(r"(?<!\w)%s(?!\w)" % re.escape(needle), lowered):
                    return reason.with_env(self.env)
        return self.browse()


class MagentoReturnLocationRule(models.Model):
    _name = "magento.return.location.rule"
    _description = "Magento Return Location Rule"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Warehouse", ondelete="cascade",
        domain="[('company_id', '=', company_id)]",
        help="Empty: any warehouse. The warehouse of the sale order.",
    )
    categ_id = fields.Many2one(
        "product.category", string="Product Category", ondelete="cascade",
        help="Empty: any product. Sub-categories match too.",
    )
    reason_id = fields.Many2one(
        "magento.return.reason", string="Reason", ondelete="cascade",
        help="Empty: any reason (or none).",
    )
    location_id = fields.Many2one(
        "stock.location", string="Return Location", required=True, ondelete="restrict",
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
    )

    @api.constrains("location_id")
    def _check_location(self):
        for rule in self:
            if rule.location_id.usage != "internal":
                raise ValidationError(_("A return location must be an internal location."))

    @api.depends("warehouse_id", "categ_id", "reason_id", "location_id")
    def _compute_display_name(self):
        for rule in self:
            conditions = [
                rec.display_name for rec in (rule.warehouse_id, rule.categ_id, rule.reason_id) if rec
            ]
            rule.display_name = "%s → %s" % (
                " & ".join(conditions) or _("Any return"), rule.location_id.display_name or "?")

    def _magento_matches(self, warehouse, product, reason):
        self.ensure_one()
        if self.warehouse_id and self.warehouse_id != warehouse:
            return False
        if self.reason_id and self.reason_id != reason:
            return False
        if self.categ_id:
            categ = product.categ_id
            if not categ or not (categ == self.categ_id or self.categ_id.parent_path
                                 and (categ.parent_path or "").startswith(self.categ_id.parent_path)):
                return False
        return True


class MagentoPaymentMap(models.Model):
    _inherit = "magento.payment.map"

    refund_mode = fields.Selection(
        [("instance", "Instance default"), ("online", "Online"), ("offline", "Offline")],
        string="Refunds", default="instance", required=True,
        help="How Odoo credit notes of orders paid with this method are sent to Magento. "
             "Instance default: online when the instance option is on and the method can "
             "refund online (not check/money order, bank transfer, cash on delivery...).",
    )


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    magento_refund_online = fields.Boolean(
        string="Refund Online When Possible",
        help="Refund online when the payment method supports it: an Odoo credit note is sent "
             "to /V1/invoice/{id}/refund (gateway refund) when the order's payment method can "
             "refund online and the Magento invoice is known. Otherwise, and when off, the "
             "offline refund /V1/order/{id}/refund is used.",
    )
    magento_refund_notify = fields.Boolean(
        string="Email Credit Memo to Customer",
        help="Ask Magento to email the credit memo created from an Odoo credit note.",
    )
    magento_import_restock = fields.Selection(
        [("flagged", "When Magento flags the items"), ("always", "Always"), ("never", "Never")],
        string="Restock Imported Credit Memos", default="flagged", required=True,
        help="When an imported Magento credit memo creates a return receipt in Odoo.\n"
             "When Magento flags the items: only for return_to_stock_items / back_to_stock "
             "items (sent by webhooks and the companion module; Magento does not keep the flag "
             "on the stored credit memo).\n"
             "Always: every refunded item came back (mirror of Magento's \"Automatically Return "
             "Credit Memo Item to Stock\").\nNever: refunds only.",
    )
    refund_adjustment_product_id = fields.Many2one(
        "product.product", string="Refund Adjustment Product", domain="[('type', '=', 'service')]",
        help="Line of an extra refund (Magento adjustment_positive). Created on first use.",
    )
    refund_adjustment_account_id = fields.Many2one(
        "account.account", string="Refund Adjustment Account",
        help="Empty: the product's income account.",
    )
    return_fee_product_id = fields.Many2one(
        "product.product", string="Return Fee Product", domain="[('type', '=', 'service')]",
        help="Line of a return fee kept by the merchant (Magento adjustment_negative). "
             "Created on first use.",
    )
    return_fee_account_id = fields.Many2one(
        "account.account", string="Return Fee Account",
        help="Empty: the product's income account.",
    )
    return_location_rule_ids = fields.One2many(
        "magento.return.location.rule", "instance_id", string="Return Locations",
        context={"active_test": False},
    )
    return_reason_ids = fields.One2many(
        "magento.return.reason", "instance_id", string="Return Reasons",
    )

    def _magento_return_product(self, kind):
        """Service product of a refund adjustment / return fee line (created on first use)."""
        self.ensure_one()
        field_name, _account_field, name, code = RETURN_PRODUCTS[kind]
        product = self.sudo()[field_name]
        if product:
            return product.with_env(self.env)
        Product = self.env["product.product"].sudo().with_context(**PUSH_SKIP_CTX)
        product = Product.with_context(active_test=False).search([
            ("default_code", "=", code),
            ("company_id", "in", (False, self.company_id.id)),
        ], limit=1)
        if not product:
            product = Product.create({
                "name": name,
                "default_code": code,
                "detailed_type": "service",
                "sale_ok": True,
                "purchase_ok": False,
                "list_price": 0.0,
                "taxes_id": [Command.clear()],
                "invoice_policy": "order",
            })
        self.sudo().write({field_name: product.id})
        return product.with_env(self.env)

    def _magento_return_line_vals(self, kind, amount, label=None):
        """Credit note line of a Magento adjustment (``amount`` > 0 refunds more)."""
        self.ensure_one()
        _field, account_field, _name, _code = RETURN_PRODUCTS[kind]
        product = self._magento_return_product(kind)
        vals = {
            "product_id": product.id,
            "name": label or (_("Refund adjustment") if kind == "refund_adjustment" else _("Return fee")),
            "quantity": 1.0,
            "price_unit": amount,
            "tax_ids": [Command.clear()],
        }
        account = self.sudo()[account_field]
        if account:
            vals["account_id"] = account.id
        return vals

    def _magento_return_location(self, warehouse, product, reason=None):
        """Destination of returned goods: first matching rule, else the warehouse stock."""
        self.ensure_one()
        reason = reason or self.env["magento.return.reason"]
        rules = self.env["magento.return.location.rule"].sudo().search([("instance_id", "=", self.id)])
        for rule in rules:
            if rule._magento_matches(warehouse, product, reason):
                return rule.location_id
        if warehouse and warehouse.lot_stock_id:
            return warehouse.lot_stock_id
        warehouse = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.company_id.id)], limit=1)
        return warehouse.lot_stock_id

    def _magento_order_followup_steps(self, api, order):
        steps = super()._magento_order_followup_steps(api, order)
        # Restock quantities of imported credit memos that waited for the delivery
        # (the workflow validates deliveries after the credit memo sync).
        steps.append(("returns: restock", lambda: order._magento_process_pending_returns()))
        return steps


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _magento_document_kinds(self):
        kinds = super()._magento_document_kinds()
        online = dict(kinds["credit_memo"])
        online.update(label=_("online credit memo"), create="create_invoice_refund")
        kinds["credit_memo_online"] = online
        return kinds

    # ------------------------------------------------------------------
    # Magento -> Odoo
    # ------------------------------------------------------------------
    def _magento_document_line_vals(self, doc, kind):
        """Credit memo adjustments go to the instance refund adjustment / return fee lines."""
        if kind != "credit_memo":
            return super()._magento_document_line_vals(doc, kind)
        self.ensure_one()
        positive = _to_float(doc.get("adjustment_positive"))
        negative = _to_float(doc.get("adjustment_negative"))
        stripped = dict(doc, adjustment_positive=0, adjustment_negative=0)
        result, missing = super()._magento_document_line_vals(stripped, kind)
        currency = self.currency_id or self.env.company.currency_id
        instance = self.magento_instance_id
        if not currency.is_zero(positive):
            result.append(instance._magento_return_line_vals("refund_adjustment", abs(positive)))
        if not currency.is_zero(negative):
            result.append(instance._magento_return_line_vals("return_fee", -abs(negative)))
        return result, missing

    def _magento_create_credit_memo_from_magento(self, credit_data):
        self.ensure_one()
        credit_id = credit_data.get("entity_id") or credit_data.get("id")
        existed = bool(credit_id) and bool(self._magento_find_move("magento_credit_memo_id", credit_id))
        move = super()._magento_create_credit_memo_from_magento(credit_data)
        if move and not existed:
            self._magento_after_credit_memo_import(move, credit_data)
        return move

    @api.model
    def _magento_credit_memo_comment(self, credit_data):
        texts = []
        for comment in credit_data.get("comments") or []:
            if isinstance(comment, dict) and comment.get("comment"):
                texts.append(str(comment["comment"]))
        if credit_data.get("customer_note"):
            texts.append(str(credit_data["customer_note"]))
        return "\n".join(texts)

    def _magento_restock_request(self, credit_data, reason=None):
        """{sale line id: qty} to return to stock for an imported Magento credit memo."""
        self.ensure_one()
        instance = self.magento_instance_id
        policy = instance.magento_import_restock or "flagged"
        if policy == "never" or (reason and not reason.restock):
            return {}
        items = [i for i in credit_data.get("items") or [] if isinstance(i, dict)]
        extension = credit_data.get("extension_attributes") or {}
        flagged = {
            str(item_id)
            for item_id in (extension.get("return_to_stock_items")
                            or credit_data.get("return_to_stock_items") or [])
        }
        for item in items:
            item_ext = item.get("extension_attributes") or {}
            if item.get("back_to_stock") or item_ext.get("back_to_stock"):
                flagged.add(str(item.get("order_item_id") or item.get("item_id") or ""))
        flagged.discard("")
        if flagged:
            selected = [i for i in items if str(i.get("order_item_id") or i.get("item_id")) in flagged]
        elif policy == "always":
            selected = items
        else:
            return {}
        line_qty, _missing = self._magento_map_document_items(selected, ("qty", "qty_refunded"))
        lines = self.env["sale.order.line"].browse(list(line_qty))
        return {
            line.id: line_qty[line.id]
            for line in lines
            if line.product_id.type in ("consu", "product") and line_qty[line.id] > 0
        }

    def _magento_after_credit_memo_import(self, move, credit_data):
        """Reason, refund mode, restock request and chatter of a newly imported credit memo."""
        self.ensure_one()
        instance = self.magento_instance_id
        comment = self._magento_credit_memo_comment(credit_data)
        reason = self.env["magento.return.reason"]._magento_match_text(instance, comment)
        mode = "online" if credit_data.get("transaction_id") else "offline"
        restock = self._magento_restock_request(credit_data, reason)
        move.with_context(skip_magento_sync=True).write({
            "magento_refund_mode": mode,
            "magento_return_reason_id": reason.id or False,
            "magento_restock": bool(restock),
            "magento_restock_pending": {str(k): v for k, v in restock.items()} or False,
            "magento_restock_waiting": bool(restock),
        })
        memo = credit_data.get("increment_id") or credit_data.get("entity_id")
        details = [
            _("Refund: %s", dict(REFUND_MODES)[mode]),
            _("Restock: %s", _("yes") if restock else _("no (refund only)")),
        ]
        if reason:
            details.append(_("Reason: %s", reason.display_name))
        elif comment:
            details.append(_("Comment: %s", comment))
        positive = _to_float(credit_data.get("adjustment_positive"))
        negative = _to_float(credit_data.get("adjustment_negative"))
        if positive:
            details.append(_("Adjustment refund: %s", positive))
        if negative:
            details.append(_("Return fee: %s", negative))
        body = Markup("%s<br/>%s") % (
            _("Magento credit memo %s imported.", memo),
            Markup("<br/>").join(escape(d) for d in details),
        )
        move.message_post(body=body)
        self.message_post(body=Markup("%s %s<br/>%s") % (
            _("Magento credit memo %s imported as", memo), move._get_html_link(),
            Markup("<br/>").join(escape(d) for d in details),
        ))
        if restock:
            self._magento_process_pending_returns(moves=move)

    def _magento_process_pending_returns(self, moves=None):
        """Create (and validate) the return receipts waiting on imported credit notes.

        A credit note waits until Odoo delivered the goods: what was not delivered cannot
        come back. Quantities are capped to the delivered quantity of each line.
        """
        for order in self:
            todo = moves if moves is not None else self.env["account.move"].search([
                ("magento_sale_order_id", "=", order.id),
                ("move_type", "=", "out_refund"),
                ("magento_restock_waiting", "=", True),
            ])
            for move in todo.filtered(lambda m: m.magento_restock_waiting and m.magento_restock_pending):
                order._magento_restock_credit_note(move)

    def _magento_restock_credit_note(self, move):
        self.ensure_one()
        pending = move.magento_restock_pending or {}
        if isinstance(pending, str):
            pending = json.loads(pending)
        lines = self.env["sale.order.line"].browse([int(k) for k in pending]).exists()
        if not lines or all(line.qty_delivered <= 0 for line in lines):
            return False  # wait for the delivery
        qtys = {}
        capped = []
        for line in lines:
            wanted = _to_float(pending.get(str(line.id)))
            qty = min(wanted, max(line.qty_delivered, 0.0))
            if float_is_zero(qty, precision_rounding=line.product_uom.rounding or 0.01):
                capped.append(line.product_id.display_name)
                continue
            if qty < wanted:
                capped.append(line.product_id.display_name)
            qtys[line] = qty
        pickings = self._magento_create_return_pickings(move, qtys, move.magento_return_reason_id)
        move.with_context(skip_magento_sync=True).write({
            "magento_restock_pending": False,
            "magento_restock_waiting": False,
        })
        if capped:
            move.message_post(body=_(
                "Return to stock capped to the quantity delivered in Odoo for: %s",
                ", ".join(capped)))
        return pickings

    def _magento_return_picking_type(self, origin_picking):
        self.ensure_one()
        picking_type = origin_picking.picking_type_id.return_picking_type_id if origin_picking else False
        if not picking_type or picking_type.code != "incoming":
            warehouse = self.warehouse_id or self.env["stock.warehouse"].search(
                [("company_id", "=", self.company_id.id)], limit=1)
            picking_type = warehouse.in_type_id
        return picking_type

    def _magento_create_return_pickings(self, move, qtys, reason=None):
        """One validated receipt per return location for ``qtys`` ({sale line: qty})."""
        self.ensure_one()
        if not qtys:
            return self.env["stock.picking"]
        instance = self.magento_instance_id
        warehouse = self.warehouse_id
        customer_location = (
            self.partner_shipping_id.property_stock_customer
            or self.env.ref("stock.stock_location_customers")
        )
        Move = self.env["stock.move"]
        groups = defaultdict(dict)
        for line, qty in qtys.items():
            location = instance._magento_return_location(warehouse, line.product_id, reason)
            groups[location][line] = qty
        pickings = self.env["stock.picking"]
        Picking = self.env["stock.picking"].with_context(skip_magento_sync=True)
        memo = move.ref or move.magento_credit_memo_id or move.name
        for location, line_qtys in groups.items():
            delivered = self.env["stock.move"]
            for line in line_qtys:
                delivered |= line.move_ids.filtered(
                    lambda m: m.state == "done" and m.location_dest_id.usage == "customer")
            origin_picking = delivered.picking_id[:1]
            picking_type = self._magento_return_picking_type(origin_picking)
            vals = {
                "picking_type_id": picking_type.id,
                "partner_id": self.partner_shipping_id.id,
                "origin": _("Return of %(order)s (Magento credit memo %(memo)s)",
                            order=self.name, memo=memo),
                "location_id": customer_location.id,
                "location_dest_id": location.id,
                "magento_refund_move_id": move.id,
                "magento_return_reason_id": reason.id if reason else False,
            }
            if origin_picking:
                vals["return_id"] = origin_picking.id
            # Odoo 18 links a picking to its order through the procurement group: reuse
            # the delivery's, else let the sale_id inverse create the order's group.
            group = origin_picking.group_id or self.procurement_group_id
            if group:
                vals["group_id"] = group.id
            vals["sale_id"] = self.id
            move_vals = []
            for line, qty in line_qtys.items():
                origin_move = delivered.filtered(lambda m, l=line: m.sale_line_id == l)[:1]
                mv = {
                    "name": line.product_id.display_name,
                    "product_id": line.product_id.id,
                    "product_uom_qty": qty,
                    "product_uom": line.product_uom.id,
                    "sale_line_id": line.id,
                    "location_id": customer_location.id,
                    "location_dest_id": location.id,
                    "picking_type_id": picking_type.id,
                    "warehouse_id": picking_type.warehouse_id.id,
                    "procure_method": "make_to_stock",
                    "origin_returned_move_id": origin_move.id or False,
                }
                if "to_refund" in Move._fields:
                    mv["to_refund"] = True
                if origin_move and "reference_ids" in Move._fields:
                    mv["reference_ids"] = [Command.set(origin_move.reference_ids.ids)]
                move_vals.append(Command.create(mv))
            vals["move_ids"] = move_vals
            picking = Picking.create(vals)
            picking.action_confirm()
            validated = False
            try:
                with self.env.cr.savepoint():
                    for stock_move in picking.move_ids:
                        stock_move.quantity = stock_move.product_uom_qty
                        stock_move.picked = True
                    picking._action_done()
                    validated = picking.state == "done"
            except (UserError, ValidationError) as exc:
                picking.message_post(body=_("The return could not be validated automatically: %s", exc))
            picking = picking.with_env(self.env)
            lines_text = ", ".join(
                "%s × %s" % (line.product_id.display_name, qty) for line, qty in line_qtys.items())
            picking.message_post(body=Markup("%s %s<br/>%s") % (
                _("Return of Magento credit memo %s:", memo), move._get_html_link(),
                _("%(lines)s into %(location)s%(reason)s.",
                  lines=lines_text, location=location.display_name,
                  reason=(" (%s)" % reason.display_name) if reason else ""),
            ))
            message = (_("Returned to stock: %(lines)s into %(location)s (%(picking)s).")
                       if validated else
                       _("Return receipt %(picking)s created for %(lines)s into %(location)s; "
                         "validate it to restock."))
            text = message % {"lines": lines_text, "location": location.display_name,
                              "picking": picking.name}
            move.message_post(body=text)
            self.message_post(body=text)
            pickings |= picking
        return pickings


class StockPicking(models.Model):
    _inherit = "stock.picking"

    magento_refund_move_id = fields.Many2one(
        "account.move", string="Magento Credit Note", copy=False, index="btree_not_null",
        readonly=True, ondelete="set null",
        help="Credit note this return belongs to (sent to Magento with return_to_stock_items, "
             "or created from an imported Magento credit memo).",
    )
    magento_return_reason_id = fields.Many2one(
        "magento.return.reason", string="Return Reason", copy=False, ondelete="set null",
    )

    def _action_done(self):
        res = super()._action_done()
        # A delivery of a Magento order: restock what imported credit memos were waiting for.
        orders = self.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state == "done"
            and p.sale_id.magento_instance_id
        ).sale_id
        if orders:
            waiting = self.env["account.move"].sudo().search([
                ("magento_sale_order_id", "in", orders.ids),
                ("move_type", "=", "out_refund"),
                ("magento_restock_waiting", "=", True),
            ])
            for order in waiting.magento_sale_order_id:
                order.sudo()._magento_process_pending_returns()
        return res


class StockReturnPicking(models.TransientModel):
    _inherit = "stock.return.picking"

    magento_return_reason_id = fields.Many2one(
        "magento.return.reason", string="Return Reason",
        help="Sent to Magento (credit memo comment) with the credit note of this return.",
    )
    magento_order = fields.Boolean(compute="_compute_magento_order")

    @api.depends("picking_id")
    def _compute_magento_order(self):
        for wizard in self:
            wizard.magento_order = bool(wizard.picking_id.sale_id.magento_instance_id)

    def _create_returns(self):
        # Odoo 17 hook: returns (new picking id, picking type id).
        new_picking_id, picking_type_id = super()._create_returns()
        if self.magento_return_reason_id:
            self.env["stock.picking"].browse(new_picking_id).magento_return_reason_id = self.magento_return_reason_id
        return new_picking_id, picking_type_id
