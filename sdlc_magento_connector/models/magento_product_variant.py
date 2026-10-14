"""Phase 2 catalog: configurable, bundle and grouped products, attribute options, and the
Magento category tree mirrored into ``product.category``.

Bindings (all instance scoped)
------------------------------
* ``magento.product.map`` stays the one binding per Magento SKU. Order import resolves an
  order item through it (Magento id, else SKU -> ``odoo_product_id``), so:

  - a configurable's **child SKU** keeps its own map row, pointing to the
    ``product.product`` *variant*;
  - the **configurable** itself gets a map row (``type_id='configurable'``) bound to the
    whole template through ``odoo_template_id``; its ``odoo_product_id`` stays empty;
  - a **bundle** or **grouped** product is one ordinary product (map ->
    ``odoo_product_id``); a grouped product is not sellable (``sale_ok=False``).

* ``magento.product.link`` records which SKUs make up a configurable (its variants), a
  bundle (option components) or a grouped product (associated products):
  ``parent_map_id`` -> ``child_map_id`` / ``child_sku`` with qty, position and the bundle
  option. A child not imported yet keeps its SKU and is linked when it arrives.

* ``magento.product.attribute`` / ``magento.product.attribute.option`` cache, per
  instance, Magento attribute ids and option value ids with their labels, and the Odoo
  ``product.attribute`` / ``product.attribute.value`` they map to. They are the
  attribute and option mapping a later configurable export will need.

Existing standalone children (merge path)
-----------------------------------------
Child SKUs imported earlier as standalone simple products are merged into the
configurable's template, never duplicated. A standalone product (template with one
variant and no attribute lines) is *moved* into the template, keeping its
``product.product`` id, so sale lines, stock and the map stay attached to it. Its old,
now empty template is deleted when nothing references it, archived otherwise. When the
product cannot be moved safely (other unit of measure, product type or company), a new
variant is created, the map is re-pointed to it and the old product is archived. A child
SKU that already is a variant of another template is left where it is.

Contract for the order import: ``magento.product.map._magento_bundle_components()``.
"""
import logging
import time

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from ..services.magento_api import MagentoAPI
from .magento_instance import _SYNC_BATCH_SIZE, _SyncStats, _short_reason
from .magento_product import magento_variant_products
from .sql_compat import UniqueIndex

_logger = logging.getLogger(__name__)

# Context of every Odoo write done by the catalog import: no push back to Magento and no
# map <-> product mirroring.
_IMPORT_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}
# Same, and Odoo does not generate variants on its own: a configurable's variants are
# exactly its Magento child SKUs.
_NO_VARIANTS_CTX = dict(_IMPORT_CTX, create_product_product=False)

_COMPOSITE_TYPES = ("configurable", "bundle", "grouped")
# Composite SKUs imported per queued job by "Import Configurable / Bundle / Grouped".
_COMPOSITE_JOB_CHUNK = 10
# A composite job stops after this many seconds and queues the rest, so one job
# stays well under Odoo's cron time limit (configurables can merge many variants).
_COMPOSITE_JOB_TIME_BUDGET = 60
_clock = time.monotonic  # module-level so tests can drive the budget

# Single-column foreign keys to product_template (see _magento_template_is_referenced).
_TEMPLATE_FK_SQL = """
    SELECT cl.relname, att.attname
      FROM pg_constraint con
      JOIN pg_class cl ON cl.oid = con.conrelid
      JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
     WHERE con.contype = 'f'
       AND con.confrelid = 'product_template'::regclass
       AND array_length(con.conkey, 1) = 1
"""


def _custom_attribute(data, code):
    for item in (data or {}).get("custom_attributes") or []:
        if item.get("attribute_code") == code:
            return item.get("value")
    return None


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _b64_text(value):
    """Base64 image value as text (Binary fields read as bytes)."""
    if isinstance(value, bytes):
        return value.decode()
    return value or ""


