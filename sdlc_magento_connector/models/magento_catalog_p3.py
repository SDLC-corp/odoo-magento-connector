"""Phase 3 catalog: tier / customer-group price import (roadmap 3.4) and bundle / grouped
product export (roadmap 3.5).

Tier price import (Magento -> Odoo)
-----------------------------------
Magento tier prices (``POST /V1/products/tier-prices-information``, or the ``tier_prices``
of a product payload during the product sync) become ``product.pricelist.item`` rows:

* ``ALL GROUPS`` -> the instance's tier pricelist (``magento_tier_pricelist_id``,
  "Magento tier prices", created on the first import);
* a customer group -> that ``magento.customer.group``'s pricelist; when the group has none
  and "Create Group Pricelists" is on, a "Magento <group>" pricelist is created (it falls
  back to the tier pricelist for everything else) and set on the group. Otherwise the tier
  is skipped and counted.

Each item applies to the product variant of the SKU (``0_product_variant``), from
``min_quantity`` = the tier quantity, at a fixed price (``price_type`` fixed) or as a
percentage discount (``price_type`` discount). Tiers of website 0 and of the instance's
website are read; for the same group and quantity the website's own tier wins.

Ownership and idempotency
    ``magento.tier.price.import`` records which item the import owns, per SKU, customer
    group and quantity. A re-import updates only what changed, and removes the items (and
    the records) of tiers gone from Magento. An existing item with the same pricelist,
    variant and minimum quantity (and no dates) is adopted instead of duplicated.

No ping-pong with the price export (roadmap 2.9)
    The export marks an entry priced by an owned item ``magento_owned`` and neither sends
    nor withdraws it (see ``magento_pricing``). When an imported tier disappears, the
    export's push states for the same key are dropped too: Magento no longer has it, so
    the export must not delete it again.

Bundle and grouped export (Odoo -> Magento)
-------------------------------------------
A product template flagged "Export as Magento" bundle or grouped (or a product binding of
that type) is exported with its components through the product save:

* components come from the product's kit (an ``mrp`` phantom BoM the user made: the BoM
  the bundle import itself creates, code "Magento <sku>", is not a source) when there is
  one, else from the binding's ``magento.product.link`` rows;
* a kit becomes one required "select" option per component (the component is the
  default, its quantity fixed); links keep their option title / type / required flag,
  default, selection price and quantity settings;
* grouped: ``product_links`` of type ``associated`` with the quantity in
  ``extension_attributes.qty`` (the product's other links are sent back unchanged,
  since Magento replaces every link type on save);
* bundle: ``extension_attributes.bundle_product_options`` and the dynamic / fixed price
  type (``price_type`` 0 / 1, with dynamic SKU and weight).

Every component SKU must exist in Magento (one search per 100 SKUs); the export stops
with the list of the missing ones. Before writing, the Magento product is read: when its
options / links equal ours and the rest of the payload is the one last sent (fingerprint
``magento_composite_hash``), nothing is written. Changed options are replaced as a whole.
Configurable export stays blocked.
"""
import hashlib
import json
import logging
from collections import Counter, OrderedDict

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare

from ..services.magento_api import MagentoAPI
from .magento_pricing import ALL_GROUPS, _PRICED_TYPES

_logger = logging.getLogger(__name__)

COMPOSITE_EXPORT_TYPES = ("bundle", "grouped")
# Magento's "all groups" customer group id in a product payload's tier_prices.
ALL_GROUPS_ID = 32000
# SKUs per tier price request / queued job.
TIER_IMPORT_PAGE = 100
TIER_PRICE_TYPES = [("fixed", "Fixed Price"), ("discount", "Discount (%)")]
BUNDLE_OPTION_TYPES = ("select", "radio", "checkbox", "multi")
# Component types Magento accepts in a grouped product / a bundle option.
_GROUPED_CHILD_TYPES = ("simple", "virtual", "downloadable")
_BUNDLE_CHILD_TYPES = ("simple", "virtual")
_QUIET_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _is_all_groups(code):
    return str(code or "").strip().upper() == ALL_GROUPS


def _group_key(code):
    return ALL_GROUPS if _is_all_groups(code) else str(code or "").strip().lower()


def _qty_key(qty):
    return round(_to_float(qty), 4)


