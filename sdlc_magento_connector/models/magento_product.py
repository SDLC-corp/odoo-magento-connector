import base64
import functools
import logging
import re

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.mimetypes import guess_mimetype

from ..services.magento_api import MagentoAPI

_logger = logging.getLogger(__name__)

# Context keys that suppress the automatic (post-commit) push to Magento.
# ``magento_skip_push`` is the public key that import/sync code must pass;
# ``skip_magento_sync`` is the historical key, still honoured.
PUSH_SKIP_CONTEXT_KEYS = ("magento_skip_push", "skip_magento_sync")

# Product types Magento may return that the connector can read but not export yet.
UNSUPPORTED_EXPORT_TYPES = ("configurable", "bundle", "grouped")

_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def magento_push_skipped(env):
    return any(env.context.get(key) for key in PUSH_SKIP_CONTEXT_KEYS)


def magento_error_message(exc):
    """Human readable message for any exception raised while talking to Magento."""
    if isinstance(exc, UserError):
        return exc.args[0] if exc.args else str(exc)
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            message = response.json().get("message") or response.text
        except Exception:
            message = response.text
        return f"Magento API error ({response.status_code}): {message}"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return f"Could not reach Magento: {exc}"
    return str(exc) or exc.__class__.__name__


def raise_magento_user_error(exc):
    """Re-raise a requests exception as a UserError with a clear message."""
    raise UserError(magento_error_message(exc)) from exc


def schedule_magento_push(records, flags=()):
    """Queue ``records._magento_auto_push(flags)`` to run after the current
    transaction commits, in its own cursor.

    Pushes are deduplicated per record within one transaction; ``flags`` of
    repeated calls are merged. Nothing is queued when the context carries
    ``magento_skip_push`` (or the legacy ``skip_magento_sync``). If the
    transaction rolls back, Odoo discards the queue and nothing is pushed.
    """
    if not records or magento_push_skipped(records.env):
        return
    ids = [rid for rid in records.ids if isinstance(rid, int)]
    if not ids:
        return
    env = records.env
    postcommit = env.cr.postcommit
    key = f"sdlc_magento_connector.push.{records._name}"
    entry = postcommit.data.get(key)
    if entry is None:
        entry = postcommit.data[key] = {
            "uid": env.uid,
            "su": env.su,
            "context": dict(env.context),
            "ids": {},
        }
        postcommit.add(functools.partial(_run_magento_push, env.registry, records._name, entry))
    for rid in ids:
        entry["ids"].setdefault(rid, set()).update(flags or ())


def _run_magento_push(registry, model_name, entry):
    pending = entry.get("ids") or {}
    if not pending:
        return
    context = dict(entry["context"], magento_skip_push=True)
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, entry["uid"], context, su=entry["su"])
            for record in env[model_name].browse(list(pending)).exists():
                try:
                    with cr.savepoint():
                        record._magento_auto_push(pending.get(record.id) or set())
                except Exception as exc:  # noqa: BLE001 - must never break the caller
                    message = magento_error_message(exc)
                    _logger.warning(
                        "Magento push failed for %s(%s): %s", model_name, record.id, message
                    )
                    record._magento_set_push_error(message)
                else:
                    if record.magento_last_error:
                        record._magento_set_push_error(False)
    except Exception:  # noqa: BLE001
        _logger.warning("Magento post-commit push for %s could not run", model_name, exc_info=True)


