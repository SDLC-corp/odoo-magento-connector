from odoo import models, fields
from odoo.exceptions import UserError
import requests
from ..services.magento_api import MagentoAPI


class MagentoCategory(models.Model):
    _name = "magento.category"
    _description = "Magento Category"

    name = fields.Char(required=True)
    magento_id = fields.Char()
    parent_id = fields.Many2one("magento.category", ondelete="set null")
    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    is_active = fields.Boolean(default=True)

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
            "magento_id": str(data.get("id") or self.magento_id or ""),
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
                parent = self.env["magento.category"].create({
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

    def _raise_magento_http_error(self, exc):
        response = exc.response
        if response is None:
            raise UserError(f"Magento API error: {exc}") from exc
        try:
            payload = response.json()
            message = payload.get("message") or response.text
        except Exception:
            message = response.text
        status = response.status_code
        raise UserError(f"Magento API error ({status}): {message}") from exc

    def action_create_magento_category(self):
        for record in self:
            if not record.instance_id:
                raise UserError("Magento Instance is required.")
            if not record.parent_id:
                if record.instance_id.root_category_id:
                    record.parent_id = False
                else:
                    raise UserError("Parent Category with Magento ID is required.")
            api = MagentoAPI(record.instance_id)
            if not record.parent_id.magento_id:
                record.parent_id.action_create_magento_category()
            try:
                parent_id = int(record.parent_id.magento_id) if record.parent_id else int(record.instance_id.root_category_id)
            except (TypeError, ValueError):
                if record.instance_id.root_category_id:
                    parent_id = int(record.instance_id.root_category_id)
                else:
                    raise UserError(
                        "Parent Category Magento ID must be a number. "
                        "Run 'Sync Categories' or set the correct Magento ID."
                    )
            payload = record._build_magento_payload()
            try:
                response = api.create_category(payload)
            except requests.exceptions.HTTPError as exc:
                response_obj = exc.response
                if response_obj is not None:
                    status = response_obj.status_code
                    message = ""
                    try:
                        payload_err = response_obj.json()
                        message = payload_err.get("message") or response_obj.text
                    except Exception:
                        message = response_obj.text
                    if not message:
                        message = f"HTTP {status}"
                    raise UserError(f"Magento API error ({status}): {message}") from exc
                raise UserError(f"Magento API error: {exc}") from exc
            if isinstance(response, dict):
                magento_id = response.get("id")
                if magento_id is not None:
                    record.write({"magento_id": str(magento_id)})
        return self._rainbow_man_action(f"Created {len(self)} category(s) in Magento.")

    def action_update_magento_category(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to update a category in Magento.")
            api = MagentoAPI(record.instance_id)
            try:
                api.get_category(record.magento_id)
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code == 404:
                    raise UserError("Magento category not found. Use 'Create in Magento' first.") from exc
                record._raise_magento_http_error(exc)
            payload = record._build_magento_payload(for_update=True)
            try:
                response = api.update_category(record.magento_id, payload)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._apply_magento_data(response)
                record.with_context(skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Updated {len(self)} category(s) in Magento.")

    def action_pull_magento_category(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to pull category from Magento.")
            api = MagentoAPI(record.instance_id)
            try:
                data = api.get_category(record.magento_id)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
            values = record._apply_magento_data(data)
            record.with_context(skip_magento_sync=True).write(values)
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

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_magento_sync"):
            return res

        magento_sync_fields = {"name", "is_active", "parent_id", "instance_id", "magento_id"}
        mapped_sync_fields = set()
        for inst in self.mapped("instance_id"):
            mapped_sync_fields.update(
                self.env["magento.field.mapping"].sudo().mapped_source_fields("category", inst)
            )
        if not (magento_sync_fields | mapped_sync_fields).intersection(vals.keys()):
            return res

        for record in self:
            if not record.instance_id:
                continue
            if record.parent_id and not record.parent_id.magento_id:
                record.parent_id.with_context(skip_magento_sync=True).action_create_magento_category()
            if not record.magento_id:
                record.with_context(skip_magento_sync=True).action_create_magento_category()
                continue
            api = MagentoAPI(record.instance_id)
            payload = record._build_magento_payload(for_update=True)
            try:
                api.update_category(record.magento_id, payload)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
        return res
