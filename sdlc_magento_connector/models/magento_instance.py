import calendar
import logging
import time
from contextlib import nullcontext
from datetime import datetime, timedelta

import pytz
import requests
from requests.compat import urlparse, urlunparse

from odoo import api, models, fields
from odoo.modules import module as odoo_module
from odoo.addons.base.models.res_partner import _tz_get
from odoo.exceptions import ValidationError, UserError
from ..services.magento_api import MagentoAPI
from . import cron_compat


_logger = logging.getLogger(__name__)

# Records processed between two commits when a sync runs from cron.
_SYNC_BATCH_SIZE = 50
# Magento product types the product import supports. Configurable products become a
# template with variants; bundle and grouped products keep their components in
# ``magento.product.link`` (see magento_product_variant.py).
_IMPORTABLE_PRODUCT_TYPES = frozenset({
    "simple", "virtual", "downloadable", "configurable", "bundle", "grouped",
})
# Error/skip lines kept in a sync report's notes.
_MAX_NOTE_LINES = 50

# Queued syncs (roadmap 1.1): entity key -> (sync method, label, report sync_type, priority).
# The entity key names the job channel ``magento.<instance id>.<entity>`` and the identity
# key ``sync-<instance id>-<entity>`` (the first job of a run; the next pages of a paged
# run use ``sync-<instance id>-<entity>-p<page>-r<report id>``). Order = run order of
# "Sync All" and scheduled syncs.
_SYNC_JOB_SPECS = {
    "categories": ("sync_categories", "Category", "category", 8),
    "attribute_sets": ("sync_attribute_sets", "Attribute set", None, 8),
    "products": ("sync_products", "Product", "product", 10),
    "orders": ("sync_orders", "Order", "order", 12),
    "customers": ("sync_customers", "Customer", "customer", 10),
}
_SYNC_ALL_ENTITIES = ("categories", "products", "orders", "customers")
# Scheduled-sync scope key (``_get_auto_sync_scope``) -> queued entity key.
_SCOPE_ENTITIES = (
    ("category", "categories"),
    ("product", "products"),
    ("order", "orders"),
    ("customer", "customers"),
)
# Queued syncs of these entity keys run ONE page per job (``_magento_run_sync_page``), so
# a big catalogue never becomes one cron transaction longer than the cron time limit.
# Entity key -> (record kind, MagentoAPI list getter). Categories and attribute sets are
# small trees and stay single-job.
_PAGED_SYNC_ENTITIES = {
    "products": ("product", "get_products"),
    "orders": ("order", "get_orders"),
    "customers": ("customer", "get_customers"),
}
# A page job stops importing after this many seconds and leaves the rest of its page to
# the continuation job (the keyset cursor makes a partial page resumable), so a single
# job stays well under ``limit_time_real_cron`` (120 s by default) even on a slow Magento.
_SYNC_PAGE_TIME_BUDGET = 60

# Data Ownership (roadmap 3.8): system of record per entity. ``two_way`` (default) is the
# behaviour before 3.8. Import paths check ``_magento_import_block_reason``, push paths
# ``_magento_export_block_reason`` (see magento_dryrun.py for the push-side overrides).
OWNERSHIP_ENTITIES = (
    ("product", "Products"),
    ("category", "Categories"),
    ("customer", "Customers"),
    ("order", "Orders"),
    ("stock", "Stock"),
    ("price", "Prices"),
)
OWNERSHIP_DIRECTIONS = [
    ("two_way", "Two-way"),
    ("magento_to_odoo", "Magento → Odoo"),
    ("odoo_to_magento", "Odoo → Magento"),
    ("off", "Off"),
]
OWNERSHIP_UPDATE_MODES = [
    ("create_update", "Create and update"),
    ("create_only", "Create only"),
]
# Plain-ASCII wording of the directions for skip reasons (they are logged too).
_OWNERSHIP_DIRECTION_TEXT = {
    "two_way": "two-way",
    "magento_to_odoo": "Magento -> Odoo",
    "odoo_to_magento": "Odoo -> Magento",
    "off": "off",
}
_INBOUND_DIRECTIONS = frozenset({"two_way", "magento_to_odoo"})
_OUTBOUND_DIRECTIONS = frozenset({"two_way", "odoo_to_magento"})


def _ownership_direction_field(entity, label):
    return fields.Selection(
        OWNERSHIP_DIRECTIONS, string="%s Direction" % label, default="two_way", required=True,
        help="Which side may write %s: Two-way (both), Magento → Odoo (imports only, Odoo "
             "edits are not pushed), Odoo → Magento (pushes only, imports skip them) or Off "
             "(neither)." % label.lower(),
    )


def _ownership_update_field(entity, label):
    return fields.Selection(
        OWNERSHIP_UPDATE_MODES, string="%s Existing Records" % label, default="create_update",
        required=True,
        help="Create only: a sync creates missing %s but never overwrites one that already "
             "exists on the receiving side (skipped and reported)." % label.lower(),
    )


