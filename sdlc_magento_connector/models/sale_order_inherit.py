import logging
from collections import defaultdict

import requests
from markupsafe import Markup, escape

from odoo import Command, api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import email_normalize, float_compare

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)

# Odoo delivery_type values that are also valid Magento carrier codes.
MAGENTO_CARRIER_CODES = ("ups", "usps", "fedex", "dhl")


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
        vals = {
            "magento_instance_id": instance.id if instance else False,
            "magento_order_id": magento_id,
            "magento_increment_id": data.get("increment_id") or "",
            "magento_status": data.get("status") or "",
            "magento_currency": currency_code,
            "magento_total_invoiced": total_invoiced,
            "magento_invoice_state": invoice_state,
            "magento_total_paid": total_paid,
            "magento_payment_state": payment_state,
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
        qty = _to_float(item.get("qty_ordered") or item.get("qty"))
        return {
            "product_id": product.id,
            "product_uom_id": product.uom_id.id,
            "name": item.get("name") or product.display_name or sku,
            "product_uom_qty": qty or 1.0,
            "price_unit": _to_float(item.get("price")),
            "magento_item_id": str(item.get("item_id") or ""),
            "magento_parent_item_id": item.get("_magento_parent_item_id") or False,
            "magento_product_id": product_map.id if product_map else False,
            "magento_sku": sku,
        }

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

        Draft/sent orders: lines are upserted by Magento item id (stable line ids, so the
        sale line <-> invoice line links survive re-syncs). Any other state: lines are never
        touched; differences are flagged with ``magento_sync_conflict``.
        """
        self.ensure_one()
        items = self._magento_effective_items(data.get("items"))
        if not items:
            return
        if self.state in ("draft", "sent"):
            self._magento_write_order_lines(items)
            self._magento_set_conflict([])
        else:
            self._magento_set_conflict(self._magento_compare_order_lines(items))

    def _magento_write_order_lines(self, items):
        self.ensure_one()
        instance = self.magento_instance_id
        keep_price = self.magento_exported_from_odoo
        existing = {
            line.magento_item_id: line
            for line in self.order_line
            if line.magento_item_id and not line.display_type
        }
        commands = []
        seen = set()
        for item in items:
            vals = self._magento_prepare_order_line_vals(item, instance)
            if keep_price:
                vals.pop("price_unit")
            key = vals["magento_item_id"]
            line = existing.get(key) if key else None
            if not line:
                commands.append(Command.create(vals))
                continue
            seen.add(key)
            changed = {
                fname: value
                for fname, value in vals.items()
                if fname != "magento_item_id" and self._magento_line_value_differs(line, fname, value)
            }
            if changed:
                # Always rewrite qty and price together so Odoo's pricelist recompute
                # (triggered by product/qty changes) can't override the Magento price.
                # Exported orders keep their Odoo price.
                changed.setdefault("product_uom_qty", vals["product_uom_qty"])
                changed.setdefault("price_unit", line.price_unit if keep_price else vals["price_unit"])
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
        if fname == "product_uom_qty":
            rounding = line.product_uom_id.rounding or 0.0001
            return float_compare(current, value or 0.0, precision_rounding=rounding) != 0
        if fname == "price_unit":
            currency = line.currency_id or line.order_id.currency_id or self.env.company.currency_id
            return currency.compare_amounts(current, value or 0.0) != 0
        return (current or False) != (value or False)

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

    def _magento_create_invoice_from_magento(self, invoice_data):
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
                return match

        line_qty, missing_items = self._magento_map_document_items(
            invoice_data.get("items"), ("qty", "qty_invoiced", "qty_ordered")
        )
        if not line_qty:
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
        sale_lines = self.env["sale.order.line"].browse(list(line_qty))
        line_vals = [
            Command.create(line._prepare_invoice_line(quantity=line_qty[line.id]))
            for line in sale_lines
        ]

        invoice_date = invoice_data.get("created_at")
        if invoice_date:
            invoice_date = fields.Date.to_date(fields.Datetime.to_datetime(invoice_date))

        vals = {
            "move_type": "out_invoice",
            "partner_id": (self.partner_invoice_id or self.partner_id).id,
            "partner_shipping_id": self.partner_shipping_id.id,
            "invoice_origin": self.name,
            "invoice_date": invoice_date or False,
            "invoice_user_id": False,
            "currency_id": self.currency_id.id,
            "company_id": self.company_id.id,
            "invoice_line_ids": line_vals,
            "ref": invoice_data.get("increment_id") or "",
            "magento_invoice_id": str(invoice_id),
        }
        move = self.env["account.move"].with_context(
            default_move_type="out_invoice",
            skip_magento_sync=True,
        ).create(vals)
        move.with_context(skip_magento_sync=True).action_post()
        return move

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

        line_qty, missing_items = self._magento_map_document_items(
            credit_data.get("items"), ("qty", "qty_refunded")
        )
        if not line_qty:
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
        sale_lines = self.env["sale.order.line"].browse(list(line_qty))
        line_vals = [
            Command.create(line._prepare_invoice_line(quantity=line_qty[line.id]))
            for line in sale_lines
        ]

        credit_date = credit_data.get("created_at")
        if credit_date:
            credit_date = fields.Date.to_date(fields.Datetime.to_datetime(credit_date))

        vals = {
            "move_type": "out_refund",
            "partner_id": (self.partner_invoice_id or self.partner_id).id,
            "invoice_origin": self.name,
            "invoice_date": credit_date or False,
            "invoice_user_id": False,
            "currency_id": self.currency_id.id,
            "company_id": self.company_id.id,
            "invoice_line_ids": line_vals,
            "ref": credit_data.get("increment_id") or "",
            "magento_credit_memo_id": str(credit_id),
        }
        if credit_data.get("created_at"):
            vals["magento_credit_memo_created_at"] = fields.Datetime.to_datetime(
                credit_data.get("created_at")
            )
        move = self.env["account.move"].with_context(
            default_move_type="out_refund",
            skip_magento_sync=True,
        ).create(vals)
        self._magento_update_credit_memo_amounts(move, credit_data)
        move.with_context(skip_magento_sync=True).action_post()
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

    def _magento_sync_credit_memos(self, api=None):
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
            self._magento_create_credit_memo_from_magento(credit_data)

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

    def _magento_apply_payments_from_magento(self):
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance or not instance.auto_register_magento_payment:
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

        journal, method = instance._get_magento_payment_config(self.company_id)
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
            })
            wizard.action_create_payments()
            remaining -= amount

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
        if self.magento_invoice_state in ("partial", "invoiced"):
            self._magento_sync_invoices(api)
        if self.magento_payment_state in ("partial", "paid"):
            self._magento_apply_payments_from_magento()
        self._magento_sync_credit_memos(api)
        self._magento_sync_shipments(api)
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
