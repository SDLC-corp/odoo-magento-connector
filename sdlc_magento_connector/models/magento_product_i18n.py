"""Phase 2 (2.8): per-store-view translations and the full media gallery of products.

Translations
------------
The Magento default (``/rest/all/V1``) values are the Odoo base values (the default store
view's language, else ``en_US``). Each store view ticked "Sync" whose Odoo language is
installed and is not the default one carries translations:

* **import** reads ``/rest/<store_code>/V1/products/<sku>`` and stores, with
  ``update_field_translations``, the values that differ from the default ones (name,
  description, short description and, with eCommerce, the meta fields) on the product
  template. A variant of a configurable is skipped: its template's text belongs to the
  configurable.
* **export**, after the default (all-scope) push, sends to
  ``PUT /rest/<store_code>/V1/products/<sku>`` only the fields translated in that view's
  language. A digest per store code (``magento_translation_state``) avoids sending the
  same translation twice; an import records it too, so imported values are not echoed.

Media gallery
-------------
``magento.product.media`` tracks every Magento gallery entry of a product binding, by
entry id: position, label, roles, hidden flag, file, who created it and content digests.

* **import**: the main image (role ``image``, else the first entry) is the product's
  ``image_1920``, as before. Every other image becomes a ``product.image`` of the
  template (of the variant, for a configurable's child SKU) when eCommerce
  (``website_sale``) is installed, else it is stored on the tracking record. Contents are
  fetched only for new entries or entries whose file changed, and rewritten only when
  their digest changed, so a re-import is a no-op.
* **export**: the main image keeps its behaviour (entry id, real mime type). Extra images
  (``product.image`` or tracking records) are added, or updated by entry id when they
  changed. Images the connector did not create are never deleted in Magento; images it
  created are deleted when their Odoo image is removed.
"""
import base64
import hashlib
import json
import logging
import re

import requests
from markupsafe import Markup
from psycopg2.extras import Json

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, html_sanitize
from odoo.tools.mimetypes import guess_mimetype

from .magento_instance import _short_reason
from .magento_product import (
    UNSUPPORTED_EXPORT_TYPES, _IMAGE_EXTENSIONS, magento_push_skipped, magento_variant_products,
    schedule_magento_push,
)
from .sql_compat import UniqueIndex

_logger = logging.getLogger(__name__)

# Writes done by the connector itself: no push back, no map <-> product mirroring.
_SYNC_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}

# Magento attribute -> product.template field, for store-view translations. Fields missing
# from product.template (meta fields without eCommerce) are ignored.
_TRANSLATABLE_ATTRIBUTES = (
    ("name", "name"),
    ("description", "description"),
    ("short_description", "description_sale"),
    ("meta_title", "website_meta_title"),
    ("meta_description", "website_meta_description"),
    ("meta_keyword", "website_meta_keywords"),
)

_MAIN_ROLES = ("image", "small_image", "thumbnail")


def _norm(value):
    """Comparable text of a translatable value (None / False / '' are the same)."""
    if value in (None, False):
        return ""
    return str(value).strip()


def _magento_value(data, code):
    """Value of the attribute ``code`` in a Magento product JSON (None when absent)."""
    data = data or {}
    if code == "name":
        return data.get("name")
    for item in data.get("custom_attributes") or []:
        if isinstance(item, dict) and item.get("attribute_code") == code:
            return item.get("value")
    return None


def _digest(payload):
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _raw(b64_value):
    """Bytes of a base64 value (str or bytes), or None."""
    if not b64_value:
        return None
    if isinstance(b64_value, str):
        b64_value = b64_value.encode()
    try:
        return base64.b64decode(b64_value.strip())
    except (ValueError, TypeError):
        return None


def _sha1(raw):
    return hashlib.sha1(raw).hexdigest() if raw else False


def _signature(label, position, disabled, content_hash):
    return _digest([label or "", int(position or 0), bool(disabled), content_hash or ""])


