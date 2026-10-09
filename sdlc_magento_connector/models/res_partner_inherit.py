from odoo import fields, models


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

    _magento_customer_uniq = models.Constraint(
        "unique(magento_instance_id, magento_customer_id)",
        "A Magento customer can only be linked to one contact per Magento instance.",
    )