class MagentoPushErrorMixin(models.AbstractModel):
    _name = "magento.push.error.mixin"
    _description = "Magento automatic push error tracking"

    magento_last_error = fields.Char(
        string="Last Magento Push Error",
        readonly=True,
        copy=False,
        help="Error of the last automatic push to Magento. Cleared on the next successful push.",
    )

    def _magento_set_push_error(self, message):
        if message:
            message = f"{fields.Datetime.to_string(fields.Datetime.now())} UTC: {message}"[:1000]
        self.with_context(
            magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True
        ).write({"magento_last_error": message or False})

    def _magento_api(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError("Magento Instance is required.")
        return MagentoAPI(self.instance_id.sudo())

    def _raise_magento_http_error(self, exc):
        raise_magento_user_error(exc)

    def _magento_auto_push(self, flags):
        raise NotImplementedError


def _get_maps_for_products(products):
    """Resolve Magento maps for product variants, with SKU fallback for orphan links."""
    if not products:
        return products.env["magento.product.map"]

    map_model = products.env["magento.product.map"]
    maps = map_model.search([("odoo_product_id", "in", products.ids)])
    skus = [sku for sku in products.mapped("default_code") if sku]
    if not skus:
        return maps

    sku_maps = map_model.search([("sku", "in", skus)])
    products_by_sku = {}
    ambiguous_skus = set()
    for product in products:
        sku = product.default_code
        if not sku:
            continue
        if sku in products_by_sku and products_by_sku[sku].id != product.id:
            ambiguous_skus.add(sku)
            continue
        products_by_sku.setdefault(sku, product)

    # Backfill missing map->product links so future sync uses direct relation.
    for mapping in sku_maps:
        if mapping.odoo_product_id or not mapping.sku:
            continue
        if mapping.sku in ambiguous_skus:
            continue
        product = products_by_sku.get(mapping.sku)
        if not product:
            continue
        mapping.with_context(
            magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True
        ).write({
            "odoo_product_id": product.id,
        })

    maps |= sku_maps.filtered(lambda rec: rec.odoo_product_id and rec.odoo_product_id.id in products.ids)
    return maps


# Fields of magento.product.map whose change must not trigger a push to Magento.
_MAP_NO_PUSH_FIELDS = {
    "magento_last_error",
    "magento_image_entry_id",
    "magento_id",
    "odoo_product_id",
}


class MagentoProductMap(models.Model):
    _name = "magento.product.map"
    _inherit = ["magento.push.error.mixin"]
    _description = "Magento Product Mapping"

    _uniq_instance_magento_id = models.UniqueIndex(
        "(instance_id, magento_id) WHERE magento_id IS NOT NULL AND magento_id != ''",
        "This Magento product is already mapped for this instance.",
    )
    _uniq_instance_sku = models.UniqueIndex(
        "(instance_id, sku) WHERE sku IS NOT NULL AND sku != ''",
        "A product mapping with this SKU already exists for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    magento_id = fields.Char()
    sku = fields.Char()
    name = fields.Char()
    price = fields.Float()
    attribute_set_id = fields.Many2one(
        "magento.attribute.set",
        string="Attribute Set",
        ondelete="set null",
    )
    enable_product = fields.Boolean(default=True)
    tax_class = fields.Selection(
        [
            ("none", "None"),
            ("taxable_goods", "Taxable Goods"),
        ],
        default="taxable_goods",
    )
    tax_class_id = fields.Integer(string="Tax Class ID")
    country_of_manufacturer_id = fields.Many2one(
        "res.country",
        string="Country of Manufacturer",
    )
    magento_category_ids = fields.Many2many(
        "magento.category",
        "magento_product_category_rel",
        "product_id",
        "category_id",
        string="Categories",
    )
    magento_parent_category_id = fields.Many2one(
        "magento.category",
        string="Parent Category",
    )
    type_id = fields.Selection(
        [
            ("simple", "Simple"),
            ("virtual", "Virtual"),
            ("downloadable", "Downloadable"),
            ("configurable", "Configurable"),
            ("bundle", "Bundle"),
            ("grouped", "Grouped"),
        ],
        default="simple",
        help="Configurable, bundle and grouped products can be imported but not exported yet.",
    )
    status = fields.Selection([("1", "Enabled"), ("2", "Disabled")], default="1")
    visibility = fields.Selection(
        [("1", "Not Visible"), ("2", "Catalog"), ("3", "Search"), ("4", "Catalog, Search")],
        default="4",
    )
    weight = fields.Float()
    quantity = fields.Float()
    stock_status = fields.Selection(
        [("in_stock", "In Stock"), ("out_of_stock", "Out of Stock")],
        default="in_stock",
    )
    has_weight = fields.Selection(
        [("has_weight", "This item has weight"), ("no_weight", "This item has no weight")],
        default="has_weight",
    )
    new_from_date = fields.Date()
    new_to_date = fields.Date()
    description = fields.Text()
    short_description = fields.Text()
    image_1920 = fields.Image(string="Image")
    magento_image_entry_id = fields.Char(
        string="Magento Image Entry ID",
        readonly=True,
        copy=False,
        help="Media gallery entry of the image exported from Odoo.",
    )
    odoo_product_id = fields.Many2one("product.product")

    def _check_exportable(self):
        for record in self:
            if record.type_id in UNSUPPORTED_EXPORT_TYPES:
                type_label = dict(record._fields["type_id"].selection).get(record.type_id)
                raise UserError(
                    f"Exporting {type_label} products to Magento is not supported yet "
                    f"(SKU: {record.sku or record.name or record.id}). "
                    "Manage this product in Magento directly."
                )

    def _build_magento_payload(self):
        self.ensure_one()
        self._check_exportable()
        if not self.sku or not self.name:
            raise UserError("SKU and Name are required.")
        if not self.attribute_set_id:
            raise UserError("Attribute Set ID is required.")
        attribute_set_magento_id = self.attribute_set_id.magento_id
        if not attribute_set_magento_id:
            raise UserError("Attribute Set is missing Magento ID.")
        status_val = int(self.status) if self.status else (1 if self.enable_product else 2)
        payload = {
            "product": {
                "sku": self.sku,
                "name": self.name,
                "attribute_set_id": int(attribute_set_magento_id),
                "price": self.price or 0.0,
                "status": status_val,
                "type_id": self.type_id,
                "visibility": int(self.visibility),
            }
        }
        if self.has_weight == "has_weight" and self.weight:
            payload["product"]["weight"] = self.weight
        if self.has_weight == "no_weight":
            payload["product"]["weight"] = 0.0
        if self.tax_class_id:
            payload["product"]["tax_class_id"] = int(self.tax_class_id)
        custom_attributes = []
        custom_attributes.append({
            "attribute_code": "description",
            "value": self.description or "",
        })
        custom_attributes.append({
            "attribute_code": "short_description",
            "value": self.short_description or "",
        })
        custom_attributes.append({
            "attribute_code": "country_of_manufacture",
            "value": self.country_of_manufacturer_id.code if self.country_of_manufacturer_id else "",
        })
        custom_attributes.append({
            "attribute_code": "news_from_date",
            "value": fields.Date.to_string(self.new_from_date) if self.new_from_date else "",
        })
        custom_attributes.append({
            "attribute_code": "news_to_date",
            "value": fields.Date.to_string(self.new_to_date) if self.new_to_date else "",
        })
        if custom_attributes:
            payload["product"]["custom_attributes"] = custom_attributes
        category_links = []
        for category in self.magento_category_ids:
            if category.magento_id:
                cat_id = self._category_magento_id_int(category)
                if cat_id is not None:
                    category_links.append({"position": 0, "category_id": cat_id})
        if self.magento_parent_category_id and self.magento_parent_category_id.magento_id:
            parent_id = self._category_magento_id_int(self.magento_parent_category_id)
            if parent_id is not None:
                category_links.append({
                    "position": 0,
                    "category_id": parent_id,
                })
        if category_links:
            payload["product"].setdefault("extension_attributes", {})
            payload["product"]["extension_attributes"]["category_links"] = category_links
        if self.quantity or self.stock_status:
            payload["product"].setdefault("extension_attributes", {})
            payload["product"]["extension_attributes"]["stock_item"] = {
                "qty": float(self.quantity or 0.0),
                "is_in_stock": self.stock_status == "in_stock",
            }
        # Apply user-defined field mappings on top of default payload.
        self.env["magento.field.mapping"].sudo().apply_outbound_mapping_to_payload(
            mapping_type="product",
            instance=self.instance_id,
            source_record=self,
            payload=payload,
            wrapper_key="product",
        )
        return payload

    def _build_mapped_payload_only(self):
        """Build payload with only mapped fields, to avoid full-product required fields."""
        self.ensure_one()
        payload = {"product": {}}
        self.env["magento.field.mapping"].sudo().apply_outbound_mapping_to_payload(
            mapping_type="product",
            instance=self.instance_id,
            source_record=self,
            payload=payload,
            wrapper_key="product",
        )
        return payload

    def _get_custom_attribute(self, data, code):
        for item in data.get("custom_attributes", []) or []:
            if item.get("attribute_code") == code:
                return item.get("value")
        return None

    def _category_magento_id_int(self, category):
        try:
            return int(category.magento_id)
        except (TypeError, ValueError):
            return None

    def _image_export_info(self):
        """Return (base64 str, mimetype, file name) for the image to export, or None."""
        self.ensure_one()
        data = self.image_1920
        if not data or not self.sku:
            return None
        if isinstance(data, bytes):
            data = data.decode()
        data = data.strip()
        try:
            raw = base64.b64decode(data)
        except (ValueError, TypeError):
            _logger.warning("Product map %s has an invalid image; image not exported.", self.id)
            return None
        mimetype = guess_mimetype(raw, default="image/jpeg")
        extension = _IMAGE_EXTENSIONS.get(mimetype)
        if not extension:
            raise UserError(
                f"Image type {mimetype} is not supported by Magento "
                f"(SKU: {self.sku}). Use a JPEG, PNG, GIF or WEBP image."
            )
        safe_sku = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.sku).strip("._") or "product"
        return data, mimetype, f"{safe_sku}.{extension}"

    def _sync_magento_image(self, api):
        self.ensure_one()
        info = self._image_export_info()
        if not info:
            return
        base64_data, mimetype, file_name = info
        try:
            product = api.get_product(self.sku)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)
        media_entries = product.get("media_gallery_entries") or []
        existing = None
        for entry in media_entries:
            if self.magento_image_entry_id and str(entry.get("id")) == self.magento_image_entry_id:
                existing = entry
                break
        if not existing:
            # Entries exported by older versions of the connector used this fixed label.
            existing = next((e for e in media_entries if e.get("label") == "Odoo Image"), None)
        payload = {
            "entry": {
                "media_type": "image",
                "label": self.name or self.sku,
                "position": 1,
                "disabled": False,
                "types": ["image", "small_image", "thumbnail"],
                "content": {
                    "base64_encoded_data": base64_data,
                    "type": mimetype,
                    "name": file_name,
                },
            }
        }
        try:
            result = None
            if existing and existing.get("id"):
                payload["entry"]["id"] = existing["id"]
                try:
                    api.update_product_media(self.sku, existing["id"], payload)
                    result = existing["id"]
                except requests.exceptions.HTTPError as exc:
                    response = exc.response
                    if response is not None and response.status_code == 404:
                        payload["entry"].pop("id", None)
                        result = api.add_product_media(self.sku, payload)
                    else:
                        raise
            else:
                result = api.add_product_media(self.sku, payload)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)
        if isinstance(result, (int, str)) and str(result).isdigit():
            self.with_context(
                magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True
            ).write({"magento_image_entry_id": str(result)})

    def _apply_magento_data(self, data):
        attribute_set_id = data.get("attribute_set_id")
        attribute_set = False
        if attribute_set_id is not None:
            attr_model = self.env["magento.attribute.set"].with_context(active_test=False)
            attribute_set = attr_model.search([
                ("instance_id", "=", self.instance_id.id),
                ("magento_id", "=", str(attribute_set_id)),
            ], limit=1)
            if not attribute_set:
                attribute_set = attr_model.with_context(
                    magento_skip_push=True, skip_magento_sync=True
                ).create({
                    "instance_id": self.instance_id.id,
                    "magento_id": str(attribute_set_id),
                    "name": f"Attribute Set {attribute_set_id}",
                })
        type_id = data.get("type_id") or self.type_id
        valid_types = {key for key, _label in self._fields["type_id"].selection}
        if type_id not in valid_types:
            _logger.info(
                "Magento product %s has unsupported type %r; keeping %r.",
                data.get("sku"), type_id, self.type_id,
            )
            type_id = self.type_id
        values = {
            "magento_id": str(data["id"]) if data.get("id") else (self.magento_id or False),
            "sku": data.get("sku") or self.sku,
            "name": data.get("name") or self.name,
            "price": data.get("price") or 0.0,
            "status": str(data.get("status")) if data.get("status") is not None else self.status,
            "visibility": str(data.get("visibility")) if data.get("visibility") is not None else self.visibility,
            "attribute_set_id": attribute_set.id if attribute_set else self.attribute_set_id.id,
            "type_id": type_id,
            "weight": data.get("weight") or 0.0,
            "tax_class_id": data.get("tax_class_id") or self.tax_class_id,
        }
        values["enable_product"] = str(values["status"]) == "1"
        values["has_weight"] = "has_weight" if values.get("weight") else "no_weight"
        description = self._get_custom_attribute(data, "description")
        if description is not None:
            values["description"] = description
        short_description = self._get_custom_attribute(data, "short_description")
        if short_description is not None:
            values["short_description"] = short_description
        country_code = self._get_custom_attribute(data, "country_of_manufacture")
        if country_code is not None:
            country = self.env["res.country"].search([("code", "=", country_code)], limit=1)
            values["country_of_manufacturer_id"] = country.id if country else False
        news_from = self._get_custom_attribute(data, "news_from_date")
        news_to = self._get_custom_attribute(data, "news_to_date")
        if news_from is not None:
            values["new_from_date"] = news_from or False
        if news_to is not None:
            values["new_to_date"] = news_to or False
        category_links = (data.get("extension_attributes") or {}).get("category_links")
        if category_links is not None:
            category_ids = []
            category_model = self.env["magento.category"]
            for link in category_links:
                cat_id = link.get("category_id")
                if cat_id is None:
                    continue
                category = category_model.search([
                    ("instance_id", "=", self.instance_id.id),
                    ("magento_id", "=", str(cat_id)),
                ], limit=1)
                if not category:
                    category = category_model.create({
                        "instance_id": self.instance_id.id,
                        "magento_id": str(cat_id),
                        "name": f"Magento Category {cat_id}",
                    })
                category_ids.append(category.id)
            values["magento_category_ids"] = [(6, 0, category_ids)]
            parent_id = False
            for cat in category_model.browse(category_ids):
                if cat.parent_id:
                    parent_id = cat.parent_id.id
                    break
            values["magento_parent_category_id"] = parent_id
        stock_item = (data.get("extension_attributes") or {}).get("stock_item") or {}
        if stock_item:
            values["quantity"] = stock_item.get("qty", self.quantity or 0.0)
            values["stock_status"] = "in_stock" if stock_item.get("is_in_stock") else "out_of_stock"

        # Apply mapped Magento fields back into Odoo model values.
        mapped_vals = self.env["magento.field.mapping"].sudo().apply_inbound_mapping_to_vals(
            mapping_type="product",
            instance=self.instance_id,
            source_payload=data,
        )
        if mapped_vals:
            values.update(mapped_vals)
        return values

    def action_create_magento_product(self):
        for record in self:
            api = record._magento_api()
            payload = record._build_magento_payload()
            try:
                response = api.create_product(payload)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._apply_magento_data(response)
                values["magento_last_error"] = False
                record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
                record._sync_magento_image(api)
        return self._rainbow_man_action(f"Created {len(self)} product(s) in Magento.")

    def action_update_magento_product(self):
        for record in self:
            if not record.sku:
                raise UserError("SKU is required to update a product in Magento.")
            api = record._magento_api()
            try:
                api.get_product(record.sku)
            except requests.exceptions.RequestException as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code == 404:
                    raise UserError("Magento product not found for this SKU. Use 'Create in Magento' first.") from exc
                record._raise_magento_http_error(exc)
            payload = record._build_magento_payload()
            try:
                response = api.update_product(record.sku, payload)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._apply_magento_data(response)
                values["magento_last_error"] = False
                record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
                record._sync_magento_image(api)
        return self._rainbow_man_action(f"Updated {len(self)} product(s) in Magento.")

    def action_update_magento_mapped_fields(self):
        """Push only mapped fields to Magento product."""
        for record in self:
            if not record.sku:
                continue
            if not record.instance_id:
                continue
            record._check_exportable()
            payload = record._build_mapped_payload_only()
            if not payload.get("product"):
                continue
            api = record._magento_api()
            try:
                response = api.update_product(record.sku, payload)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._apply_magento_data(response)
                record.with_context(magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True).write(values)

    def _rainbow_man_action(self, message):
        return {
            "type": "ir.actions.act_window_close",
            "effect": {
                "fadeout": "slow",
                "message": message,
                "type": "rainbow_man",
            }
        }

    def action_pull_magento_product(self):
        for record in self:
            if not record.sku:
                raise UserError("SKU is required to pull product from Magento.")
            api = record._magento_api()
            try:
                data = api.get_product(record.sku)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            values = record._apply_magento_data(data)
            media_entries = data.get("media_gallery_entries") or []
            if media_entries:
                entry_id = media_entries[0].get("id")
                if entry_id:
                    try:
                        media = api.get_product_media(record.sku, entry_id)
                        content = (media or {}).get("content") or {}
                        if content.get("base64_encoded_data"):
                            values["image_1920"] = content["base64_encoded_data"]
                    except requests.exceptions.RequestException:
                        _logger.debug("Could not fetch image of %s", record.sku, exc_info=True)
            record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Pulled {len(self)} product(s) from Magento.")

    def action_refresh_inventory(self):
        records = self
        if not records:
            domain = self.env.context.get("active_domain") or []
            records = self.search(domain)
        for record in records:
            if not record.sku:
                raise UserError("SKU is required to refresh inventory from Magento.")
            api = record._magento_api()
            try:
                data = api.get_product(record.sku)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            stock_item = (data.get("extension_attributes") or {}).get("stock_item") or {}
            if not stock_item:
                continue
            vals = {
                "quantity": stock_item.get("qty", record.quantity or 0.0),
                "stock_status": "in_stock" if stock_item.get("is_in_stock") else "out_of_stock",
            }
            record.with_context(magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True).write(vals)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Inventory Refresh",
                "message": f"Inventory refreshed for {len(records)} record(s).",
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_attribute_sets(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError("Select a Magento Instance first.")
        counts = self.instance_id.sync_attribute_sets()
        total_records = counts.get("created", 0) + counts.get("updated", 0)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Attribute set sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    def _ensure_odoo_product(self):
        self.ensure_one()
        if self.odoo_product_id or not self.sku:
            return
        product = self.env["product.product"].search([("default_code", "=", self.sku)], limit=1)
        if not product:
            product = self.env["product.product"].create({
                "name": self.name or self.sku,
                "default_code": self.sku,
            })
        self.with_context(magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True).write({
            "odoo_product_id": product.id,
        })

    def _magento_auto_push(self, flags):
        """Automatic (post-commit) push of this map to Magento. Raises on failure."""
        self.ensure_one()
        if not self.instance_id or not self.sku or self.type_id in UNSUPPORTED_EXPORT_TYPES:
            return
        api = self._magento_api()
        payload = self._build_magento_payload()
        try:
            api.update_product(self.sku, payload)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)
        if "image" in flags:
            self._sync_magento_image(api)

    def _schedule_magento_push(self, vals):
        if magento_push_skipped(self.env) or not set(vals) - _MAP_NO_PUSH_FIELDS:
            return
        to_push = self.filtered(
            lambda r: r.instance_id and r.sku and r.type_id not in UNSUPPORTED_EXPORT_TYPES
        )
        schedule_magento_push(to_push, flags={"image"} if "image_1920" in vals else ())

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("skip_odoo_sync"):
            for record in self:
                if record.sku and not record.odoo_product_id:
                    record._ensure_odoo_product()
        # Never call Magento inside the user's transaction: queue a push that
        # runs after commit (see schedule_magento_push).
        self._schedule_magento_push(vals)
        if not self.env.context.get("skip_odoo_sync"):
            for record in self:
                if not record.odoo_product_id:
                    continue
                odoo_vals = {}
                if "name" in vals:
                    odoo_vals["name"] = record.name
                if "sku" in vals:
                    odoo_vals["default_code"] = record.sku
                if "weight" in vals:
                    odoo_vals["weight"] = record.weight
                if "price" in vals:
                    odoo_vals["list_price"] = record.price
                if "description" in vals:
                    odoo_vals["description"] = record.description or ""
                if "short_description" in vals:
                    odoo_vals["description_sale"] = record.short_description or ""
                if "image_1920" in vals:
                    odoo_vals["image_1920"] = record.image_1920
                if "status" in vals or "enable_product" in vals:
                    odoo_vals["active"] = record.enable_product
                if odoo_vals:
                    record.odoo_product_id.with_context(
                        skip_magento_sync=True,
                        skip_odoo_sync=True,
                    ).write(odoo_vals)
        return res