# ======================================================================
# Gallery entries
# ======================================================================
class MagentoProductMedia(models.Model):
    _name = "magento.product.media"
    _description = "Magento Product Image"
    _order = "map_id, is_main desc, position, id"
    _rec_name = "label"

    _uniq_map_entry = UniqueIndex(
        "(map_id, magento_entry_id) WHERE magento_entry_id IS NOT NULL AND magento_entry_id != ''",
        "This Magento gallery entry is already tracked for this product.",
    )

    map_id = fields.Many2one(
        "magento.product.map", string="Magento Product", required=True, ondelete="cascade", index=True,
    )
    instance_id = fields.Many2one(related="map_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="map_id.company_id", store=True, index=True)
    sku = fields.Char(related="map_id.sku")
    magento_entry_id = fields.Char(string="Magento Entry ID", readonly=True, copy=False, index=True)
    position = fields.Integer(default=10)
    label = fields.Char()
    roles = fields.Char(
        readonly=True,
        help="Magento image roles of the entry (image, small_image, thumbnail, swatch_image).",
    )
    is_main = fields.Boolean(
        string="Main Image", readonly=True,
        help="Magento's main image: it is the product's own image in Odoo.",
    )
    disabled = fields.Boolean(string="Hidden in Magento")
    file = fields.Char(string="Magento File", readonly=True)
    mimetype = fields.Char(readonly=True)
    image = fields.Image(
        string="Image",
        help="Image content, when it is not kept as an eCommerce product image.",
    )
    odoo_image_model = fields.Char(readonly=True)
    odoo_image_id = fields.Many2oneReference(
        string="Odoo Image", model_field="odoo_image_model", readonly=True,
    )
    image_preview = fields.Binary(string="Preview", compute="_compute_image_preview")
    origin = fields.Selection(
        [("magento", "Magento"), ("odoo", "Odoo")], default="odoo", required=True, readonly=True,
        help="Where the image was first added.",
    )
    created_by_connector = fields.Boolean(
        readonly=True, copy=False,
        help="The connector created this Magento gallery entry: only such entries are ever "
             "deleted in Magento, when their Odoo image is removed.",
    )
    magento_hash = fields.Char(readonly=True, copy=False, help="Digest of the Magento file content.")
    sync_signature = fields.Char(readonly=True, copy=False)

    @api.depends("image", "odoo_image_model", "odoo_image_id")
    def _compute_image_preview(self):
        for record in self:
            preview = record.image
            if not preview:
                linked = record._magento_odoo_image()
                preview = linked.image_128 if linked and "image_128" in linked._fields else False
            record.image_preview = preview

    def _magento_odoo_image(self):
        """The ``product.image`` this entry is mirrored to (empty when none)."""
        self.ensure_one()
        if not self.odoo_image_model or not self.odoo_image_id or self.odoo_image_model not in self.env:
            return None
        return self.env[self.odoo_image_model].sudo().browse(self.odoo_image_id).exists()

    # ------------------------------------------------------------------
    # Storage of an imported extra image (eCommerce image or this record)
    # ------------------------------------------------------------------
    def _magento_store_extra_image(self, content, use_product_image, owner_field, owner):
        self.ensure_one()
        tracker = self.with_context(**_SYNC_CTX)
        if use_product_image and owner and not self.disabled:
            image = self._magento_odoo_image()
            vals = {
                "name": self.label or self.map_id.sku or "Image",
                "sequence": self.position,
                "image_1920": content,
                owner_field: owner.id,
            }
            if image:
                image.write(vals)
            else:
                image = self.env["product.image"].sudo().create(vals)
            tracker.write({"odoo_image_model": "product.image", "odoo_image_id": image.id, "image": False})
        else:
            tracker.write({"image": content})

    def _magento_drop_extra_image(self):
        """Remove the Odoo copy of an imported extra image (Magento's own entry stays)."""
        self.ensure_one()
        image = self._magento_odoo_image()
        if image and self.origin == "magento":
            image.unlink()
        if image or self.image:
            self.with_context(**_SYNC_CTX).write({"odoo_image_model": False, "odoo_image_id": False, "image": False})

    def _magento_refresh_signature(self):
        """Signature of the Odoo side as it is now: an export skips it until it changes."""
        self.ensure_one()
        image = self._magento_odoo_image()
        if image:
            label, position, b64 = image.name, image.sequence, image.image_1920
        else:
            label, position, b64 = self.label, self.position, self.image
        signature = _signature(label, position, self.disabled, _sha1(_raw(b64)))
        if self.sync_signature != signature:
            self.with_context(**_SYNC_CTX).write({"sync_signature": signature})

    # ------------------------------------------------------------------
    # Odoo-side edits are pushed; connector-created entries are deleted in Magento
    # ------------------------------------------------------------------
    _PUSH_FIELDS = {"image", "label", "position", "disabled"}

    def _magento_schedule_map_push(self, maps):
        maps = maps.filtered(lambda m: m.instance_id and m.sku and m.type_id not in UNSUPPORTED_EXPORT_TYPES)
        if maps:
            schedule_magento_push(maps, flags={"gallery"})

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not magento_push_skipped(self.env):
            self._magento_schedule_map_push(records.filtered("image").map_id)
        return records

    def write(self, vals):
        res = super().write(vals)
        if not magento_push_skipped(self.env) and self._PUSH_FIELDS & set(vals):
            self._magento_schedule_map_push(self.map_id)
        return res

    def unlink(self):
        if not magento_push_skipped(self.env):
            created = self.filtered(lambda m: m.created_by_connector and m.magento_entry_id)
            for product_map in created.map_id:
                ids = created.filtered(lambda m, p=product_map: m.map_id == p).mapped("magento_entry_id")
                product_map._magento_queue_media_deletion(ids)
        return super().unlink()


# ======================================================================
# Product binding: translations and gallery
# ======================================================================
class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    magento_media_ids = fields.One2many("magento.product.media", "map_id", string="Images")
    magento_media_count = fields.Integer(compute="_compute_magento_media_count", string="Image Count")
    magento_gallery_pending_delete = fields.Char(
        readonly=True, copy=False,
        help="Entry ids of connector-created Magento images to delete on the next push.",
    )
    magento_translation_state = fields.Json(
        readonly=True, copy=False,
        help="Digest of the translation last sent to (or read from) each store code.",
    )
    magento_translation_html = fields.Html(
        string="Translations", compute="_compute_magento_translation_html", sanitize=False,
    )

    @api.depends("magento_media_ids")
    def _compute_magento_media_count(self):
        for record in self:
            record.magento_media_count = len(record.magento_media_ids)

    @api.depends("instance_id", "odoo_product_id", "odoo_template_id", "magento_translation_state")
    def _compute_magento_translation_html(self):
        for record in self:
            record.magento_translation_html = record._magento_translation_table()

    def _magento_translation_table(self):
        self.ensure_one()
        instance = self.instance_id
        views = instance._magento_translation_store_views() if instance else self.env["magento.store.view"]
        template = self._magento_translation_target()
        if not views:
            return Markup(
                '<p class="text-muted small mb-0">No store view carries translations. Discover '
                "the stores on the instance (Stores tab) and install the languages of the "
                "store views to sync.</p>"
            )
        if not template:
            return Markup(
                '<p class="text-muted small mb-0">Translations belong to the configurable '
                "product's template, not to this variant.</p>"
            )
        default_lang = instance._magento_default_lang_code()
        base_name = _norm(template.with_context(lang=default_lang).name)
        state = self.magento_translation_state or {}
        rows = []
        for view in views:
            name = template.with_context(lang=view.lang_id.code).name
            translated = _norm(name) != base_name
            badge = (
                '<span class="badge rounded-pill text-bg-success">Translated</span>' if translated
                else '<span class="badge rounded-pill text-bg-light">Default</span>'
            )
            synced = (
                '<span class="badge rounded-pill text-bg-info">Synced</span>'
                if state.get(view.code) else ""
            )
            rows.append(Markup(
                "<tr><td>{code}</td><td>{lang}</td><td>{name}</td><td>{badge} {synced}</td></tr>"
            ).format(
                code=view.code, lang=view.lang_id.name, name=name or "",
                badge=Markup(badge), synced=Markup(synced),
            ))
        return Markup(
            '<table class="table table-sm o_list_table mb-0"><thead><tr>'
            "<th>Store View</th><th>Language</th><th>Name</th><th>Status</th>"
            "</tr></thead><tbody>{rows}</tbody></table>"
        ).format(rows=Markup("").join(rows))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _magento_translation_target(self):
        """Product template holding this binding's translatable text, or an empty
        recordset for a configurable's child SKU (its template is the configurable's)."""
        self.ensure_one()
        if self.type_id == "configurable":
            return self.odoo_template_id
        product = self.odoo_product_id
        if not product or magento_variant_products(product):
            return self.env["product.template"]
        return product.product_tmpl_id

    @api.model
    def _magento_translatable_fields(self):
        """``[(magento attribute code, product.template field)]`` available here."""
        template_fields = self.env["product.template"]._fields
        return [
            (code, fname) for code, fname in _TRANSLATABLE_ATTRIBUTES
            if fname in template_fields and template_fields[fname].translate
        ]

    def _magento_write_sync(self, vals):
        self.with_context(**_SYNC_CTX).write(vals)

    def _magento_store_data_exportable(self):
        self.ensure_one()
        return bool(self.instance_id and self.sku and self.type_id not in UNSUPPORTED_EXPORT_TYPES)

    # ------------------------------------------------------------------
    # Translations: import
    # ------------------------------------------------------------------
    def _magento_import_translations(self, api, product_data=None):
        """Read each translation store view's values and store those that differ from the
        Magento default values as translations of the template. Returns
        ``{lang: [field, ...]}`` of the translations written."""
        self.ensure_one()
        instance = self.instance_id
        template = self._magento_translation_target()
        views = instance._magento_translation_store_views() if instance else None
        if not template or not views or not self.sku:
            return {}
        if product_data is None:
            product_data = api.get_product(self.sku) or {}
        attributes = self._magento_translatable_fields()
        defaults = {code: _norm(_magento_value(product_data, code)) for code, _fname in attributes}
        found = {}
        for view in views:
            data = api.get_product_for_store(self.sku, view.code) or {}
            lang = view.lang_id.code
            for code, fname in attributes:
                value = _magento_value(data, code)
                if value is None or _norm(value) == defaults[code] or not _norm(value):
                    continue
                # Several views of one language: the first one (default website first) wins.
                found.setdefault(lang, {}).setdefault(fname, value)

        written = {}
        template = template.sudo().with_context(**_SYNC_CTX)
        for lang, values in found.items():
            for fname, value in values.items():
                field = template._fields[fname]
                if field.type == "html":
                    value = html_sanitize(value)
                if _norm(template.with_context(lang=lang)[fname]) == _norm(value):
                    continue
                self._magento_set_translation(template, fname, lang, str(value))
                written.setdefault(lang, []).append(fname)
        # What Magento has now is in sync: the next push must not send it back.
        self._magento_remember_translations()
        return written

    @api.model
    def _magento_set_translation(self, record, fname, lang, value):
        """Store ``value`` as the ``lang`` translation of ``record[fname]``, leaving the
        other languages as they are.

        A model-translated field (``translate=True``) goes through
        ``update_field_translations``. A term-translated field (HTML descriptions use
        ``html_translate``) maps translations term by term, which a Magento store value
        with another HTML structure cannot follow: the language's whole value is stored
        instead, as Odoo keeps it in the field's JSONB column."""
        field = record._fields[fname]
        stored = field._get_stored_translations(record) or {}
        translations = {lang: value}
        if "en_US" not in stored:
            # Never let a translation become the base value of an empty field.
            translations["en_US"] = ""
        if field.translate is True:
            record.update_field_translations(fname, translations)
            return
        record.flush_recordset([fname])
        self.env.cr.execute(SQL(
            "UPDATE %(table)s SET %(column)s = COALESCE(%(column)s, '{}'::jsonb) || %(value)s "
            "WHERE id = %(id)s",
            table=SQL.identifier(record._table), column=SQL.identifier(fname),
            value=Json(translations), id=record.id,
        ))
        record.invalidate_recordset([fname])
        record.modified([fname])

    def _magento_remember_translations(self):
        self.ensure_one()
        payloads = self._magento_translation_payloads()
        state = dict(self.magento_translation_state or {})
        for code, payload in payloads.items():
            state[code] = _digest(payload)
        if state != (self.magento_translation_state or {}):
            self._magento_write_sync({"magento_translation_state": state})

    # ------------------------------------------------------------------
    # Translations: export
    # ------------------------------------------------------------------
    def _magento_translation_payloads(self):
        """``{store_code: payload}``: for each translation store view, the fields translated
        in its language (different from the default language's value)."""
        self.ensure_one()
        instance = self.instance_id
        template = self._magento_translation_target()
        if not template or not self.sku or not instance:
            return {}
        views = instance._magento_translation_store_views()
        if not views:
            return {}
        attributes = self._magento_translatable_fields()
        base = template.with_context(lang=instance._magento_default_lang_code())
        defaults = {fname: _norm(base[fname]) for _code, fname in attributes}
        payloads = {}
        for view in views:
            record = template.with_context(lang=view.lang_id.code)
            product = {}
            custom = []
            for code, fname in attributes:
                value = record[fname]
                if not _norm(value) or _norm(value) == defaults[fname]:
                    continue
                if code == "name":
                    product["name"] = str(value)
                else:
                    custom.append({"attribute_code": code, "value": str(value)})
            if product or custom:
                product["sku"] = self.sku
                if custom:
                    product["custom_attributes"] = custom
                payloads[view.code] = {"product": product}
        return payloads

    def _magento_export_translations(self, api, force=False):
        """Send the translated fields to each translation store view. Returns the store
        codes written. Raises on a Magento error."""
        self.ensure_one()
        if not self._magento_store_data_exportable():
            return []
        payloads = self._magento_translation_payloads()
        state = dict(self.magento_translation_state or {})
        sent = []
        for code, payload in payloads.items():
            digest = _digest(payload)
            if not force and state.get(code) == digest:
                continue
            api.update_product_for_store(self.sku, code, payload)
            state[code] = digest
            sent.append(code)
        if state != (self.magento_translation_state or {}):
            self._magento_write_sync({"magento_translation_state": state})
        return sent

    # ------------------------------------------------------------------
    # Gallery: helpers
    # ------------------------------------------------------------------
    @api.model
    def _magento_use_product_image(self):
        """eCommerce installed: extra images are ``product.image`` records."""
        return "product.image" in self.env and "product_template_image_ids" in self.env["product.template"]._fields

    def _magento_gallery_owner(self):
        """``(field, record)`` that extra ``product.image`` records hang on: the variant
        for a configurable's child SKU, else the template. ``(None, None)`` without one."""
        self.ensure_one()
        if self.type_id == "configurable":
            return ("product_tmpl_id", self.odoo_template_id) if self.odoo_template_id else (None, None)
        product = self.odoo_product_id
        if not product:
            return (None, None)
        if magento_variant_products(product):
            return ("product_variant_id", product)
        return ("product_tmpl_id", product.product_tmpl_id)

    def _magento_queue_media_deletion(self, entry_ids):
        self.ensure_one()
        pending = [i for i in (self.magento_gallery_pending_delete or "").split(",") if i]
        pending += [str(i) for i in entry_ids if i and str(i) not in pending]
        self._magento_write_sync({"magento_gallery_pending_delete": ",".join(pending) or False})
        if self._magento_store_data_exportable():
            schedule_magento_push(self, flags={"gallery"})

    @staticmethod
    def _magento_main_entry(entries, current_entry_id=None):
        """The main image entry: the one exported from Odoo, else role ``image``, else the
        first one."""
        if current_entry_id:
            match = next((e for e in entries if str(e.get("id")) == str(current_entry_id)), None)
            if match:
                return match
        return next((e for e in entries if "image" in (e.get("types") or [])), entries[0] if entries else None)

    def _magento_fetch_media_content(self, api, entry_id):
        """Base64 content of a gallery entry, or None."""
        media = api.get_product_media(self.sku, entry_id)
        return ((media or {}).get("content") or {}).get("base64_encoded_data") or None

    # ------------------------------------------------------------------
    # Gallery: import
    # ------------------------------------------------------------------
    def _magento_import_gallery(self, api, product_data=None):
        """Track every Magento gallery entry of this product; store the extra images.

        Idempotent: entries are matched by entry id; a content is fetched only for a new
        entry or a changed file, and written only when its digest changed. Returns the
        counts ``{"created", "updated", "removed", "fetched"}``."""
        self.ensure_one()
        counts = {"created": 0, "updated": 0, "removed": 0, "fetched": 0}
        if not self.sku:
            return counts
        if product_data is None:
            product_data = api.get_product(self.sku) or {}
        entries = sorted(
            [
                e for e in product_data.get("media_gallery_entries") or []
                if isinstance(e, dict) and e.get("id") is not None
                and (e.get("media_type") or "image") == "image"
            ],
            key=lambda e: (int(e.get("position") or 0), int(e.get("id"))),
        )
        Media = self.env["magento.product.media"].with_context(**_SYNC_CTX)
        trackers = {
            m.magento_entry_id: m for m in self.with_context(**_SYNC_CTX).magento_media_ids if m.magento_entry_id
        }
        if not entries and not trackers:
            return counts
        raw_entries = product_data.get("media_gallery_entries") or []
        main = self._magento_main_entry(entries, self.magento_image_entry_id)
        use_product_image = self._magento_use_product_image()
        owner_field, owner = self._magento_gallery_owner()
        map_vals = {}

        if main:
            core_first = next((e for e in raw_entries if isinstance(e, dict) and e.get("id") is not None), None)
            if core_first is not None and str(core_first.get("id")) != str(main.get("id")):
                # The product import took the first entry as image: use the main one.
                content = self._magento_fetch_media_content(api, main["id"])
                counts["fetched"] += 1
                if content:
                    map_vals["image_1920"] = content
            if not self.magento_image_entry_id:
                map_vals["magento_image_entry_id"] = str(main["id"])

        seen = set()
        for entry in entries:
            entry_id = str(entry["id"])
            seen.add(entry_id)
            tracker = trackers.get(entry_id)
            is_main = entry is main
            vals = {
                "position": int(entry.get("position") or 0),
                "label": entry.get("label") or False,
                "roles": ",".join(entry.get("types") or []) or False,
                "disabled": bool(entry.get("disabled")),
                "is_main": is_main,
                "file": entry.get("file") or False,
            }
            if is_main:
                # The main image is the product's own image (image_1920).
                if tracker:
                    tracker._magento_drop_extra_image()
                vals["image"] = False
            else:
                needs_content = (
                    not tracker or tracker.file != vals["file"] or not tracker.magento_hash
                    or (not tracker.image and not tracker._magento_odoo_image())
                )
                if needs_content:
                    content = self._magento_fetch_media_content(api, entry["id"])
                    counts["fetched"] += 1
                    raw = _raw(content)
                    if not raw:
                        _logger.info("Magento image %s of %s has no content; skipped.", entry_id, self.sku)
                        if not tracker:
                            continue
                    else:
                        digest = _sha1(raw)
                        vals["mimetype"] = guess_mimetype(raw, default="image/jpeg")
                        if not tracker or tracker.magento_hash != digest or (
                                not tracker.image and not tracker._magento_odoo_image()):
                            vals["magento_hash"] = digest
                            vals["_content"] = content
            content = vals.pop("_content", None)
            if tracker:
                changed = {k: v for k, v in vals.items() if tracker[k] != v}
                if changed or content:
                    tracker.write(changed)
                    counts["updated"] += 1
            else:
                vals.update({
                    "map_id": self.id,
                    "magento_entry_id": entry_id,
                    "origin": "magento",
                })
                tracker = Media.create(vals)
                counts["created"] += 1
            if not is_main and content:
                tracker._magento_store_extra_image(content, use_product_image, owner_field, owner)
            if not is_main:
                tracker._magento_refresh_signature()

        # Entries gone from Magento: Magento's images go; Odoo's are added again.
        for entry_id, tracker in trackers.items():
            if entry_id in seen:
                continue
            if tracker.origin == "magento":
                tracker._magento_drop_extra_image()
                tracker.with_context(**_SYNC_CTX).unlink()
            else:
                tracker.write({"magento_entry_id": False, "created_by_connector": False,
                               "sync_signature": False, "file": False})
            counts["removed"] += 1
        if map_vals:
            # Like the product import: the image is mirrored to the Odoo product.
            self.with_context(magento_skip_push=True, skip_magento_sync=True).write(map_vals)
        return counts

    # ------------------------------------------------------------------
    # Gallery: export
    # ------------------------------------------------------------------
    def _magento_gallery_sources(self):
        """Extra images to keep in Magento: ``[(tracker or None, odoo image or None,
        label, position, disabled, base64)]``."""
        self.ensure_one()
        sources = []
        extras = self.magento_media_ids.filtered(lambda m: not m.is_main)
        if self._magento_use_product_image():
            owner_field, owner = self._magento_gallery_owner()
            images = self.env["product.image"]
            if owner_field == "product_variant_id":
                images = owner.product_variant_image_ids
            elif owner_field == "product_tmpl_id":
                images = owner.product_template_image_ids
            by_image = {
                m.odoo_image_id: m for m in extras
                if m.odoo_image_model == "product.image" and m.odoo_image_id
            }
            for image in images.filtered(lambda i: i.image_1920 and not i.video_url):
                tracker = by_image.get(image.id)
                sources.append((tracker, image, image.name, image.sequence,
                                tracker.disabled if tracker else False, image.image_1920))
            # Extra images kept on the tracker only (hidden in Magento).
            for tracker in extras.filtered(lambda m: m.image and not m.odoo_image_id):
                sources.append((tracker, None, tracker.label, tracker.position, tracker.disabled, tracker.image))
        else:
            for tracker in extras.filtered("image"):
                sources.append((tracker, None, tracker.label, tracker.position, tracker.disabled, tracker.image))
        return sources

    def _magento_export_gallery(self, api, force=False):
        """Add or update the extra images in Magento; delete the connector-created ones
        whose Odoo image is gone. Never deletes an image the connector did not create.
        Returns ``{"added", "updated", "deleted"}``. Raises on a Magento error."""
        self.ensure_one()
        counts = {"added": 0, "updated": 0, "deleted": 0}
        if not self._magento_store_data_exportable():
            return counts
        sources = self.with_context(**_SYNC_CTX)._magento_gallery_sources()
        to_push = []
        for source in sources:
            tracker, _image, label, position, disabled, b64 = source
            raw = _raw(b64)
            if not raw:
                continue
            signature = _signature(label, position, disabled, _sha1(raw))
            if force or not tracker or not tracker.magento_entry_id or tracker.sync_signature != signature:
                to_push.append((source, raw, signature))

        # Connector-created entries whose Odoo image was removed.
        stale = self.with_context(**_SYNC_CTX).magento_media_ids.filtered(
            lambda m: not m.is_main and m.odoo_image_model and m.odoo_image_id and not m._magento_odoo_image()
        )
        pending = [i for i in (self.magento_gallery_pending_delete or "").split(",") if i]
        pending += [m.magento_entry_id for m in stale if m.created_by_connector and m.magento_entry_id]
        if not to_push and not pending:
            if stale:
                stale.with_context(**_SYNC_CTX).unlink()
            return counts

        product = api.get_product(self.sku) or {}
        entries = {str(e.get("id")): e for e in product.get("media_gallery_entries") or []
                   if isinstance(e, dict) and e.get("id") is not None}
        Media = self.env["magento.product.media"].with_context(**_SYNC_CTX)
        safe_sku = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.sku).strip("._") or "product"
        for (tracker, image, label, position, disabled, b64), raw, signature in to_push:
            mimetype = guess_mimetype(raw, default="image/jpeg")
            extension = _IMAGE_EXTENSIONS.get(mimetype)
            if not extension:
                _logger.warning("Magento product %s: image type %s is not supported by Magento; "
                                "extra image not exported.", self.sku, mimetype)
                continue
            content_hash = _sha1(raw)
            existing = entries.get(tracker.magento_entry_id) if tracker and tracker.magento_entry_id else None
            entry = {
                "media_type": "image",
                "label": label or "",
                "position": int(position or 0),
                "disabled": bool(disabled),
                "types": [t for t in (tracker.roles or "").split(",") if t] if tracker else [],
            }
            if not existing or not tracker or tracker.magento_hash != content_hash:
                entry["content"] = {
                    "base64_encoded_data": b64.decode() if isinstance(b64, bytes) else b64,
                    "type": mimetype,
                    "name": "%s-%s.%s" % (safe_sku, content_hash[:8], extension),
                }
            vals = {"magento_hash": content_hash, "sync_signature": signature, "mimetype": mimetype,
                    "label": label or False, "position": int(position or 0)}
            if existing:
                entry["id"] = int(existing["id"]) if str(existing["id"]).isdigit() else existing["id"]
                api.update_product_media(self.sku, existing["id"], {"entry": entry})
                counts["updated"] += 1
            else:
                result = api.add_product_media(self.sku, {"entry": entry})
                if not (isinstance(result, (int, str)) and str(result).isdigit()):
                    raise UserError("Magento did not return the id of the image added to %s." % self.sku)
                vals.update({"magento_entry_id": str(result), "created_by_connector": True, "file": False})
                counts["added"] += 1
            if tracker:
                tracker.write(vals)
            else:
                vals.update({"map_id": self.id, "origin": "odoo", "roles": False})
                if image is not None:
                    vals.update({"odoo_image_model": image._name, "odoo_image_id": image.id})
                Media.create(vals)

        for entry_id in dict.fromkeys(pending):
            if entry_id in entries:
                tracked = self.magento_media_ids.filtered(lambda m, e=entry_id: m.magento_entry_id == e)
                if tracked and not tracked.created_by_connector:
                    continue  # never delete an image the connector did not create
                api.delete_product_media(self.sku, entry_id)
                counts["deleted"] += 1
        if stale:
            stale.with_context(**_SYNC_CTX).unlink()
        if self.magento_gallery_pending_delete:
            self._magento_write_sync({"magento_gallery_pending_delete": False})
        return counts

    # ------------------------------------------------------------------
    # Hooks on the existing export / pull paths
    # ------------------------------------------------------------------
    def _magento_export_store_data(self, api, force_translations=False):
        """Translations (per store code) and extra images, after the default push."""
        self.ensure_one()
        if not self._magento_store_data_exportable():
            return
        self._magento_export_translations(api, force=force_translations)
        self._magento_export_gallery(api)

    def _magento_push_now(self, flags):
        super()._magento_push_now(flags)
        if self._magento_store_data_exportable():
            self._magento_export_store_data(self._magento_api())

    def action_create_magento_product(self):
        res = super().action_create_magento_product()
        for record in self:
            try:
                record._magento_export_store_data(record._magento_api(), force_translations=True)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
        return res

    def action_update_magento_product(self):
        res = super().action_update_magento_product()
        for record in self:
            try:
                record._magento_export_store_data(record._magento_api(), force_translations=True)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
        return res

    def action_pull_magento_product(self):
        res = super().action_pull_magento_product()
        for record in self:
            record._magento_pull_store_data()
        return res

    def _magento_pull_store_data(self):
        self.ensure_one()
        api = self._magento_api()
        try:
            data = api.get_product(self.sku) or {}
            self._magento_import_gallery(api, data)
            self._magento_import_translations(api, data)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)

    def action_magento_pull_store_data(self):
        """Button: import the images and translations of these products."""
        for record in self:
            if not record.sku:
                raise UserError("SKU is required to read the product from Magento.")
            record._magento_pull_store_data()
        return self._rainbow_man_action(f"Images and translations of {len(self)} product(s) imported.")

    def action_magento_push_store_data(self):
        """Button: export the translations and extra images of these products."""
        for record in self:
            if not record._magento_store_data_exportable():
                raise UserError(
                    "Only simple, virtual and downloadable products with a SKU are exported "
                    "(SKU: %s)." % (record.sku or record.name or record.id)
                )
            try:
                record._magento_export_store_data(record._magento_api(), force_translations=True)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
        return self._rainbow_man_action(f"Images and translations of {len(self)} product(s) exported.")


