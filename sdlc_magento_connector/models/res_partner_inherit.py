from odoo import fields, models

from .magento_product import magento_push_skipped

# Contact fields mirrored on the linked magento.customer; editing one of them marks the
# Magento customer for a (queued) push when the instance has ``push_partner_changes`` on.
PARTNER_PUSH_FIELDS = frozenset({
    "name", "email", "phone", "street", "street2", "city", "zip", "country_id", "state_id",
})


class ResPartner(models.Model):
    _inherit = "res.partner"

    magento_customer_id = fields.Char(
        string="Magento Customer ID",
        index=True,
        copy=False,
        help="Magento customer entity id this contact was matched to (registered customers only).",
    )
    magento_instance_id = fields.Many2one(
        "magento.instance",
        string="Magento Instance",
        index=True,
        copy=False,
        ondelete="set null",
    )
    magento_address_id = fields.Char(
        string="Magento Address ID",
        index=True,
        copy=False,
        help="Magento customer address id this address contact mirrors. Customer syncs update "
             "this contact instead of creating a new one.",
    )
    magento_customer_group_id = fields.Many2one(
        "magento.customer.group",
        string="Magento Customer Group",
        copy=False,
        ondelete="set null",
        groups="sdlc_magento_connector.group_magento_user",
    )
    magento_customer_ids = fields.One2many(
        "magento.customer", "partner_id",
        string="Magento Customers",
        groups="sdlc_magento_connector.group_magento_user",
    )
    magento_vat_invalid = fields.Boolean(
        string="Invalid Magento VAT",
        copy=False,
        help="Magento sent a VAT number that failed validation. It was not copied to the Tax ID.",
    )
    magento_vat_raw = fields.Char(
        string="VAT Sent by Magento",
        copy=False,
        help="VAT number as Magento sent it, kept when it failed validation.",
    )

    _magento_customer_uniq = models.Constraint(
        "unique(magento_instance_id, magento_customer_id)",
        "A Magento customer can only be linked to one contact per Magento instance.",
    )

    def write(self, vals):
        res = super().write(vals)
        if PARTNER_PUSH_FIELDS.intersection(vals) and not magento_push_skipped(self.env):
            self._magento_mark_customers_for_push(set(vals))
        return res

    def _magento_mark_customers_for_push(self, changed_fields):
        """Copy the edited contact fields to the linked Magento customers whose instance
        pushes contact changes; their own ``write`` queues the push job. Off by default."""
        customers = self.env["magento.customer"].sudo().search([
            ("partner_id", "in", self.ids),
            ("instance_id.push_partner_changes", "=", True),
        ])
        for customer in customers:
            vals = customer._magento_vals_from_partner(customer.partner_id, changed_fields)
            if vals:
                customer.write(vals)
