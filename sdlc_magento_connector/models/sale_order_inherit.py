import logging
from collections import defaultdict

import psycopg2
import requests
from markupsafe import Markup, escape

from odoo import Command, api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import SQL, email_normalize, float_compare

from ..services.magento_api import MagentoAPI
from .magento_instance import _SyncStats


_logger = logging.getLogger(__name__)

# Odoo delivery_type values that are also valid Magento carrier codes.
MAGENTO_CARRIER_CODES = ("ups", "usps", "fedex", "dhl")

# Amount fields of an order item that live on the configurable parent row.
ITEM_AMOUNT_KEYS = (
    "price_incl_tax", "row_total", "row_total_incl_tax", "tax_percent", "tax_amount",
    "discount_amount", "discount_percent", "weee_tax_applied_amount",
    "weee_tax_applied_row_amount", "weee_tax_applied",
)


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SaleOrder(models.Model):
    _inherit = "sale.order"

    is_magento_order = fields.Boolean(
        string="Is Magento Order",
        compute="_compute_is_magento_order",
        store=False,
    )
    magento_order_id = fields.Char("Magento Order ID", readonly=True, copy=False, index=True)
    magento_sync_conflict = fields.Boolean(
        "Magento Conflict", copy=False, readonly=True,
        help="Set when Magento changed an order that is no longer a draft in Odoo; the lines were not overwritten.",
    )
    magento_conflict_note = fields.Text(copy=False, readonly=True)
    magento_exported_from_odoo = fields.Boolean(
        "Created from Odoo",
        copy=False,
        readonly=True,
        help="The order was created in Odoo and pushed to Magento. Magento prices never overwrite "
             "the Odoo line prices of such orders.",
    )
    magento_increment_id = fields.Char("Magento Increment ID", readonly=True, copy=False)
    magento_status = fields.Char("Magento Status", readonly=True, copy=False)
    magento_currency = fields.Char("Magento Currency", readonly=True, copy=False)
    magento_customer_email = fields.Char("Magento Customer Email", copy=False)
    magento_instance_id = fields.Many2one(
        "magento.instance",
        string="Magento Instance",
        copy=False,
        index=True,
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

    # --- Phase 2 order fidelity (2.4) / auto-workflow (2.5) ---
    magento_state = fields.Char("Magento State", readonly=True, copy=False,
                                help="Magento order state (new, processing, complete, closed...).")
    magento_payment_method = fields.Char("Magento Payment Method", readonly=True, copy=False)
    magento_shipping_method = fields.Char("Magento Shipping Method", readonly=True, copy=False)
    magento_shipping_description = fields.Char("Magento Shipping", readonly=True, copy=False)
    magento_coupon_code = fields.Char("Coupon Code", readonly=True, copy=False)
    magento_discount_description = fields.Char("Discount Description", readonly=True, copy=False)
    magento_grand_total = fields.Monetary(
        "Magento Grand Total", currency_field="currency_id", readonly=True, copy=False,
    )
    magento_total_difference = fields.Monetary(
        "Difference with Magento", currency_field="currency_id", readonly=True, copy=False,
        help="Magento grand total minus the Odoo total. Never booked as a plug line: the "
             "difference stays visible until the cause (tax rate, rounding...) is fixed.",
    )
    magento_total_differs = fields.Boolean(
        "Total Differs from Magento", readonly=True, copy=False, index=True,
    )
    magento_total_refunded = fields.Monetary(
        "Magento Refunded", currency_field="currency_id", readonly=True, copy=False,
    )
    magento_refund_state = fields.Selection(
        [("none", "Not Refunded"), ("partial", "Partially Refunded"), ("refunded", "Refunded")],
        string="Magento Refund Status", readonly=True, copy=False,
    )
    magento_workflow_id = fields.Many2one(
        "magento.order.workflow", string="Magento Workflow", readonly=True, copy=False,
        ondelete="set null", help="Workflow rule last applied to this order.",
    )

    _magento_order_uniq = models.Constraint(
        "unique(magento_instance_id, magento_order_id)",
        "This Magento order is already imported for this Magento instance.",
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

    def _prepare_confirmation_values(self):
        # Confirming an imported order must keep the Magento order date.
        values = super()._prepare_confirmation_values()
        if len(self) == 1 and self.magento_order_id and self.magento_created_at:
            values["date_order"] = self.magento_created_at
        return values

    # -------------------------
    # LOOKUPS
    # -------------------------
    @api.model
    def _magento_find_order(self, instance, magento_order_id):
        """Return the sale order imported from ``magento_order_id`` on ``instance``."""
        if not instance or not magento_order_id:
            return self.browse()
        return self.search([
            ("magento_instance_id", "=", instance.id),
            ("magento_order_id", "=", str(magento_order_id)),
        ], limit=1)

    def _magento_api(self):
        self.ensure_one()
        # access_token is manager-only: build API clients from a sudo'ed instance.
        return MagentoAPI(self.magento_instance_id.sudo())

    # -------------------------
    # CUSTOMER / ADDRESS MAPPING
    # -------------------------
    @api.model
    def _magento_extract_billing_address(self, data):
        billing = data.get("billing_address") or {}
        if billing:
            return billing
        addresses = data.get("addresses") or []
        for address in addresses:
            if (address or {}).get("address_type") == "billing":
                return address
        if addresses:
            return addresses[0] or {}
        return {}

    @api.model
    def _magento_extract_shipping_address(self, data):
        extension = data.get("extension_attributes") or {}
        for assignment in extension.get("shipping_assignments") or []:
            address = ((assignment or {}).get("shipping") or {}).get("address") or {}
            if address:
                return address
        for address in data.get("addresses") or []:
            if (address or {}).get("address_type") == "shipping":
                return address
        return {}

    @api.model
    def _magento_is_guest(self, data):
        flag = data.get("customer_is_guest")
        if flag in (None, ""):
            return not data.get("customer_id")
        if isinstance(flag, str):
            return flag.strip().lower() in ("1", "true", "yes")
        return bool(flag)

    @api.model
    def _magento_normalize_email(self, email):
        email = (email or "").strip()
        if not email:
            return ""
        return email_normalize(email) or email.lower()

    @api.model
    def _magento_address_vals(self, address):
        address = address or {}
        firstname = (address.get("firstname") or "").strip()
        lastname = (address.get("lastname") or "").strip()
        vals = {"name": " ".join(p for p in (firstname, lastname) if p)}
        street = address.get("street") or []
        if isinstance(street, str):
            street = [street]
        street = [s for s in street if s]
        if street:
            vals["street"] = street[0]
            if len(street) > 1:
                vals["street2"] = " ".join(street[1:])
        if address.get("city"):
            vals["city"] = address["city"]
        if address.get("postcode"):
            vals["zip"] = address["postcode"]
        if address.get("telephone"):
            vals["phone"] = address["telephone"]
        country_code = address.get("country_id") or ""
        if country_code:
            country = self.env["res.country"].sudo().search([("code", "=", country_code)], limit=1)
            if country:
                vals["country_id"] = country.id
                state = self.env["res.country.state"]
                region_code = address.get("region_code") or ""
                region = address.get("region") or ""
                if isinstance(region, dict):
                    region_code = region_code or region.get("region_code") or ""
                    region = region.get("region") or ""
                if region_code:
                    state = state.sudo().search(
                        [("code", "=", region_code), ("country_id", "=", country.id)], limit=1
                    )
                if not state and region:
                    state = self.env["res.country.state"].sudo().search(
                        [("name", "=ilike", region), ("country_id", "=", country.id)], limit=1
                    )
                if state:
                    vals["state_id"] = state.id
        return vals

    @api.model
    def _magento_address_key(self, vals):
        def norm(value):
            return " ".join(str(value or "").split()).lower()

        return (
            norm(vals.get("name")),
            norm(vals.get("street")),
            norm(vals.get("street2")),
            norm(vals.get("city")),
            norm(vals.get("zip")),
            vals.get("country_id") or False,
            vals.get("state_id") or False,
        )

    @api.model
    def _magento_partner_address_key(self, partner):
        return self._magento_address_key({
            "name": partner.name,
            "street": partner.street,
            "street2": partner.street2,
            "city": partner.city,
            "zip": partner.zip,
            "country_id": partner.country_id.id,
            "state_id": partner.state_id.id,
        })

    @api.model
    def _magento_get_address_partner(self, partner, address, address_type):
        """Return the child contact of ``partner`` of type ``address_type`` matching ``address``.

        Children are deduplicated on name + address fields; a new child is created when none
        matches. Falls back to ``partner`` when Magento sent no usable address.
        """
        vals = self._magento_address_vals(address)
        if not any(vals.get(f) for f in ("street", "city", "zip")):
            return partner
        vals["name"] = vals.get("name") or partner.name
        key = self._magento_address_key(vals)
        for child in partner.child_ids.filtered(lambda c: c.type == address_type):
            if self._magento_partner_address_key(child) == key:
                return child
        vals.update({"parent_id": partner.id, "type": address_type})
        return self.env["res.partner"].sudo().create(vals)

    @api.model
    def _magento_resolve_customer(self, data, instance=None):
        """Find or create the commercial partner of a Magento order. Never matches by name.

        Registered customers: Magento customer_id (per instance), then normalized email.
        Guests: normalized email only; a guest without email always gets a new contact.
        """
        billing = self._magento_extract_billing_address(data)
        email = self._magento_normalize_email(data.get("customer_email") or billing.get("email"))
        is_guest = self._magento_is_guest(data)
        customer_id = "" if is_guest else str(data.get("customer_id") or "")
        partner_model = self.env["res.partner"].sudo()
        partner = partner_model.browse()

        if customer_id and instance:
            partner = partner_model.search([
                ("magento_instance_id", "=", instance.id),
                ("magento_customer_id", "=", customer_id),
            ], limit=1)

        if not partner and email:
            domain = [("email_normalized", "=", email)]
            if customer_id and instance:
                # Never steal a contact already bound to another customer of this instance.
                domain += [
                    "|",
                    ("magento_customer_id", "=", False),
                    ("magento_instance_id", "!=", instance.id),
                ]
            partner = partner_model.search(domain + [("parent_id", "=", False)], limit=1)
            if not partner:
                partner = partner_model.search(domain + [("type", "=", "contact")], limit=1)

        if partner:
            if customer_id and instance and not partner.magento_customer_id:
                partner.write({
                    "magento_customer_id": customer_id,
                    "magento_instance_id": instance.id,
                })
            return partner, email or self._magento_normalize_email(partner.email)

        firstname = data.get("customer_firstname") or billing.get("firstname") or ""
        lastname = data.get("customer_lastname") or billing.get("lastname") or ""
        name = " ".join(p for p in (firstname.strip(), lastname.strip()) if p) or email or _("Magento Guest")
        vals = self._magento_address_vals(billing)
        vals.update({
            "name": name,
            "email": email or False,
            "magento_instance_id": instance.id if instance else False,
            "magento_customer_id": customer_id or False,
        })
        partner = partner_model.create(vals)
        return partner, email

    # -------------------------
    # ORDER MAPPING
    # -------------------------
    @api.model
    def _magento_prepare_order_vals(self, data, instance, order=None):
        """Map a Magento order payload to sale.order values.

        Partner and address fields are only set when the order does not exist yet (looked up
        by instance + Magento id unless ``order`` is given), so re-syncs never re-assign the
        customer, never create duplicate guest contacts, and never touch confirmed orders.
        """
        magento_id = str(data.get("entity_id") or data.get("id") or "")
        if order is None:
            order = self._magento_find_order(instance, magento_id)
        currency_code = data.get("order_currency_code") or data.get("base_currency_code") or ""
        total_invoiced = data.get("total_invoiced")
        if total_invoiced is None:
            total_invoiced = data.get("base_total_invoiced")
        total_invoiced = _to_float(total_invoiced)
        total_paid = data.get("total_paid")
        if total_paid is None:
            total_paid = data.get("base_total_paid")
        total_paid = _to_float(total_paid)
        grand_total = data.get("grand_total")
        if grand_total is None:
            grand_total = data.get("base_grand_total")
        grand_total = _to_float(grand_total)
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
        total_refunded = _to_float(data.get("total_refunded"))
        if total_refunded <= 0:
            refund_state = "none"
        elif total_refunded + 0.0001 < (total_paid or grand_total):
            refund_state = "partial"
        else:
            refund_state = "refunded"
        payment = data.get("payment") or {}
        vals = {
            "magento_instance_id": instance.id if instance else False,
            "magento_order_id": magento_id,
            "magento_increment_id": data.get("increment_id") or "",
            "magento_status": data.get("status") or "",
            "magento_state": data.get("state") or "",
            "magento_currency": currency_code,
            "magento_total_invoiced": total_invoiced,
            "magento_invoice_state": invoice_state,
            "magento_total_paid": total_paid,
            "magento_payment_state": payment_state,
            "magento_total_refunded": total_refunded,
            "magento_refund_state": refund_state,
            "magento_grand_total": grand_total,
            "magento_payment_method": (payment.get("method") or "") if isinstance(payment, dict) else "",
            "magento_shipping_method": self._magento_shipping_method_code(data),
            "magento_shipping_description": data.get("shipping_description") or "",
            "magento_coupon_code": data.get("coupon_code") or "",
            "magento_discount_description": data.get("discount_description") or "",
        }
        if data.get("created_at"):
            vals["magento_created_at"] = data.get("created_at")
        if data.get("updated_at"):
            vals["magento_updated_at"] = data.get("updated_at")

        if order:
            email = self._magento_normalize_email(data.get("customer_email"))
            if email:
                vals["magento_customer_email"] = email
            return vals

        partner, email = self._magento_resolve_customer(data, instance)
        billing = self._magento_extract_billing_address(data)
        shipping = self._magento_extract_shipping_address(data)
        invoice_partner = self._magento_get_address_partner(partner, billing, "invoice")
        shipping_partner = (
            self._magento_get_address_partner(partner, shipping, "delivery")
            if shipping else invoice_partner
        )
        vals.update({
            "partner_id": partner.id,
            "partner_invoice_id": invoice_partner.id,
            "partner_shipping_id": shipping_partner.id,
            "magento_customer_email": email,
        })
        if data.get("created_at"):
            vals["date_order"] = data.get("created_at")
        if currency_code:
            currency = self.env["res.currency"].search([("name", "=", currency_code)], limit=1)
            if currency:
                vals["currency_id"] = currency.id
        return vals

    @api.model
    def _magento_shipping_method_code(self, data):
        code = data.get("shipping_method") or ""
        if not code:
            extension = data.get("extension_attributes") or {}
            for assignment in extension.get("shipping_assignments") or []:
                code = ((assignment or {}).get("shipping") or {}).get("method") or ""
                if code:
                    break
        return code

    @api.model
    def _magento_effective_items(self, items):
        """Return the Magento order items that become Odoo lines.

        Configurable products: Magento sends the configurable parent and its simple child
        (child.parent_item_id = parent.item_id). The child becomes the line (product from the
        child sku, price/qty from the parent when the child has none); the parent is skipped.
        Its item id is kept in ``_magento_parent_item_id`` so Magento invoice/shipment items
        that reference the parent map to the same line.
        Bundles: children are imported, unless no child carries a price (fixed-price bundle),
        in which case the bundle parent is imported instead.
        """
        items = [item for item in (items or []) if isinstance(item, dict)]
        by_id = {str(i.get("item_id")): i for i in items if i.get("item_id")}
        children = defaultdict(list)
        for item in items:
            parent_id = str(item.get("parent_item_id") or "")
            if parent_id and parent_id in by_id:
                children[parent_id].append(item)

        def parent_carries_price(parent, kids):
            return (
                parent.get("product_type") != "configurable"
                and not any(_to_float(k.get("price")) for k in kids)
                and _to_float(parent.get("price")) > 0
            )

        result = []
        for item in items:
            item_id = str(item.get("item_id") or "")
            parent_id = str(item.get("parent_item_id") or "")
            if parent_id and parent_id in by_id:
                parent = by_id[parent_id]
                if parent.get("product_type") == "configurable":
                    merged = dict(item)
                    if _to_float(item.get("price")) <= 0:
                        merged["price"] = parent.get("price")
                        # Magento keeps the amounts (tax, discount, FPT) on the configurable
                        # parent row when the child carries no price.
                        for key in ITEM_AMOUNT_KEYS:
                            if not _to_float(item.get(key)) and parent.get(key) not in (None, ""):
                                merged[key] = parent.get(key)
                    if _to_float(item.get("qty_ordered") or item.get("qty")) <= 0:
                        merged["qty_ordered"] = parent.get("qty_ordered") or parent.get("qty")
                    merged["name"] = item.get("name") or parent.get("name")
                    merged["_magento_parent_item_id"] = parent_id
                    result.append(merged)
                elif not parent_carries_price(parent, children[parent_id]):
                    result.append(dict(item))
                continue
            if item_id in children:
                if parent_carries_price(item, children[item_id]):
                    result.append(dict(item))
                continue
            result.append(dict(item))
        return result

    @api.model
    def _magento_find_item_product(self, item, instance):
        sku = item.get("sku") or ""
        product = self.env["product.product"]
        product_map = self.env["magento.product.map"]
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
        return product, product_map

    @api.model
    def _magento_prepare_order_line_vals(self, item, instance):
        sku = item.get("sku") or ""
        product, product_map = self._magento_find_item_product(item, instance)
        if not product:
            raise UserError(
                _("No Odoo product found for Magento SKU '%(sku)s' (item %(item)s). "
                  "Import or map the product first.")
                % {"sku": sku or "-", "item": item.get("item_id") or "-"}
            )
        qty = _to_float(item.get("qty_ordered") or item.get("qty")) or 1.0
        return {
            "product_id": product.id,
            "product_uom_id": product.uom_id.id,
            "name": item.get("name") or product.display_name or sku,
            "product_uom_qty": qty,
            "price_unit": self._magento_item_unit_price(item, instance, qty),
            "magento_item_id": str(item.get("item_id") or ""),
            "magento_parent_item_id": item.get("_magento_parent_item_id") or False,
            "magento_product_id": product_map.id if product_map else False,
            "magento_sku": sku,
        }

    @api.model
    def _magento_item_unit_price(self, item, instance, qty):
        """Magento unit price: tax-included when the instance mirrors tax-included catalog
        prices (the line then carries price-included taxes), tax-excluded otherwise."""
        price = _to_float(item.get("price"))
        if instance and instance.prices_include_tax:
            if item.get("price_incl_tax") not in (None, ""):
                return _to_float(item.get("price_incl_tax"))
            return price + _to_float(item.get("tax_amount")) / (qty or 1.0)
        return price

    @api.model
    def _magento_compute_invoice_summary(self, order_data, invoices):
        grand_total = order_data.get("grand_total")
        if grand_total is None:
            grand_total = order_data.get("base_grand_total")
        grand_total = _to_float(grand_total)

        total_invoiced = 0.0
        invoices = invoices or []
        for inv in invoices:
            amount = inv.get("grand_total")
            if amount is None:
                amount = inv.get("base_grand_total")
            total_invoiced += _to_float(amount)

        if total_invoiced <= 0 and invoices:
            invoice_state = "partial"
        elif total_invoiced <= 0:
            invoice_state = "not_invoiced"
        elif grand_total and total_invoiced + 0.0001 < grand_total:
            invoice_state = "partial"
        else:
            invoice_state = "invoiced"
        return total_invoiced, invoice_state

    # -------------------------
    # ORDER LINES
    # -------------------------
    def _magento_sync_order_lines(self, data):
        """Bring the order lines in line with the Magento order.

        Draft/sent orders: lines are upserted by a stable key (Magento item id for product
        lines, ``shipping`` / ``discount:<item>`` / ``fpt:<item>`` for the other roles), so
        the sale line <-> invoice line links survive re-syncs. Any other state: lines are
        never touched; differences are flagged with ``magento_sync_conflict``. In both cases
        the Odoo total is then compared with Magento's grand total.
        """
        self.ensure_one()
        self._magento_register_order_mappings(data)
        items = self._magento_effective_items(data.get("items"))
        if not items:
            return
        if self.state in ("draft", "sent"):
            self._magento_write_order_lines(items, data=data)
            self._magento_set_conflict([])
        else:
            self._magento_set_conflict(self._magento_compare_order_lines(items))
        self._magento_update_total_difference(data)

    def _magento_register_order_mappings(self, data):
        """Record the payment and shipping methods met (mapping rows) and apply the carrier."""
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance or not data:
            return
        payment = data.get("payment") or {}
        if isinstance(payment, dict) and payment.get("method"):
            title = ""
            info = payment.get("additional_information") or []
            if isinstance(info, list) and info and isinstance(info[0], str):
                title = info[0]
            instance._magento_note_payment_method(payment["method"], title)
        code = self._magento_shipping_method_code(data)
        if code:
            mapping = instance._magento_note_carrier(code, data.get("shipping_description"))
            carrier = mapping._carrier() if mapping else False
            if (carrier and "carrier_id" in self._fields and self.state in ("draft", "sent")
                    and self.carrier_id != carrier):
                self.carrier_id = carrier.id

    # -- line specs ---------------------------------------------------------
    @api.model
    def _magento_line_key_of(self, line):
        return line.magento_line_key or line.magento_item_id or ""

    def _magento_line_by_key(self, key):
        self.ensure_one()
        return self.order_line.filtered(lambda l: not l.display_type and self._magento_line_key_of(l) == key)[:1]

    @api.model
    def _magento_tax_details(self, data):
        """Magento tax breakdown: order-level rates, per-item rates, shipping rates."""
        extension = data.get("extension_attributes") or {}
        applied = [t for t in extension.get("applied_taxes") or [] if isinstance(t, dict)]
        by_item = {}
        shipping = None
        for entry in extension.get("item_applied_taxes") or []:
            if not isinstance(entry, dict):
                continue
            rates = [t for t in entry.get("applied_taxes") or [] if isinstance(t, dict)]
            kind = entry.get("type") or "product"
            item_id = entry.get("item_id") or entry.get("associated_item_id")
            if kind == "shipping":
                shipping = rates
            elif kind == "product" and item_id:
                by_item[str(item_id)] = rates
        return {"applied": applied, "by_item": by_item, "shipping": shipping}

    def _magento_pick_rates(self, details, explicit, percent, tax_amount, base_amount):
        """Magento rates (dicts with code/title/percent) that taxed an amount.

        ``explicit``: the per-item / shipping breakdown when Magento sent one. Otherwise the
        order-level rates are matched on the item's ``tax_percent``, then on the amount
        (combined rates, then each single rate); last resort, a rate without code at the
        Magento percentage.
        """
        explicit = [r for r in explicit or [] if _to_float(r.get("percent"))]
        if explicit:
            return explicit
        currency = self.currency_id or self.env.company.currency_id
        if percent <= 0 and (currency.is_zero(tax_amount) or tax_amount <= 0):
            return []
        applied = [r for r in details["applied"] if _to_float(r.get("percent"))]
        if percent > 0:
            total = sum(_to_float(r.get("percent")) for r in applied)
            if applied and abs(total - percent) < 0.00005:
                return applied
            for rate in applied:
                if abs(_to_float(rate.get("percent")) - percent) < 0.00005:
                    return [rate]
            return [{"code": "", "title": "", "percent": percent}]
        if base_amount:
            candidates = []
            if applied:
                candidates.append(applied)
                candidates.extend([rate] for rate in applied)
            for rates in candidates:
                pct = sum(_to_float(r.get("percent")) for r in rates)
                if currency.compare_amounts(base_amount * pct / 100.0, tax_amount) == 0:
                    return rates
            return [{"code": "", "title": "", "percent": round(tax_amount / base_amount * 100.0, 4)}]
        return []

    def _magento_taxes_for_rates(self, rates, price_include):
        self.ensure_one()
        taxes = self.env["account.tax"]
        instance = self.magento_instance_id
        for rate in rates:
            taxes |= instance._magento_tax_for_rate(rate, price_include, company=self.company_id)
        return taxes

    def _magento_line_specs(self, data, items):
        """[(key, vals)] of every Magento line of the order: products, FPT, discounts, shipping.

        Discounts: a discount that Odoo's line discount (%) reproduces to the cent (same
        discounted row total, e.g. a 10% coupon) becomes the line discount; any other
        discount becomes a separate line (discount product, price = -Magento discount,
        same taxes as the product when Magento taxes after discount), so the taxed base is
        exactly Magento's ``row_total - discount_amount``.
        """
        self.ensure_one()
        instance = self.magento_instance_id
        currency = self.currency_id or self.env.company.currency_id
        details = self._magento_tax_details(data)
        incl = bool(instance.prices_include_tax)
        after_discount = instance.tax_after_discount
        digits = self.env["decimal.precision"].precision_get("Discount")
        specs = []
        extra = []
        for item in items:
            vals = self._magento_prepare_order_line_vals(item, instance)
            key = vals["magento_item_id"] or "sku:%s" % (vals.get("magento_sku") or "")
            qty = vals["product_uom_qty"]
            price = vals["price_unit"]
            row = _to_float(item.get("row_total_incl_tax") if incl else item.get("row_total")) \
                or price * qty
            explicit = details["by_item"].get(str(item.get("item_id") or "")) \
                or details["by_item"].get(str(item.get("_magento_parent_item_id") or ""))
            rates = self._magento_pick_rates(
                details, explicit, _to_float(item.get("tax_percent")),
                _to_float(item.get("tax_amount")), _to_float(item.get("row_total")),
            )
            taxes = self._magento_taxes_for_rates(rates, incl)
            vals.update({
                "tax_ids": [Command.set(taxes.ids)],
                "discount": 0.0,
                "magento_line_role": "product",
                "magento_line_key": key,
            })
            label = vals.get("magento_sku") or vals["name"]
            discount = abs(_to_float(item.get("discount_amount")))
            if discount and not currency.is_zero(discount):
                pct = 0.0
                if after_discount and row:
                    pct = round(_to_float(item.get("discount_percent")) or discount / row * 100.0, digits)
                    exact = price * qty * (1.0 - pct / 100.0)
                    if not 0 < pct <= 100 or abs(exact - (row - discount)) > 1e-6:
                        pct = 0.0
                if pct:
                    vals["discount"] = pct
                else:
                    extra.append(("discount:%s" % key, self._magento_extra_line_vals(
                        "discount", self._magento_discount_label(data, label), -discount,
                        taxes if after_discount else self.env["account.tax"], key="discount:%s" % key,
                    )))
            fpt = _to_float(item.get("weee_tax_applied_row_amount"))
            if fpt and not currency.is_zero(fpt):
                extra.append(("fpt:%s" % key, self._magento_extra_line_vals(
                    "fpt", _("FPT: %s") % label, fpt, self.env["account.tax"], key="fpt:%s" % key,
                )))
            specs.append((key, vals))
        specs.extend(extra)

        shipping_incl = bool(instance.shipping_includes_tax)
        shipping_excl = _to_float(data.get("shipping_amount"))
        shipping = shipping_excl
        if shipping_incl and data.get("shipping_incl_tax") not in (None, ""):
            shipping = _to_float(data.get("shipping_incl_tax"))
        if shipping and not currency.is_zero(shipping):
            rates = self._magento_pick_rates(
                details, details["shipping"], 0.0, _to_float(data.get("shipping_tax_amount")),
                shipping_excl,
            )
            shipping_taxes = self._magento_taxes_for_rates(rates, shipping_incl)
            specs.append(("shipping", self._magento_extra_line_vals(
                "shipping", data.get("shipping_description") or _("Shipping"), shipping,
                shipping_taxes, key="shipping",
            )))
            shipping_discount = abs(_to_float(data.get("shipping_discount_amount")))
            if shipping_discount and not currency.is_zero(shipping_discount):
                specs.append(("discount:shipping", self._magento_extra_line_vals(
                    "discount", self._magento_discount_label(data, _("shipping")), -shipping_discount,
                    shipping_taxes if after_discount else self.env["account.tax"],
                    key="discount:shipping",
                )))
        return specs

    @api.model
    def _magento_discount_label(self, data, what):
        reason = data.get("discount_description") or data.get("coupon_code") or ""
        if reason:
            return _("Discount %(reason)s (%(what)s)", reason=reason, what=what)
        return _("Discount (%s)", what)

    def _magento_extra_line_vals(self, role, name, price, taxes, key):
        self.ensure_one()
        product = self.magento_instance_id._magento_service_product(
            "discount" if role == "discount" else role
        )
        return {
            "product_id": product.id,
            "product_uom_id": product.uom_id.id,
            "name": name,
            "product_uom_qty": 1.0,
            "price_unit": price,
            "tax_ids": [Command.set(taxes.ids)],
            "discount": 0.0,
            "magento_line_role": role,
            "magento_line_key": key,
        }

    def _magento_write_order_lines(self, items, data=None):
        self.ensure_one()
        instance = self.magento_instance_id
        keep_price = self.magento_exported_from_odoo
        if keep_price or not data:
            # Orders created in Odoo keep their Odoo prices, taxes and extra lines.
            specs = []
            for item in items:
                vals = self._magento_prepare_order_line_vals(item, instance)
                if keep_price:
                    vals.pop("price_unit")
                specs.append((vals["magento_item_id"], vals))
        else:
            specs = self._magento_line_specs(data, items)
        existing = {}
        for line in self.order_line:
            key = self._magento_line_key_of(line)
            if key and not line.display_type:
                existing.setdefault(key, line)
        commands = []
        seen = set()
        sequence = max(self.order_line.mapped("sequence") or [10])
        for key, vals in specs:
            line = existing.get(key) if key else None
            if not line:
                sequence += 1
                commands.append(Command.create(dict(vals, sequence=sequence)))
                continue
            seen.add(key)
            changed = {
                fname: value
                for fname, value in vals.items()
                if fname not in ("magento_item_id", "magento_line_key")
                and self._magento_line_value_differs(line, fname, value)
            }
            if changed:
                # Always rewrite qty, price, taxes and discount together so Odoo's
                # recomputes (triggered by product/qty changes) can't override Magento's
                # values. Exported orders keep their Odoo price.
                changed.setdefault("product_uom_qty", vals["product_uom_qty"])
                changed.setdefault("price_unit", line.price_unit if keep_price else vals["price_unit"])
                for fname in ("tax_ids", "discount"):
                    if fname in vals:
                        changed.setdefault(fname, vals[fname])
                commands.append(Command.update(line.id, changed))
        for key, line in existing.items():
            if key not in seen:
                commands.append(Command.unlink(line.id))
        if commands:
            self.with_context(sale_no_log_for_new_lines=True).write({"order_line": commands})

    @api.model
    def _magento_line_value_differs(self, line, fname, value):
        current = line[fname]
        field = line._fields[fname]
        if field.type == "many2one":
            return current.id != (value or False)
        if field.type == "many2many":
            ids = set()
            for command in value or []:
                if command[0] == Command.SET:
                    ids = set(command[2])
            return set(current.ids) != ids
        if fname == "product_uom_qty":
            rounding = line.product_uom_id.rounding or 0.0001
            return float_compare(current, value or 0.0, precision_rounding=rounding) != 0
        if fname == "price_unit":
            currency = line.currency_id or line.order_id.currency_id or self.env.company.currency_id
            return currency.compare_amounts(current, value or 0.0) != 0
        if fname == "discount":
            digits = self.env["decimal.precision"].precision_get("Discount")
            return float_compare(current or 0.0, value or 0.0, precision_digits=digits) != 0
        return (current or False) != (value or False)

    # -- totals -------------------------------------------------------------
    def _magento_update_total_difference(self, data):
        """Compare the Odoo total with Magento's grand total; flag (never plug) a difference."""
        self.ensure_one()
        grand_total = data.get("grand_total")
        if grand_total in (None, ""):
            grand_total = data.get("base_grand_total")
        if grand_total in (None, "") or self.magento_exported_from_odoo:
            return
        grand_total = _to_float(grand_total)
        currency = self.currency_id or self.env.company.currency_id
        difference = currency.round(grand_total - self.amount_total)
        differs = not currency.is_zero(difference)
        vals = {
            "magento_grand_total": grand_total,
            "magento_total_difference": difference if differs else 0.0,
            "magento_total_differs": differs,
        }
        post = differs and (
            not self.magento_total_differs
            or currency.compare_amounts(self.magento_total_difference, difference) != 0
        )
        if any(
            (self[f] != v) if f == "magento_total_differs" else currency.compare_amounts(self[f], v) != 0
            for f, v in vals.items()
        ):
            self.sudo().write(vals)
        if post:
            self.message_post(body=self._magento_total_difference_note(data, difference))

    def _magento_total_difference_note(self, data, difference):
        self.ensure_one()
        currency = self.currency_id or self.env.company.currency_id
        lines = self.order_line.filtered(lambda l: not l.display_type)

        def odoo(role):
            return sum(lines.filtered(lambda l: (l.magento_line_role or "product") == role).mapped("price_subtotal"))

        def fmt(amount):
            return "%.*f" % (currency.decimal_places, amount)

        rows = [
            (_("Products"), _to_float(data.get("subtotal")), odoo("product")),
            (_("Discounts"), -abs(_to_float(data.get("discount_amount"))), odoo("discount")),
            (_("Shipping"), _to_float(data.get("shipping_amount")), odoo("shipping")),
            (_("FPT"), sum(_to_float(i.get("weee_tax_applied_row_amount"))
                           for i in data.get("items") or [] if isinstance(i, dict)), odoo("fpt")),
            (_("Tax"), _to_float(data.get("tax_amount")), self.amount_tax),
            (_("Total"), _to_float(data.get("grand_total")), self.amount_total),
        ]
        hints = []
        instance = self.magento_instance_id
        company_method = self.company_id.tax_calculation_rounding_method
        if instance and company_method and company_method != instance.tax_rounding_method:
            hints.append(_("Odoo rounds taxes '%(odoo)s' but Magento is set to '%(magento)s' on the "
                           "instance (Orders & Payments > Tax Rounding).",
                           odoo=company_method, magento=instance.tax_rounding_method))
        if not currency.is_zero(_to_float(data.get("discount_tax_compensation_amount"))):
            hints.append(_("Magento applied a discount tax compensation (discount on tax-included "
                           "prices)."))
        extension = data.get("extension_attributes") or {}
        for key in ("gift_cards_amount", "customer_balance_amount", "reward_currency_amount",
                    "base_customer_balance_amount"):
            if _to_float(data.get(key) or extension.get(key)):
                hints.append(_("Magento deducted %(what)s (%(amount)s) from the grand total.",
                               what=key, amount=_to_float(data.get(key) or extension.get(key))))
        if self.state not in ("draft", "sent"):
            hints.append(_("The order is no longer a quotation in Odoo; its lines were not updated."))
        body = Markup("<p>%s</p><table class='table table-sm o_magento_total_diff'>"
                      "<thead><tr><th></th><th class='text-end'>%s</th><th class='text-end'>%s</th>"
                      "</tr></thead><tbody>%s</tbody></table>") % (
            _("The Odoo total differs from the Magento grand total by %(diff)s %(cur)s. "
              "No adjustment line was added.", diff=fmt(difference), cur=currency.name),
            _("Magento"), _("Odoo"),
            Markup("").join(
                Markup("<tr><td>%s</td><td class='text-end'>%s</td><td class='text-end'>%s</td></tr>")
                % (label, fmt(magento), fmt(odoo_amount))
                for label, magento, odoo_amount in rows
            ),
        )
        if hints:
            body += Markup("<ul>%s</ul>") % Markup("").join(Markup("<li>%s</li>") % h for h in hints)
        return body

    def _magento_compare_order_lines(self, items):
        """Return human-readable differences between Magento items and the current lines."""
        self.ensure_one()
        instance = self.magento_instance_id
        check_price = not self.magento_exported_from_odoo
        existing = {
            line.magento_item_id: line
            for line in self.order_line
            if line.magento_item_id and not line.display_type
        }
        diffs = []
        seen = set()
        for item in items:
            key = str(item.get("item_id") or "")
            sku = item.get("sku") or key
            seen.add(key)
            try:
                vals = self._magento_prepare_order_line_vals(item, instance)
            except UserError as exc:
                diffs.append(str(exc))
                continue
            line = existing.get(key)
            if not line:
                diffs.append(
                    _("New Magento item %(sku)s (qty %(qty)s).")
                    % {"sku": sku, "qty": vals["product_uom_qty"]}
                )
                continue
            if self._magento_line_value_differs(line, "product_id", vals["product_id"]):
                diffs.append(
                    _("%(sku)s: product changed (Odoo: %(odoo)s).")
                    % {"sku": sku, "odoo": line.product_id.display_name}
                )
            if self._magento_line_value_differs(line, "product_uom_qty", vals["product_uom_qty"]):
                diffs.append(
                    _("%(sku)s: quantity %(magento)s in Magento, %(odoo)s in Odoo.")
                    % {"sku": sku, "magento": vals["product_uom_qty"], "odoo": line.product_uom_qty}
                )
            if check_price and self._magento_line_value_differs(line, "price_unit", vals["price_unit"]):
                diffs.append(
                    _("%(sku)s: unit price %(magento)s in Magento, %(odoo)s in Odoo.")
                    % {"sku": sku, "magento": vals["price_unit"], "odoo": line.price_unit}
                )
        for key, line in existing.items():
            if key not in seen:
                diffs.append(
                    _("%(sku)s: line no longer present in Magento.")
                    % {"sku": line.magento_sku or line.product_id.display_name}
                )
        return diffs

    def _magento_set_conflict(self, diffs):
        self.ensure_one()
        if not diffs:
            if self.magento_sync_conflict or self.magento_conflict_note:
                self.write({"magento_sync_conflict": False, "magento_conflict_note": False})
            return
        note = "\n".join(diffs)
        if self.magento_sync_conflict and self.magento_conflict_note == note:
            return
        self.write({"magento_sync_conflict": True, "magento_conflict_note": note})
        body = Markup("%s<ul>%s</ul>") % (
            _("Magento changed this order after it left draft; the Odoo lines were not modified:"),
            Markup("").join(Markup("<li>%s</li>") % escape(d) for d in diffs),
        )
        self.message_post(body=body)

    def _magento_link_exported_lines(self, data):
        """After pushing an Odoo order to Magento, link the existing Odoo lines to the Magento
        items (item ids, sku) without changing their product, quantity or price."""
        self.ensure_one()
        instance = self.magento_instance_id
        free_lines = self.order_line.filtered(
            lambda l: not l.display_type and l.product_id and not l.magento_item_id
        )
        unmatched = []
        for item in self._magento_effective_items(data.get("items")):
            sku = item.get("sku") or ""
            product, _product_map = self._magento_find_item_product(item, instance)
            match = free_lines.filtered(lambda l: l.magento_sku and l.magento_sku == sku)[:1]
            if not match and product:
                match = free_lines.filtered(lambda l: l.product_id == product)[:1]
            if not match:
                unmatched.append(sku or str(item.get("item_id") or ""))
                continue
            match.write({
                "magento_item_id": str(item.get("item_id") or ""),
                "magento_parent_item_id": item.get("_magento_parent_item_id") or False,
                "magento_sku": sku or match.magento_sku,
            })
            free_lines -= match
        if unmatched:
            self.message_post(
                body=_("Magento order items not matched to an Odoo line: %s") % ", ".join(unmatched)
            )

    def _magento_line_for_item(self, order_item_id):
        """Odoo sale line for a Magento order item id (child or configurable parent id)."""
        self.ensure_one()
        key = str(order_item_id or "")
        if not key:
            return self.env["sale.order.line"]
        lines = self.order_line.filtered(lambda l: l.magento_item_id == key)
        if not lines:
            lines = self.order_line.filtered(lambda l: l.magento_parent_item_id == key)
        return lines[:1]

    def _magento_map_document_items(self, items, qty_keys):
        """Map Magento invoice/credit memo/shipment items to sale lines.

        Returns ({sale_line_id: qty}, [unmapped order item ids]). A configurable parent and
        its child both map to one line; their quantities are not summed (max is kept).
        """
        self.ensure_one()
        line_qty = {}
        missing = []
        for item in items or []:
            order_item_id = item.get("order_item_id") or item.get("item_id")
            if not order_item_id:
                continue
            qty = 0.0
            for qty_key in qty_keys:
                if item.get(qty_key) not in (None, ""):
                    qty = _to_float(item.get(qty_key))
                    break
            if qty <= 0:
                continue
            line = self._magento_line_for_item(order_item_id)
            if not line:
                missing.append(str(order_item_id))
                continue
            line_qty[line.id] = max(line_qty.get(line.id, 0.0), qty)
        return line_qty, missing

    # -------------------------
    # INVOICES / CREDIT MEMOS / PAYMENTS
    # -------------------------
    @api.model
    def _magento_release_invoice_user(self, move):
        # Sales users only see invoices assigned to them or unassigned (sale record rule);
        # imports run as cron/admin, so keep Magento documents unassigned.
        if (
            move.invoice_user_id
            and move.create_uid
            and move.invoice_user_id.id == move.create_uid.id
        ):
            move.with_context(skip_magento_sync=True).write({"invoice_user_id": False})

    def _magento_find_move(self, field_name, magento_id):
        self.ensure_one()
        return self.env["account.move"].search([
            (field_name, "=", str(magento_id)),
            ("magento_instance_id", "=", self.magento_instance_id.id),
        ], limit=1)

    def _magento_document_line_vals(self, doc, kind):
        """Invoice line values of a Magento invoice (``kind`` = ``invoice``) or credit memo.

        Product lines come from the mapped sale lines (``_prepare_invoice_line``: same
        price, line discount and taxes, linked to the sale line). The document's own
        amounts then drive the extra lines: per-item discount lines, FPT, shipping, the
        shipping discount and, for credit memos, the adjustment refund / fee. Returns
        ``(vals_list, unmapped Magento item ids)``.
        """
        self.ensure_one()
        instance = self.magento_instance_id
        currency = self.currency_id or self.env.company.currency_id
        qty_keys = ("qty", "qty_invoiced", "qty_ordered") if kind == "invoice" else ("qty", "qty_refunded")
        line_qty, missing = self._magento_map_document_items(doc.get("items"), qty_keys)
        sale_lines = self.env["sale.order.line"].browse(list(line_qty))
        result = [line._prepare_invoice_line(quantity=line_qty[line.id]) for line in sale_lines]

        discounts = defaultdict(float)
        fpts = defaultdict(float)
        for item in doc.get("items") or []:
            line = self._magento_line_for_item(item.get("order_item_id") or item.get("item_id"))
            if not line:
                continue
            discounts[line] = max(discounts[line], abs(_to_float(item.get("discount_amount"))))
            fpts[line] = max(fpts[line], _to_float(item.get("weee_tax_applied_row_amount")))
        items_discount = 0.0
        for line, amount in discounts.items():
            items_discount += amount
            extra = self._magento_line_by_key("discount:%s" % self._magento_line_key_of(line))
            if extra and not currency.is_zero(amount):
                result.append(extra._prepare_invoice_line(quantity=1.0, price_unit=-amount))
        for line, amount in fpts.items():
            extra = self._magento_line_by_key("fpt:%s" % self._magento_line_key_of(line))
            if extra and not currency.is_zero(amount):
                result.append(extra._prepare_invoice_line(quantity=1.0, price_unit=amount))

        shipping = _to_float(doc.get("shipping_amount"))
        if instance.shipping_includes_tax and doc.get("shipping_incl_tax") not in (None, ""):
            shipping = _to_float(doc.get("shipping_incl_tax"))
        if not currency.is_zero(shipping):
            line = self._magento_line_by_key("shipping")
            if line:
                result.append(line._prepare_invoice_line(quantity=1.0, price_unit=shipping))
            else:
                product = instance._magento_service_product("shipping")
                result.append({
                    "product_id": product.id, "name": self.magento_shipping_description or _("Shipping"),
                    "quantity": 1.0, "price_unit": shipping, "tax_ids": [Command.clear()],
                })
        shipping_discount = abs(_to_float(doc.get("discount_amount"))) - items_discount
        if shipping_discount > 0 and not currency.is_zero(shipping_discount):
            line = self._magento_line_by_key("discount:shipping")
            if line:
                result.append(line._prepare_invoice_line(quantity=1.0, price_unit=-shipping_discount))

        if kind == "credit_memo":
            adjustments = (
                (_to_float(doc.get("adjustment_positive")), _("Adjustment refund")),
                (-_to_float(doc.get("adjustment_negative")), _("Adjustment fee")),
            )
            for amount, label in adjustments:
                if not currency.is_zero(amount):
                    product = instance._magento_service_product("discount")
                    result.append({
                        "product_id": product.id, "name": label, "quantity": 1.0,
                        "price_unit": amount, "tax_ids": [Command.clear()],
                    })
        return result, missing

    def _magento_document_move_vals(self, doc, kind, line_vals):
        """Header values of an imported Magento invoice / credit memo (from the order)."""
        self.ensure_one()
        doc_date = doc.get("created_at")
        if doc_date:
            doc_date = fields.Date.to_date(fields.Datetime.to_datetime(doc_date))
        vals = self._prepare_invoice()
        vals.pop("transaction_ids", None)
        vals.update({
            "move_type": "out_invoice" if kind == "invoice" else "out_refund",
            "invoice_date": doc_date or False,
            "invoice_user_id": False,
            "ref": doc.get("increment_id") or "",
            "invoice_line_ids": [Command.create(v) for v in line_vals],
        })
        return vals

    def _magento_create_invoice_from_magento(self, invoice_data):
        """Import one Magento invoice: exactly one Odoo invoice per Magento invoice.

        An Odoo invoice already linked to it is reused; else an Odoo invoice of the order
        not linked to any Magento invoice (created by the workflow or a user) is linked
        instead of creating a second one; else the invoice is created from the Magento
        document, linked to the sale lines. Imported invoices are posted without any push
        back to Magento (they came from Magento).
        """
        self.ensure_one()
        invoice_id = invoice_data.get("entity_id") or invoice_data.get("id")
        if not invoice_id:
            return False

        existing_move = self._magento_find_move("magento_invoice_id", invoice_id)
        if existing_move:
            self._magento_release_invoice_user(existing_move)
            if existing_move.state == "draft":
                existing_move.with_context(skip_magento_sync=True).action_post()
            return existing_move

        candidates = self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("magento_sale_order_id", "=", self.id),
            ("magento_invoice_id", "=", False),
            ("state", "!=", "cancel"),
        ])
        if candidates:
            total = invoice_data.get("grand_total")
            if total is None:
                total = invoice_data.get("base_grand_total")
            total = _to_float(total)
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
                vals = {"magento_invoice_id": str(invoice_id)}
                invoice_date = invoice_data.get("created_at")
                if invoice_date and not match.invoice_date and match.state == "draft":
                    vals["invoice_date"] = fields.Date.to_date(fields.Datetime.to_datetime(invoice_date))
                invoice_ref = invoice_data.get("increment_id") or ""
                if invoice_ref and not match.ref:
                    vals["ref"] = invoice_ref
                match.with_context(skip_magento_sync=True).write(vals)
                self._magento_release_invoice_user(match)
                if match.state == "draft":
                    match.with_context(skip_magento_sync=True).action_post()
                return match

        line_vals, missing_items = self._magento_document_line_vals(invoice_data, "invoice")
        if not any(v.get("sale_line_ids") for v in line_vals):
            raise UserError(
                _("Magento invoice %(invoice)s: no item could be mapped to a line of order %(order)s.")
                % {"invoice": invoice_data.get("increment_id") or invoice_id, "order": self.name}
            )
        if missing_items:
            _logger.warning(
                "Magento invoice %s for order %s has unmapped items: %s",
                invoice_id,
                self.name,
                ",".join(missing_items),
            )
        vals = self._magento_document_move_vals(invoice_data, "invoice", line_vals)
        vals["magento_invoice_id"] = str(invoice_id)
        move = self.env["account.move"].with_context(
            default_move_type="out_invoice",
            skip_magento_sync=True,
        ).create(vals)
        move.with_context(skip_magento_sync=True).action_post()
        self._magento_check_document_total(move, invoice_data, _("invoice"))
        return move

    def _magento_check_document_total(self, move, doc, label):
        """Note on the move when its total differs from the Magento document's."""
        total = doc.get("grand_total")
        if total in (None, ""):
            total = doc.get("base_grand_total")
        if total in (None, ""):
            return
        currency = move.currency_id
        difference = currency.round(abs(_to_float(total)) - move.amount_total)
        if not currency.is_zero(difference):
            move.message_post(body=_(
                "The total of this %(doc)s differs from the Magento %(doc)s %(ref)s by %(diff)s "
                "(Magento %(magento)s, Odoo %(odoo)s). No adjustment line was added.",
                doc=label, ref=doc.get("increment_id") or "", diff=difference,
                magento=_to_float(total), odoo=move.amount_total,
            ))

    def _magento_sync_invoices(self, api=None):
        self.ensure_one()
        if not self.magento_order_id or not self.magento_instance_id:
            return
        api = api or self._magento_api()
        try:
            invoices = api.get_invoices(order_id=self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        for invoice in invoices or []:
            invoice_id = invoice.get("entity_id") or invoice.get("id")
            if not invoice_id:
                continue
            existing = self._magento_find_move("magento_invoice_id", invoice_id)
            if existing and existing.state != "draft":
                continue
            if invoice.get("items"):
                invoice_data = invoice
            else:
                try:
                    invoice_data = api.get_invoice(invoice_id)
                except requests.exceptions.RequestException as exc:
                    self._magento_raise_http_error(exc)
            self._magento_create_invoice_from_magento(invoice_data)

    def _magento_create_credit_memo_from_magento(self, credit_data):
        self.ensure_one()
        credit_id = credit_data.get("entity_id") or credit_data.get("id")
        if not credit_id:
            return False

        existing_move = self._magento_find_move("magento_credit_memo_id", credit_id)
        if existing_move:
            self._magento_release_invoice_user(existing_move)
            self._magento_update_credit_memo_amounts(existing_move, credit_data)
            if existing_move.state == "draft":
                existing_move.with_context(skip_magento_sync=True).action_post()
            return existing_move

        line_vals, missing_items = self._magento_document_line_vals(credit_data, "credit_memo")
        if not any(v.get("sale_line_ids") for v in line_vals):
            raise UserError(
                _("Magento credit memo %(memo)s: no item could be mapped to a line of order %(order)s.")
                % {"memo": credit_data.get("increment_id") or credit_id, "order": self.name}
            )
        if missing_items:
            _logger.warning(
                "Magento credit memo %s for order %s has unmapped items: %s",
                credit_id,
                self.name,
                ",".join(missing_items),
            )
        vals = self._magento_document_move_vals(credit_data, "credit_memo", line_vals)
        vals["magento_credit_memo_id"] = str(credit_id)
        if credit_data.get("created_at"):
            vals["magento_credit_memo_created_at"] = fields.Datetime.to_datetime(
                credit_data.get("created_at")
            )
        if credit_data.get("invoice_id"):
            invoice = self._magento_find_move("magento_invoice_id", credit_data["invoice_id"])
            if invoice and invoice.state == "posted":
                vals["reversed_entry_id"] = invoice.id
        move = self.env["account.move"].with_context(
            default_move_type="out_refund",
            skip_magento_sync=True,
        ).create(vals)
        self._magento_update_credit_memo_amounts(move, credit_data)
        move.with_context(skip_magento_sync=True).action_post()
        self._magento_check_document_total(move, credit_data, _("credit memo"))
        return move

    def _magento_update_credit_memo_amounts(self, move, credit_data):
        """Persist Magento credit memo totals on account.move for display consistency."""
        if not move or not credit_data:
            return
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

    def _magento_sync_credit_memos(self, api=None, settle=False):
        """Import the Magento credit memos of the order. ``settle``: also settle each
        refunded credit note (against open invoices, then with a refund payment)."""
        self.ensure_one()
        if not self.magento_order_id or not self.magento_instance_id:
            return
        api = api or self._magento_api()
        try:
            credit_memos = api.get_credit_memos(order_id=self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        for memo in credit_memos or []:
            credit_id = memo.get("entity_id") or memo.get("id")
            if not credit_id:
                continue
            if memo.get("items"):
                credit_data = memo
            else:
                try:
                    credit_data = api.get_credit_memo(credit_id)
                except requests.exceptions.RequestException as exc:
                    self._magento_raise_http_error(exc)
            move = self._magento_create_credit_memo_from_magento(credit_data)
            if settle and move:
                self._magento_settle_credit_note(move, credit_data)

    def _magento_settle_credit_note(self, move, credit_data):
        """Make an imported, refunded Magento credit note paid in Odoo.

        It is first reconciled with the order's open invoices (refund before payment);
        what remains was paid back to the customer in Magento (credit memo state
        "refunded"), so a refund payment is registered on the payment method's journal.
        Idempotent: a settled credit note is left alone.
        """
        self.ensure_one()
        if not move or move.move_type != "out_refund" or move.state != "posted":
            return
        if move.payment_state in ("paid", "in_payment", "reversed"):
            return
        state = credit_data.get("state")
        if state not in (None, "", 2, "2"):
            return  # 1 = open (refund pending), 3 = canceled
        currency = move.currency_id
        invoices = self._magento_get_customer_invoices().filtered(
            lambda m: not m.currency_id.is_zero(m.amount_residual)
        )
        if invoices:
            receivables = (move.line_ids | invoices.line_ids).filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled
            )
            if len(receivables) > 1:
                receivables.with_context(skip_magento_sync=True).reconcile()
        if currency.is_zero(move.amount_residual):
            return
        instance = self.magento_instance_id
        journal, method = instance._magento_payment_config(
            self.magento_payment_method, self.company_id, outbound=True
        )
        if not journal or not method:
            _logger.warning(
                "Magento credit memo %s of order %s is refunded but instance %s has no refund "
                "journal/method.", move.magento_credit_memo_id, self.name, instance.display_name,
            )
            return
        self.env["account.payment.register"].with_context(
            active_model="account.move",
            active_ids=move.ids,
            dont_redirect_to_payments=True,
            skip_magento_sync=True,
        ).create({
            "journal_id": journal.id,
            "payment_method_line_id": method.id,
            "amount": move.amount_residual,
            "payment_date": move.invoice_date or fields.Date.context_today(self),
        }).action_create_payments()

    def _magento_get_customer_invoices(self):
        """Posted customer invoices of this order.

        ``invoice_ids`` depends on the sale line <-> invoice line link; also look the invoices
        up through the Magento order id + instance so payments are registered even when that
        link was lost (e.g. lines recreated by older versions of the connector).
        """
        self.ensure_one()
        moves = self.invoice_ids
        move_model = self.env["account.move"]
        moves |= move_model.search([
            ("move_type", "=", "out_invoice"),
            ("magento_sale_order_id", "=", self.id),
        ])
        if self.magento_order_id and self.magento_instance_id:
            moves |= move_model.search([
                ("move_type", "=", "out_invoice"),
                ("magento_instance_id", "=", self.magento_instance_id.id),
                ("magento_sale_order_id.magento_order_id", "=", self.magento_order_id),
            ])
        return moves.filtered(lambda m: m.move_type == "out_invoice" and m.state == "posted")

    def _magento_apply_payments_from_magento(self, force=False):
        """Register in Odoo what Magento reports as paid.

        ``force``: the order workflow asked for it (otherwise the instance's "Auto Register
        Magento Payments" decides). The journal comes from the payment method mapping,
        falling back to the instance default. Payments are dated like the invoice.
        """
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance or not (force or instance.auto_register_magento_payment):
            return
        if self.magento_payment_state not in ("partial", "paid"):
            return
        total_paid = float(self.magento_total_paid or 0.0)
        if total_paid <= 0:
            return

        invoices = self._magento_get_customer_invoices()
        if not invoices:
            return

        already_paid = sum(inv.amount_total - inv.amount_residual for inv in invoices)
        currency = self.currency_id or self.company_id.currency_id
        if currency.compare_amounts(already_paid, total_paid) >= 0:
            return

        journal, method = instance._magento_payment_config(self.magento_payment_method, self.company_id)
        if not journal or not method:
            _logger.warning(
                "Magento order %s is paid but instance %s has no payment journal/method configured.",
                self.name,
                instance.display_name,
            )
            return

        remaining = total_paid - already_paid
        for invoice in invoices.sorted(key=lambda m: (m.invoice_date or m.date, m.id)):
            if currency.compare_amounts(remaining, 0.0) <= 0:
                break
            if invoice.amount_residual <= 0:
                continue
            amount = min(invoice.amount_residual, remaining)
            wizard = self.env["account.payment.register"].with_context(
                active_model="account.move",
                active_ids=invoice.ids,
                dont_redirect_to_payments=True,
                skip_magento_sync=True,
            ).create({
                "journal_id": journal.id,
                "payment_method_line_id": method.id,
                "amount": amount,
                "payment_date": invoice.invoice_date or fields.Date.context_today(self),
            })
            wizard.action_create_payments()
            remaining -= amount

    # -------------------------
    # ORDER WORKFLOW STEPS (2.5)
    # -------------------------
    def _magento_wf_confirm(self):
        for order in self:
            if order.state in ("draft", "sent"):
                order.with_context(skip_magento_sync=True, magento_skip_push=True).action_confirm()

    def _magento_wf_cancel(self):
        for order in self:
            if order.state == "cancel":
                continue
            if order.locked:
                order.action_unlock()
            order.with_context(skip_magento_sync=True, magento_skip_push=True)._action_cancel()

    def _magento_wf_create_invoice(self):
        """Invoice in Odoo only what Magento did not invoice itself.

        Magento invoices are imported (and reused) by the invoice sync step; an Odoo invoice
        is created only for a confirmed order that Magento has not invoiced and that has no
        invoice yet. The workflow never pushes it to Magento.
        """
        for order in self:
            if order.state != "sale" or order.magento_invoice_state in ("partial", "invoiced"):
                continue
            existing = order.invoice_ids.filtered(
                lambda m: m.move_type == "out_invoice" and m.state != "cancel"
            )
            if existing or order._magento_get_customer_invoices():
                continue
            if not order._get_invoiceable_lines():
                continue
            moves = order.with_context(skip_magento_sync=True, magento_skip_push=True)._create_invoices()
            for move in moves:
                order._magento_release_invoice_user(move)

    def _magento_wf_post_invoices(self):
        for order in self:
            drafts = (order.invoice_ids | self.env["account.move"].search([
                ("magento_sale_order_id", "=", order.id),
            ])).filtered(lambda m: m.state == "draft" and m.move_type == "out_invoice")
            if drafts:
                drafts.with_context(skip_magento_sync=True).action_post()

    def _magento_wf_validate_delivery(self, api=None):
        """Validate the deliveries Magento shipped (imported Magento shipments only, so
        nothing is ever pushed back as a new Magento shipment)."""
        for order in self:
            if order.state != "sale":
                continue
            order._magento_sync_shipments(api)

    def _magento_wf_lock(self):
        for order in self:
            if order.state == "sale" and not order.locked:
                order.action_lock()

    # -------------------------
    # SHIPMENTS
    # -------------------------
    def _magento_find_picking_for_shipment(self):
        self.ensure_one()
        pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )
        if not pickings:
            return False
        done_candidates = pickings.filtered(lambda p: p.state == "done" and not p.magento_shipment_id)
        if done_candidates:
            return done_candidates.sorted(key=lambda p: (p.date_done or p.scheduled_date, p.id))[0]
        open_candidates = pickings.filtered(lambda p: p.state != "done" and not p.magento_shipment_id)
        if open_candidates:
            return open_candidates.sorted(key=lambda p: (p.scheduled_date or p.date, p.id))[0]
        return False

    def _magento_apply_shipment_quantities(self, picking, shipment_data):
        self.ensure_one()
        line_qty, _missing = self._magento_map_document_items(
            shipment_data.get("items"), ("qty", "qty_shipped")
        )
        if not line_qty:
            return False
        applied = False
        for move in picking.move_ids:
            if move.state == "cancel":
                continue
            sale_line = move.sale_line_id
            if not sale_line or sale_line.id not in line_qty:
                continue
            qty = line_qty[sale_line.id]
            if sale_line.product_uom_id and move.product_uom and sale_line.product_uom_id != move.product_uom:
                qty = sale_line.product_uom_id._compute_quantity(
                    qty, move.product_uom, rounding_method="HALF-UP"
                )
            move.quantity = qty
            if qty:
                move.picked = True
                applied = True
        return applied

    def _magento_find_picking_by_shipment(self, shipment_id):
        self.ensure_one()
        return self.env["stock.picking"].search([
            ("magento_shipment_id", "=", str(shipment_id)),
            ("magento_instance_id", "=", self.magento_instance_id.id),
        ], limit=1)

    def _magento_apply_shipment(self, shipment_data):
        self.ensure_one()
        shipment_id = shipment_data.get("entity_id") or shipment_data.get("id")
        if not shipment_id:
            return False
        shipment_id = str(shipment_id)
        if self._magento_find_picking_by_shipment(shipment_id):
            return False

        picking = self._magento_find_picking_for_shipment()
        if not picking:
            # Expected while the order is still a quotation in Odoo (no delivery yet).
            _logger.info(
                "No delivery to link Magento shipment %s of order %s.", shipment_id, self.name
            )
            return False

        if picking.state != "done":
            if self._magento_apply_shipment_quantities(picking, shipment_data):
                picking.with_context(
                    skip_magento_sync=True,
                    skip_sanity_check=True,
                )._action_done()

        picking.write({
            "magento_shipment_id": shipment_id,
            "magento_shipment_increment_id": shipment_data.get("increment_id") or "",
        })
        return picking

    def _magento_sync_shipments(self, api=None):
        self.ensure_one()
        if not self.magento_order_id or not self.magento_instance_id:
            return
        api = api or self._magento_api()
        try:
            shipments = api.get_shipments(order_id=self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        for shipment in shipments or []:
            shipment_id = shipment.get("entity_id") or shipment.get("id")
            if not shipment_id or self._magento_find_picking_by_shipment(shipment_id):
                continue
            if shipment.get("items"):
                shipment_data = shipment
            else:
                try:
                    shipment_data = api.get_shipment(shipment_id)
                except requests.exceptions.RequestException as exc:
                    self._magento_raise_http_error(exc)
            self._magento_apply_shipment(shipment_data)

    def _magento_push_shipments(self):
        """Push validated, not yet linked deliveries to Magento. Raises on failure."""
        for order in self:
            for picking in order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
                and p.state == "done"
                and not p.magento_shipment_id
            ):
                picking._magento_create_shipment()

    # -------------------------
    # IDEMPOTENT DOCUMENT PUSH (invoice / shipment / credit memo)
    # -------------------------
    # POST /V1/order/{id}/invoice|ship|refund is not idempotent: a retry after a
    # timeout can create a second document in Magento. Every push therefore
    # (1) locks the Odoo document row, (2) looks for a matching, unlinked Magento
    # document and links it instead of posting, (3) posts, and (4) on an ambiguous
    # failure (timeout, connection error, 5xx) looks again once before raising.
    def _magento_document_kinds(self):
        return {
            "invoice": {
                "label": _("invoice"),
                "model": "account.move",
                "field": "magento_invoice_id",
                "list": "get_invoices",
                "read": "get_invoice",
                "create": "create_invoice",
                "qty_keys": ("qty", "qty_invoiced"),
            },
            "shipment": {
                "label": _("shipment"),
                "model": "stock.picking",
                "field": "magento_shipment_id",
                "list": "get_shipments",
                "read": "get_shipment",
                "create": "create_shipment",
                "qty_keys": ("qty", "qty_shipped"),
            },
            "credit_memo": {
                "label": _("credit memo"),
                "model": "account.move",
                "field": "magento_credit_memo_id",
                "list": "get_credit_memos",
                "read": "get_credit_memo",
                "create": "create_credit_memo",
                "qty_keys": ("qty", "qty_refunded"),
            },
        }

    @api.model
    def _magento_lock_push_row(self, record):
        """``SELECT ... FOR UPDATE NOWAIT`` on the document row for the rest of the
        transaction, so two concurrent pushes of the same document cannot both POST."""
        record.ensure_one()
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    SQL(
                        "SELECT id FROM %s WHERE id = %s FOR UPDATE NOWAIT",
                        SQL.identifier(record._table),
                        record.id,
                    ),
                    log_exceptions=False,
                )
        except psycopg2.errors.LockNotAvailable as exc:
            raise UserError(
                _("A Magento push for %s is already running. Try again in a moment.")
                % record.display_name
            ) from exc

    @api.model
    def _magento_is_ambiguous_push_error(self, exc):
        """True when a POST may have been applied by Magento although it failed."""
        if isinstance(exc, requests.exceptions.SSLError):
            return False  # the TLS handshake failed: nothing was sent
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return False  # never connected
        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            response = exc.response
            return response is None or response.status_code >= 500
        return False

    @api.model
    def _magento_payload_item_qtys(self, items):
        """{order item id (str): qty} of a push payload (``items`` list)."""
        qtys = defaultdict(float)
        for item in items or []:
            key = str(item.get("order_item_id") or "")
            qty = _to_float(item.get("qty"))
            if key and qty > 0:
                qtys[key] += qty
        return dict(qtys)

    def _magento_document_item_qtys(self, items, qty_keys):
        """{push item id (str): qty} of a Magento document, keyed like a push payload.

        Each item is mapped to its sale line; the line's ``_magento_push_item_id()``
        (configurable parent id when there is one) is the key. A configurable parent
        and its child map to one line and count once. Items without a sale line keep
        their own id, so they make the document differ from any Odoo payload.
        """
        self.ensure_one()
        per_line = {}
        unmapped = defaultdict(float)
        for item in items or []:
            order_item_id = item.get("order_item_id") or item.get("item_id")
            if not order_item_id:
                continue
            qty = 0.0
            for qty_key in qty_keys:
                if item.get(qty_key) not in (None, ""):
                    qty = _to_float(item.get(qty_key))
                    break
            if qty <= 0:
                continue
            line = self._magento_line_for_item(order_item_id)
            if not line:
                unmapped[str(order_item_id)] += qty
                continue
            per_line[line] = max(per_line.get(line, 0.0), qty)
        qtys = defaultdict(float, unmapped)
        for line, qty in per_line.items():
            qtys[str(line._magento_push_item_id())] += qty
        return dict(qtys)

    @api.model
    def _magento_same_item_qtys(self, left, right):
        if set(left) != set(right):
            return False
        return all(
            float_compare(left[key], right[key], precision_digits=4) == 0 for key in left
        )

    @api.model
    def _magento_document_sort_key(self, doc):
        doc_id = doc.get("entity_id") or doc.get("id") or 0
        try:
            doc_id = int(doc_id)
        except (TypeError, ValueError):
            doc_id = 0
        return (str(doc.get("created_at") or ""), doc_id)

    def _magento_find_matching_document(self, api, kind, record, payload, amount=None, currency=None):
        """Most recent Magento document of ``kind`` on this order that matches ``payload``
        and is not linked to any Odoo document of the instance, or ``None``.

        Match: same {push item id: qty} after mapping the Magento items through the sale
        lines; for credit memos also the same grand total (``currency`` rounding).
        Raises ``requests`` exceptions from the GET.
        """
        self.ensure_one()
        spec = self._magento_document_kinds()[kind]
        docs = getattr(api, spec["list"])(order_id=self.magento_order_id) or []
        docs = [d for d in docs if isinstance(d, dict) and (d.get("entity_id") or d.get("id"))]
        if not docs:
            return None
        doc_ids = [str(d.get("entity_id") or d.get("id")) for d in docs]
        Model = self.env[spec["model"]].sudo().with_context(active_test=False)
        linked = Model.search([
            (spec["field"], "in", doc_ids),
            ("magento_instance_id", "=", self.magento_instance_id.id),
            ("id", "!=", record.id),
        ])
        linked_ids = set(linked.mapped(spec["field"]))
        wanted = self._magento_payload_item_qtys(payload.get("items"))
        for doc in sorted(docs, key=self._magento_document_sort_key, reverse=True):
            doc_id = str(doc.get("entity_id") or doc.get("id"))
            if doc_id in linked_ids:
                continue
            if "items" not in doc:
                doc = getattr(api, spec["read"])(doc_id) or {}
            found = self._magento_document_item_qtys(doc.get("items"), spec["qty_keys"])
            if not self._magento_same_item_qtys(wanted, found):
                continue
            if amount is not None:
                total = doc.get("grand_total")
                if total in (None, ""):
                    total = doc.get("base_grand_total")
                currency = currency or self.currency_id
                if currency.compare_amounts(abs(_to_float(total)), abs(amount)) != 0:
                    continue
            return doc
        return None

    def _magento_push_document(self, kind, record, payload, amount=None, currency=None):
        """Idempotently create a Magento document of ``kind`` for ``record``.

        Returns ``(magento_id, magento_doc)``: ``magento_doc`` is the matching Magento
        document when an existing one was linked, ``None`` when it was POSTed
        (``magento_id`` may then be ``None`` if Magento returned no id). Raises
        ``UserError`` on failure; a retry is safe.
        """
        self.ensure_one()
        spec = self._magento_document_kinds()[kind]
        self._magento_lock_push_row(record)
        api = MagentoAPI(self.magento_instance_id.sudo())

        def find():
            return self._magento_find_matching_document(
                api, kind, record, payload, amount=amount, currency=currency
            )

        try:
            match = find()
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        if match:
            doc_id = str(match.get("entity_id") or match.get("id"))
            _logger.info(
                "Magento %s %s already exists for order %s: linked to %s without POST.",
                kind, doc_id, self.name, record.display_name,
            )
            return doc_id, match

        try:
            result = getattr(api, spec["create"])(self.magento_order_id, payload)
        except requests.exceptions.RequestException as exc:
            if not self._magento_is_ambiguous_push_error(exc):
                self._magento_raise_http_error(exc)
            _logger.warning(
                "Magento %s POST for order %s failed with an unknown outcome (%s); re-checking.",
                kind, self.name, exc,
            )
            try:
                match = find()
            except requests.exceptions.RequestException as check_exc:
                _logger.warning("Magento %s re-check for order %s failed: %s", kind, self.name, check_exc)
                match = None
            if match:
                doc_id = str(match.get("entity_id") or match.get("id"))
                _logger.info(
                    "Magento %s %s was created despite the error; linked to %s.",
                    kind, doc_id, record.display_name,
                )
                return doc_id, match
            raise UserError(
                _(
                    "Magento did not confirm the %(doc)s for order %(order)s and it was not "
                    "found in Magento afterwards (%(error)s). Retrying is safe."
                )
                % {"doc": spec["label"], "order": self.name, "error": self._magento_http_error_message(exc)}
            ) from exc

        if isinstance(result, dict):
            result = result.get("entity_id") or result.get("id")
        if result in (None, "", False):
            return None, None
        return str(result), None

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
        email = partner.email or self.partner_id.email or self.magento_customer_email or ""
        if include_email and not email:
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
            address["email"] = email
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
        for line in self.order_line.filtered(lambda l: not l.display_type):
            sku_candidates = self._magento_get_line_sku_candidates(line)
            if not sku_candidates:
                raise UserError(
                    _("Order line '%s' has no SKU; it cannot be sent to Magento.")
                    % (line.name or line.display_name)
                )
            qty = float(line.product_uom_qty or 0.0)
            if qty <= 0:
                raise UserError(_("Each order line must have a quantity greater than 0."))
            add_error = None
            added = False
            attempted_skus = []
            idx = 0
            while idx < len(sku_candidates):
                sku = sku_candidates[idx]
                idx += 1
                attempted_skus.append(sku)
                try:
                    api.add_guest_cart_item(cart_id, sku, qty)
                except requests.exceptions.HTTPError as exc:
                    add_error = exc
                    if self._magento_is_not_saleable_error(exc):
                        raise UserError(
                            _(
                                "Magento SKU '%(sku)s' (order line '%(line)s') is not saleable: "
                                "%(error)s Check its status, website assignment and salable "
                                "quantity in Magento."
                            )
                            % {
                                "sku": sku,
                                "line": line.name or line.display_name,
                                "error": self._magento_http_error_message(exc),
                            }
                        ) from exc
                    if self._magento_should_try_next_sku(exc):
                        status = exc.response.status_code if exc.response is not None else 0
                        # The SKU may have been renamed in Magento: fetch it by Magento id.
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
                added = True
                if line.magento_sku != sku:
                    line.magento_sku = sku
                if line.magento_product_id and line.magento_product_id.sku != sku:
                    line.magento_product_id.with_context(
                        skip_magento_sync=True,
                        skip_odoo_sync=True,
                    ).write({"sku": sku})
                break
            if added:
                continue
            if add_error is not None and self._magento_should_try_next_sku(add_error):
                raise UserError(
                    _(
                        "No Magento product found for order line '%(line)s'. "
                        "Tried SKU(s): %(skus)s. %(error)s"
                    )
                    % {
                        "line": line.name or line.display_name,
                        "skus": ", ".join(attempted_skus or sku_candidates),
                        "error": self._magento_http_error_message(add_error),
                    }
                )
            if add_error is not None:
                self._magento_raise_http_error(add_error)

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

    def _magento_refresh_from_magento(self, api, magento_id):
        self.ensure_one()
        if not magento_id:
            return
        try:
            data = api.get_order(magento_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        values = self._magento_prepare_order_vals(data, self.magento_instance_id, order=self)
        self.write(values)
        self._magento_sync_order_lines(data)

    def _magento_push_status(self, status, comment, api=None):
        """Set the Magento order status through an order comment.

        ``PUT /V1/orders/{id}`` is Magento's order *address* endpoint, so status changes are
        pushed with ``POST /V1/orders/{id}/comments`` instead.
        """
        self.ensure_one()
        api = api or self._magento_api()
        api.add_order_comment(self.magento_order_id, comment=comment, status=status, notify=False)
        self.magento_status = status

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
        if self.magento_order_id:
            raise UserError(_("This order is already linked to Magento order %s.")
                            % (self.magento_increment_id or self.magento_order_id))
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
        api = MagentoAPI(instance.sudo())
        try:
            response = self._magento_create_order_via_cart(api)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        if isinstance(response, dict):
            values = self._magento_prepare_order_vals(response, instance, order=self)
            values["magento_exported_from_odoo"] = True
            self.write(values)
            # Link the Odoo lines to the Magento items; Magento's cart prices never
            # overwrite the Odoo prices.
            self._magento_link_exported_lines(response)
        return self._magento_rainbow_man_action(_("Order created in Magento."))

    def action_update_magento_order(self):
        self.ensure_one()
        if not self.magento_order_id:
            raise UserError(_("Magento Order ID is required to update an order in Magento."))
        if not self.magento_instance_id:
            raise UserError(_("Magento Instance is required."))
        status = (self.magento_status or "").strip()
        if not status:
            raise UserError(_("Set the Magento status to push first."))
        api = self._magento_api()
        try:
            self._magento_push_status(
                status,
                _("Status set to '%(status)s' from Odoo by %(user)s.")
                % {"status": status, "user": self.env.user.name},
                api=api,
            )
            self._magento_refresh_from_magento(api, self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        return self._magento_rainbow_man_action(_("Order updated in Magento."))

    def action_pull_magento_order(self):
        self.ensure_one()
        if not self.magento_order_id:
            raise UserError(_("Magento Order ID is required to pull order from Magento."))
        instance = self.magento_instance_id
        if not instance:
            raise UserError(_("Magento Instance is required."))
        api = self._magento_api()
        try:
            data = api.get_order(self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        values = self._magento_prepare_order_vals(data, instance, order=self)
        if values.get("magento_invoice_state") == "not_invoiced":
            try:
                invoices = api.get_invoices(order_id=self.magento_order_id)
            except requests.exceptions.RequestException:
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
        # Same follow-up steps (documents + order workflow) as the scheduled import.
        stats = _SyncStats("order")
        failures = instance._sync_order_followups(api, self, self.magento_order_id, stats)
        instance._magento_notify_record_result(
            "order", self.magento_order_id, not failures,
            exc=failures[0][1] if failures else None, payload=data, record=self,
        )
        if failures:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Magento"),
                    "message": _("Order pulled, but %(step)s failed: %(error)s",
                                 step=failures[0][0], error=failures[0][1]),
                    "type": "warning",
                    "sticky": True,
                },
            }
        return self._magento_rainbow_man_action(_("Order pulled from Magento."))


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    magento_item_id = fields.Char("Magento Item ID", readonly=True, copy=False, index=True)
    magento_parent_item_id = fields.Char(
        "Magento Parent Item ID",
        readonly=True,
        copy=False,
        help="Item id of the configurable parent in Magento. The line is the configurable's "
             "simple child; Magento invoices/shipments reference the parent item.",
    )
    magento_product_id = fields.Many2one(
        "magento.product.map",
        string="Magento Product",
        ondelete="set null",
    )
    magento_sku = fields.Char("Magento SKU")
    magento_line_role = fields.Selection(
        [("product", "Product"), ("shipping", "Shipping"), ("discount", "Discount"), ("fpt", "FPT")],
        string="Magento Line Role", readonly=True, copy=False,
    )
    magento_line_key = fields.Char(
        "Magento Line Key", readonly=True, copy=False, index="btree_not_null",
        help="Stable key of an imported line (item id, 'shipping', 'discount:<item>'...) used to "
             "update quotation lines in place on re-sync.",
    )

    def _magento_push_item_id(self):
        """Magento order item id to send in invoices/shipments/credit memos created in Odoo."""
        self.ensure_one()
        return self.magento_parent_item_id or self.magento_item_id

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