# ======================================================================
# Imported tier prices
# ======================================================================
class MagentoTierPriceImport(models.Model):
    _name = "magento.tier.price.import"
    _description = "Magento Imported Tier Price"
    _order = "instance_id, sku, customer_group, quantity"
    _rec_name = "sku"

    _uniq_instance_tier = models.UniqueIndex(
        "(instance_id, sku, customer_group, quantity)",
        "This Magento tier price was already imported for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    sku = fields.Char(string="SKU", required=True, index=True)
    map_id = fields.Many2one("magento.product.map", string="Product Mapping", ondelete="set null")
    product_id = fields.Many2one("product.product", string="Product", ondelete="cascade", index=True)
    website_id = fields.Integer(string="Website ID")
    customer_group = fields.Char(required=True, help="Magento customer group code, or ALL GROUPS.")
    quantity = fields.Float(digits="Product Unit")
    price_type = fields.Selection(TIER_PRICE_TYPES, required=True, default="fixed")
    value = fields.Float(help="Price per unit (fixed) or discount percentage (discount).")
    pricelist_id = fields.Many2one("product.pricelist", string="Pricelist", ondelete="cascade", index=True)
    item_id = fields.Many2one(
        "product.pricelist.item", string="Pricelist Item", ondelete="cascade", index=True,
        help="Item owned by the import: updated or removed with the Magento tier.",
    )
    imported_at = fields.Datetime(string="Imported")


class ProductPricelistItem(models.Model):
    _inherit = "product.pricelist.item"

    magento_tier_import_ids = fields.One2many("magento.tier.price.import", "item_id", string="Magento Tier")
    magento_tier_imported = fields.Boolean(
        string="From Magento", compute="_compute_magento_tier_imported",
        help="Imported from a Magento tier price: kept in step by the tier price import and "
             "never pushed back by the price export.",
    )

    @api.depends("magento_tier_import_ids")
    def _compute_magento_tier_imported(self):
        for item in self:
            item.magento_tier_imported = bool(item.magento_tier_import_ids)


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    import_tier_prices = fields.Boolean(
        string="Import Tier Prices with Products", default=False,
        help="During the product sync, also import each product's tier prices into "
             "pricelists. 'Import Tier Prices' does it for every product on demand.",
    )
    magento_tier_pricelist_id = fields.Many2one(
        "product.pricelist", string="Tier Price Pricelist", ondelete="set null",
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Pricelist of the ALL GROUPS tier prices. Created ('Magento tier prices') on "
             "the first import when empty.",
    )
    magento_tier_group_pricelists = fields.Boolean(
        string="Create Group Pricelists", default=False,
        help="Tier prices of a customer group go to that group's pricelist. When the group "
             "has none: on, a 'Magento <group>' pricelist is created and set on the group; "
             "off, the group's tier prices are skipped.",
    )
    tier_import_count = fields.Integer(string="Imported Tier Prices", compute="_compute_tier_import_count")

    def _compute_tier_import_count(self):
        counts = dict(self.env["magento.tier.price.import"]._read_group(
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"],
        ))
        for instance in self:
            instance.tier_import_count = counts.get(instance, 0)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _magento_check_tier_manager(self):
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can import tier prices."))

    def action_import_tier_prices(self):
        """Button: import the tier prices of every mapped product (queued, one job per
        page of SKUs, unless the instance runs its syncs in the foreground)."""
        self.ensure_one()
        self._magento_check_tier_manager()
        if self._magento_sync_in_background():
            job = self.env["magento.job"].enqueue(
                self, "_magento_enqueue_tier_import", instance=self,
                channel=self._magento_sync_channel("prices"), priority=20,
                identity_key="tier-import-%s" % self.id,
                description=_("Magento tier prices (%s): list", self.name),
            )
            message = _("Queued: tier price import (job %s), follow progress in Job Queue.", job.id)
        else:
            message = self._magento_import_tier_prices()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Tier Prices"), "message": message, "type": "info", "sticky": False},
        }

    def action_view_tier_imports(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_tier_price_import"
        )
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"default_instance_id": self.id}
        return action

    # ------------------------------------------------------------------
    # Job bodies
    # ------------------------------------------------------------------
    def _magento_tier_maps(self, skus=None):
        """Mapped priced products of the instance (their SKU carries tier prices)."""
        self.ensure_one()
        domain = [
            ("instance_id", "=", self.id), ("odoo_product_id", "!=", False),
            ("sku", "!=", False), ("sku", "!=", ""),
            "|", ("type_id", "in", _PRICED_TYPES), ("type_id", "=", False),
        ]
        if skus is not None:
            domain.append(("sku", "in", list(skus)))
        return self.env["magento.product.map"].sudo().search(domain, order="id")

    def _magento_enqueue_tier_import(self):
        """Job body: drop the tiers of unmapped SKUs, queue one job per page of SKUs."""
        self.ensure_one()
        skus = self._magento_tier_maps().mapped("sku")
        removed = self._magento_tier_drop_unmapped(skus)
        jobs = self.env["magento.job"]
        for start in range(0, len(skus), TIER_IMPORT_PAGE):
            chunk = skus[start:start + TIER_IMPORT_PAGE]
            jobs |= jobs.enqueue(
                self, "_magento_import_tier_prices_page", args=(chunk,), instance=self,
                channel=self._magento_sync_channel("prices"), priority=20,
                identity_key="tier-import-%s-%s" % (self.id, chunk[0]),
                description=_("Magento tier prices (%(name)s): %(count)s SKU(s) from %(sku)s",
                              name=self.name, count=len(chunk), sku=chunk[0]),
            )
        return _("%(skus)s SKU(s) queued in %(jobs)s job(s); %(removed)s tier(s) of unmapped SKUs removed",
                 skus=len(skus), jobs=len(jobs), removed=removed)

    def _magento_import_tier_prices(self):
        """Synchronous import of every page; returns the summary."""
        self.ensure_one()
        stats = Counter()
        skus = self._magento_tier_maps().mapped("sku")
        stats["removed"] += self._magento_tier_drop_unmapped(skus)
        api = MagentoAPI(self)
        for start in range(0, len(skus), TIER_IMPORT_PAGE):
            self._magento_import_tier_prices_page(skus[start:start + TIER_IMPORT_PAGE], stats=stats, api=api)
        return self._magento_tier_summary(stats)

    def _magento_import_tier_prices_page(self, skus, stats=None, api=None):
        """Job body: import the tier prices of ``skus`` (one Magento request)."""
        self.ensure_one()
        stats = Counter() if stats is None else stats
        skus = [s for s in skus or () if s]
        if not skus:
            return self._magento_tier_summary(stats)
        api = api or MagentoAPI(self)
        rows = [self._magento_tier_row(row) for row in api.get_tier_price_rows(skus)]
        self._magento_apply_tier_rows(skus, [row for row in rows if row], stats)
        return self._magento_tier_summary(stats)

    def _magento_tier_summary(self, stats):
        parts = [
            _("%s created", stats["created"]),
            _("%s updated", stats["updated"]),
            _("%s unchanged", stats["unchanged"]),
            _("%s removed", stats["removed"]),
        ]
        if stats["skipped"]:
            parts.append(_("%s skipped", stats["skipped"]))
        if stats["other_website"]:
            parts.append(_("%s of other websites ignored", stats["other_website"]))
        summary = _("Tier prices: %s", ", ".join(parts))
        reasons = sorted(k[len("skip:"):] for k in stats if isinstance(k, str) and k.startswith("skip:"))
        if reasons:
            summary += " (%s)" % "; ".join(reasons)
        return summary

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _magento_tier_row(self, row, sku=None):
        """Normalised tier price ``{sku, website_id, customer_group, quantity, price_type,
        value}`` of a ``tier-prices-information`` row, or of a product payload's
        ``tier_prices`` entry (``sku`` given); None when unusable."""
        self.ensure_one()
        if not isinstance(row, dict):
            return None
        if "customer_group_id" in row or "qty" in row:  # product payload shape
            ext = row.get("extension_attributes") or {}
            group_id = _to_int(row.get("customer_group_id"), -1)
            if group_id == ALL_GROUPS_ID:
                code = ALL_GROUPS
            else:
                group = self.env["magento.customer.group"].sudo().search([
                    ("instance_id", "=", self.id), ("magento_group_id", "=", group_id),
                ], limit=1)
                code = group.name if group else "#%s" % group_id
            percentage = ext.get("percentage_value")
            discount = percentage not in (None, "", False)
            return {
                "sku": sku or row.get("sku"),
                "website_id": _to_int(ext.get("website_id")),
                "customer_group": code,
                "quantity": _to_float(row.get("qty")),
                "price_type": "discount" if discount else "fixed",
                "value": _to_float(percentage if discount else row.get("value")),
            }
        if not row.get("sku"):
            return None
        price_type = str(row.get("price_type") or "fixed").lower()
        return {
            "sku": row["sku"],
            "website_id": _to_int(row.get("website_id")),
            "customer_group": ALL_GROUPS if _is_all_groups(row.get("customer_group")) else str(
                row.get("customer_group") or "").strip(),
            "quantity": _to_float(row.get("quantity")),
            "price_type": "discount" if price_type == "discount" else "fixed",
            "value": _to_float(row.get("price")),
        }

    def _magento_apply_tier_rows(self, skus, rows, stats):
        """Make the owned items of ``skus`` exactly the tier ``rows`` (idempotent)."""
        self.ensure_one()
        maps = self._magento_tier_maps(skus)
        by_sku = {m.sku.lower(): m for m in maps}
        websites = {0, self.website_id or 0}
        desired = OrderedDict()
        for row in rows:
            product_map = by_sku.get(str(row.get("sku") or "").lower())
            if not product_map or not row.get("customer_group") or row["quantity"] <= 0:
                continue
            if row["website_id"] not in websites:
                stats["other_website"] += 1
                continue
            key = (product_map.sku, _group_key(row["customer_group"]), _qty_key(row["quantity"]))
            current = desired.get(key)
            if current and current["website_id"] and not row["website_id"]:
                continue  # the website's own tier wins over the global one
            desired[key] = dict(row, sku=product_map.sku, map=product_map)
        Tracker = self.env["magento.tier.price.import"].sudo()
        trackers = Tracker.search([("instance_id", "=", self.id), ("sku", "in", maps.mapped("sku"))])
        by_key = {(t.sku, _group_key(t.customer_group), _qty_key(t.quantity)): t for t in trackers}
        for key, tracker in by_key.items():
            if key not in desired:
                self._magento_tier_remove(tracker)
                stats["removed"] += 1
        cache = {}
        for key, row in desired.items():
            tracker = by_key.get(key, Tracker)
            pricelist = self._magento_tier_pricelist_for(row["customer_group"], cache, stats)
            if not pricelist:
                if tracker:
                    self._magento_tier_remove(tracker)
                    stats["removed"] += 1
                stats["skipped"] += 1
                continue
            stats[self._magento_tier_upsert(row, tracker, pricelist)] += 1

    # ------------------------------------------------------------------
    # Pricelists
    # ------------------------------------------------------------------
    def _magento_tier_new_pricelist(self, name):
        company = self.company_id or self.env.company
        return self.env["product.pricelist"].sudo().create({
            "name": name,
            "company_id": self.company_id.id or False,
            "currency_id": company.currency_id.id,
        })

    def _magento_tier_pricelist(self):
        """Pricelist of the ALL GROUPS tier prices (created on first use)."""
        self.ensure_one()
        if not self.magento_tier_pricelist_id:
            pricelist = self._magento_tier_new_pricelist(_("Magento tier prices"))
            self.sudo().write({"magento_tier_pricelist_id": pricelist.id})
        return self.magento_tier_pricelist_id

    def _magento_tier_pricelist_for(self, code, cache, stats):
        """Pricelist of the tier prices of customer group ``code``, or None (reason
        counted in ``stats``)."""
        self.ensure_one()
        key = _group_key(code)
        if key in cache:
            return cache[key]
        if key == ALL_GROUPS:
            cache[key] = self._magento_tier_pricelist()
            return cache[key]
        groups = self.env["magento.customer.group"].sudo().search([
            ("instance_id", "=", self.id), ("name", "=ilike", code),
        ])
        group = groups[:1]
        pricelist = None
        if not group:
            stats["skip:unknown customer group %s, sync customers first" % code] += 1
        elif group.pricelist_id:
            pricelist = group.pricelist_id
        elif self.magento_tier_group_pricelists:
            pricelist = self._magento_tier_new_pricelist(_("Magento %s", group.name))
            fallback = self._magento_tier_pricelist()
            self.env["product.pricelist.item"].sudo().create({
                "pricelist_id": pricelist.id,
                "applied_on": "3_global",
                "compute_price": "discount",
                "base": "pricelist",
                "base_pricelist_id": fallback.id,
            })
            group.write({"pricelist_id": pricelist.id})
        else:
            stats["skip:customer group %s has no pricelist" % group.name] += 1
        cache[key] = pricelist
        return pricelist

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    @api.model
    def _magento_tier_item_vals(self, row, product, pricelist):
        vals = {
            "pricelist_id": pricelist.id,
            "applied_on": "0_product_variant",
            "product_id": product.id,
            "product_tmpl_id": product.product_tmpl_id.id,
            "min_quantity": row["quantity"],
        }
        if row["price_type"] == "discount":
            vals.update({"compute_price": "discount", "price_discount": row["value"], "fixed_price": 0.0})
        else:
            vals.update({"compute_price": "fixed", "fixed_price": row["value"], "price_discount": 0.0})
        return vals

    @api.model
    def _magento_tier_item_changes(self, item, vals):
        digits = self.env["decimal.precision"].precision_get("Product Price")
        changed = {}
        for name, value in vals.items():
            current = item[name]
            if isinstance(current, models.BaseModel):
                if current.id != value:
                    changed[name] = value
            elif isinstance(current, float):
                precision = digits if name == "fixed_price" else 4
                if float_compare(current, value or 0.0, precision_digits=precision):
                    changed[name] = value
            elif current != value:
                changed[name] = value
        return changed

    def _magento_tier_upsert(self, row, tracker, pricelist):
        """Create, adopt or update the item of one tier; returns the stats key."""
        self.ensure_one()
        product = row["map"].odoo_product_id
        Item = self.env["product.pricelist.item"].sudo()
        vals = self._magento_tier_item_vals(row, product, pricelist)
        item = tracker.item_id if tracker and tracker.pricelist_id == pricelist else Item
        if tracker and tracker.item_id and tracker.pricelist_id != pricelist:
            tracker.item_id.unlink()  # the group moved to another pricelist
        outcome = "unchanged"
        if not item:
            owned = self.env["magento.tier.price.import"].sudo().search([
                ("pricelist_id", "=", pricelist.id), ("item_id", "!=", False),
            ]).item_id
            item = Item.search([
                ("pricelist_id", "=", pricelist.id), ("applied_on", "=", "0_product_variant"),
                ("product_id", "=", product.id), ("min_quantity", "=", row["quantity"]),
                ("date_start", "=", False), ("date_end", "=", False), ("id", "not in", owned.ids),
            ], limit=1)
            outcome = "updated" if item else "created"
        if item:
            changed = self._magento_tier_item_changes(item, vals)
            if changed:
                item.write(changed)
                outcome = "updated"
        else:
            item = Item.create(vals)
        tracker_vals = {
            "map_id": row["map"].id,
            "product_id": product.id,
            "website_id": row["website_id"],
            "price_type": row["price_type"],
            "value": row["value"],
            "pricelist_id": pricelist.id,
            "item_id": item.id,
        }
        if tracker:
            changed = {k: v for k, v in tracker_vals.items()
                       if (tracker[k].id if isinstance(tracker[k], models.BaseModel) else tracker[k]) != v}
            if changed:
                changed["imported_at"] = fields.Datetime.now()
                tracker.write(changed)
                if outcome == "unchanged":
                    outcome = "updated"
        else:
            tracker_vals.update({
                "instance_id": self.id, "sku": row["sku"], "customer_group": row["customer_group"],
                "quantity": row["quantity"], "imported_at": fields.Datetime.now(),
            })
            tracker.create(tracker_vals)
            if outcome == "unchanged":
                outcome = "updated"
        return outcome

    def _magento_tier_remove(self, trackers):
        """Remove imported tiers gone from Magento: their items, and the export's push
        states of the same keys (Magento no longer has them: nothing to delete there)."""
        States = self.env["magento.price.push.state"].sudo()
        for tracker in trackers:
            key = "tier|w%s|%s|%s" % (tracker.website_id, tracker.customer_group, "%g" % tracker.quantity)
            if tracker.pricelist_id:
                States.search([
                    ("rule_id.pricelist_id", "=", tracker.pricelist_id.id), ("sku", "=", tracker.sku),
                    ("kind", "=", "tier"), ("key", "=", key),
                ]).unlink()
            item = tracker.item_id
            tracker.unlink()
            item.exists().unlink()

    def _magento_tier_drop_unmapped(self, skus):
        """Remove the imported tiers of SKUs that are not mapped (priced) any more."""
        self.ensure_one()
        trackers = self.env["magento.tier.price.import"].sudo().search([
            ("instance_id", "=", self.id), ("sku", "not in", list(skus)),
        ])
        count = len(trackers)
        if trackers:
            self._magento_tier_remove(trackers)
        return count

    # ------------------------------------------------------------------
    # Product sync hook
    # ------------------------------------------------------------------
    def _magento_after_product_import(self, mapping, product_data):
        res = super()._magento_after_product_import(mapping, product_data)
        tiers = (product_data or {}).get("tier_prices")
        if (self.import_tier_prices and isinstance(tiers, list) and mapping.sku
                and mapping.odoo_product_id and (mapping.type_id or "simple") in _PRICED_TYPES):
            try:
                with self.env.cr.savepoint():
                    rows = [self._magento_tier_row(row, sku=mapping.sku) for row in tiers]
                    self._magento_apply_tier_rows([mapping.sku], [r for r in rows if r], Counter())
            except Exception as exc:  # noqa: BLE001 - tier prices never fail the product import
                _logger.warning("Magento product %s (instance %s): tier prices not imported: %s",
                                mapping.sku, self.name, exc)
        return res