class ProductProduct(models.Model):
    _inherit = "product.product"

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_odoo_sync"):
            return res
        sync_fields = {
            "name",
            "default_code",
            "weight",
            "active",
            "list_price",
            "description",
            "description_sale",
            "image_1920",
        }
        if not sync_fields.intersection(vals.keys()):
            return res
        maps = _get_maps_for_products(self)
        if not maps:
            return res
        for record in maps:
            map_vals = {}
            if "name" in vals:
                map_vals["name"] = record.odoo_product_id.name
            if "default_code" in vals:
                map_vals["sku"] = record.odoo_product_id.default_code
            if "weight" in vals:
                map_vals["weight"] = record.odoo_product_id.weight
            if "active" in vals:
                map_vals["enable_product"] = record.odoo_product_id.active
                map_vals["status"] = "1" if record.odoo_product_id.active else "2"
            if "list_price" in vals:
                map_vals["price"] = record.odoo_product_id.lst_price
            if "description" in vals:
                map_vals["description"] = record.odoo_product_id.description or ""
            if "description_sale" in vals:
                map_vals["short_description"] = record.odoo_product_id.description_sale or ""
            if "image_1920" in vals:
                map_vals["image_1920"] = record.odoo_product_id.image_1920
            if map_vals:
                record.with_context(skip_odoo_sync=True).write(map_vals)
        return res


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_odoo_sync"):
            return res
        sync_fields = {
            "name",
            "default_code",
            "weight",
            "active",
            "list_price",
            "description",
            "description_sale",
            "image_1920",
        }
        if not sync_fields.intersection(vals.keys()):
            return res
        variants = self.mapped("product_variant_ids")
        if not variants:
            return res
        maps = _get_maps_for_products(variants)
        if not maps:
            return res
        for record in maps:
            map_vals = {}
            if "name" in vals:
                map_vals["name"] = record.odoo_product_id.name
            if "default_code" in vals:
                map_vals["sku"] = record.odoo_product_id.default_code
            if "weight" in vals:
                map_vals["weight"] = record.odoo_product_id.weight
            if "active" in vals:
                map_vals["enable_product"] = record.odoo_product_id.active
                map_vals["status"] = "1" if record.odoo_product_id.active else "2"
            if "list_price" in vals:
                map_vals["price"] = record.odoo_product_id.lst_price
            if "description" in vals:
                map_vals["description"] = record.odoo_product_id.description or ""
            if "description_sale" in vals:
                map_vals["short_description"] = record.odoo_product_id.description_sale or ""
            if "image_1920" in vals:
                map_vals["image_1920"] = record.odoo_product_id.image_1920
            if map_vals:
                record.with_context(skip_odoo_sync=True).write(map_vals)
        return res
