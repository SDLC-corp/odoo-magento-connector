from odoo import models, fields


class MagentoSyncReport(models.Model):
    _name = "magento.sync.report"
    _description = "Magento Sync Report"
    _order = "synced_at desc, id desc"

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    synced_at = fields.Datetime(default=fields.Datetime.now, required=True)
    sync_type = fields.Selection(
        [
            ("all", "All"),
            ("category", "Category"),
            ("product", "Product"),
            ("order", "Order"),
            ("customer", "Customer"),
        ],
        default="all",
        required=True,
    )
    mode = fields.Selection(
        [("manual", "Manual"), ("cron", "Cron")],
        default="manual",
        required=True,
    )
    source_action = fields.Char(default="manual")
    total_records = fields.Integer(default=0)
    success = fields.Integer(default=0)
    errors = fields.Integer(default=0)
    start_time = fields.Datetime()
    end_time = fields.Datetime()
    categories_created = fields.Integer(default=0)
    categories_updated = fields.Integer(default=0)
    products_created = fields.Integer(default=0)
    products_updated = fields.Integer(default=0)
    orders_created = fields.Integer(default=0)
    orders_updated = fields.Integer(default=0)
    customers_created = fields.Integer(default=0)
    customers_updated = fields.Integer(default=0)
    inventory_updated = fields.Integer(default=0)
    notes = fields.Text()