# ======================================================================
# Bundle / grouped export
# ======================================================================
class ProductTemplate(models.Model):
    _inherit = "product.template"

    magento_composite_type = fields.Selection(
        [("bundle", "Bundle"), ("grouped", "Grouped")], string="Export as Magento",
        help="Export this product to Magento as a bundle (from its kit, or the components "
             "of its Magento binding) or as a grouped product.",
    )
    magento_bundle_price_type = fields.Selection(
        [("dynamic", "Dynamic"), ("fixed", "Fixed")], string="Bundle Price", default="dynamic",
        help="Dynamic: the bundle costs the sum of its selected components. Fixed: the "
             "product's price, plus each selection's own price.",
    )
    magento_composite_preview = fields.Html(
        string="Magento Components", compute="_compute_magento_composite_preview", sanitize=False,
    )

    @api.depends("magento_composite_type", "magento_bundle_price_type", "default_code")
    def _compute_magento_composite_preview(self):
        Map = self.env["magento.product.map"]
        for template in self:
            if not template.magento_composite_type or not template.id:
                template.magento_composite_preview = False
                continue
            product_map = template._magento_composite_maps()[:1]
            try:
                source, components = Map._magento_composite_source(
                    template.product_variant_id, template.magento_composite_type,
                    product_map.instance_id, product_map,
                )
            except UserError as exc:
                template.magento_composite_preview = Markup(
                    '<div class="alert alert-warning mb-0" role="status">%s</div>') % exc.args[0]
                continue
            template.magento_composite_preview = Map._magento_composite_preview_html(
                template.magento_composite_type, source, components)

    def _magento_composite_maps(self, create=False):
        """Product bindings of this template (one per instance); with ``create``, a new
        binding on the company's only instance when there is none."""
        self.ensure_one()
        Map = self.env["magento.product.map"].sudo()
        maps = Map.search([("odoo_template_id", "=", self.id)]) if self.id else Map
        maps = maps.filtered(lambda m: m.type_id != "configurable")
        if maps or not create:
            return maps
        variant = self.product_variant_id
        if len(self.product_variant_ids) != 1 or not variant:
            raise UserError(_("Product %s has variants: only single-variant products are exported "
                              "as Magento bundle or grouped products.", self.display_name))
        sku = variant.default_code
        if not sku:
            raise UserError(_("Set an Internal Reference (the Magento SKU) on %s first.", self.display_name))
        company = self.company_id or self.env.company
        instances = self.env["magento.instance"].sudo().search([("company_id", "in", (company.id, False))])
        if len(instances) != 1:
            raise UserError(_(
                "Product %s has no Magento binding and %s Magento instances could take it: "
                "create its binding (Magento > Catalog > Products) first.",
                self.display_name, len(instances)))
        attribute_sets = self.env["magento.attribute.set"].sudo().search(
            [("instance_id", "=", instances.id)], order="id")
        attribute_set = attribute_sets.filtered(lambda s: s.magento_id == "4")[:1] or attribute_sets[:1]
        if not attribute_set:
            raise UserError(_("Sync the attribute sets of %s first.", instances.display_name))
        return Map.with_context(**_QUIET_CTX).create({
            "instance_id": instances.id,
            "sku": sku,
            "name": self.name,
            "price": self.list_price,
            "type_id": self.magento_composite_type,
            "odoo_product_id": variant.id,
            "attribute_set_id": attribute_set.id,
        })

    def action_magento_export_composite(self):
        """Button: export these products to Magento as bundle / grouped products."""
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can export products to Magento."))
        outcomes = Counter()
        for template in self:
            if not template.magento_composite_type:
                raise UserError(_("Choose 'Export as Magento' (bundle or grouped) on %s first.",
                                  template.display_name))
            for product_map in template._magento_composite_maps(create=True):
                if product_map.type_id != template.magento_composite_type:
                    product_map.with_context(**_QUIET_CTX).write({"type_id": template.magento_composite_type})
                outcomes[product_map._magento_export_composite()] += 1
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento Export"),
                "message": _("%(created)s created, %(updated)s updated, %(unchanged)s unchanged in Magento.",
                             created=outcomes["created"], updated=outcomes["updated"],
                             unchanged=outcomes["unchanged"]),
                "type": "success",
                "sticky": False,
            },
        }


