import requests

from odoo import fields, models
from odoo.exceptions import UserError

from .magento_product import magento_push_skipped, schedule_magento_push
from .sql_compat import UniqueIndex

# Fields whose change triggers the automatic (queued) push to Magento.
_CATEGORY_SYNC_FIELDS = {"name", "is_active", "parent_id", "instance_id"}


class MagentoCategory(models.Model):
    _name = "magento.category"
    _inherit = ["magento.push.error.mixin"]
    _description = "Magento Category"
    _magento_push_entity = "category"

    _uniq_instance_magento_id = UniqueIndex(
        "(instance_id, magento_id) WHERE magento_id IS NOT NULL AND magento_id != ''",
        "This Magento category already exists for this instance.",
    )

    name = fields.Char(required=True)
    magento_id = fields.Char()
    parent_id = fields.Many2one("magento.category", ondelete="set null")
    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    is_active = fields.Boolean(default=True)
    categ_id = fields.Many2one(
        "product.category",
        string="Odoo Category",
        ondelete="set null",
        copy=False,
        help="Odoo product category of this Magento category. Kept in line with the Magento "
             "tree by the category sync when the instance mirrors categories; otherwise set "
             "it by hand. Products in this Magento category get it as their category.",
    )
    public_categ_ref = fields.Integer(
        string="Website Category ID",
        copy=False,
        readonly=True,
        help="Technical: id of the mirrored eCommerce category (website_sale).",
    )

    # ------------------------------------------------------------------
    # Mirror into product.category (and eCommerce categories)
    # ------------------------------------------------------------------
    def _magento_depth(self):
        """Number of ancestors (cycle safe)."""
        self.ensure_one()
        depth, node, seen = 0, self.parent_id, {self.id}
        while node and node.id not in seen:
            seen.add(node.id)
            depth += 1
            node = node.parent_id
        return depth

    def _magento_mirror_product_category(self):
        """Create or update the Odoo category of each Magento category (same name, parent
        mirrored, the tree's top under the instance's category root). Returns them."""
        result = self.env["product.category"]
        for category in self:
            result |= category._magento_mirror_one(set())
        return result

    def _magento_mirror_one(self, seen):
        self.ensure_one()
        if self.id in seen or not self.parent_id:
            parent = self.instance_id._magento_product_category_root()
        else:
            seen.add(self.id)
            parent = self.parent_id._magento_mirror_one(seen)
        categories = self.env["product.category"].sudo()
        categ = self.categ_id.sudo().exists()
        vals = {"name": self.name, "parent_id": parent.id}
        if categ:
            changed = {k: v for k, v in vals.items() if (categ[k].id if k == "parent_id" else categ[k]) != v}
            if changed and categ != parent:
                categ.write(changed)
        else:
            categ = categories.create(vals)
            self.with_context(magento_skip_push=True, skip_magento_sync=True).write({"categ_id": categ.id})
        return categ.with_env(self.env)

    def _magento_mirror_public_category(self):
        """eCommerce categories (website_sale) mirroring these Magento categories."""
        if "product.public.category" not in self.env:
            return self.env["product.category"].browse()
        result = self.env["product.public.category"]
        for category in self:
            result |= category._magento_mirror_public_one(set())
        return result

    def _magento_mirror_public_one(self, seen):
        self.ensure_one()
        public = self.env["product.public.category"].sudo()
        parent = public.browse()
        if self.parent_id and self.id not in seen:
            seen.add(self.id)
            parent = self.parent_id._magento_mirror_public_one(seen)
        categ = public.browse(self.public_categ_ref).exists() if self.public_categ_ref else public.browse()
        vals = {"name": self.name, "parent_id": parent.id or False}
        if categ:
            if categ.name != vals["name"] or categ.parent_id.id != (parent.id or False):
                categ.write(vals)
        else:
            categ = public.create(vals)
            self.with_context(magento_skip_push=True, skip_magento_sync=True).write(
                {"public_categ_ref": categ.id})
        return categ.with_env(self.env)

    def _build_magento_payload(self, for_update=False):
        self.ensure_one()
        if not self.name:
            raise UserError("Category Name is required.")
        parent_id = None
        if self.parent_id and self.parent_id.magento_id:
            try:
                parent_id = int(self.parent_id.magento_id)
            except (TypeError, ValueError):
                parent_id = None
        if not for_update and parent_id is None:
            if self.instance_id.root_category_id:
                parent_id = int(self.instance_id.root_category_id)
            else:
                raise UserError("Parent Category with Magento ID is required.")
        category_vals = {
            "name": self.name,
            "is_active": bool(self.is_active),
        }
        if not for_update:
            category_vals["parent_id"] = parent_id
        elif parent_id is not None:
            category_vals["parent_id"] = parent_id
        if for_update and self.magento_id:
            try:
                category_vals["id"] = int(self.magento_id)
            except (TypeError, ValueError):
                pass
        payload = {"category": category_vals}
        # Apply user-defined field mappings on top of default payload.
        self.env["magento.field.mapping"].sudo().apply_outbound_mapping_to_payload(
            mapping_type="category",
            instance=self.instance_id,
            source_record=self,
            payload=payload,
            wrapper_key="category",
        )
        return payload

    def _apply_magento_data(self, data):
        vals = {
            "magento_id": str(data.get("id") or self.magento_id or "") or False,
            "name": data.get("name") or self.name,
            "is_active": bool(data.get("is_active")) if data.get("is_active") is not None else self.is_active,
        }
        parent_id = data.get("parent_id")
        if parent_id:
            parent = self.env["magento.category"].search([
                ("instance_id", "=", self.instance_id.id),
                ("magento_id", "=", str(parent_id)),
            ], limit=1)
            if not parent:
                parent = self.env["magento.category"].with_context(
                    magento_skip_push=True, skip_magento_sync=True
                ).create({
                    "instance_id": self.instance_id.id,
                    "magento_id": str(parent_id),
                    "name": f"Magento Category {parent_id}",
                })
            vals["parent_id"] = parent.id
        else:
            vals["parent_id"] = False

        mapped_vals = self.env["magento.field.mapping"].sudo().apply_inbound_mapping_to_vals(
            mapping_type="category",
            instance=self.instance_id,
            source_payload=data,
        )
        if mapped_vals:
            vals.update(mapped_vals)
        return vals

    def action_create_magento_category(self):
        for record in self:
            if not record.instance_id:
                raise UserError("Magento Instance is required.")
            if not record.parent_id and not record.instance_id.root_category_id:
                raise UserError("Parent Category with Magento ID is required.")
            if record.parent_id and not record.parent_id.magento_id:
                record.parent_id.action_create_magento_category()
            if record.parent_id:
                try:
                    int(record.parent_id.magento_id)
                except (TypeError, ValueError):
                    if not record.instance_id.root_category_id:
                        raise UserError(
                            "Parent Category Magento ID must be a number. "
                            "Run 'Sync Categories' or set the correct Magento ID."
                        ) from None
            api = record._magento_api()
            payload = record._build_magento_payload()
            try:
                response = api.create_category(payload)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                magento_id = response.get("id")
                if magento_id is not None:
                    record.with_context(magento_skip_push=True, skip_magento_sync=True).write(
                        {"magento_id": str(magento_id)}
                    )
        return self._rainbow_man_action(f"Created {len(self)} category(s) in Magento.")

    def action_update_magento_category(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to update a category in Magento.")
            api = record._magento_api()
            try:
                api.get_category(record.magento_id)
            except requests.exceptions.RequestException as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code == 404:
                    raise UserError("Magento category not found. Use 'Create in Magento' first.") from exc
                record._raise_magento_http_error(exc)
            payload = record._build_magento_payload(for_update=True)
            try:
                response = api.update_category(record.magento_id, payload)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._apply_magento_data(response)
                record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Updated {len(self)} category(s) in Magento.")

    def action_pull_magento_category(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to pull category from Magento.")
            api = record._magento_api()
            try:
                data = api.get_category(record.magento_id)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            values = record._apply_magento_data(data)
            record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Pulled {len(self)} category(s) from Magento.")

    def _rainbow_man_action(self, message):
        return {
            "type": "ir.actions.act_window_close",
            "effect": {
                "fadeout": "slow",
                "message": message,
                "type": "rainbow_man",
            }
        }

    def _magento_push_now(self, flags):
        """Automatic (queued) push to Magento. Raises on failure."""
        self.ensure_one()
        if not self.instance_id:
            return
        if self.parent_id and not self.parent_id.magento_id:
            self.parent_id.action_create_magento_category()
        if not self.magento_id:
            self.action_create_magento_category()
            return
        api = self._magento_api()
        payload = self._build_magento_payload(for_update=True)
        try:
            api.update_category(self.magento_id, payload)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)

    def write(self, vals):
        res = super().write(vals)
        if magento_push_skipped(self.env):
            return res
        sync_fields = set(_CATEGORY_SYNC_FIELDS)
        for inst in self.mapped("instance_id"):
            sync_fields |= self.env["magento.field.mapping"].sudo().mapped_source_fields("category", inst)
        if sync_fields.intersection(vals):
            # Pushed by a queued job: Magento being slow or down never blocks the save.
            schedule_magento_push(self.filtered("instance_id"))
        return res