def _short_reason(exc, limit=300):
    """One-line failure reason for logs/reports; never dumps payloads."""
    if isinstance(exc, UserError):
        text = str(exc.args[0] if exc.args else exc)
    else:
        text = "%s: %s" % (type(exc).__name__, exc)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _magento_datetime(value):
    """Magento timestamp (UTC ``YYYY-MM-DD HH:MM:SS``) -> naive UTC datetime, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(pytz.utc).replace(
            tzinfo=None)
    text = str(value).strip().replace("T", " ")
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


class _WatermarkTracker:
    """Where an incremental run may safely move an entity's ``updated_at`` watermark.

    Gap-free rule: the next run fetches ``updated_at >= watermark - overlap``.

    * Nothing failed: the watermark advances to the newest ``updated_at`` among the
      records processed successfully (skipped records count as processed).
    * Something failed: it advances at most to the oldest failed ``updated_at``
      minus one second, so every failed record is fetched again next run and is
      never skipped, even when newer records succeeded.
    * A failed record without a usable ``updated_at`` holds the watermark where it
      was. A listing cut short by a fetch error is still safe: results are sorted
      by ``updated_at`` ascending, so every record not yet seen is at least as new
      as the last one processed and ``gteq`` fetches it next run.
    """

    def __init__(self, before):
        self.before = before
        self.max_ok = None
        self.min_failed = None
        self.unknown_failure = False

    def record(self, updated_at, ok):
        ts = _magento_datetime(updated_at)
        if ok:
            if ts and (self.max_ok is None or ts > self.max_ok):
                self.max_ok = ts
        elif ts is None:
            self.unknown_failure = True
        elif self.min_failed is None or ts < self.min_failed:
            self.min_failed = ts

    def target(self):
        if self.unknown_failure:
            return self.before
        if self.min_failed is not None:
            return self.min_failed - timedelta(seconds=1)
        return self.max_ok or self.before

    def state(self):
        """JSON-serialisable state, carried by the jobs of a paged sync run."""
        to_str = fields.Datetime.to_string
        return {
            "max_ok": to_str(self.max_ok) if self.max_ok else None,
            "min_failed": to_str(self.min_failed) if self.min_failed else None,
            "unknown_failure": bool(self.unknown_failure),
        }

    @classmethod
    def from_state(cls, before, state):
        tracker = cls(before)
        state = state or {}
        tracker.max_ok = _magento_datetime(state.get("max_ok"))
        tracker.min_failed = _magento_datetime(state.get("min_failed"))
        tracker.unknown_failure = bool(state.get("unknown_failure"))
        return tracker


class _SyncStats:
    """Counters of one sync run, returned by the ``sync_*`` methods as a dict."""

    def __init__(self, label):
        self.label = label
        self.created = 0
        self.updated = 0
        self.errors = 0
        self.skipped = 0
        self.processed = 0
        self.inventory_updated = 0
        self.pages_fetched = 0
        self.records_seen = 0
        self.error_messages = []
        self.skip_messages = []
        self.sync_notes = []
        self.watermark_before = None
        self.watermark_after = None
        # Exception of the last record that failed in ``_sync_record`` (for re-raise).
        self.last_exception = None

    def add(self, outcome):
        setattr(self, outcome, getattr(self, outcome) + 1)

    def error(self, kind, magento_id, exc, instance=None, reason=None, step=None):
        self.errors += 1
        reason = reason or _short_reason(exc)
        what = "%s %s" % (kind, magento_id) if magento_id is not None else "%s list fetch" % kind
        if step:
            what += " [%s]" % step
        self.error_messages.append("%s: %s" % (what, reason))
        _logger.warning(
            "Magento sync (instance %s): %s failed: %s",
            instance.name if instance else "-", what, reason,
            exc_info=_logger.isEnabledFor(logging.DEBUG),
        )

    def skip(self, kind, magento_id, reason, instance=None):
        self.skipped += 1
        self.skip_messages.append("%s %s: %s" % (kind, magento_id, reason))
        _logger.info(
            "Magento sync (instance %s): %s %s skipped: %s",
            instance.name if instance else "-", kind, magento_id, reason,
        )

    def merge(self, counts):
        for key in ("created", "updated", "errors", "skipped", "processed", "inventory_updated",
                    "pages_fetched", "records_seen"):
            setattr(self, key, getattr(self, key) + int((counts or {}).get(key) or 0))
        self.error_messages.extend((counts or {}).get("error_messages") or [])
        self.skip_messages.extend((counts or {}).get("skip_messages") or [])
        self.sync_notes.extend((counts or {}).get("sync_notes") or [])

    def as_dict(self):
        return {
            "created": self.created,
            "updated": self.updated,
            "errors": self.errors,
            "skipped": self.skipped,
            "processed": self.processed,
            "inventory_updated": self.inventory_updated,
            "pages_fetched": self.pages_fetched,
            "records_seen": self.records_seen,
            "watermark_before": self.watermark_before,
            "watermark_after": self.watermark_after,
            "error_messages": list(self.error_messages),
            "skip_messages": list(self.skip_messages),
            "sync_notes": list(self.sync_notes),
        }


class MagentoInstance(models.Model):
    _name = "magento.instance"
    _description = "Magento Instance"
    _inherit = ["mail.thread"]

    _AUTO_SYNC_TRIGGER_FIELDS = {
        "auto_sync_enabled",
        "auto_sync_frequency",
        "auto_sync_hour",
        "auto_sync_minute",
        "auto_sync_tz",
        "auto_sync_hour_interval",
        "auto_sync_weekday",
        "auto_sync_weekly_date",
        "auto_sync_month_day",
        "auto_sync_monthly_date",
        "auto_sync_products",
        "auto_sync_customers",
        "auto_sync_categories",
        "auto_sync_orders",
        "active",
        "name",
    }

    # =====================
    # FIELDS
    # =====================
    name = fields.Char(required=True)
    base_url = fields.Char(
        required=True,
        help=(
            "Magento base URL, e.g. https://magento.example.com. "
            "Use localhost only when Magento runs on the same host/container as Odoo."
        ),
    )
    access_token = fields.Char(required=True, groups="sdlc_magento_connector.group_magento_manager")
    active = fields.Boolean(default=True)
    verify_ssl = fields.Boolean(default=True)
    website_id = fields.Integer(string="Magento Website ID")
    store_id = fields.Integer(string="Magento Store ID")
    store_code = fields.Char(string="Magento Store Code")
    customer_default_password = fields.Char(
        string="Magento User Password", groups="sdlc_magento_connector.group_magento_manager"
    )
    customer_password_min_length = fields.Integer(
        string="Customer Password Min Length",
        default=8,
    )
    default_payment_method = fields.Char(
        string="Default Payment Method Code",
        help="Magento payment method code used when creating orders (e.g. checkmo, banktransfer).",
    )
    default_shipping_method = fields.Char(
        string="Default Shipping Method Code",
        help="Magento shipping method code used when creating orders (e.g. flatrate_flatrate).",
    )
    default_shipping_description = fields.Char(
        string="Default Shipping Description",
        help="Description shown on Magento orders (optional).",
    )
    default_shipping_amount = fields.Float(
        string="Default Shipping Amount",
        default=0.0,
    )
    auto_register_magento_payment = fields.Boolean(
        string="Auto Register Magento Payments",
        default=True,
        help="When Magento shows an order as paid, register payments on the Odoo invoices.",
    )
    payment_journal_id = fields.Many2one(
        "account.journal",
        string="Payment Journal",
        domain="[('type', 'in', ('bank', 'cash', 'credit'))]",
        help="Journal used to register Magento payments in Odoo.",
    )
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line",
        string="Payment Method",
        help="Payment method used when registering Magento payments in Odoo.",
    )
    update_magento_status_on_paid = fields.Boolean(
        string="Update Magento Status When Paid",
        default=True,
        help="When an Odoo invoice is paid, update the Magento order status.",
    )
    magento_paid_status = fields.Char(
        string="Magento Paid Status Code",
        help="Magento order status code to set when Odoo invoice is paid (e.g. complete).",
    )
    root_category_id = fields.Integer(string="Magento Root Category ID")
    attribute_set_skeleton_id = fields.Integer(
        string="Attribute Set Skeleton ID",
        default=4,
        help="Magento Attribute Set template ID used when creating new sets.",
    )
    auto_sync_enabled = fields.Boolean(
        string="Auto Sync Enabled",
        default=False,
        help="Automatically run Magento sync for this instance using the schedule below.",
    )
    auto_sync_frequency = fields.Selection(
        [
            ("hourly", "Hourly"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
            ("monthly", "Monthly"),
        ],
        string="Auto Sync Frequency",
        default="hourly",
        required=True,
    )
    auto_sync_hour = fields.Integer(
        string="Hour",
        default=1,
        help="Hour in 24-hour format (0-23), used for daily/weekly/monthly schedules.",
    )
    auto_sync_minute = fields.Integer(
        string="Minute",
        default=0,
        help="Minute in hour (0-59), used for daily/weekly/monthly schedules.",
    )
    auto_sync_tz = fields.Selection(
        _tz_get,
        string="Schedule Timezone",
        default=lambda self: self.env.user.tz or "UTC",
        help="Timezone in which the auto sync hour/minute/day are interpreted.",
    )
    auto_sync_hour_interval = fields.Selection(
        [(str(i), "Every %s hour(s)" % i) for i in range(1, 25)],
        string="Hourly Interval",
        default="1",
        help="Used when frequency is Hourly.",
    )
    auto_sync_weekday = fields.Selection(
        [
            ("0", "Monday"),
            ("1", "Tuesday"),
            ("2", "Wednesday"),
            ("3", "Thursday"),
            ("4", "Friday"),
            ("5", "Saturday"),
            ("6", "Sunday"),
        ],
        string="Weekday",
        default="0",
    )
    auto_sync_weekly_date = fields.Date(
        string="Weekly Date",
        default=fields.Date.today,
        help="Select date from calendar; weekday of this date is used for weekly schedule.",
    )
    auto_sync_month_day = fields.Integer(
        string="Day of Month",
        default=1,
    )
    auto_sync_monthly_date = fields.Date(
        string="Monthly Date",
        default=fields.Date.today,
        help="Select date from calendar; day of this date is used for monthly schedule.",
    )
    auto_sync_products = fields.Boolean(
        string="Products",
        default=True,
    )
    auto_sync_customers = fields.Boolean(
        string="Customers",
        default=True,
    )
    auto_sync_categories = fields.Boolean(
        string="Categories",
        default=True,
    )
    auto_sync_orders = fields.Boolean(
        string="Orders",
        default=True,
    )
    auto_sync_cron_id = fields.Many2one(
        "ir.cron",
        string="Auto Sync Cron",
        readonly=True,
        copy=False,
    )

    sync_product_ids = fields.One2many(
        "magento.product.map", "instance_id", string="Synced Products"
    )

    # ---- Incremental sync (Phase 1.4) ----
    # Watermarks are naive UTC, the format Magento uses for ``updated_at``.
    orders_sync_watermark = fields.Datetime(
        string="Orders Synced Up To",
        copy=False,
        help="Magento updated_at (UTC) up to which orders are known to be imported. The next sync "
             "fetches orders updated since this time minus the overlap. Empty = full sync.",
    )
    products_sync_watermark = fields.Datetime(
        string="Products Synced Up To",
        copy=False,
        help="Magento updated_at (UTC) up to which products are known to be imported. The next sync "
             "fetches products updated since this time minus the overlap. Empty = full sync.",
    )
    customers_sync_watermark = fields.Datetime(
        string="Customers Synced Up To",
        copy=False,
        help="Magento updated_at (UTC) up to which customers are known to be imported. The next sync "
             "fetches customers updated since this time minus the overlap. Empty = full sync.",
    )
    incremental_overlap_minutes = fields.Integer(
        string="Sync Overlap (minutes)",
        default=10,
        help="Incremental syncs re-read records updated this many minutes before the stored marker, "
             "to absorb clock skew and records saved while the previous sync was paging.",
    )
    run_syncs_in_background = fields.Boolean(
        string="Run Syncs in Background",
        default=True,
        help="Sync buttons and scheduled syncs queue one job per entity (see Job Queue) instead of "
             "running inside the request; pushes of Odoo edits are left to the job runner. "
             "Untick to run syncs synchronously and push edits right after saving (the failed "
             "pushes are still retried by the queue).",
    )
    pending_job_count = fields.Integer(
        string="Queued Jobs", compute="_compute_pending_job_count",
        help="Jobs of this instance waiting or running in the Job Queue.",
    )
    sync_page_size = fields.Integer(
        string="Sync Page Size",
        default=100,
        help="Records requested per Magento API page (1-300; Magento refuses more than 300). "
             "A queued order or customer sync runs one page per job.",
    )
    product_sync_page_size = fields.Integer(
        string="Product Sync Page Size",
        default=50,
        help="Products imported per job by a queued product sync (1-300). Each product also "
             "fetches its details and image, so keep a page well under the cron time limit.",
    )

    # ---- Data Ownership (roadmap 3.8): direction + "can update existing" per entity ----
    ownership_product_direction = _ownership_direction_field("product", "Products")
    ownership_product_update = _ownership_update_field("product", "Products")
    ownership_category_direction = _ownership_direction_field("category", "Categories")
    ownership_category_update = _ownership_update_field("category", "Categories")
    ownership_customer_direction = _ownership_direction_field("customer", "Customers")
    ownership_customer_update = _ownership_update_field("customer", "Customers")
    ownership_order_direction = _ownership_direction_field("order", "Orders")
    ownership_order_update = _ownership_update_field("order", "Orders")
    ownership_stock_direction = _ownership_direction_field("stock", "Stock")
    ownership_stock_update = _ownership_update_field("stock", "Stock")
    ownership_price_direction = _ownership_direction_field("price", "Prices")
    ownership_price_update = _ownership_update_field("price", "Prices")

    # ---- Shared contract fields (Phase 0); consumed by views, dashboard and record rules ----
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company
    )
    state = fields.Selection(
        [("draft", "Draft"), ("connected", "Connected"), ("error", "Connection Error")],
        default="draft", required=True, copy=False, tracking=True,
    )
    last_connection_message = fields.Char(readonly=True, copy=False)
    last_sync_at = fields.Datetime(readonly=True, copy=False)
    product_count = fields.Integer(compute="_compute_record_counts")
    customer_count = fields.Integer(compute="_compute_record_counts")
    category_count = fields.Integer(compute="_compute_record_counts")
    order_count = fields.Integer(compute="_compute_record_counts")
    sync_error_count = fields.Integer(compute="_compute_record_counts")

    def _compute_record_counts(self):
        counts = {}
        for model, field_name, key in (
            ("magento.product.map", "instance_id", "product_count"),
            ("magento.customer", "instance_id", "customer_count"),
            ("magento.category", "instance_id", "category_count"),
            ("sale.order", "magento_instance_id", "order_count"),
        ):
            rows = self.env[model]._read_group(
                [(field_name, "in", self.ids)], [field_name], ["__count"]
            )
            counts[key] = {inst.id: n for inst, n in rows}
        rows = self.env["magento.sync.report"]._read_group(
            [("instance_id", "in", self.ids), ("errors", ">", 0)], ["instance_id"], ["__count"]
        )
        counts["sync_error_count"] = {inst.id: n for inst, n in rows}
        for rec in self:
            for key, per_instance in counts.items():
                rec[key] = per_instance.get(rec.id, 0)

    def _compute_pending_job_count(self):
        rows = self.env["magento.job"]._read_group(
            [("instance_id", "in", self.ids), ("state", "in", ("pending", "started"))],
            ["instance_id"], ["__count"],
        )
        counts = {inst.id: n for inst, n in rows}
        for rec in self:
            rec.pending_job_count = counts.get(rec.id, 0)

    def action_view_queued_jobs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sdlc_magento_connector.action_magento_job")
        action["domain"] = [("instance_id", "=", self.id), ("state", "in", ("pending", "started"))]
        action["context"] = {}
        return action

    def _action_open_related(self, model, field_name, name):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": "tree,form",
            "domain": [(field_name, "=", self.id)],
            "context": {"default_%s" % field_name: self.id},
        }

    def action_view_products(self):
        return self._action_open_related("magento.product.map", "instance_id", "Products")

    def action_view_customers(self):
        return self._action_open_related("magento.customer", "instance_id", "Customers")

    def action_view_categories(self):
        return self._action_open_related("magento.category", "instance_id", "Categories")

    def action_view_orders(self):
        return self._action_open_related("sale.order", "magento_instance_id", "Orders")

    def action_view_sync_errors(self):
        action = self._action_open_related("magento.sync.report", "instance_id", "Sync Errors")
        action["domain"].append(("errors", ">", 0))
        return action

    # =====================
    # HELPERS
    # =====================
    @api.model
    def _cleanup_legacy_billing_agreement_artifacts(self):
        """Remove old Billing Agreement views/menus/actions/cron left from previous versions."""
        legacy_xmlids = [
            "sdlc_magento_connector.view_magento_instance_form_billing_agreement_sync",
            "sdlc_magento_connector.sale_order_form_magento_billing_agreement_sync",
            "sdlc_magento_connector.view_sale_order_tree_magento_billing_agreements",
            "sdlc_magento_connector.view_sale_order_search_magento_billing_agreements",
            "sdlc_magento_connector.view_move_form_magento_billing_agreement_sync",
            "sdlc_magento_connector.action_magento_billing_agreements",
            "sdlc_magento_connector.menu_magento_billing_agreements",
            "sdlc_magento_connector.menu_sale_magento_billing_agreements",
            "sdlc_magento_connector.cron_magento_billing_agreement_payment_sync",
        ]
        imd = self.env["ir.model.data"].sudo()
        for xmlid in legacy_xmlids:
            module, name = xmlid.split(".", 1)
            imd_record = imd.search(
                [("module", "=", module), ("name", "=", name)],
                limit=1,
            )
            if not imd_record:
                continue
            target = self.env[imd_record.model].sudo().browse(imd_record.res_id)
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    if imd_record.model in ("ir.ui.view", "ir.ui.menu", "ir.cron"):
                        try:
                            target.write({"active": False})
                        except Exception:
                            pass
            imd_record.unlink()
        return True

    @api.constrains("auto_sync_hour")
    def _check_auto_sync_hour(self):
        for instance in self:
            hour = int(instance.auto_sync_hour or 0)
            if hour < 0 or hour > 23:
                raise ValidationError("Auto Sync Hour must be between 0 and 23.")

    @api.constrains("auto_sync_minute")
    def _check_auto_sync_minute(self):
        for instance in self:
            minute = int(instance.auto_sync_minute or 0)
            if minute < 0 or minute > 59:
                raise ValidationError("Auto Sync Minute must be between 0 and 59.")

    @api.constrains("auto_sync_month_day")
    def _check_auto_sync_month_day(self):
        for instance in self:
            month_day = int(instance.auto_sync_month_day or 0)
            if month_day < 1 or month_day > 31:
                raise ValidationError("Auto Sync Day of Month must be between 1 and 31.")

    @api.constrains("sync_page_size", "product_sync_page_size")
    def _check_sync_page_size(self):
        for instance in self:
            if not 1 <= int(instance.sync_page_size or 0) <= MagentoAPI.MAX_PAGE_SIZE:
                raise ValidationError(
                    "Sync Page Size must be between 1 and %s." % MagentoAPI.MAX_PAGE_SIZE
                )
            if not 1 <= int(instance.product_sync_page_size or 0) <= MagentoAPI.MAX_PAGE_SIZE:
                raise ValidationError(
                    "Product Sync Page Size must be between 1 and %s." % MagentoAPI.MAX_PAGE_SIZE
                )

    @api.constrains("incremental_overlap_minutes")
    def _check_incremental_overlap_minutes(self):
        for instance in self:
            if int(instance.incremental_overlap_minutes or 0) < 0:
                raise ValidationError("Sync Overlap cannot be negative.")

    @api.constrains(
        "auto_sync_enabled",
        "auto_sync_products",
        "auto_sync_customers",
        "auto_sync_categories",
        "auto_sync_orders",
    )
    def _check_auto_sync_scope_selection(self):
        for instance in self:
            if instance.auto_sync_enabled and not any(
                [
                    instance.auto_sync_products,
                    instance.auto_sync_customers,
                    instance.auto_sync_categories,
                    instance.auto_sync_orders,
                ]
            ):
                raise ValidationError(
                    "Select at least one Auto Sync scope: Products, Customers, Categories, or Orders."
                )

    def _normalize_base_url(self, value):
        url = (value or "").strip()
        if not url:
            raise UserError("Magento base URL is required.")
        if "://" not in url:
            url = f"https://{url}"
        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        if scheme not in {"http", "https"}:
            raise UserError("Magento base URL must start with http:// or https://")
        netloc = (parsed.netloc or "").strip().lower()
        if not netloc:
            raise UserError("Please provide a valid Magento base URL.")
        path = (parsed.path or "").rstrip("/")
        # Keep only Magento root path.
        # Users often paste full REST URLs (e.g. /rest/V1), which would duplicate
        # endpoint prefixes when the connector builds API routes.
        rest_suffixes = ("/rest/default/v1", "/rest/all/v1", "/rest/v1", "/rest")
        lowered_path = path.lower()
        trimmed = True
        while trimmed and lowered_path:
            trimmed = False
            for suffix in rest_suffixes:
                if lowered_path.endswith(suffix):
                    path = path[: -len(suffix)].rstrip("/")
                    lowered_path = path.lower()
                    trimmed = True
                    break
        return urlunparse((scheme, netloc, path, "", "", ""))

    def _sanitize_connection_vals(self, vals):
        vals = dict(vals or {})
        if "base_url" in vals:
            vals["base_url"] = self._normalize_base_url(vals.get("base_url"))
        if "access_token" in vals and isinstance(vals.get("access_token"), str):
            vals["access_token"] = vals["access_token"].strip()
        return vals

    def _normalized_auto_sync_hour(self):
        self.ensure_one()
        return max(0, min(23, int(self.auto_sync_hour or 0)))

    def _normalized_auto_sync_minute(self):
        self.ensure_one()
        return max(0, min(59, int(self.auto_sync_minute or 0)))

    def _normalized_auto_sync_hour_interval(self):
        self.ensure_one()
        try:
            interval = int(self.auto_sync_hour_interval or "1")
        except (TypeError, ValueError):
            interval = 1
        return max(1, min(24, interval))

    def _normalized_auto_sync_month_day(self):
        self.ensure_one()
        if self.auto_sync_monthly_date:
            try:
                dt_value = fields.Date.to_date(self.auto_sync_monthly_date)
                if dt_value:
                    return dt_value.day
            except Exception:
                pass
        return max(1, min(31, int(self.auto_sync_month_day or 1)))

    def _normalized_auto_sync_weekday(self):
        self.ensure_one()
        if self.auto_sync_weekly_date:
            try:
                dt_value = fields.Date.to_date(self.auto_sync_weekly_date)
                if dt_value:
                    return dt_value.weekday()
            except Exception:
                pass
        return max(0, min(6, int(self.auto_sync_weekday or "0")))

    # =====================
    # AUTO SYNC SCHEDULE
    # =====================
    def _get_auto_sync_tz(self):
        """Timezone the schedule hours/minutes are expressed in."""
        self.ensure_one()
        tz_name = self.auto_sync_tz or self.env.user.tz or self.company_id.partner_id.tz or "UTC"
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    @api.model
    def _build_monthly_datetime(self, year, month, day, hour, minute=0):
        last_day = calendar.monthrange(year, month)[1]
        safe_day = min(day, last_day)
        return datetime(year, month, safe_day, hour, minute, 0)

    def _compute_auto_sync_nextcall(self, now=None):
        """Next run as a naive UTC datetime.

        Hour/minute/weekday/day settings are interpreted in the instance's
        timezone (``auto_sync_tz``, falling back to the user's timezone).
        """
        self.ensure_one()
        tz = self._get_auto_sync_tz()
        now_utc = fields.Datetime.to_datetime(now or fields.Datetime.now())
        now_dt = pytz.utc.localize(now_utc).astimezone(tz).replace(tzinfo=None)
        frequency = self.auto_sync_frequency or "hourly"
        hour = self._normalized_auto_sync_hour()
        minute = self._normalized_auto_sync_minute()

        if frequency == "hourly":
            interval = self._normalized_auto_sync_hour_interval()
            next_dt = now_dt.replace(minute=minute, second=0, microsecond=0)
            if next_dt <= now_dt:
                next_dt += timedelta(hours=1)
            if interval > 1:
                while next_dt.hour % interval != 0:
                    next_dt += timedelta(hours=1)
        elif frequency == "daily":
            next_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_dt <= now_dt:
                next_dt += timedelta(days=1)
        elif frequency == "weekly":
            target_weekday = self._normalized_auto_sync_weekday()
            next_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta_days = (target_weekday - next_dt.weekday()) % 7
            next_dt += timedelta(days=delta_days)
            if next_dt <= now_dt:
                next_dt += timedelta(days=7)
        else:
            day = self._normalized_auto_sync_month_day()
            year = now_dt.year
            month = now_dt.month
            next_dt = self._build_monthly_datetime(year, month, day, hour, minute)
            if next_dt <= now_dt:
                if month == 12:
                    year += 1
                    month = 1
                else:
                    month += 1
                next_dt = self._build_monthly_datetime(year, month, day, hour, minute)

        # Local wall-clock time -> naive UTC (ir.cron stores UTC).
        local_dt = tz.localize(next_dt, is_dst=False)
        return local_dt.astimezone(pytz.utc).replace(tzinfo=None)

    def _prepare_auto_sync_cron_vals(self):
        self.ensure_one()
        model = self.env["ir.model"].sudo().search([("model", "=", "magento.instance")], limit=1)
        if not model:
            raise UserError("Could not find model 'magento.instance' to create auto sync cron.")

        interval_map = {
            "hourly": "hours",
            "daily": "days",
            "weekly": "weeks",
            "monthly": "months",
        }
        interval_type = interval_map.get(self.auto_sync_frequency or "hourly", "hours")
        interval_number = (
            self._normalized_auto_sync_hour_interval()
            if interval_type == "hours"
            else 1
        )
        nextcall = fields.Datetime.to_string(self._compute_auto_sync_nextcall())
        code = "model.browse(%s).cron_run_auto_sync()" % self.id

        try:
            cron_user = self.env.ref("base.user_root")
        except ValueError:
            cron_user = self.env.user

        return {
            "name": "Magento Auto Sync - %s" % (self.name or self.id),
            "model_id": model.id,
            "state": "code",
            "code": code,
            "interval_number": interval_number,
            "interval_type": interval_type,
            "nextcall": nextcall,
            "user_id": cron_user.id,
            "active": True,
        }

    def _apply_auto_sync_cron(self):
        cron_model = self.env["ir.cron"].sudo()
        for instance in self.sudo():
            cron = instance.auto_sync_cron_id.sudo()
            if not instance.auto_sync_enabled or not instance.active:
                if cron:
                    cron.write({"active": False})
                continue

            vals = instance._prepare_auto_sync_cron_vals()
            if cron:
                cron.write(vals)
            else:
                cron = cron_model.create(vals)
                instance.write({"auto_sync_cron_id": cron.id})

    def action_apply_auto_sync_cron(self):
        self.ensure_one()
        self._apply_auto_sync_cron()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Auto Sync",
                "message": "Auto sync schedule has been applied.",
                "type": "success",
                "sticky": False,
            },
        }

    @api.model_create_multi
    def create(self, vals_list):
        sanitized_vals_list = []
        for vals in vals_list:
            clean_vals = self._sanitize_connection_vals(vals)
            if "access_token" in clean_vals and not clean_vals.get("access_token"):
                raise UserError("Magento access token is required.")
            sanitized_vals_list.append(clean_vals)

        records = super().create(sanitized_vals_list)
        records._apply_auto_sync_cron()
        return records

    def write(self, vals):
        clean_vals = self._sanitize_connection_vals(vals)
        if "access_token" in clean_vals and not clean_vals.get("access_token"):
            raise UserError("Magento access token is required.")

        result = super().write(clean_vals)
        if self._AUTO_SYNC_TRIGGER_FIELDS.intersection(clean_vals.keys()):
            self._apply_auto_sync_cron()
        return result

    def unlink(self):
        crons = self.mapped("auto_sync_cron_id").sudo()
        result = super().unlink()
        if crons:
            crons.unlink()
        return result

    # =====================
    # CRON ENTRYPOINTS
    # =====================
    def _is_magento_cron(self):
        return bool(self.env.context.get("magento_cron"))

    def _magento_can_commit(self):
        """Commit only inside a real ``ir.cron`` job (its progress record is in the
        context), never for manual/server-action calls of the cron entrypoints, never
        in interactive runs and never under tests."""
        in_test_mode = getattr(self.env.registry, "in_test_mode", None)
        return bool(
            self._is_magento_cron()
            and cron_compat.in_cron_run(self.env)
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    def _magento_commit_batch(self, processed):
        """Commit progress during real cron runs only. Interactive runs never commit."""
        if self._magento_can_commit():
            cron_compat.commit_progress(self.env, processed)

    def _magento_isolation(self):
        """Savepoint isolating a whole step/instance.

        When batches are committed (real cron) a savepoint cannot span the commits;
        records are then isolated by their own savepoints and everything before the
        failure is already committed, so no rollback is needed (or done).
        """
        if self._magento_can_commit():
            return nullcontext()
        return self.env.cr.savepoint()

    def _run_isolated_step(self, label, func):
        """Run a whole sync step; an unexpected crash must not stop the other steps."""
        self.ensure_one()
        try:
            with self._magento_isolation():
                return func()
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Magento %s sync aborted for instance %s: %s",
                label, self.name, _short_reason(exc), exc_info=_logger.isEnabledFor(logging.DEBUG),
            )
            return {"errors": 1, "error_messages": ["%s sync aborted: %s" % (label, _short_reason(exc))]}

    def _run_scheduled_sync(self, source_action):
        """Scheduled run: queue one job per entity in scope (background mode), or sync inline."""
        for instance in self.with_context(magento_cron=True):
            if not instance.active:
                continue
            try:
                with instance._magento_isolation():
                    if instance._magento_sync_in_background():
                        instance._magento_enqueue_scheduled_sync(source_action)
                    else:
                        instance._sync_selected_auto_and_report(source_action=source_action)
            except Exception:  # noqa: BLE001
                _logger.exception("Magento scheduled sync failed for instance %s", instance.name)
            else:
                instance._magento_commit_batch(0)
        return True

    def cron_run_auto_sync(self):
        """Per-instance scheduled run (code of the cron built by ``_apply_auto_sync_cron``)."""
        return self._run_scheduled_sync("cron_auto_scope")

    @api.model
    def _cron_sync_all(self):
        """Global scheduled run over every active instance.

        Instances with their own auto-sync schedule are skipped (their per-instance
        cron runs them), so an instance is never synced twice by two schedulers.
        The scope of each instance follows its Products/Customers/Categories/Orders
        auto-sync options.
        """
        instances = self.search([("active", "=", True), ("auto_sync_enabled", "=", False)])
        return instances._run_scheduled_sync("cron_sync_all")

    def _get_auto_sync_scope(self):
        self.ensure_one()
        return {
            "category": bool(self.auto_sync_categories),
            "product": bool(self.auto_sync_products),
            "order": bool(self.auto_sync_orders),
            "customer": bool(self.auto_sync_customers),
        }

    def _resolve_report_sync_type(self, scope):
        enabled = [key for key, value in scope.items() if value]
        if len(enabled) != 1:
            return "all"
        mapping = {
            "category": "category",
            "product": "product",
            "order": "order",
            "customer": "customer",
        }
        return mapping.get(enabled[0], "all")

    def _sync_selected_auto_and_report(self, source_action="cron_auto_scope"):
        self.ensure_one()
        scope = self._get_auto_sync_scope()
        if not any(scope.values()):
            self._report_empty_auto_sync_scope(source_action)
            return False

        sync_steps = [
            ("category", "sync_categories"),
            ("product", "sync_products"),
            ("order", "sync_orders"),
            ("customer", "sync_customers"),
        ]
        for sync_type, method_name in sync_steps:
            if not scope.get(sync_type):
                continue
            start_time = fields.Datetime.now()
            counts = self._run_isolated_step(sync_type, getattr(self, method_name)) or {}
            self._create_sync_report(
                sync_type, counts, start_time, fields.Datetime.now(), source_action=source_action,
            )
            self._magento_commit_batch(0)
        return True

    def _report_empty_auto_sync_scope(self, source_action):
        self.ensure_one()
        now_time = fields.Datetime.now()
        return self._create_sync_report(
            "all", {"errors": 1}, now_time, now_time, source_action=source_action,
            notes="Auto sync is enabled but no scope is selected.",
        )

    # =====================
    # QUEUED SYNCS (roadmap 1.1)
    # =====================
    def _magento_sync_in_background(self):
        """Whether the sync buttons and scheduled syncs of these instances queue jobs.

        * context ``magento_sync_now=True``: never (synchronous, for scripts and power users);
        * inside a queued job (``magento_job_id``): never, the job *is* the background run;
        * an instance with ``run_syncs_in_background`` off: never;
        * context ``magento_sync_queue=True``: always (forces the queue, e.g. in tests);
        * under the test runner (``odoo.modules.module.current_test``): never, so tests
          that call ``action_sync_*`` / the crons keep getting a synchronous run;
        * otherwise: always.
        """
        ctx = self.env.context
        if ctx.get("magento_sync_now") or ctx.get("magento_job_id"):
            return False
        if not all(self.mapped("run_syncs_in_background")):
            return False
        if ctx.get("magento_sync_queue"):
            return True
        return not odoo_module.current_test

    def _magento_enqueue_sync(self, entities, source_action=None, mode=None):
        """Queue one sync job per entity key of ``_SYNC_JOB_SPECS``.

        One channel per entity (``magento.<id>.<entity>``): an instance's orders run one
        at a time while its products may run alongside. The identity key
        ``sync-<id>-<entity>`` makes a second click (or a scheduled run) reuse the job
        already waiting. Returns ``[(entity, job, created)]``.
        """
        self.ensure_one()
        jobs = self.env["magento.job"]
        queued = []
        for entity in entities:
            _method, label, _report_type, priority = _SYNC_JOB_SPECS[entity]
            identity_key = self._magento_sync_identity_key(entity)
            # A paged run in progress (a continuation job ``<key>-p<page>-r<run>`` is
            # pending or started) also counts as queued: no second run alongside it.
            existing = jobs.sudo()._find_active(identity_key) or self._magento_active_sync_run(entity)
            kwargs = {}
            if source_action:
                kwargs["source_action"] = source_action
            if mode:
                kwargs["mode"] = mode
            job = existing or jobs.enqueue(
                self, "_magento_run_sync_job", args=(entity,), kwargs=kwargs,
                instance=self,
                channel=self._magento_sync_channel(entity),
                priority=priority,
                identity_key=identity_key,
                description="Magento %s sync (%s)" % (label.lower(), self.name),
            )
            queued.append((entity, job, not existing))
        return queued

    def _magento_sync_identity_key(self, entity):
        return "sync-%s-%s" % (self.id, entity)

    def _magento_sync_channel(self, entity):
        return "magento.%s.%s" % (self.id, entity)

    def _magento_active_sync_run(self, entity):
        """Pending or started continuation job of a paged ``entity`` sync run, if any."""
        self.ensure_one()
        jobs = self.env["magento.job"].sudo()
        if entity not in _PAGED_SYNC_ENTITIES:
            return jobs.browse()
        return jobs.search([
            ("identity_key", "=like", "%s-p%%" % self._magento_sync_identity_key(entity)),
            ("state", "in", ("pending", "started")),
        ], limit=1)

    def _magento_enqueue_scheduled_sync(self, source_action):
        """Queue the auto-sync scope of this instance; reports an empty scope as before."""
        self.ensure_one()
        scope = self._get_auto_sync_scope()
        if not any(scope.values()):
            self._report_empty_auto_sync_scope(source_action)
            return []
        entities = [entity for key, entity in _SCOPE_ENTITIES if scope.get(key)]
        return self._magento_enqueue_sync(entities, source_action=source_action, mode="cron")

    def _magento_queue_notification(self, entities):
        """Queue ``entities`` and tell the user where to follow them."""
        self.ensure_one()
        queued = self._magento_enqueue_sync(entities)
        new = ", ".join(entity.replace("_", " ") for entity, _job, created in queued if created)
        old = ", ".join(entity.replace("_", " ") for entity, _job, created in queued if not created)
        if new:
            message = "Queued: %s sync" % new
            if old:
                message += " (already queued: %s)" % old
        else:
            message = "Already queued: %s sync" % old
        message += " — follow progress in Job Queue."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": message,
                "type": "info",
                "sticky": False,
            },
        }

    def _magento_run_sync_job(self, entity, source_action=None, mode=None, run=None):
        """Job body of a queued sync: run the entity sync and write its sync report.

        Runs without the cron context, so the sync engine never commits inside the job;
        per-record failures are counted in the report and the Error Center as usual.
        An exception (e.g. the instance cannot build its API client) fails or retries
        the job according to the queue's classification.

        Products, orders and customers run one page per job (``_magento_run_sync_page``);
        ``run`` is the state of the run, carried from one page job to the next.
        """
        self.ensure_one()
        method, label, report_type, _priority = _SYNC_JOB_SPECS[entity]
        if not self.active:
            return "%s sync skipped: instance %s is archived." % (label, self.name)
        if entity in _PAGED_SYNC_ENTITIES:
            return self._magento_run_sync_page(entity, run=run, source_action=source_action, mode=mode)
        start_time = fields.Datetime.now()
        counts = getattr(self, method)() or {}
        if report_type:
            self._create_sync_report(
                report_type, counts, start_time, fields.Datetime.now(),
                source_action=source_action, mode=mode,
            )
        return "%s sync: %s synced, %s skipped, %s errors" % (
            label,
            int(counts.get("created") or 0) + int(counts.get("updated") or 0),
            int(counts.get("skipped") or 0),
            int(counts.get("errors") or 0),
        )

    # =====================
    # PAGED QUEUED SYNCS (one page per job)
    # =====================
    # A queued product/order/customer sync is a *run* of page jobs. Each job imports at
    # most one page, then queues the next page itself, so no job comes near the cron time
    # limit and the runner commits after every page (a killed worker loses one page).
    #
    # Paging is keyset based, never offset based: offsets over a moving ``updated_at``
    # window skip or repeat records between jobs. The window is fixed when the run starts:
    #   updated_from (watermark - overlap) <= updated_at <= upper (run start time)
    # and pages are sorted ``updated_at ASC, entity_id ASC``. Each page asks for the rows
    # after the cursor, the ``(updated_at, entity_id)`` of the last row processed:
    #   (updated_at, entity_id) > (T, ID)
    #   == updated_at >= T  AND  (updated_at > T  OR  entity_id > ID)
    # Magento searchCriteria ANDs filter groups and ORs the filters of one group, and both
    # the flat (orders) and the EAV (products, customers) collection processors accept an
    # OR over different fields in one group, so this is two filter groups: an exact keyset.
    # Rows at or before the cursor are also skipped client side, as a guard against a
    # server that ignores a filter. A record saved during the run gets an ``updated_at``
    # past ``upper``: it is not fetched twice in this run, and the next run fetches it.
    #
    # Run state, carried in the ``run`` kwarg of every page job (JSON):
    #   upper, updated_from, before   window and watermark of the run (naive UTC strings)
    #   cursor                        [updated_at, entity_id] of the last row processed
    #   page, pages                   page number of the job, pages fetched so far
    #   stats, tracker                cumulative counters and ``_WatermarkTracker`` state
    #   report_id                     the run's single sync report, updated by every page
    # The watermark only moves when the whole run is finished.

    def _get_sync_job_page_size(self, kind):
        """Records per page job: ``product_sync_page_size`` for products (each product
        also costs a detail and an image request), ``sync_page_size`` otherwise."""
        self.ensure_one()
        size = self.product_sync_page_size if kind == "product" else self.sync_page_size
        return MagentoAPI.clamp_page_size(size)

    @staticmethod
    def _magento_cursor_key(kind, item):
        """Keyset position ``(updated_at, entity_id)`` of a search row, or None."""
        updated_at = _magento_datetime(item.get("updated_at"))
        raw_id = item.get(MagentoAPI.ID_KEYS[kind])
        if raw_id is None:
            raw_id = item.get("entity_id", item.get("id"))
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        return (updated_at, record_id) if updated_at else None

    @staticmethod
    def _magento_parse_cursor(cursor):
        if not cursor:
            return None
        updated_at = _magento_datetime(cursor[0])
        try:
            record_id = int(cursor[1])
        except (TypeError, ValueError, IndexError):
            return None
        return (updated_at, record_id) if updated_at else None

    @staticmethod
    def _magento_cursor_filters(run):
        """searchCriteria filter groups of the next page of ``run`` (see above)."""
        filters = [("updated_at", run["upper"], "lteq")]
        cursor = run.get("cursor")
        if cursor:
            updated_at, record_id = cursor
            filters.append(("updated_at", updated_at, "gteq"))
            filters.append([("updated_at", updated_at, "gt"), ("entity_id", str(record_id), "gt")])
        elif run.get("updated_from"):
            filters.append(("updated_at", run["updated_from"], "gteq"))
        return filters

    def _magento_sync_run_start(self, kind):
        """State of a new paged run (see above)."""
        self.ensure_one()
        tracker, updated_from = self._incremental_start(kind)
        to_str = fields.Datetime.to_string
        return {
            "upper": to_str(fields.Datetime.now()),
            "updated_from": to_str(updated_from) if updated_from else None,
            "before": to_str(tracker.before) if tracker.before else None,
            "cursor": None,
            "page": 1,
            "pages": 0,
            "stats": {},
            "tracker": {},
            "report_id": False,
        }

    def _magento_page_item_importer(self, kind, api, stats):
        """``row -> ok`` importing one search row exactly like the direct ``sync_*``
        loops; None when the row is ignored (product without SKU, order without id)."""
        self.ensure_one()
        if kind == "product":
            supported_types = self._supported_product_types()

            def _import(p):
                if not p.get("sku"):
                    return None
                return self._sync_one_product(api, p, supported_types, stats)
        elif kind == "order":
            def _import(data):
                magento_id = data.get("entity_id") or data.get("id")
                if not magento_id:
                    return None
                return self._sync_one_order(api, data, magento_id, stats)
        else:
            customer_model = self.env["magento.customer"].with_context(
                skip_magento_sync=True, active_test=False
            )

            def _import(data):
                return self._sync_one_customer(customer_model, data, stats)
        return _import

    @staticmethod
    def _magento_run_counts(counts):
        """Counters carried to the next page job (messages capped like the report)."""
        kept = {
            key: value for key, value in counts.items()
            if key not in ("watermark_before", "watermark_after", "sync_notes")
        }
        for key in ("error_messages", "skip_messages"):
            kept[key] = list(kept.get(key) or [])[:_MAX_NOTE_LINES]
        return kept

    def _magento_save_run_report(self, report_type, run, counts, finished, source_action=None, mode=None):
        """Create the run's sync report on its first page, update it on the next ones."""
        self.ensure_one()
        notes = None
        if not finished:
            notes = "Running: %s page(s) done so far; the next page is queued." % run["pages"]
        vals = self._prepare_sync_report_vals(
            report_type, counts, fields.Datetime.to_datetime(run["upper"]), fields.Datetime.now(),
            source_action=source_action, notes=notes, mode=mode,
        )
        reports = self.env["magento.sync.report"].sudo()
        report = reports.browse(run.get("report_id") or ()).exists()
        if report:
            report.write(vals)
        else:
            report = reports.create(vals)
        return report

    def _magento_enqueue_sync_page(self, entity, run, source_action=None, mode=None):
        """Queue the next page job of a run: same channel and priority as the current
        job. Its identity key ``sync-<id>-<entity>-p<page>-r<report>`` is unique to the
        run, so neither the first job's key nor another run can swallow it."""
        self.ensure_one()
        _method, label, _report_type, priority = _SYNC_JOB_SPECS[entity]
        channel = self._magento_sync_channel(entity)
        jobs = self.env["magento.job"]
        current = jobs.sudo().browse(self.env.context.get("magento_job_id") or ()).exists()
        if current and current.channel == channel:
            priority = current.priority
        kwargs = {"run": run}
        if source_action:
            kwargs["source_action"] = source_action
        if mode:
            kwargs["mode"] = mode
        return jobs.enqueue(
            self, "_magento_run_sync_job", args=(entity,), kwargs=kwargs,
            instance=self, channel=channel, priority=priority,
            max_attempts=current.max_attempts if current else 5,
            identity_key="%s-p%s-r%s" % (
                self._magento_sync_identity_key(entity), run["page"], run["report_id"]),
            description="Magento %s sync (%s), page %s" % (label.lower(), self.name, run["page"]),
        )

    def _magento_run_sync_page(self, entity, run=None, source_action=None, mode=None):
        """Import one page of a queued ``entity`` sync run; queue the next page if any.

        Without ``run`` a new run starts (window, watermark and report). The run ends
        on a short page, an empty page, a page that does not move the cursor, or a
        listing error (counted, as in the direct sync). Only then is the watermark
        saved, with the usual gap-free rule over the whole run. A page stops early
        after ``_SYNC_PAGE_TIME_BUDGET`` seconds; the next job resumes at the cursor.
        """
        self.ensure_one()
        kind, getter = _PAGED_SYNC_ENTITIES[entity]
        _method, label, report_type, _priority = _SYNC_JOB_SPECS[entity]
        run = dict(run) if run else self._magento_sync_run_start(kind)
        to_dt = fields.Datetime.to_datetime
        stats = _SyncStats(kind)
        stats.merge(run.get("stats"))
        if self._magento_skip_blocked_import(stats, kind):
            # Data Ownership: nothing fetched, watermark untouched, the run ends here.
            self._magento_save_run_report(
                report_type, run, stats.as_dict(), True, source_action=source_action, mode=mode,
            )
            return "%s sync skipped: %s" % (label, stats.skip_messages[-1])
        tracker = _WatermarkTracker.from_state(to_dt(run.get("before")), run.get("tracker"))
        start_cursor = self._magento_parse_cursor(run.get("cursor"))
        cursor = start_cursor
        api = MagentoAPI(self)
        page_size = self._get_sync_job_page_size(kind)
        filters = self._magento_cursor_filters(run)
        items = self._fetch_list(stats, kind, lambda: getattr(api, getter)(
            None, filters=filters, page_size=page_size, limit=page_size,
        ))
        import_item = self._magento_page_item_importer(kind, api, stats)
        deadline = time.monotonic() + _SYNC_PAGE_TIME_BUDGET
        out_of_time = False
        for item in items or ():
            if not isinstance(item, dict):
                continue
            key = self._magento_cursor_key(kind, item)
            if key and start_cursor and key <= start_cursor:
                continue  # processed by an earlier page of this run
            stats.records_seen += 1
            ok = import_item(item)
            if ok is not None:
                tracker.record(item.get("updated_at"), ok)
            if key and (cursor is None or key > cursor):
                cursor = key
            if time.monotonic() >= deadline:
                out_of_time = True
                break
        run["pages"] = int(run.get("pages") or 0) + 1
        finished = (
            items is None
            or cursor == start_cursor
            or (not out_of_time and len(items) < page_size)
        )
        if finished:
            self._incremental_finish(
                stats, kind, tracker, to_dt(run.get("updated_from")), {"pages": run["pages"]},
                stats.records_seen,
            )
            self._mark_synced()
        counts = stats.as_dict()
        report = self._magento_save_run_report(
            report_type, run, counts, finished, source_action=source_action, mode=mode,
        )
        summary = "%s sync: %s synced, %s skipped, %s errors" % (
            label,
            int(counts.get("created") or 0) + int(counts.get("updated") or 0),
            int(counts.get("skipped") or 0),
            int(counts.get("errors") or 0),
        )
        if finished:
            return summary
        run.update({
            "cursor": [fields.Datetime.to_string(cursor[0]), cursor[1]],
            "page": int(run.get("page") or 1) + 1,
            "stats": self._magento_run_counts(counts),
            "tracker": tracker.state(),
            "report_id": report.id,
        })
        job = self._magento_enqueue_sync_page(entity, run, source_action=source_action, mode=mode)
        return "%s so far; page %s done, next page in job %s" % (summary, run["page"] - 1, job.id)

    # =====================
    # DATA OWNERSHIP (roadmap 3.8)
    # =====================
    def _magento_ownership(self, entity):
        """``(direction, update mode)`` of ``entity`` (a key of ``OWNERSHIP_ENTITIES``)."""
        self.ensure_one()
        direction = self["ownership_%s_direction" % entity] or "two_way"
        update = self["ownership_%s_update" % entity] or "create_update"
        return direction, update

    def _magento_ownership_block_reason(self, entity, inbound, existing=None):
        """Why Data Ownership forbids this sync step, or None when it is allowed.

        ``inbound``: Magento -> Odoo (else Odoo -> Magento). ``existing``: the step
        would overwrite a record that already exists on the receiving side (None:
        check the direction only).
        """
        if not self:
            return None
        self.ensure_one()
        direction, update = self._magento_ownership(entity)
        label = dict(OWNERSHIP_ENTITIES).get(entity, entity).lower()
        verb = "is" if entity == "stock" else "are"
        if direction not in (_INBOUND_DIRECTIONS if inbound else _OUTBOUND_DIRECTIONS):
            return "Data Ownership: %s %s %s, %s is off" % (
                label, verb, _OWNERSHIP_DIRECTION_TEXT.get(direction, direction),
                "import from Magento" if inbound else "push to Magento",
            )
        if existing and update == "create_only":
            return "Data Ownership: %s %s create-only, the existing %s record is not updated" % (
                label, verb, "Odoo" if inbound else "Magento",
            )
        return None

    def _magento_import_block_reason(self, entity, existing=None):
        return self._magento_ownership_block_reason(entity, True, existing=existing)

    def _magento_export_block_reason(self, entity, existing=None):
        return self._magento_ownership_block_reason(entity, False, existing=existing)

    def _magento_skip_blocked_import(self, stats, kind):
        """Entity-level import guard of the list syncs: counts one skip with the reason
        and returns True when Data Ownership turns this import off."""
        self.ensure_one()
        reason = self._magento_import_block_reason(kind)
        if reason:
            stats.skip(kind, "(all)", reason, instance=self)
        return bool(reason)

    # =====================
    # SYNC BOOKKEEPING
    # =====================
    _REPORT_COUNT_FIELDS = {
        "category": ("categories_created", "categories_updated"),
        "product": ("products_created", "products_updated"),
        "order": ("orders_created", "orders_updated"),
        "customer": ("customers_created", "customers_updated"),
    }

    def _create_sync_report(self, sync_type, counts, start_time, end_time, *, source_action=None,
                            notes=None, per_type_counts=None, mode=None):
        """Write an honest ``magento.sync.report``: real error and skip counts.

        ``total_records`` = records seen; ``success`` = created + updated;
        ``errors`` = record failures plus failed sub-steps and fetch errors.
        """
        vals = self._prepare_sync_report_vals(
            sync_type, counts, start_time, end_time, source_action=source_action, notes=notes,
            per_type_counts=per_type_counts, mode=mode,
        )
        return self.env["magento.sync.report"].sudo().create(vals)

    def _prepare_sync_report_vals(self, sync_type, counts, start_time, end_time, *, source_action=None,
                                  notes=None, per_type_counts=None, mode=None):
        """Values of a ``magento.sync.report`` (see ``_create_sync_report``)."""
        self.ensure_one()
        counts = counts or {}
        created = int(counts.get("created") or 0)
        updated = int(counts.get("updated") or 0)
        errors = int(counts.get("errors") or 0)
        skipped = int(counts.get("skipped") or 0)
        is_cron = mode == "cron" if mode else self._is_magento_cron()
        vals = {
            "instance_id": self.id,
            "sync_type": sync_type,
            "mode": "cron" if is_cron else "manual",
            "source_action": source_action or ("cron" if is_cron else "manual"),
            "total_records": int(counts.get("processed") or (created + updated + errors + skipped)),
            "success": created + updated,
            "errors": errors,
            "start_time": start_time,
            "end_time": end_time,
            "inventory_updated": int(counts.get("inventory_updated") or 0),
        }
        for type_key, type_counts in (per_type_counts or {sync_type: counts}).items():
            field_names = self._REPORT_COUNT_FIELDS.get(type_key)
            if field_names:
                vals[field_names[0]] = int(type_counts.get("created") or 0)
                vals[field_names[1]] = int(type_counts.get("updated") or 0)
        report_model = self.env["magento.sync.report"].sudo()
        if "skipped" in report_model._fields:
            vals["skipped"] = skipped
        note_lines = [notes] if notes else []
        # Paging and watermark summary of incremental fetches (pages, records seen,
        # watermark before -> after).
        note_lines.extend(counts.get("sync_notes") or [])
        if skipped:
            note_lines.append("Skipped: %s" % skipped)
            note_lines.extend("  - %s" % msg for msg in (counts.get("skip_messages") or [])[:_MAX_NOTE_LINES])
        if errors:
            note_lines.append("Errors: %s" % errors)
            note_lines.extend("  - %s" % msg for msg in (counts.get("error_messages") or [])[:_MAX_NOTE_LINES])
        vals["notes"] = "\n".join(note_lines) or False
        return vals

    def _sync_notification(self, label, counts):
        counts = counts or {}
        total = int(counts.get("created") or 0) + int(counts.get("updated") or 0)
        errors = int(counts.get("errors") or 0)
        skipped = int(counts.get("skipped") or 0)
        message = f"{label} sync complete. Synced: {total}"
        if skipped:
            message += f", skipped: {skipped}"
        if errors:
            message += f", errors: {errors} (see Sync Reports)"
            first = (counts.get("error_messages") or [])[:1]
            if first:
                message += f". First error: {first[0]}"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": message,
                "type": "warning" if errors else "success",
                "sticky": bool(errors),
            },
        }

    def _mark_synced(self):
        """Stamp ``last_sync_at``; bookkeeping, so allowed for Magento users too."""
        self.sudo().write({"last_sync_at": fields.Datetime.now()})

    def _sync_record(self, stats, kind, magento_id, func, *, entity=None, magento_ref=None, payload=None,
                     record_box=None, notify=True):
        """Run ``func`` for one Magento record inside a savepoint.

        ``func`` returns ``"created"``, ``"updated"``, ``("skipped", reason)`` or None.
        A failure rolls back only this record, is logged at WARNING with the Magento
        id and reason, and is counted in ``stats`` (``stats.last_exception`` keeps it).

        Then, unless ``notify`` is False, the error-center hook
        ``_magento_on_record_result`` is told the result: ``entity`` (default: ``kind``
        with ``_`` for spaces), ``magento_ref`` (default: ``magento_id``), the Magento
        ``payload`` and the Odoo record (first element of ``record_box``, filled by
        ``func``).
        """
        stats.processed += 1
        stats.last_exception = None
        entity = entity or kind.replace(" ", "_")
        ref = magento_ref if magento_ref is not None else magento_id
        try:
            with self.env.cr.savepoint():
                outcome = func()
        except Exception as exc:  # noqa: BLE001
            stats.error(kind, magento_id, exc, instance=self)
            stats.last_exception = exc
            if notify:
                self._magento_notify_record_result(entity, ref, False, exc=exc, payload=payload)
            return False
        if isinstance(outcome, tuple) and outcome and outcome[0] == "skipped":
            stats.skip(kind, magento_id, outcome[1], instance=self)
        elif outcome in ("created", "updated"):
            stats.add(outcome)
        if notify:
            self._magento_notify_record_result(
                entity, ref, True, payload=payload, record=record_box[0] if record_box else None,
            )
        return True

    def _magento_on_record_result(self, entity, magento_ref, ok, exc=None, payload=None, record=None):
        """Hook called once per synced Magento record with its result. No-op here.

        ``entity`` is one of ``order``, ``product``, ``customer``, ``category``,
        ``attribute_set``; ``magento_ref`` is what ``_magento_resync_one`` accepts
        (entity id; SKU for products), as a string. ``ok`` is False when the record
        failed (``exc`` is the exception; for an order, also when one of its
        follow-up steps failed). ``payload`` is the Magento JSON, ``record`` the Odoo
        record when known. Override it (error center) to open/resolve issues.
        """
        return None

    def _magento_notify_record_result(self, entity, magento_ref, ok, exc=None, payload=None, record=None):
        """Call the hook in its own savepoint; a broken hook never breaks the sync."""
        ref = str(magento_ref) if magento_ref is not None else None
        try:
            with self.env.cr.savepoint():
                self._magento_on_record_result(entity, ref, ok, exc=exc, payload=payload, record=record)
        except Exception as hook_exc:  # noqa: BLE001
            _logger.warning(
                "Magento sync (instance %s): result hook failed for %s %s: %s",
                self.name, entity, ref, _short_reason(hook_exc),
            )

    def _fetch_list(self, stats, kind, func):
        """Fetch the list of Magento records; a failure is reported, not raised."""
        entity = kind.replace(" ", "_")
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001
            stats.error(kind, None, exc, instance=self, reason=self._format_magento_request_error(exc, self))
            self._magento_notify_record_result(entity, self._LIST_REF, False, exc=exc)
            return None
        self._magento_notify_record_result(entity, self._LIST_REF, True)
        return result

    def _iter_fetched(self, stats, kind, items):
        """Iterate a (possibly lazily paged) listing, counting records seen.

        A page that fails to load mid-listing is reported like a list-fetch error
        and ends the iteration; the records already yielded stay processed.
        """
        iterator = iter(items or ())
        while True:
            try:
                item = next(iterator)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001
                stats.error(kind, None, exc, instance=self, reason=self._format_magento_request_error(exc, self))
                self._magento_notify_record_result(kind.replace(" ", "_"), self._LIST_REF, False, exc=exc)
                return
            stats.records_seen += 1
            yield item

    # =====================
    # INCREMENTAL SYNC (watermarks)
    # =====================
    _WATERMARK_FIELDS = {
        "order": "orders_sync_watermark",
        "product": "products_sync_watermark",
        "customer": "customers_sync_watermark",
    }

    def _get_sync_page_size(self):
        self.ensure_one()
        return MagentoAPI.clamp_page_size(self.sync_page_size)

    def _incremental_start(self, entity):
        """``(tracker, updated_from)`` for an entity sync of this instance.

        ``updated_from`` is ``watermark - overlap`` (naive UTC), or None for a full
        sync: no watermark yet, or context ``magento_full_sync``. There is never a
        creation-date cut-off: the filter is on ``updated_at`` only.
        """
        self.ensure_one()
        before = self[self._WATERMARK_FIELDS[entity]] or None
        updated_from = None
        if before and not self.env.context.get("magento_full_sync"):
            overlap = max(0, int(self.incremental_overlap_minutes or 0))
            updated_from = before - timedelta(minutes=overlap)
        return _WatermarkTracker(before), updated_from

    def _incremental_save(self, entity, tracker):
        """Store the gap-free watermark target of ``tracker`` (see ``_WatermarkTracker``)."""
        self.ensure_one()
        target = tracker.target()
        field_name = self._WATERMARK_FIELDS[entity]
        if target and target != self[field_name]:
            # Bookkeeping, so allowed for Magento users (who may not write the instance).
            self.sudo().write({field_name: target})
        return target

    def _incremental_commit_batch(self, entity, tracker, processed):
        """Batch commit of a real cron run, checkpointing the watermark first so a
        long initial sync that is interrupted resumes instead of starting over."""
        if self._magento_can_commit():
            self._incremental_save(entity, tracker)
        self._magento_commit_batch(processed)

    def _incremental_finish(self, stats, entity, tracker, updated_from, fetch_stats, records_seen):
        """Save the watermark and add the paging/watermark line to the report notes."""
        self.ensure_one()
        after = self._incremental_save(entity, tracker)
        to_str = fields.Datetime.to_string
        stats.pages_fetched += int(fetch_stats.get("pages") or 0)
        stats.watermark_before = to_str(tracker.before) if tracker.before else None
        stats.watermark_after = to_str(after) if after else None
        mode = "updated since %s" % to_str(updated_from) if updated_from else "full"
        stats.sync_notes.append(
            "%s [%s]: %s fetch; pages fetched: %s; records seen: %s; watermark %s -> %s" % (
                entity.capitalize(), self.name, mode, int(fetch_stats.get("pages") or 0),
                records_seen,
                stats.watermark_before or "none", stats.watermark_after or "none",
            )
        )

    def action_reset_watermarks(self):
        """Clear the incremental-sync markers: the next sync re-reads everything."""
        self.write({field_name: False for field_name in self._WATERMARK_FIELDS.values()})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": "Sync markers reset. The next sync re-reads all orders, products and customers.",
                "type": "success",
                "sticky": False,
            },
        }

    def _flatten_categories(self, node, parent_id=None):
        items = []
        if not node:
            return items

        items.append({
            "id": node.get("id"),
            "name": node.get("name") or f"Category {node.get('id')}",
            "parent_id": parent_id,
            "children": node.get("children_data") or [],
        })

        for child in node.get("children_data") or []:
            items.extend(self._flatten_categories(child, node.get("id")))

        return items

    def _get_magento_payment_config(self, company):
        self.ensure_one()
        journal = self.payment_journal_id
        if journal and journal.company_id and journal.company_id != company:
            journal = False
        if not journal:
            journal = self.env["account.journal"].search([
                ("type", "in", ("bank", "cash", "credit")),
                ("company_id", "=", company.id),
            ], limit=1)
        method = self.payment_method_line_id
        if method and journal and method not in journal.inbound_payment_method_line_ids:
            method = False
        if not method and journal:
            method = journal.inbound_payment_method_line_ids[:1]
        return journal, method

    # =====================
    # ERRORS
    # =====================
    def _format_magento_request_error(self, exc, instance):
        base_url = (getattr(instance, "base_url", "") or "").strip()
        if isinstance(exc, UserError):
            return str(exc.args[0] if exc.args else exc)
        if isinstance(exc, requests.exceptions.HTTPError):
            if exc.response is not None:
                status = exc.response.status_code
                body = (exc.response.text or "").strip()
                if len(body) > 500:
                    body = body[:500] + "..."
                extra = ""
                if status in (401, 403):
                    extra = " Check the Magento access token permissions for REST API access."
                elif status == 404:
                    extra = (
                        " Verify base URL and REST endpoint availability "
                        "(e.g. /rest/V1/store/websites). Base URL must be Magento root "
                        "(do not include /rest or /rest/V1)."
                    )
                return f"Magento HTTP {status}: {body}.{extra}"
            return f"Magento HTTP error: {exc}"
        if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL)):
            return f"Invalid Magento base URL '{base_url}'. Use a full URL like https://magento.example.com"
        if isinstance(exc, requests.exceptions.SSLError):
            # The API client already explains the verify_ssl option.
            return f"Magento SSL error for '{base_url}': {exc}"
        if isinstance(exc, requests.exceptions.Timeout):
            return f"Magento request timed out for '{base_url}'. Check server availability."
        if isinstance(exc, requests.exceptions.ConnectionError):
            host = (urlparse(base_url).hostname or "").strip().lower()
            loopback_hint = ""
            if host in {"localhost", "127.0.0.1", "::1"}:
                loopback_hint = (
                    " The configured URL is loopback (localhost/127.0.0.1/::1). "
                    "On a deployed server/container this points to that server/container itself. "
                    "If Odoo runs in Docker/Podman and Magento runs on the host, keep localhost and map "
                    "host-gateway (example Docker Compose: extra_hosts: ['host.docker.internal:host-gateway']). "
                    "Otherwise use the Magento server domain or IP."
                )
            return (
                f"Magento connection failed for '{base_url}'. "
                f"Details: {exc}. Check host, port, protocol (http/https), and if Magento is running."
                f"{loopback_hint}"
            )
        return f"Magento API error: {exc}"

    def _raise_magento_request_user_error(self, exc, instance):
        raise UserError(self._format_magento_request_error(exc, instance)) from exc

    # =====================
    # SYNC METHODS
    # =====================
    def sync_categories(self):
        stats = _SyncStats("category")
        cat_model = self.env["magento.category"].with_context(skip_magento_sync=True)
        for instance in self:
            if instance._magento_skip_blocked_import(stats, "category"):
                continue
            api = MagentoAPI(instance)
            tree = instance._fetch_list(stats, "category", api.get_categories)
            if tree is None:
                continue
            for index, item in enumerate(instance._flatten_categories(tree), start=1):
                stats.records_seen += 1
                if item.get("id") is None:
                    continue
                box = []
                instance._sync_record(
                    stats, "category", item["id"],
                    lambda item=item, box=box: instance._sync_category_record(cat_model, item, box=box),
                    entity="category", payload=item, record_box=box,
                )
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_category_record(self, cat_model, item, box=None):
        self.ensure_one()
        box = box if box is not None else []
        cat = cat_model.search([
            ("instance_id", "=", self.id),
            ("magento_id", "=", str(item["id"])),
        ], limit=1)
        blocked = self._magento_import_block_reason("category", existing=bool(cat))
        if blocked:
            return ("skipped", blocked)
        parent = cat_model.browse()
        if item.get("parent_id"):
            parent = cat_model.search([
                ("instance_id", "=", self.id),
                ("magento_id", "=", str(item["parent_id"])),
            ], limit=1)
        vals = {
            "name": item.get("name"),
            "parent_id": parent.id or False,
        }
        mapped_vals = self.env["magento.field.mapping"].sudo().apply_inbound_mapping_to_vals(
            mapping_type="category",
            instance=self,
            source_payload=item,
        )
        if mapped_vals:
            vals.update(mapped_vals)
        if cat:
            cat.write(vals)
            box.append(cat)
            self._magento_after_category_import(cat)
            return "updated"
        vals.update({"instance_id": self.id, "magento_id": str(item["id"])})
        cat = cat_model.create(vals)
        box.append(cat)
        self._magento_after_category_import(cat)
        return "created"

    def sync_attribute_sets(self):
        stats = _SyncStats("attribute set")
        # active_test=False: an archived set must be found again, not duplicated.
        attr_model = self.env["magento.attribute.set"].with_context(
            skip_magento_sync=True, active_test=False
        )
        for instance in self:
            api = MagentoAPI(instance)
            sets = instance._fetch_list(stats, "attribute set", api.get_attribute_sets)
            if sets is None:
                continue
            for item in sets:
                set_id = item.get("attribute_set_id") or item.get("id")
                if set_id is None:
                    continue
                instance._sync_record(
                    stats, "attribute set", set_id,
                    lambda item=item, set_id=set_id: instance._sync_attribute_set_record(attr_model, item, set_id),
                    entity="attribute_set", payload=item,
                )
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_attribute_set_record(self, attr_model, item, set_id):
        self.ensure_one()
        name = item.get("attribute_set_name") or item.get("name") or f"Attribute Set {set_id}"
        attr_set = attr_model.search([
            ("instance_id", "=", self.id),
            ("magento_id", "=", str(set_id)),
        ], limit=1)
        vals = {
            "name": name,
            "instance_id": self.id,
            "magento_id": str(set_id),
            "active": True,
        }
        if attr_set:
            attr_set.write(vals)
            return "updated"
        attr_model.create([vals])
        return "created"

    def action_sync_categories(self):
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(["categories"])
        start_time = fields.Datetime.now()
        counts = self.sync_categories()
        self._create_sync_report("category", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Category", counts)

    def action_sync_attribute_sets(self):
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(["attribute_sets"])
        counts = self.sync_attribute_sets()
        return self._sync_notification("Attribute set", counts)

    @api.model
    def _supported_product_types(self):
        """Magento product types the importer handles (other types, e.g. gift cards, are
        skipped). Configurable, bundle and grouped are imported but not exported."""
        field = self.env["magento.product.map"]._fields["type_id"]
        return _IMPORTABLE_PRODUCT_TYPES & set(field.get_values(self.env))

    def sync_products(self):
        stats = _SyncStats("product")
        supported_types = self._supported_product_types()
        for instance in self:
            if instance._magento_skip_blocked_import(stats, "product"):
                continue
            api = MagentoAPI(instance)
            tracker, updated_from = instance._incremental_start("product")
            fetch_stats = {}
            seen_start = stats.records_seen
            products = instance._fetch_list(stats, "product", lambda: api.get_products(
                updated_from, page_size=instance._get_sync_page_size(), lazy=True, stats=fetch_stats,
            ))
            if products is None:
                continue
            for index, p in enumerate(instance._iter_fetched(stats, "product", products), start=1):
                sku = p.get("sku")
                if not sku:
                    continue
                ok = instance._sync_one_product(api, p, supported_types, stats)
                tracker.record(p.get("updated_at"), ok)
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._incremental_commit_batch("product", tracker, _SYNC_BATCH_SIZE)
            instance._incremental_finish(
                stats, "product", tracker, updated_from, fetch_stats, stats.records_seen - seen_start,
            )
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_one_product(self, api, p, supported_types, stats, product_data=None):
        """Import one product search row ``p`` (SKU is the resync reference)."""
        self.ensure_one()
        sku = p.get("sku")
        box = []
        return self._sync_record(
            stats, "product", p.get("id") or sku,
            lambda: self._sync_product_record(api, p, supported_types, stats, product_data=product_data, box=box),
            entity="product", magento_ref=sku, payload=p, record_box=box,
        )

    def _sync_product_record(self, api, p, supported_types, stats, product_data=None, box=None):
        self.ensure_one()
        box = box if box is not None else []
        sku = p.get("sku")
        type_id = p.get("type_id")
        if type_id and type_id not in supported_types:
            return ("skipped", f"unsupported product type '{type_id}' (sku {sku})")
        blocked = self._magento_import_block_reason("product")
        if blocked:
            return ("skipped", blocked)

        if product_data is None:
            product_data = api.get_product(sku) or {}
        type_id = product_data.get("type_id") or type_id
        if type_id and type_id not in supported_types:
            return ("skipped", f"unsupported product type '{type_id}' (sku {sku})")

        # active_test=False everywhere: a product disabled in Magento is archived in
        # Odoo and must be found again on the next sync, not duplicated.
        map_model = self.env["magento.product.map"].with_context(active_test=False)
        product_model = self.env["product.product"].with_context(active_test=False)

        magento_id = p.get("id") if p.get("id") is not None else product_data.get("id")
        mapping = map_model.browse()
        if magento_id is not None:
            mapping = map_model.search([
                ("instance_id", "=", self.id),
                ("magento_id", "=", str(magento_id)),
            ], limit=1)
        if not mapping:
            mapping = map_model.search([
                ("instance_id", "=", self.id),
                ("sku", "=", sku),
            ], limit=1)

        if type_id == "configurable":
            blocked = self._magento_import_block_reason("product", existing=bool(mapping))
            if blocked:
                return ("skipped", blocked)
            # One template, a variant per child SKU (magento_product_variant.py).
            # ``magento_import_new`` tells the stock/price ownership guards (create-only)
            # whether this import creates the product.
            return self.with_context(magento_import_new=not mapping)._magento_import_configurable(
                api, p, product_data, stats, box=box,
            )

        product = mapping.odoo_product_id
        if not product:
            product = product_model.search([("default_code", "=", sku)], limit=1)
        blocked = self._magento_import_block_reason("product", existing=bool(mapping or product))
        if blocked:
            return ("skipped", blocked)
        is_new = not (mapping or product)
        if not product:
            product = product_model.create({
                "name": p.get("name") or product_data.get("name") or sku,
                "default_code": sku,
            })

        if not mapping:
            mapping = map_model.search([
                ("instance_id", "=", self.id),
                ("odoo_product_id", "=", product.id),
            ], limit=1)

        if not mapping:
            mapping = map_model.create({
                "instance_id": self.id,
                "magento_id": str(magento_id) if magento_id is not None else False,
                "sku": sku,
                "name": p.get("name"),
                "odoo_product_id": product.id,
            })
            outcome = "created"
        else:
            outcome = "updated"

        values = mapping.with_context(magento_import_new=is_new)._apply_magento_data(product_data)
        values["odoo_product_id"] = product.id
        media_entries = product_data.get("media_gallery_entries") or []
        entry_id = media_entries[0].get("id") if media_entries else None
        if entry_id:
            try:
                media = api.get_product_media(sku, entry_id)
                content = (media or {}).get("content") or {}
                if content.get("base64_encoded_data"):
                    values["image_1920"] = content["base64_encoded_data"]
            except requests.exceptions.RequestException as exc:
                # Image is cosmetic: keep the product, log the reason.
                _logger.warning(
                    "Magento product %s (instance %s): image fetch failed: %s",
                    sku, self.name, _short_reason(exc),
                )
        mapping.with_context(skip_magento_sync=True).write(values)
        box.append(mapping)
        # Category, bundle/grouped components, pending links (magento_product_variant.py).
        self._magento_after_product_import(mapping, product_data)
        stock_item = (product_data.get("extension_attributes") or {}).get("stock_item")
        if stock_item is not None:
            stats.inventory_updated += 1
        return outcome

    def action_sync_products(self):
        """Sync Products button: queued (default) or synchronous, see
        ``_magento_sync_in_background``."""
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(["products"])
        start_time = fields.Datetime.now()
        counts = self.sync_products()
        self._create_sync_report("product", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Product", counts)

    def sync_orders(self):
        stats = _SyncStats("order")
        for instance in self:
            if instance._magento_skip_blocked_import(stats, "order"):
                continue
            api = MagentoAPI(instance)
            tracker, updated_from = instance._incremental_start("order")
            fetch_stats = {}
            seen_start = stats.records_seen
            orders = instance._fetch_list(stats, "order", lambda: api.get_orders(
                updated_from, page_size=instance._get_sync_page_size(), lazy=True, stats=fetch_stats,
            ))
            if orders is None:
                continue
            for index, data in enumerate(instance._iter_fetched(stats, "order", orders), start=1):
                magento_id = data.get("entity_id") or data.get("id")
                if not magento_id:
                    continue
                ok = instance._sync_one_order(api, data, magento_id, stats)
                tracker.record(data.get("updated_at"), ok)
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._incremental_commit_batch("order", tracker, _SYNC_BATCH_SIZE)
            instance._incremental_finish(
                stats, "order", tracker, updated_from, fetch_stats, stats.records_seen - seen_start,
            )
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_one_order(self, api, data, magento_id, stats):
        """Import one Magento order, then its follow-up steps; returns True if the
        order itself was imported.

        The result hook is called once, after the follow-ups: ``ok`` only when the
        order and every follow-up step succeeded. Follow-up failures do not hold
        the watermark back (the order is imported; the error center offers a
        single-record resync), only an order import failure does.
        """
        self.ensure_one()
        order_box = []
        ok = self._sync_record(
            stats, "order", magento_id,
            lambda: self._sync_order_record(api, data, magento_id, order_box),
            entity="order", payload=data, notify=False,
        )
        exc = None if ok else stats.last_exception
        if ok and order_box:
            failures = self._sync_order_followups(api, order_box[0], magento_id, stats)
            if failures:
                exc = failures[0][1]
        self._magento_notify_record_result(
            "order", magento_id, exc is None, exc=exc, payload=data,
            record=order_box[0] if order_box else None,
        )
        return ok

    def _sync_order_record(self, api, data, magento_id, order_box):
        self.ensure_one()
        order_model = self.env["sale.order"].with_context(skip_magento_sync=True)
        # Instance-scoped lookup (Agent B's helper), done with sudo so a salesman-restricted
        # user cannot create a duplicate of an order they may not see; the order is then
        # used in the caller's environment, so writing it still respects access rights.
        found = order_model.sudo()._magento_find_order(self, magento_id)
        order = order_model.browse(found.ids)
        blocked = self._magento_import_block_reason("order", existing=bool(order))
        if blocked:
            return ("skipped", blocked)

        values = order_model._magento_prepare_order_vals(data, self, order=order)
        if values.get("magento_invoice_state") == "not_invoiced":
            try:
                invoices = api.get_invoices(order_id=magento_id)
            except requests.exceptions.RequestException as exc:
                _logger.warning(
                    "Magento order %s (instance %s): invoice lookup failed: %s",
                    magento_id, self.name, _short_reason(exc),
                )
                invoices = []
            if invoices:
                total_invoiced, invoice_state = order_model._magento_compute_invoice_summary(
                    data, invoices
                )
                if invoice_state != "not_invoiced":
                    values["magento_total_invoiced"] = total_invoiced
                    values["magento_invoice_state"] = invoice_state

        if order:
            # If the order is still assigned to the user who created it (typical for cron/admin
            # sync), clear it so Sales users can see it under Sale's personal record rule.
            if order.user_id and order.create_uid and order.user_id.id == order.create_uid.id:
                values.setdefault("user_id", False)
            order.write(values)
            order._magento_sync_order_lines(data)
            outcome = "updated"
        else:
            # Sale's default record rule for salesmen is:
            #   user_id = current user OR user_id is False
            # Sync jobs typically run under cron/admin; keep Magento orders
            # unassigned so Sales users can still see them in the UI.
            values.setdefault("user_id", False)
            order = order_model.create(values)
            order._magento_sync_order_lines(data)
            outcome = "created"
        order_box.append(order)
        return outcome

    def _sync_order_followups(self, api, order, magento_id, stats):
        """Invoices, payments, credit memos, shipment import and shipment push of one order.

        Run after the order's own savepoint. The helpers raise on real failure; each step
        has its own savepoint, so a failure (e.g. Magento rejecting a shipment) is counted
        and logged but keeps the imported order and the other steps.
        Returns the failures as a list of ``(step label, exception)``.
        """
        self.ensure_one()
        failures = []
        steps = self._magento_order_followup_steps(api, order)
        for label, step in steps:
            try:
                with self.env.cr.savepoint():
                    step()
            except Exception as exc:  # noqa: BLE001
                stats.error("order", magento_id, exc, instance=self, step=label)
                failures.append((label, exc))
        return failures

    def _magento_order_followup_steps(self, api, order):
        """``[(label, callable)]`` run by ``_sync_order_followups`` (order workflow hook)."""
        steps = []
        if order.magento_invoice_state in ("partial", "invoiced"):
            steps.append(("invoice sync", lambda: order._magento_sync_invoices(api)))
        if order.magento_payment_state in ("partial", "paid"):
            steps.append(("payment sync", lambda: order._magento_apply_payments_from_magento()))
        steps.append(("credit memo sync", lambda: order._magento_sync_credit_memos(api)))
        steps.append(("shipment sync", lambda: order._magento_sync_shipments(api)))
        steps.append(("shipment push", lambda: order._magento_push_shipments()))
        return steps

    def action_sync_orders(self):
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(["orders"])
        start_time = fields.Datetime.now()
        counts = self.sync_orders()
        self._create_sync_report("order", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Order", counts)

    def sync_customers(self):
        stats = _SyncStats("customer")
        customer_model = self.env["magento.customer"].with_context(
            skip_magento_sync=True, active_test=False
        )
        for instance in self:
            if instance._magento_skip_blocked_import(stats, "customer"):
                continue
            api = MagentoAPI(instance)
            tracker, updated_from = instance._incremental_start("customer")
            fetch_stats = {}
            seen_start = stats.records_seen
            customers = instance._fetch_list(stats, "customer", lambda: api.get_customers(
                updated_from, page_size=instance._get_sync_page_size(), lazy=True, stats=fetch_stats,
            ))
            if customers is None:
                continue
            for index, data in enumerate(instance._iter_fetched(stats, "customer", customers), start=1):
                ok = instance._sync_one_customer(customer_model, data, stats)
                tracker.record(data.get("updated_at"), ok)
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._incremental_commit_batch("customer", tracker, _SYNC_BATCH_SIZE)
            instance._incremental_finish(
                stats, "customer", tracker, updated_from, fetch_stats, stats.records_seen - seen_start,
            )
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_one_customer(self, customer_model, data, stats):
        self.ensure_one()
        box = []
        return self._sync_record(
            stats, "customer", data.get("id") or data.get("email"),
            lambda: self._sync_customer_record(customer_model, data, box=box),
            entity="customer", payload=data, record_box=box,
        )

    def _sync_customer_record(self, customer_model, data, box=None):
        self.ensure_one()
        box = box if box is not None else []
        magento_id = str(data.get("id") or "")
        email = data.get("email") or ""
        customer = customer_model.browse()
        if magento_id:
            customer = customer_model.search([
                ("instance_id", "=", self.id),
                ("magento_id", "=", magento_id),
            ], limit=1)
        if not customer and email:
            customer = customer_model.search([
                ("instance_id", "=", self.id),
                ("email", "=", email),
            ], limit=1)
        blocked = self._magento_import_block_reason("customer", existing=bool(customer))
        if blocked:
            return ("skipped", blocked)
        values = customer_model._extract_magento_values(data, self.id)
        outcome = "updated"
        if customer:
            customer.write(values)
        else:
            # List form: works whether or not magento.customer.create is model_create_multi.
            customer = customer_model.create([values])
            outcome = "created"
        # Same contact as the order import: res.partner + address contacts + group/VAT.
        customer._magento_sync_partner(data)
        box.append(customer)
        return outcome

    # =====================
    # SINGLE-RECORD RESYNC (error center contract)
    # =====================
    _RESYNC_ENTITIES = ("order", "product", "customer", "category")

    # Error-center reference for a failure to fetch a whole listing.
    _LIST_REF = "list"
    _LIST_RESYNC_METHODS = {
        "order": "sync_orders",
        "product": "sync_products",
        "customer": "sync_customers",
        "category": "sync_categories",
        "attribute_set": "sync_attribute_sets",
    }

    def _magento_resync_one(self, entity, magento_ref):
        """Fetch one record from Magento and import it through the normal per-record path.

        ``entity`` is ``order``, ``product``, ``customer`` or ``category``;
        ``magento_ref`` is the Magento entity id (the SKU for products). Returns
        the same counts dict as the ``sync_*`` methods. Raises when the fetch or the
        record import fails (the original exception, after the result hook and
        the stats saw it) so a job queue can classify it. For an order, a failed
        follow-up step (invoice, shipment...) is counted in ``errors`` and reported
        to the hook but not raised: the order itself is imported and kept.
        Watermarks are not touched.
        """
        self.ensure_one()
        if str(magento_ref or "") == self._LIST_REF and entity in self._LIST_RESYNC_METHODS:
            # A failed listing (auth, outage) is retried by running that sync again.
            paged = {"product": "products", "order": "orders", "customer": "customers"}.get(entity)
            if paged and self.env.context.get("magento_job_id"):
                # Inside a queued job a whole listing would be one unbounded job: start
                # (or join) a paged run instead; its first page resolves the issue.
                (_entity, job, created), = self._magento_enqueue_sync([paged])
                return "%s sync %s (job %s)" % (
                    entity.capitalize(), "queued" if created else "already queued", job.id)
            return getattr(self, self._LIST_RESYNC_METHODS[entity])()
        if entity not in self._RESYNC_ENTITIES:
            raise UserError("Cannot resync Magento entity '%s'." % entity)
        ref = str(magento_ref or "").strip()
        if not ref:
            raise UserError("A Magento reference is required to resync a %s." % entity)
        stats = _SyncStats(entity)
        api = MagentoAPI(self)
        fetchers = {
            "order": api.get_order,
            "product": api.get_product,
            "customer": api.get_customer,
            "category": api.get_category,
        }
        try:
            data = fetchers[entity](ref)
            if not isinstance(data, dict) or not data:
                raise UserError("Magento returned no %s for reference %s." % (entity, ref))
        except Exception as exc:
            stats.error(entity, ref, exc, instance=self, reason=self._format_magento_request_error(exc, self))
            self._magento_notify_record_result(entity, ref, False, exc=exc)
            raise
        stats.records_seen += 1

        if entity == "order":
            ok = self._sync_one_order(api, data, data.get("entity_id") or ref, stats)
        elif entity == "product":
            data.setdefault("sku", ref)
            ok = self._sync_one_product(api, data, self._supported_product_types(), stats, product_data=data)
        elif entity == "customer":
            customer_model = self.env["magento.customer"].with_context(
                skip_magento_sync=True, active_test=False
            )
            ok = self._sync_one_customer(customer_model, data, stats)
        else:
            cat_model = self.env["magento.category"].with_context(skip_magento_sync=True)
            item = {
                "id": data.get("id") or ref,
                "name": data.get("name") or "Category %s" % ref,
                "parent_id": data.get("parent_id") or None,
            }
            box = []
            ok = self._sync_record(
                stats, "category", item["id"],
                lambda: self._sync_category_record(cat_model, item, box=box),
                entity="category", payload=data, record_box=box,
            )
        if not ok:
            raise stats.last_exception or UserError(
                "Magento %s %s could not be imported." % (entity, ref)
            )
        return stats.as_dict()

    def action_sync_customers(self):
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(["customers"])
        start_time = fields.Datetime.now()
        counts = self.sync_customers()
        self._create_sync_report("customer", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Customer", counts)

    def sync_all_and_report(self):
        """Sync All button: one queued job per entity (default), or one synchronous run."""
        self.ensure_one()
        if self._magento_sync_in_background():
            return self._magento_queue_notification(list(_SYNC_ALL_ENTITIES))
        start_time = fields.Datetime.now()
        per_type = {}
        for sync_type, method_name in (
            ("category", "sync_categories"),
            ("product", "sync_products"),
            ("order", "sync_orders"),
            ("customer", "sync_customers"),
        ):
            per_type[sync_type] = self._run_isolated_step(sync_type, getattr(self, method_name)) or {}
        total = _SyncStats("all")
        for counts in per_type.values():
            total.merge(counts)
        totals = total.as_dict()
        self._create_sync_report(
            "all", totals, start_time, fields.Datetime.now(), per_type_counts=per_type,
        )
        return self._sync_notification("Full", totals)

    # =====================
    # TEST CONNECTION
    # =====================
    def action_test_connection(self):
        self.ensure_one()
        base_url = self._normalize_base_url(self.base_url)
        # The token is manager-only; read it (and only it) with sudo.
        if not (self.sudo().access_token or "").strip():
            raise UserError("Magento access token is missing on this instance.")
        if base_url != (self.base_url or ""):
            self.write({"base_url": base_url})

        try:
            MagentoAPI(self).get_websites()  # light endpoint
        except Exception as exc:  # noqa: BLE001
            message = self._format_magento_request_error(exc, self)
            self.sudo().write({"state": "error", "last_connection_message": message[:1000]})
            _logger.warning("Magento connection test failed for instance %s: %s", self.name, _short_reason(exc))
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Magento Connection",
                    "message": message,
                    "type": "danger",
                    "sticky": True,
                    "next": {"type": "ir.actions.client", "tag": "soft_reload"},
                },
            }

        message = "Connection successful (%s)." % fields.Datetime.to_string(fields.Datetime.now())
        self.sudo().write({"state": "connected", "last_connection_message": message})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Connection",
                "message": "Connection successful.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }
