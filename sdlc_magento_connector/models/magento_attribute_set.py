import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.magento_api import MagentoAPI
from .magento_product import magento_push_skipped, raise_magento_user_error
from .sql_compat import UniqueIndex


class MagentoAttributeSet(models.Model):
    _name = "magento.attribute.set"
    _description = "Magento Attribute Set"

    _uniq_instance_magento_id = UniqueIndex(
        "(instance_id, magento_id) WHERE magento_id IS NOT NULL AND magento_id != ''",
        "This Magento attribute set already exists for this instance.",
    )

    name = fields.Char(required=True)
    magento_id = fields.Char(readonly=True)
    instance_id = fields.Many2one(
        "magento.instance",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    active = fields.Boolean(default=True)

    def _magento_api(self):
        self.ensure_one()
        return MagentoAPI(self.instance_id.sudo())

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if magento_push_skipped(self.env):
            return records
        # Creating an attribute set from the UI is an explicit action: it needs
        # the Magento id back, so it stays synchronous and raises on failure.
        for record in records:
            if record.magento_id:
                continue
            try:
                created = record._magento_api().create_attribute_set(
                    name=record.name,
                    skeleton_id=record.instance_id.attribute_set_skeleton_id or 4,
                )
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            magento_id = created.get("attribute_set_id") or created.get("id")
            if not magento_id:
                raise UserError("Magento did not return an Attribute Set ID.")
            record.with_context(magento_skip_push=True, skip_magento_sync=True).write({
                "magento_id": str(magento_id),
            })
        return records

    def write(self, vals):
        res = super().write(vals)
        if magento_push_skipped(self.env) or "name" not in vals:
            return res
        for record in self:
            if not record.magento_id:
                continue
            try:
                record._magento_api().update_attribute_set(record.magento_id, record.name)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
        return res

    def _raise_magento_http_error(self, exc):
        raise_magento_user_error(exc)