class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    type_id = fields.Selection(
        help="Configurable products are imported, not exported. Bundle and grouped products "
             "are exported with their components (kit or links).",
    )
    magento_composite_hash = fields.Char(
        string="Composite Export Fingerprint", readonly=True, copy=False,
        help="Fingerprint of the product data last exported with a bundle / grouped "
             "product: unchanged data is not sent again.",
    )

    # ------------------------------------------------------------------
    # Export block: lifted for bundle and grouped
    # ------------------------------------------------------------------
    def _check_exportable(self):
        return super(MagentoProductMap, self.filtered(
            lambda r: r.type_id not in COMPOSITE_EXPORT_TYPES))._check_exportable()

    def action_create_magento_product(self):
        composite = self.filtered(lambda r: r.type_id in COMPOSITE_EXPORT_TYPES)
        for record in composite:
            record._magento_export_composite()
        rest = self - composite
        if rest:
            return super(MagentoProductMap, rest).action_create_magento_product()
        return self._rainbow_man_action(f"Exported {len(composite)} product(s) to Magento.")

    def action_update_magento_product(self):
        composite = self.filtered(lambda r: r.type_id in COMPOSITE_EXPORT_TYPES)
        for record in composite:
            record._magento_export_composite()
        rest = self - composite
        if rest:
            return super(MagentoProductMap, rest).action_update_magento_product()
        return self._rainbow_man_action(f"Exported {len(composite)} product(s) to Magento.")

    # ------------------------------------------------------------------
    # Import side: the template remembers what it is in Magento
    # ------------------------------------------------------------------
    def _magento_sync_composite(self, data):
        res = super()._magento_sync_composite(data)
        template = self.odoo_product_id.product_tmpl_id
        if template and self.type_id in COMPOSITE_EXPORT_TYPES:
            vals = {}
            if template.magento_composite_type != self.type_id:
                vals["magento_composite_type"] = self.type_id
            if self.type_id == "bundle":
                raw = self._get_custom_attribute(data or {}, "price_type")
                if raw is not None:
                    price_type = "fixed" if str(raw) == "1" else "dynamic"
                    if template.magento_bundle_price_type != price_type:
                        vals["magento_bundle_price_type"] = price_type
            if vals:
                template.with_context(**_QUIET_CTX).write(vals)
        return res

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------
    @api.model
    def _magento_kit_lines(self, product, sku=None):
        """``[(product.product, qty per unit)]`` of the product's kit: its first phantom
        BoM made in Odoo (the BoM the bundle import makes, code "Magento <sku>", is
        not one). Empty without Manufacturing."""
        if "mrp.bom" not in self.env or not product:
            return []
        boms = self.env["mrp.bom"].sudo().search([
            ("type", "=", "phantom"),
            "|", ("product_id", "=", product.id),
            "&", ("product_id", "=", False), ("product_tmpl_id", "=", product.product_tmpl_id.id),
        ], order="sequence, product_id, id")
        own = "Magento %s" % (sku or product.default_code or "")
        bom = boms.filtered(lambda b: (b.code or "") != own)[:1]
        if not bom:
            return []
        lines = []
        for line in bom.bom_line_ids:
            if not line.product_id:
                continue
            if hasattr(line, "_skip_bom_line") and line._skip_bom_line(product):
                continue
            lines.append((line.product_id, line.product_qty / (bom.product_qty or 1.0)))
        return lines

    @api.model
    def _magento_component_sku(self, product, instance):
        if instance:
            product_map = self.sudo().search([
                ("instance_id", "=", instance.id), ("odoo_product_id", "=", product.id),
                ("sku", "!=", False),
            ], limit=1)
            if product_map:
                return product_map.sku
        return product.default_code or False

    @api.model
    def _magento_composite_source(self, product, type_id, instance, product_map=None):
        """``(source, components)`` of a bundle / grouped product: ``source`` is "kit",
        "links" or None. A bundle's components are options ``{title, type, required,
        position, links: [{sku, qty, position, is_default, price, price_type,
        can_change_quantity}]}``; a grouped product's are ``{sku, qty, position}``."""
        sku = product_map.sku if product_map else (product.default_code if product else False)
        kit = self._magento_kit_lines(product, sku)
        if kit:
            missing = [p.display_name for p, _qty in kit if not self._magento_component_sku(p, instance)]
            if missing:
                raise UserError(_("Kit components without a SKU (Internal Reference): %s.", ", ".join(missing)))
            if type_id == "grouped":
                return "kit", [{
                    "sku": self._magento_component_sku(p, instance), "qty": qty, "position": index,
                    "name": p.display_name,
                } for index, (p, qty) in enumerate(kit, start=1)]
            return "kit", [{
                "title": p.name, "type": "select", "required": True, "position": index,
                "links": [{
                    "sku": self._magento_component_sku(p, instance), "qty": qty, "position": 1,
                    "is_default": True, "price": 0.0, "price_type": 0, "can_change_quantity": False,
                    "name": p.display_name,
                }],
            } for index, (p, qty) in enumerate(kit, start=1)]
        links = product_map.child_link_ids.filtered(lambda l: l.link_type == type_id) if product_map else []
        if not links:
            return None, []
        links = links.sorted(lambda l: (l.option_position, l.option_id, l.position, l.id))
        if type_id == "grouped":
            return "links", [{
                "sku": link.child_sku, "qty": link.qty, "position": index,
                "name": link.child_product_id.display_name or link.child_sku,
            } for index, link in enumerate(links, start=1)]
        options = OrderedDict()
        for link in links:
            key = (link.option_id or 0, (link.option_title or "").strip() or link.child_sku)
            option = options.get(key)
            if option is None:
                option_type = (link.option_type or "select").strip().lower()
                option = options[key] = {
                    "title": key[1],
                    "type": option_type if option_type in BUNDLE_OPTION_TYPES else "select",
                    "required": bool(link.option_required),
                    "position": len(options) + 1,
                    "links": [],
                }
            option["required"] = option["required"] or bool(link.option_required)
            option["links"].append({
                "sku": link.child_sku, "qty": link.qty or 1.0, "position": len(option["links"]) + 1,
                "is_default": bool(link.is_default), "price": link.price or 0.0,
                "price_type": _to_int(link.price_type), "can_change_quantity": bool(link.can_change_quantity),
                "name": link.child_product_id.display_name or link.child_sku,
            })
        return "links", list(options.values())

    @staticmethod
    def _magento_component_skus(type_id, components):
        if type_id == "grouped":
            skus = [c["sku"] for c in components]
        else:
            skus = [link["sku"] for option in components for link in option["links"]]
        return list(dict.fromkeys(skus))

    @api.model
    def _magento_composite_preview_html(self, type_id, source, components):
        if not components:
            return Markup(
                '<div class="alert alert-info mb-0" role="status">%s</div>') % (
                _("No component yet: give the product a kit (Manufacturing), or add components "
                  "to its Magento binding.") if type_id == "bundle" else
                _("No component yet: add the associated products to its Magento binding "
                  "(or give it a kit)."))
        label = {"kit": _("From the kit (bill of materials)"), "links": _("From the Magento binding")}.get(source, "")
        rows = []
        if type_id == "grouped":
            head = Markup("<th>%s</th><th>%s</th><th class='text-end'>%s</th>") % (_("SKU"), _("Product"), _("Qty"))
            for c in components:
                rows.append(Markup("<tr><td>%s</td><td>%s</td><td class='text-end'>%s</td></tr>") % (
                    c["sku"], c.get("name") or "", "%g" % c["qty"]))
        else:
            head = Markup("<th>%s</th><th>%s</th><th>%s</th><th class='text-end'>%s</th><th>%s</th>") % (
                _("Option"), _("SKU"), _("Product"), _("Qty"), _("Default"))
            for option in components:
                title = escape(option["title"]) + (Markup(' <span class="text-muted small">(%s%s)</span>') % (
                    option["type"], _(", required") if option["required"] else ""))
                for link in option["links"]:
                    rows.append(Markup(
                        "<tr><td>%s</td><td>%s</td><td>%s</td><td class='text-end'>%s</td><td>%s</td></tr>") % (
                        title, link["sku"], link.get("name") or "", "%g" % link["qty"],
                        "✓" if link["is_default"] else ""))
        return Markup(
            '<div class="text-muted small mb-1">%s</div>'
            '<table class="table table-sm o_magento_composite_preview" style="font-variant-numeric: tabular-nums">'
            '<thead><tr>%s</tr></thead><tbody>%s</tbody></table>') % (label, head, Markup("").join(rows))

    # ------------------------------------------------------------------
    # Payload
    # ------------------------------------------------------------------
    def _magento_bundle_fixed(self):
        self.ensure_one()
        return self.odoo_product_id.product_tmpl_id.magento_bundle_price_type == "fixed"

    def _magento_composite_base_payload(self):
        """The product payload of a bundle / grouped product, without its components."""
        self.ensure_one()
        payload = self._build_magento_payload()
        product = payload["product"]
        product["type_id"] = self.type_id
        product.pop("weight", None)
        ext = product.get("extension_attributes")
        if isinstance(ext, dict) and isinstance(ext.get("stock_item"), dict):
            ext["stock_item"].pop("qty", None)  # a composite holds no stock of its own
            ext["stock_item"]["is_in_stock"] = True
        attrs = [a for a in product.get("custom_attributes") or []
                 if a.get("attribute_code") not in ("price_type", "sku_type", "weight_type",
                                                     "price_view", "shipment_type")]
        if self.type_id == "bundle":
            fixed = self._magento_bundle_fixed()
            attrs += [
                {"attribute_code": "price_type", "value": "1" if fixed else "0"},
                {"attribute_code": "sku_type", "value": "0"},
                {"attribute_code": "weight_type", "value": "0"},
                {"attribute_code": "price_view", "value": "0"},
                {"attribute_code": "shipment_type", "value": "0"},
            ]
            if not fixed:
                product["price"] = 0.0
        else:
            product["price"] = 0.0
        product["custom_attributes"] = attrs
        return payload

    def _magento_composite_part(self, components, types, live=None):
        """``(product keys)`` carrying the components: bundle options, or grouped links
        (the live product's other links kept)."""
        self.ensure_one()
        if self.type_id == "grouped":
            links = [{
                "sku": self.sku,
                "link_type": "associated",
                "linked_product_sku": c["sku"],
                "linked_product_type": types.get(c["sku"]) or "simple",
                "position": c["position"],
                "extension_attributes": {"qty": c["qty"]},
            } for c in components]
            others = [
                {k: v for k, v in link.items() if k in (
                    "sku", "link_type", "linked_product_sku", "linked_product_type", "position",
                    "extension_attributes")}
                for link in (live or {}).get("product_links") or []
                if isinstance(link, dict) and link.get("link_type") != "associated"
            ]
            return {"product_links": others + links}
        fixed = self._magento_bundle_fixed()
        options = [{
            "title": option["title"],
            "required": bool(option["required"]),
            "type": option["type"],
            "position": option["position"],
            "sku": self.sku,
            "product_links": [{
                "sku": link["sku"],
                "qty": link["qty"],
                "position": link["position"],
                "is_default": bool(link["is_default"]),
                "price": link["price"] if fixed else 0.0,
                "price_type": link["price_type"] if fixed else 0,
                "can_change_quantity": 1 if link["can_change_quantity"] else 0,
            } for link in option["links"]],
        } for option in components]
        return {"extension_attributes": {"bundle_product_options": options}}

    def _magento_composite_signature(self, components=None, live=None):
        """Comparable form of the components, from ours (``components``) or from a
        Magento product payload (``live``)."""
        self.ensure_one()
        fixed = self.type_id == "bundle" and self._magento_bundle_fixed()
        if self.type_id == "grouped":
            if live is not None:
                pairs = [
                    (str(l.get("linked_product_sku") or "").lower(),
                     _qty_key(((l.get("extension_attributes") or {}).get("qty"))))
                    for l in live.get("product_links") or []
                    if isinstance(l, dict) and l.get("link_type") == "associated"
                ]
            else:
                pairs = [(c["sku"].lower(), _qty_key(c["qty"])) for c in components]
            return sorted(pairs)
        if live is not None:
            options = (live.get("extension_attributes") or {}).get("bundle_product_options") or []
            raw = [(o.get("title"), o.get("type"), o.get("required"), o.get("product_links") or [])
                   for o in options if isinstance(o, dict)]
        else:
            raw = [(o["title"], o["type"], o["required"], o["links"]) for o in components]
        signature = []
        for title, option_type, required, links in raw:
            sig_links = sorted(
                (str(l.get("sku") or "").lower(), _qty_key(l.get("qty")), bool(l.get("is_default")),
                 bool(_to_int(l.get("can_change_quantity"))),
                 (round(_to_float(l.get("price")), 4), _to_int(l.get("price_type"))) if fixed else ())
                for l in links if isinstance(l, dict))
            signature.append((str(title or "").strip().lower(), str(option_type or "select").lower(),
                              bool(required), tuple(sig_links)))
        return sorted(signature)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _magento_export_composite(self):
        """Create or update this bundle / grouped product in Magento with its
        components. Returns "created", "updated" or "unchanged"."""
        self.ensure_one()
        if self.type_id not in COMPOSITE_EXPORT_TYPES:
            raise UserError(_("%s is not a bundle or grouped product.", self.sku or self.display_name))
        if not self.instance_id or not self.sku:
            raise UserError(_("A Magento instance and a SKU are required."))
        product = self.odoo_product_id
        source, components = self._magento_composite_source(product, self.type_id, self.instance_id, self)
        if not components:
            raise UserError(_(
                "%(sku)s has no component to export: give the product a kit (bill of materials "
                "of type Kit) or add its components to the Magento binding.", sku=self.sku))
        if self.type_id == "bundle":
            empty = [o["title"] for o in components if not o["links"]]
            if empty:
                raise UserError(_("Bundle options without product: %s.", ", ".join(empty)))
        api = self._magento_api()
        skus = self._magento_component_skus(self.type_id, components)
        if self.sku.lower() in {s.lower() for s in skus}:
            raise UserError(_("%s cannot be one of its own components.", self.sku))
        try:
            types = api.get_product_types_by_sku(skus)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)
        missing = [s for s in skus if s not in types]
        if missing:
            raise UserError(_(
                "Cannot export %(kind)s %(sku)s: these component SKUs do not exist in Magento: "
                "%(missing)s. Export them first.",
                kind=dict(self._fields["type_id"].selection)[self.type_id], sku=self.sku,
                missing=", ".join(missing)))
        allowed = _GROUPED_CHILD_TYPES if self.type_id == "grouped" else _BUNDLE_CHILD_TYPES
        wrong = ["%s (%s)" % (s, types[s]) for s in skus if types[s] not in allowed]
        if wrong:
            raise UserError(_(
                "Magento only accepts %(allowed)s products in a %(kind)s product; not: %(wrong)s.",
                allowed=", ".join(allowed), kind=self.type_id, wrong=", ".join(wrong)))

        payload = self._magento_composite_base_payload()
        fingerprint = hashlib.sha1(
            json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        try:
            live = api.get_product(self.sku)
        except requests.exceptions.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                self._raise_magento_http_error(exc)
            live = None
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)
        if not isinstance(live, dict) or not live.get("sku"):
            live = None

        if live is None:
            self._magento_merge_extension(payload, components, types)
            try:
                response = api.create_product(payload)
            except requests.exceptions.RequestException as exc:
                self._raise_magento_http_error(exc)
            outcome = "created"
        else:
            live_type = live.get("type_id")
            if live_type and live_type != self.type_id:
                raise UserError(_(
                    "Magento product %(sku)s is a %(live)s product: it cannot become a %(kind)s "
                    "product. Use another SKU, or change it in Magento.",
                    sku=self.sku, live=live_type, kind=self.type_id))
            same = (self._magento_composite_signature(live=live)
                    == self._magento_composite_signature(components=components))
            if same and self.magento_composite_hash == fingerprint:
                return "unchanged"
            if not same:
                self._magento_merge_extension(payload, components, types, live)
            try:
                response = api.update_product(self.sku, payload)
            except requests.exceptions.RequestException as exc:
                self._raise_magento_http_error(exc)
            outcome = "updated"
        vals = {"magento_composite_hash": fingerprint, "magento_last_error": False}
        if isinstance(response, dict) and response.get("id") and str(response["id"]) != (self.magento_id or ""):
            vals["magento_id"] = str(response["id"])
        self.with_context(**_QUIET_CTX).write(vals)
        _logger.info("Magento %s %s (instance %s) %s from %s: %s component(s).",
                     self.type_id, self.sku, self.instance_id.name, outcome, source, len(skus))
        return outcome

    def _magento_merge_extension(self, payload, components, types, live=None):
        """Put the components into ``payload`` (merging extension attributes)."""
        part = self._magento_composite_part(components, types, live)
        product = payload["product"]
        ext = part.pop("extension_attributes", None)
        if ext:
            product.setdefault("extension_attributes", {}).update(ext)
        product.update(part)


class MagentoProductLink(models.Model):
    _inherit = "magento.product.link"

    @api.onchange("child_map_id")
    def _onchange_child_map_id(self):
        for link in self:
            if link.child_map_id and link.child_map_id.sku:
                link.child_sku = link.child_map_id.sku