# ======================================================================
# Instance: product import hook
# ======================================================================
class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    def _sync_product_record(self, api, p, supported_types, stats, product_data=None, box=None):
        """After the product import: its extra images and store-view translations."""
        box = box if box is not None else []
        type_id = p.get("type_id")
        if product_data is None and not (type_id and type_id not in supported_types):
            product_data = api.get_product(p.get("sku")) or {}
        result = super()._sync_product_record(
            api, p, supported_types, stats, product_data=product_data, box=box,
        )
        if result in ("created", "updated") and box and product_data:
            self._magento_import_store_data(api, box[-1], product_data)
        return result

    def _magento_import_store_data(self, api, mapping, product_data):
        """Gallery and translations of an imported product. They are not part of the
        product's own data: a failure is logged and the product import stands."""
        self.ensure_one()
        steps = (("images", mapping._magento_import_gallery),
                 ("translations", mapping._magento_import_translations))
        for label, step in steps:
            try:
                with self.env.cr.savepoint():
                    step(api, product_data)
            except (requests.exceptions.RequestException, UserError, ValueError) as exc:
                _logger.warning(
                    "Magento product %s (instance %s): %s import failed: %s",
                    mapping.sku, self.name, label, _short_reason(exc),
                )


# ======================================================================
# Template translations edited in Odoo are pushed
# ======================================================================
class ProductTemplate(models.Model):
    _inherit = "product.template"

    def _update_field_translations(self, field_name, translations, digest=None, source_lang=""):
        res = super()._update_field_translations(field_name, translations, digest=digest, source_lang=source_lang)
        if magento_push_skipped(self.env):
            return res
        translatable = {fname for _code, fname in self.env["magento.product.map"]._magento_translatable_fields()}
        if field_name in translatable:
            maps = self.env["magento.product.map"].sudo().search([("odoo_template_id", "in", self.ids)])
            maps = maps.filtered(lambda m: m._magento_store_data_exportable() and m._magento_translation_target())
            if maps:
                schedule_magento_push(maps, flags={"translations"})
        return res