# ======================================================================
# Attribute and option cache / mapping
# ======================================================================
class MagentoProductAttribute(models.Model):
    _name = "magento.product.attribute"
    _description = "Magento Product Attribute"
    _order = "instance_id, attribute_code, id"
    _rec_name = "name"

    _uniq_instance_attribute = UniqueIndex(
        "(instance_id, magento_attribute_id)",
        "This Magento attribute is already mapped for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    magento_attribute_id = fields.Char(string="Magento Attribute ID", required=True, readonly=True)
    attribute_code = fields.Char(readonly=True, index=True)
    name = fields.Char(string="Label", required=True)
    odoo_attribute_id = fields.Many2one(
        "product.attribute", string="Odoo Attribute", ondelete="set null",
        help="Odoo attribute the Magento attribute's options become values of. Found by "
             "label (or created) on the first import; change it to map to another attribute.",
    )
    option_ids = fields.One2many("magento.product.attribute.option", "attribute_binding_id", string="Options")
    option_count = fields.Integer(compute="_compute_option_count", string="Option Count")

    @api.depends("option_ids")
    def _compute_option_count(self):
        for record in self:
            record.option_count = len(record.option_ids)

    def _magento_load(self, data, fallback_label=None):
        """Store the code, label and options of the Magento attribute JSON ``data``."""
        self.ensure_one()
        label = (
            data.get("default_frontend_label")
            or fallback_label
            or data.get("attribute_code")
            or self.name
        )
        vals = {}
        if data.get("attribute_code") and data["attribute_code"] != self.attribute_code:
            vals["attribute_code"] = data["attribute_code"]
        if label and label != self.name:
            vals["name"] = label
        if vals:
            self.write(vals)
        existing = {option.value: option for option in self.option_ids}
        to_create = []
        for item in data.get("options") or []:
            raw = item.get("value")
            value = str(raw).strip() if raw is not None else ""
            if not value:
                continue  # Magento's empty "please select" option
            option_label = (str(item.get("label") or "")).strip() or value
            option = existing.get(value)
            if option is None:
                to_create.append({"attribute_binding_id": self.id, "value": value, "label": option_label})
                existing[value] = True
            elif option is not True and option.label != option_label:
                option.label = option_label
        if to_create:
            self.env["magento.product.attribute.option"].create(to_create)

    def _magento_odoo_attribute(self):
        """The Odoo attribute (created variants: always) of this Magento attribute."""
        self.ensure_one()
        if self.odoo_attribute_id:
            return self.odoo_attribute_id
        attributes = self.env["product.attribute"].sudo()
        wanted = (self.name or "").strip().lower()
        attribute = attributes.search([
            ("name", "ilike", self.name), ("create_variant", "=", "always"),
        ]).filtered(lambda a: (a.name or "").strip().lower() == wanted)[:1]
        if not attribute:
            attribute = attributes.create({"name": self.name, "create_variant": "always"})
        self.odoo_attribute_id = attribute
        return attribute

    def _magento_odoo_value(self, value):
        """``product.attribute.value`` of the Magento option ``value`` (an option id)."""
        self.ensure_one()
        value = str(value)
        option = self.option_ids.filtered(lambda o: o.value == value)[:1]
        if not option:
            option = self.env["magento.product.attribute.option"].create({
                "attribute_binding_id": self.id, "value": value, "label": value,
            })
        attribute = self._magento_odoo_attribute()
        if option.odoo_value_id and option.odoo_value_id.attribute_id == attribute:
            return option.odoo_value_id
        values = self.env["product.attribute.value"].sudo()
        wanted = (option.label or "").strip().lower()
        pav = values.search([("attribute_id", "=", attribute.id)]).filtered(
            lambda v: (v.name or "").strip().lower() == wanted)[:1]
        if not pav:
            pav = values.create({"attribute_id": attribute.id, "name": option.label})
        option.odoo_value_id = pav
        return pav


class MagentoProductAttributeOption(models.Model):
    _name = "magento.product.attribute.option"
    _description = "Magento Product Attribute Option"
    _order = "attribute_binding_id, label, id"
    _rec_name = "label"

    _uniq_attribute_value = UniqueIndex(
        "(attribute_binding_id, value)",
        "This Magento option is already mapped.",
    )

    attribute_binding_id = fields.Many2one(
        "magento.product.attribute", string="Magento Attribute", required=True,
        ondelete="cascade", index=True,
    )
    instance_id = fields.Many2one(related="attribute_binding_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="attribute_binding_id.company_id", store=True, index=True)
    value = fields.Char(string="Magento Option ID", required=True, readonly=True)
    label = fields.Char()
    odoo_value_id = fields.Many2one("product.attribute.value", string="Odoo Value", ondelete="set null")


# ======================================================================
# Variants and components
# ======================================================================
class MagentoProductLink(models.Model):
    _name = "magento.product.link"
    _description = "Magento Product Link (variant or component)"
    _order = "parent_map_id, option_position, option_id, position, id"
    _rec_name = "child_sku"

    _uniq_link = UniqueIndex(
        "(parent_map_id, link_type, option_id, child_sku)",
        "This SKU is already linked to this Magento product.",
    )

    parent_map_id = fields.Many2one(
        "magento.product.map", string="Parent Product", required=True, ondelete="cascade", index=True,
    )
    instance_id = fields.Many2one(related="parent_map_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="parent_map_id.company_id", store=True, index=True)
    parent_type = fields.Selection(related="parent_map_id.type_id", string="Parent Type")
    link_type = fields.Selection(
        [("configurable", "Variant"), ("bundle", "Bundle Component"), ("grouped", "Grouped Product")],
        required=True,
    )
    child_sku = fields.Char(string="SKU", required=True, index=True)
    child_map_id = fields.Many2one("magento.product.map", string="Child Product", ondelete="set null", index=True)
    child_product_id = fields.Many2one(
        related="child_map_id.odoo_product_id", string="Odoo Product",
    )
    position = fields.Integer()
    qty = fields.Float(string="Quantity", default=1.0, digits="Product Unit")
    option_id = fields.Integer(
        string="Bundle Option ID", default=0, required=True,
        help="Magento bundle option of the component; 0 for variants and grouped products.",
    )
    option_title = fields.Char(string="Option")
    option_type = fields.Char(string="Option Input")
    option_required = fields.Boolean(string="Required Option")
    option_position = fields.Integer()
    is_default = fields.Boolean(string="Default")
    price = fields.Float(string="Price")
    price_type = fields.Char()
    can_change_quantity = fields.Boolean(string="Quantity Editable")


class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    child_link_ids = fields.One2many(
        "magento.product.link", "parent_map_id", string="Variants and Components",
    )
    parent_link_ids = fields.One2many(
        "magento.product.link", "child_map_id", string="Part Of",
        help="Configurable, bundle and grouped products this SKU belongs to.",
    )
    magento_child_count = fields.Integer(
        string="Variants / Components", compute="_compute_magento_child_count",
    )
    odoo_categ_id = fields.Many2one(
        related="odoo_template_id.categ_id", string="Odoo Category",
    )

    @api.depends("child_link_ids")
    def _compute_magento_child_count(self):
        counts = {}
        if self.ids:
            for parent, count in self.env["magento.product.link"]._read_group(
                [("parent_map_id", "in", self.ids)], ["parent_map_id"], ["__count"],
            ):
                counts[parent.id] = count
        for record in self:
            record.magento_child_count = counts.get(record.id, 0)

    # ------------------------------------------------------------------
    # Public contract (order import)
    # ------------------------------------------------------------------
    def _magento_bundle_components(self, default_only=False):
        """Components of this bundle (or the associated products of this grouped product).

        Returns a list of dicts, ordered by bundle option then position::

            {
                "option_id": int or None,     # Magento bundle option id (None: grouped)
                "option_title": str,
                "option_type": str,           # radio, select, checkbox, multi
                "required": bool,
                "sku": str,                   # component SKU
                "map": magento.product.map,   # empty until that SKU is imported
                "product": product.product,   # empty until that SKU is imported
                "qty": float,                 # default quantity per bundle
                "is_default": bool,
                "can_change_quantity": bool,
                "price": float,               # selection price (dynamic-price bundles: 0)
                "price_type": str or False,
                "position": int,
            }

        ``default_only=True`` keeps the default selection of each option (the
        ``is_default`` component, or the only one), which is the fixed kit; options
        without a default are left out. An order item of a bundle carries the selected
        components as child items, so the order import should match those by SKU and use
        this list only for what the order does not say (e.g. a fixed-price bundle sold
        as a single line).
        """
        self.ensure_one()
        links = self.child_link_ids.filtered(lambda l: l.link_type in ("bundle", "grouped"))
        if default_only:
            kept = self.env["magento.product.link"]
            for option_id in dict.fromkeys(links.mapped("option_id")):
                option_links = links.filtered(lambda l, o=option_id: l.option_id == o)
                default = option_links.filtered("is_default")[:1]
                if not default and len(option_links) == 1:
                    default = option_links
                kept |= default
            links = kept
        return [{
            "option_id": link.option_id or None,
            "option_title": link.option_title or "",
            "option_type": link.option_type or "",
            "required": link.option_required,
            "sku": link.child_sku,
            "map": link.child_map_id,
            "product": link.child_map_id.odoo_product_id,
            "qty": link.qty,
            "is_default": link.is_default,
            "can_change_quantity": link.can_change_quantity,
            "price": link.price,
            "price_type": link.price_type or False,
            "position": link.position,
        } for link in links.sorted(lambda l: (l.option_position, l.option_id, l.position, l.id))]

    def _magento_child_values(self, child):
        """Map values of a child SKU from the configurable's ``children`` listing."""
        self.ensure_one()
        values = self._apply_magento_data(child)
        # The listing carries no stock or category information: keep the child's own
        # (its own product import fills them).
        for key in ("quantity", "stock_status", "magento_category_ids", "magento_parent_category_id"):
            values.pop(key, None)
        return values

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------
    def _magento_sync_links(self, link_type, rows):
        """Make the ``link_type`` links of this map exactly ``rows`` (dicts with at least
        ``child_sku``): update matching links, create new ones, delete the others."""
        self.ensure_one()
        Link = self.env["magento.product.link"]
        current = self.child_link_ids.filtered(lambda l: l.link_type == link_type)
        by_key = {(link.option_id, link.child_sku): link for link in current}
        skus = {row["child_sku"] for row in rows}
        maps_by_sku = {
            m.sku: m for m in self.with_context(active_test=False).search([
                ("instance_id", "=", self.instance_id.id), ("sku", "in", list(skus)),
            ])
        }
        kept = Link
        to_create = []
        for row in rows:
            vals = dict(row, link_type=link_type)
            vals.setdefault("option_id", 0)
            child_map = maps_by_sku.get(row["child_sku"])
            vals.setdefault("child_map_id", child_map.id if child_map else False)
            link = by_key.get((vals["option_id"], vals["child_sku"]))
            if link:
                changed = {}
                for key, value in vals.items():
                    current_value = link[key]
                    if isinstance(current_value, models.BaseModel):
                        current_value = current_value.id or False
                    if current_value != value:
                        changed[key] = value
                if changed:
                    link.write(changed)
                kept |= link
            else:
                vals["parent_map_id"] = self.id
                to_create.append(vals)
        (current - kept).unlink()
        if to_create:
            Link.create(to_create)

    @api.model
    def _magento_composite_rows(self, data):
        """Link rows of a bundle (``bundle_product_options``) or a grouped product
        (``product_links`` of type ``associated``)."""
        type_id = data.get("type_id")
        rows = []
        if type_id == "bundle":
            options = (data.get("extension_attributes") or {}).get("bundle_product_options") or []
            for option in options:
                option_id = int(option.get("option_id") or 0)
                for link in option.get("product_links") or []:
                    if not link.get("sku"):
                        continue
                    rows.append({
                        "child_sku": link["sku"],
                        "option_id": option_id,
                        "option_title": option.get("title") or False,
                        "option_type": option.get("type") or False,
                        "option_required": bool(option.get("required")),
                        "option_position": int(option.get("position") or 0),
                        "position": int(link.get("position") or 0),
                        "qty": _to_float(link.get("qty"), 1.0) or 1.0,
                        "is_default": bool(link.get("is_default")),
                        "price": _to_float(link.get("price")),
                        "price_type": str(link["price_type"]) if link.get("price_type") is not None else False,
                        "can_change_quantity": bool(link.get("can_change_quantity")),
                    })
        elif type_id == "grouped":
            for link in data.get("product_links") or []:
                if link.get("link_type") != "associated" or not link.get("linked_product_sku"):
                    continue
                qty = (link.get("extension_attributes") or {}).get("qty")
                rows.append({
                    "child_sku": link["linked_product_sku"],
                    "position": int(link.get("position") or 0),
                    "qty": _to_float(qty),
                })
        return rows

    def _magento_sync_composite(self, data):
        """Components of a bundle / grouped product after its own import."""
        self.ensure_one()
        if self.type_id not in ("bundle", "grouped"):
            return
        self._magento_sync_links(self.type_id, self._magento_composite_rows(data))
        template = self.odoo_product_id.product_tmpl_id
        if self.type_id == "grouped" and template and (template.sale_ok or template.purchase_ok):
            # A grouped product is a display grouping: Magento never sells it as such.
            template.with_context(**_IMPORT_CTX).write({"sale_ok": False, "purchase_ok": False})
        if self.type_id == "bundle":
            self._magento_sync_bundle_bom()

    def _magento_sync_bundle_bom(self):
        """Kit BoM (type phantom) of the bundle's default selection, when Manufacturing is
        installed and the instance asks for it. Returns the BoM, or False when there is
        none to make (no mrp, option off, a required option without a default, or a
        component not imported yet: it is retried when that component arrives)."""
        self.ensure_one()
        if "mrp.bom" not in self.env or not self.instance_id.magento_bundle_kit_bom:
            return False
        template = self.odoo_product_id.product_tmpl_id
        if not template or self.type_id != "bundle":
            return False
        components = self._magento_bundle_components(default_only=True)
        chosen = {c["option_id"] for c in components}
        required = {
            link.option_id for link in self.child_link_ids
            if link.link_type == "bundle" and link.option_required
        }
        if not components or required - chosen or any(not c["product"] for c in components):
            return False
        lines = [(c["product"].id, c["qty"] or 1.0) for c in components]
        boms = self.env["mrp.bom"].sudo().with_context(active_test=False)
        code = "Magento %s" % self.sku
        bom = boms.search([("product_tmpl_id", "=", template.id), ("code", "=", code)], limit=1)
        current = sorted((line.product_id.id, line.product_qty) for line in bom.bom_line_ids)
        if bom and current == sorted(lines) and bom.type == "phantom":
            return bom
        vals = {
            "product_tmpl_id": template.id,
            "type": "phantom",
            "code": code,
            "product_qty": 1.0,
            "bom_line_ids": [(5, 0, 0)] + [
                (0, 0, {"product_id": product_id, "product_qty": qty}) for product_id, qty in lines
            ],
        }
        if bom:
            bom.write(vals)
        else:
            bom = boms.create(vals)
        return bom

    # ------------------------------------------------------------------
    # Category
    # ------------------------------------------------------------------
    def _magento_apply_product_category(self, data=None):
        """Set the Odoo product category (and website categories) of this map's product
        from its Magento categories, following the instance's category policy.

        Variants are skipped: their template's category is the configurable's."""
        self.ensure_one()
        product = self.odoo_product_id
        if not product or self.type_id == "configurable" or magento_variant_products(product):
            return
        categories = self._magento_categories_in_order(data)
        if categories is None:
            return
        self.instance_id._magento_set_template_categories(product.product_tmpl_id, categories)

    def _magento_categories_in_order(self, data=None):
        """Magento categories of the product in Magento's order (``category_links`` of
        ``data`` when given), or None when ``data`` says nothing about categories."""
        self.ensure_one()
        if data is None:
            return self.magento_category_ids
        links = (data.get("extension_attributes") or {}).get("category_links")
        if links is None:
            return None
        ids = [str(link.get("category_id")) for link in links if link.get("category_id") is not None]
        found = {
            c.magento_id: c for c in self.env["magento.category"].search([
                ("instance_id", "=", self.instance_id.id), ("magento_id", "in", ids),
            ])
        }
        return self.env["magento.category"].concat(*[found[i] for i in dict.fromkeys(ids) if i in found])


# ======================================================================
# Instance: import of configurable / bundle / grouped, category mirror
# ======================================================================
class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    magento_categ_root_id = fields.Many2one(
        "product.category", string="Odoo Category Root",
        help="Odoo product category under which the Magento category tree is mirrored. "
             "Empty: a category named after the instance is created on the first sync.",
    )
    magento_mirror_categories = fields.Boolean(
        string="Mirror Categories to Odoo", default=True,
        help="Create and keep an Odoo product category for each Magento category "
             "(same name and tree). Untick to map categories by hand on each Magento category.",
    )
    magento_product_categ_policy = fields.Selection(
        [
            ("deepest", "Deepest Magento category"),
            ("first", "First Magento category"),
            ("none", "Keep the Odoo category"),
        ],
        string="Product Category From", default="deepest", required=True,
        help="Which of a product's Magento categories sets its Odoo product category.",
    )
    magento_public_categories = fields.Boolean(
        string="Mirror eCommerce Categories", default=True,
        help="When eCommerce (website_sale) is installed, also mirror the Magento tree into "
             "the website categories and put the products in them.",
    )
    magento_bundle_kit_bom = fields.Boolean(
        string="Kit BoM for Bundles", default=True,
        help="When Manufacturing is installed, give each imported bundle a kit bill of "
             "materials made of its default components.",
    )
    import_products_storable = fields.Boolean(
        string="Import Products as Storable", default=True,
        help="Simple products and the variants of configurable products are imported as "
             "storable goods (tracked inventory, pushed by the stock push). Virtual and "
             "downloadable products are imported as services; bundles (kits) and grouped "
             "products stay non-storable. Only products still in their default state "
             "(non-storable goods) without any stock move are changed.",
    )

    # ------------------------------------------------------------------
    # Hooks called by the import (magento_instance.py)
    # ------------------------------------------------------------------
    def _magento_after_category_import(self, category):
        self.ensure_one()
        if self.magento_mirror_categories:
            category._magento_mirror_product_category()

    def _magento_after_product_import(self, mapping, product_data):
        """After a simple / virtual / downloadable / bundle / grouped product import."""
        self.ensure_one()
        if mapping.type_id in ("bundle", "grouped"):
            mapping._magento_sync_composite(product_data)
        product = mapping.odoo_product_id
        if product and not magento_variant_products(product):
            # A variant's kind is its configurable template's (set by the configurable).
            self._magento_apply_product_kind(product.product_tmpl_id, mapping.type_id)
        mapping._magento_apply_product_category(product_data)
        self._magento_resolve_pending_links(mapping)

    def _magento_apply_product_kind(self, template, magento_type):
        """Product type of an imported product, from its Magento type:

        * simple, configurable (its variants): storable goods when
          ``import_products_storable`` is on;
        * virtual, downloadable: services;
        * bundle (a kit), grouped: left as non-storable goods.

        Only a template still in the default state of an import (non-storable goods) and
        without any stock move is changed, so a product the user set up, or one with
        stock history, is never flipped."""
        self.ensure_one()
        if magento_type in ("simple", "configurable"):
            if not self.import_products_storable or "is_storable" not in template._fields:
                return False
            vals = {"is_storable": True}
        elif magento_type in ("virtual", "downloadable"):
            vals = {"type": "service"}
        else:
            return False
        if template.type != "consu" or ("is_storable" in template._fields and template.is_storable):
            return False
        variants = template.with_context(active_test=False).product_variant_ids
        if "stock.move" in self.env and self.env["stock.move"].sudo().search_count(
                [("product_id", "in", variants.ids)], limit=1):
            return False
        template.with_context(**_NO_VARIANTS_CTX).write(vals)
        return True

    def _magento_resolve_pending_links(self, mapping):
        """Link ``mapping`` to the configurables / bundles / grouped products that list its
        SKU; refresh the kit BoM of the bundles it completes."""
        self.ensure_one()
        if not mapping.sku:
            return
        links = self.env["magento.product.link"].search([
            ("instance_id", "=", self.id), ("child_sku", "=", mapping.sku),
            ("child_map_id", "!=", mapping.id),
        ])
        if not links:
            return
        links.write({"child_map_id": mapping.id})
        for parent in links.parent_map_id.filtered(lambda m: m.type_id == "bundle"):
            parent._magento_sync_bundle_bom()

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------
    def _magento_product_category_root(self):
        """Odoo category under which this instance's Magento tree is mirrored."""
        self.ensure_one()
        root = self.magento_categ_root_id
        if not root:
            root = self.env["product.category"].sudo().create({"name": self.name})
            self.sudo().write({"magento_categ_root_id": root.id})
        return root

    def _magento_set_template_categories(self, template, categories):
        """Apply the category policy to ``template`` from Magento ``categories`` (in
        Magento's order)."""
        self.ensure_one()
        template = template.with_context(**_NO_VARIANTS_CTX)
        if categories and self.magento_product_categ_policy != "none":
            if self.magento_product_categ_policy == "first":
                chosen = categories[0]
            else:
                depth = {c.id: c._magento_depth() for c in categories}
                chosen = max(categories, key=lambda c: depth[c.id])  # first of the deepest
            categ = chosen._magento_mirror_product_category() if self.magento_mirror_categories \
                else chosen.categ_id
            if categ and template.categ_id != categ:
                template.write({"categ_id": categ.id})
        if categories and self._magento_public_categories_enabled():
            public = categories._magento_mirror_public_category()
            missing = public - template.public_categ_ids
            if missing:
                template.write({"public_categ_ids": [(4, c.id) for c in missing]})

    def _magento_public_categories_enabled(self):
        self.ensure_one()
        return bool(
            self.magento_public_categories
            and self.magento_mirror_categories
            and "product.public.category" in self.env
            and "public_categ_ids" in self.env["product.template"]._fields
        )

    # ------------------------------------------------------------------
    # Magento reads (patched in tests)
    # ------------------------------------------------------------------
    def _magento_fetch_configurable_children(self, api, sku):
        """Child products of a configurable (``GET /V1/configurable-products/{sku}/children``)."""
        safe_sku = requests.utils.quote(str(sku or "").strip(), safe="")
        result = api._request_with_prefix_fallback(
            "get", lambda prefix: f"{prefix}/configurable-products/{safe_sku}/children",
            include_all_scope=True,
        )
        return [item for item in result or [] if isinstance(item, dict)]

    def _magento_fetch_attribute(self, api, attribute_id):
        """A product attribute with its options (``GET /V1/products/attributes/{id}``)."""
        safe_id = requests.utils.quote(str(attribute_id or "").strip(), safe="")
        result = api._request_with_prefix_fallback(
            "get", lambda prefix: f"{prefix}/products/attributes/{safe_id}",
            include_all_scope=True,
        )
        return result if isinstance(result, dict) else {}

    def _magento_attribute_binding(self, api, option, refreshed):
        """``magento.product.attribute`` of a configurable option, its options known.

        Fetched from Magento when unknown, or when it lacks one of the option's values
        (at most once per attribute and run: ``refreshed`` holds the ids fetched)."""
        self.ensure_one()
        attribute_id = str(option.get("attribute_id") or "").strip()
        if not attribute_id:
            raise UserError("Configurable option %r has no attribute id." % (option.get("label") or ""))
        Binding = self.env["magento.product.attribute"]
        binding = Binding.search([
            ("instance_id", "=", self.id), ("magento_attribute_id", "=", attribute_id),
        ], limit=1)
        needed = {str(v.get("value_index")) for v in option.get("values") or [] if v.get("value_index") is not None}
        known = set(binding.option_ids.mapped("value"))
        stale = not binding or not binding.attribute_code or bool(needed - known)
        if stale and attribute_id not in refreshed:
            refreshed.add(attribute_id)
            data = self._magento_fetch_attribute(api, attribute_id)
            if not binding:
                binding = Binding.create({
                    "instance_id": self.id,
                    "magento_attribute_id": attribute_id,
                    "name": data.get("default_frontend_label") or option.get("label") or attribute_id,
                })
            binding._magento_load(data, fallback_label=option.get("label"))
        if not binding.attribute_code:
            raise UserError(
                "Magento attribute %s (%s) has no attribute code; cannot read the child "
                "products' values." % (attribute_id, option.get("label") or "")
            )
        return binding

    # ------------------------------------------------------------------
    # Configurable import
    # ------------------------------------------------------------------
    def _magento_find_product_map(self, magento_id, sku):
        self.ensure_one()
        maps = self.env["magento.product.map"].with_context(active_test=False)
        mapping = maps.browse()
        if magento_id not in (None, False, ""):
            mapping = maps.search([("instance_id", "=", self.id), ("magento_id", "=", str(magento_id))], limit=1)
        if not mapping and sku:
            mapping = maps.search([("instance_id", "=", self.id), ("sku", "=", sku)], limit=1)
        return mapping

    def _magento_import_configurable(self, api, p, product_data, stats, box=None):
        """Import a Magento configurable as ONE product template whose variants are its
        child SKUs (see the module docstring for bindings and the merge path).

        Called by ``_sync_product_record`` inside the record's savepoint: any exception
        rolls back this configurable only. Returns ``"created"`` / ``"updated"`` /
        ``("skipped", reason)``.
        """
        self.ensure_one()
        box = box if box is not None else []
        data = product_data or p
        sku = data.get("sku") or p.get("sku")
        magento_id = p.get("id") if p.get("id") is not None else data.get("id")
        ext = data.get("extension_attributes") or {}
        options = sorted(
            ext.get("configurable_product_options") or [],
            key=lambda o: (int(o.get("position") or 0), str(o.get("attribute_id"))),
        )
        if not options:
            return ("skipped", "configurable %s has no configurable options" % sku)
        children = self._magento_fetch_configurable_children(api, sku)

        refreshed = set()
        bindings = [self._magento_attribute_binding(api, option, refreshed) for option in options]
        attributes = [binding._magento_odoo_attribute() for binding in bindings]
        if len(set(attributes)) != len(attributes):
            raise UserError(
                "Configurable %s: two of its Magento attributes map to the same Odoo "
                "attribute. Map them to distinct attributes (Magento > Catalog > Attributes)." % sku
            )

        # Combination (Odoo attribute values, in option order) of each child.
        rows = []
        for child in children:
            child_sku = child.get("sku")
            if not child_sku:
                continue
            values = []
            for binding in bindings:
                raw = _custom_attribute(child, binding.attribute_code)
                if raw in (None, "", False):
                    values = None
                    break
                values.append(binding._magento_odoo_value(str(raw)))
            if values is None:
                _logger.info(
                    "Magento configurable %s (instance %s): child %s has no value for one "
                    "of the options; not imported as a variant.", sku, self.name, child_sku,
                )
                continue
            rows.append((child, values))

        mapping = self._magento_find_product_map(magento_id, sku)
        template = mapping.odoo_template_id.exists() if mapping else self.env["product.template"]
        if not rows and not template:
            return ("skipped", "configurable %s has no child product with its option values" % sku)

        Product = self.env["product.product"].with_context(active_test=False)
        child_maps = {}
        for child, _values in rows:
            child_maps[child["sku"]] = self._magento_find_product_map(child.get("id"), child["sku"])
        currents = {}
        for child, _values in rows:
            child_map = child_maps[child["sku"]]
            current = child_map.odoo_product_id if child_map else Product.browse()
            if not current:
                current = Product.search([("default_code", "=", child["sku"])], limit=1)
            currents[child["sku"]] = current

        created = not mapping
        if not template:
            base = next((c for c in currents.values() if c and self._magento_is_standalone(c, c.product_tmpl_id.browse())), Product.browse())
            template = self._magento_create_configurable_template(data, base)

        self._magento_update_attribute_lines(template, bindings, attributes, rows)
        ptav_of = {
            ptav.product_attribute_value_id.id: ptav
            for ptav in self.env["product.template.attribute.value"].search([
                ("product_tmpl_id", "=", template.id), ("ptav_active", "=", True),
            ])
        }
        by_combo = {
            frozenset(v.product_template_attribute_value_ids.ids): v
            for v in template.with_context(active_test=False).product_variant_ids
        }
        counts = {"created": 0, "merged": 0, "replaced": 0, "kept": 0, "conflicts": 0}
        Map = self.env["magento.product.map"].with_context(active_test=False)
        bound = Product.browse()
        link_rows = []
        enabled_prices = []
        template_active = str(data.get("status", 1)) == "1"
        for position, (child, values) in enumerate(rows):
            child_sku = child["sku"]
            combo = self.env["product.template.attribute.value"].concat(*[ptav_of[v.id] for v in values])
            key = frozenset(combo.ids)
            child_map = child_maps[child_sku]
            current = currents[child_sku]
            target = self._magento_bind_variant(template, combo, key, by_combo, child_map, current, counts)
            link_rows.append({"child_sku": child_sku, "position": position, "qty": 1.0})
            if not target:
                continue
            bound |= target
            enabled = str(child.get("status", 1)) == "1"
            if enabled and _to_float(child.get("price")) > 0:
                enabled_prices.append(_to_float(child["price"]))
            variant_vals = {}
            if target.default_code != child_sku:
                variant_vals["default_code"] = child_sku
            if target.active != (enabled and template_active):
                variant_vals["active"] = enabled and template_active
            if child.get("weight") is not None and target.weight != _to_float(child.get("weight")):
                variant_vals["weight"] = _to_float(child.get("weight"))
            if variant_vals:
                target.with_context(**_IMPORT_CTX).write(variant_vals)
            if not child_map:
                child_map = Map.with_context(**_IMPORT_CTX).create({
                    "instance_id": self.id,
                    "magento_id": str(child["id"]) if child.get("id") is not None else False,
                    "sku": child_sku,
                    "name": child.get("name") or child_sku,
                    "odoo_product_id": target.id,
                })
            values_map = child_map._magento_child_values(child)
            values_map["odoo_product_id"] = target.id
            child_map.with_context(**_IMPORT_CTX).write(values_map)
            child_maps[child_sku] = child_map
            self._magento_resolve_pending_links(child_map)

        # Variants that no Magento child claims (e.g. left over from an earlier
        # configuration) go away, or are archived when something references them.
        if bound:
            leftovers = template.with_context(active_test=False).product_variant_ids - bound
            if leftovers:
                claimed = Map.search([("odoo_product_id", "in", leftovers.ids)]).odoo_product_id
                (leftovers - claimed).sudo()._unlink_or_archive()
        self._magento_apply_product_kind(template, "configurable")

        # Template and parent map.
        template_vals = self._magento_configurable_template_vals(data, enabled_prices)
        changed = {k: v for k, v in template_vals.items() if template[k] != v}
        image = self._magento_fetch_main_image(api, sku, data)
        if image and _b64_text(template.image_1920) != image:
            changed["image_1920"] = image
        if changed:
            template.with_context(**_NO_VARIANTS_CTX).write(changed)
        if not mapping:
            mapping = Map.with_context(**_IMPORT_CTX).create({
                "instance_id": self.id,
                "magento_id": str(magento_id) if magento_id is not None else False,
                "sku": sku,
                "name": data.get("name") or sku,
                "type_id": "configurable",
                "odoo_template_id": template.id,
            })
        map_vals = mapping._apply_magento_data(data)
        map_vals.update({"type_id": "configurable", "odoo_template_id": template.id, "odoo_product_id": False})
        if image:
            map_vals["image_1920"] = image
        mapping.with_context(**_IMPORT_CTX).write(map_vals)
        mapping._magento_sync_links("configurable", link_rows)
        categories = mapping._magento_categories_in_order(data)
        if categories is not None:
            self._magento_set_template_categories(template, categories)
        box.append(mapping)
        _logger.info(
            "Magento configurable %s (instance %s): %s variant(s); created %s, merged %s "
            "standalone product(s), replaced %s, kept %s, conflicts %s.",
            sku, self.name, len(bound), counts["created"], counts["merged"], counts["replaced"],
            counts["kept"], counts["conflicts"],
        )
        return "created" if created else "updated"

    def _magento_create_configurable_template(self, data, base):
        """New template (no variant yet) of a configurable; product type, unit and
        company follow ``base`` (an existing standalone child) so it can be merged."""
        self.ensure_one()
        vals = {
            "name": data.get("name") or data.get("sku"),
            "sale_ok": True,
        }
        if base:
            vals.update({
                "type": base.type,
                "uom_id": base.uom_id.id,
                "company_id": base.company_id.id or False,
            })
            if "is_storable" in base._fields:
                vals["is_storable"] = base.is_storable
        return self.env["product.template"].with_context(**_NO_VARIANTS_CTX).create(vals)

    def _magento_update_attribute_lines(self, template, bindings, attributes, rows):
        """Make sure the template has a line per option with every value its children use."""
        self.ensure_one()
        lines = {line.attribute_id: line for line in template.attribute_line_ids}
        # Odoo 18 skips the template attribute values when lines are created without
        # variants, and generates every combination when they are written: keep both
        # off and add the missing values ourselves (Odoo 19 does it in the line).
        Line = self.env["product.template.attribute.line"].with_context(
            **_NO_VARIANTS_CTX, update_product_template_attribute_values=False)
        touched = Line.browse()
        for index, attribute in enumerate(attributes):
            used = self.env["product.attribute.value"].concat(*[values[index] for _child, values in rows])
            line = lines.get(attribute)
            if line:
                missing = used - line.value_ids
                if missing:
                    line.with_env(Line.env).write({"value_ids": [(4, value.id) for value in missing]})
                    touched |= line
            elif used:
                touched |= Line.create({
                    "product_tmpl_id": template.id,
                    "attribute_id": attribute.id,
                    "value_ids": [(6, 0, used.ids)],
                    "sequence": index,
                })
        self._magento_ensure_template_values(touched)

    def _magento_ensure_template_values(self, lines):
        """Create (or reactivate) the template attribute value of every value of
        ``lines``, without creating product variants."""
        PTAV = self.env["product.template.attribute.value"].with_context(active_test=False)
        to_create = []
        for line in lines:
            existing = {
                ptav.product_attribute_value_id: ptav
                for ptav in PTAV.search([("attribute_line_id", "=", line.id)])
            }
            for value in line.value_ids:
                ptav = existing.get(value)
                if not ptav:
                    to_create.append({
                        "product_attribute_value_id": value.id,
                        "attribute_line_id": line.id,
                        "price_extra": value.default_extra_price,
                    })
                elif not ptav.ptav_active:
                    ptav.ptav_active = True
        if to_create:
            PTAV.create(to_create)

    def _magento_is_standalone(self, product, template):
        """Whether ``product`` is a plain product of its own (template with that single
        variant, no attribute line, not bound by a configurable): one that may be merged
        into ``template``."""
        if not product:
            return False
        own = product.product_tmpl_id
        if own == template or own.attribute_line_ids:
            return False
        if len(own.with_context(active_test=False).product_variant_ids) != 1:
            return False
        return not self.env["magento.product.map"].sudo().with_context(active_test=False).search_count([
            ("odoo_template_id", "=", own.id), ("type_id", "=", "configurable"),
        ], limit=1)

    @staticmethod
    def _magento_can_move(product, template):
        """Moving ``product`` under ``template`` changes nothing it is measured or
        stocked by."""
        if product.uom_id != template.uom_id or product.type != template.type:
            return False
        if "is_storable" in template._fields and product.is_storable != template.is_storable:
            return False
        if product.company_id and template.company_id and product.company_id != template.company_id:
            return False
        return True

    def _magento_bind_variant(self, template, combo, key, by_combo, child_map, current, counts):
        """The variant of ``template`` for a child SKU with combination ``combo``.

        ``current`` is the SKU's product today (its map's, else by internal reference).
        Merges a standalone ``current`` into the template (see module docstring).
        Returns the variant, or an empty recordset when the SKU is not (re)bound."""
        Product = self.env["product.product"].with_context(active_test=False)
        variant = by_combo.get(key, Product.browse())
        if variant and variant != current:
            others = self.env["magento.product.map"].with_context(active_test=False).search([
                ("odoo_product_id", "=", variant.id), ("id", "!=", child_map.id or 0),
            ], limit=1)
            if others:
                # Magento never lets two children share a combination: an older binding
                # (e.g. a child SKU that was renamed) still holds it.
                _logger.warning(
                    "Magento configurable %s: variant %s is already bound to SKU %s; SKU %s "
                    "not bound.", template.display_name, variant.display_name, others.sku,
                    child_map.sku or current.default_code,
                )
                counts["conflicts"] += 1
                return Product.browse()

        if current and current.product_tmpl_id == template:
            if frozenset(current.product_template_attribute_value_ids.ids) != key:
                if variant and variant != current:
                    by_combo.pop(key, None)
                    variant.sudo()._unlink_or_archive()
                current.with_context(**_IMPORT_CTX).write({
                    "product_template_attribute_value_ids": [(6, 0, combo.ids)],
                })
            by_combo[key] = current
            counts["kept"] += 1
            return current

        if current and self._magento_is_standalone(current, template):
            if self._magento_can_move(current, template):
                if variant:
                    by_combo.pop(key, None)
                    variant.sudo()._unlink_or_archive()
                self._magento_move_variant(current, template, combo)
                by_combo[key] = current
                counts["merged"] += 1
                return current
            # Cannot move it safely: a new variant takes over the SKU; the old product
            # stays (archived) for its history.
            target = self._magento_variant_for(template, combo, key, by_combo, variant)
            current.with_context(**_IMPORT_CTX).write({"active": False})
            counts["replaced"] += 1
            return target

        if current:
            _logger.info(
                "Magento configurable %s: SKU %s is already a variant of %s; left there.",
                template.display_name, current.default_code, current.product_tmpl_id.display_name,
            )
            counts["conflicts"] += 1
            return Product.browse()

        target = self._magento_variant_for(template, combo, key, by_combo, variant)
        counts["created"] += 1
        return target

    def _magento_variant_for(self, template, combo, key, by_combo, variant):
        if variant:
            return variant
        variant = self.env["product.product"].with_context(**_IMPORT_CTX).create({
            "product_tmpl_id": template.id,
            "product_template_attribute_value_ids": [(6, 0, combo.ids)],
        })
        by_combo[key] = variant
        return variant

    def _magento_move_variant(self, product, template, combo):
        """Move a standalone product under ``template`` as the variant ``combo``, keeping
        its id (sale lines, stock, map); then drop its emptied template."""
        old_template = product.product_tmpl_id
        vals = {
            "product_tmpl_id": template.id,
            "product_template_attribute_value_ids": [(6, 0, combo.ids)],
        }
        if not product.image_variant_1920 and old_template.image_1920:
            vals["image_variant_1920"] = old_template.image_1920
        product.with_context(**_IMPORT_CTX).write(vals)
        self.env.flush_all()
        old_template.invalidate_recordset()
        self._magento_drop_empty_template(old_template)

    def _magento_template_is_referenced(self, template):
        """Whether any row (foreign key) still points to ``template``, other than the
        template's own many2many values (taxes, routes, tags), which go with it."""
        cr = self.env.cr
        self.env.flush_all()
        own = {
            (field.relation, field.column1)
            for field in template._fields.values()
            if field.type == "many2many" and field.store
        }
        cr.execute(_TEMPLATE_FK_SQL)
        for table, column in cr.fetchall():
            if (table, column) in own:
                continue
            cr.execute(SQL("SELECT 1 FROM %s WHERE %s = %s LIMIT 1",
                           SQL.identifier(table), SQL.identifier(column), template.id))
            if cr.fetchone():
                return True
        return False

    def _magento_drop_empty_template(self, template):
        """Delete a template left without variants by a merge; archive it when something
        still references it. Returns ``"deleted"``, ``"archived"`` or None."""
        template = template.sudo().with_context(active_test=False).exists()
        if not template or template.product_variant_ids:
            return None
        if not self._magento_template_is_referenced(template):
            try:
                with self.env.cr.savepoint():
                    template.with_context(**_NO_VARIANTS_CTX).unlink()
                return "deleted"
            except Exception as exc:  # noqa: BLE001 - fall back to archiving
                _logger.info("Empty template %s kept archived: %s", template.id, _short_reason(exc))
                template = template.exists()
                if not template:
                    return "deleted"
        if template.active:
            template.with_context(**_NO_VARIANTS_CTX).write({"active": False})
        return "archived"

    def _magento_configurable_template_vals(self, data, enabled_prices):
        """Template values of a configurable (name, price, descriptions, active)."""
        price = _to_float(data.get("price"))
        if price <= 0 and enabled_prices:
            price = min(enabled_prices)
        vals = {
            "name": data.get("name") or data.get("sku"),
            "list_price": price,
            "active": str(data.get("status", 1)) == "1",
        }
        description = _custom_attribute(data, "description")
        if description is not None:
            vals["description"] = description or False
        short = _custom_attribute(data, "short_description")
        if short is not None:
            vals["description_sale"] = short or False
        return vals

    def _magento_fetch_main_image(self, api, sku, data):
        """Base64 of the product's first gallery image, or None (images are cosmetic)."""
        entries = data.get("media_gallery_entries") or []
        entry_id = entries[0].get("id") if entries else None
        if not entry_id:
            return None
        try:
            media = api.get_product_media(sku, entry_id)
        except requests.exceptions.RequestException as exc:
            _logger.warning("Magento product %s (instance %s): image fetch failed: %s",
                            sku, self.name, _short_reason(exc))
            return None
        return ((media or {}).get("content") or {}).get("base64_encoded_data") or None

    # ------------------------------------------------------------------
    # Composite-only import (migration of an existing catalogue)
    # ------------------------------------------------------------------
    def sync_composite_products(self, types=_COMPOSITE_TYPES):
        """Import (again) only the configurable, bundle and grouped products.

        This is the migration path of a catalogue imported before configurables were
        supported: each configurable merges its standalone children into variants.
        Watermarks are not touched. Returns the usual counts dict."""
        stats = _SyncStats("product")
        supported = self._supported_product_types()
        for instance in self:
            api = MagentoAPI(instance)
            fetch_stats = {}
            rows = instance._fetch_list(stats, "product", lambda: api.get_products(
                None, filters=[("type_id", ",".join(types), "in")],
                page_size=instance._get_sync_page_size(), lazy=True, stats=fetch_stats,
            ))
            if rows is None:
                continue
            for index, row in enumerate(instance._iter_fetched(stats, "product", rows), start=1):
                if row.get("sku"):
                    instance._sync_one_product(api, row, supported, stats)
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _magento_import_product_skus(self, skus):
        """Job body: import these SKUs through the normal per-record path.

        Stops after ``_COMPOSITE_JOB_TIME_BUDGET`` seconds and queues the remaining
        SKUs as a follow-up job on the same channel."""
        self.ensure_one()
        stats = _SyncStats("product")
        api = MagentoAPI(self)
        supported = self._supported_product_types()
        skus = list(skus or ())
        deadline = _clock() + _COMPOSITE_JOB_TIME_BUDGET
        for index, sku in enumerate(skus):
            if index and _clock() >= deadline:
                rest = skus[index:]
                self.env["magento.job"].enqueue(
                    self, "_magento_import_product_skus", args=(rest,), instance=self,
                    channel=self._magento_sync_channel("products"), priority=10,
                    identity_key="sync-%s-composites-%s" % (self.id, rest[0]),
                    description="Magento composite products (%s): %s" % (self.name, ", ".join(rest)),
                )
                break
            try:
                data = api.get_product(sku)
            except Exception as exc:  # noqa: BLE001 - reported, the next SKU runs
                stats.error("product", sku, exc, instance=self, reason=self._format_magento_request_error(exc, self))
                self._magento_notify_record_result("product", sku, False, exc=exc)
                continue
            if isinstance(data, dict) and data:
                data.setdefault("sku", sku)
                self._sync_one_product(api, data, supported, stats, product_data=data)
        counts = stats.as_dict()
        return "Products: %s synced, %s skipped, %s errors" % (
            counts["created"] + counts["updated"], counts["skipped"], counts["errors"])

    def _magento_enqueue_composite_import(self):
        """Job body: list the composite SKUs, queue them in chunks."""
        self.ensure_one()
        api = MagentoAPI(self)
        rows = api.get_products(None, filters=[("type_id", ",".join(_COMPOSITE_TYPES), "in")],
                                page_size=self._get_sync_page_size())
        skus = [row["sku"] for row in rows or [] if isinstance(row, dict) and row.get("sku")]
        jobs = self.env["magento.job"]
        for start in range(0, len(skus), _COMPOSITE_JOB_CHUNK):
            chunk = skus[start:start + _COMPOSITE_JOB_CHUNK]
            jobs.enqueue(
                self, "_magento_import_product_skus", args=(chunk,), instance=self,
                channel=self._magento_sync_channel("products"), priority=10,
                identity_key="sync-%s-composites-%s" % (self.id, chunk[0]),
                description="Magento composite products (%s): %s" % (self.name, ", ".join(chunk)),
            )
        return "%s composite product(s) queued in %s job(s)" % (
            len(skus), -(-len(skus) // _COMPOSITE_JOB_CHUNK))

    def action_import_composite_products(self):
        """Button: import configurable, bundle and grouped products (merges existing
        standalone children into variants)."""
        self.ensure_one()
        if self._magento_sync_in_background():
            job = self.env["magento.job"].enqueue(
                self, "_magento_enqueue_composite_import", instance=self,
                channel=self._magento_sync_channel("products"), priority=10,
                identity_key="sync-%s-composites" % self.id,
                description="Magento composite products (%s): list" % self.name,
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Magento Sync",
                    "message": "Queued: configurable, bundle and grouped products (job %s) "
                               "— follow progress in Job Queue." % job.id,
                    "type": "info",
                    "sticky": False,
                },
            }
        start_time = fields.Datetime.now()
        counts = self.sync_composite_products()
        self._create_sync_report("product", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Configurable / bundle / grouped product", counts)
