import calendar
import logging
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


_logger = logging.getLogger(__name__)

# Records processed between two commits when a sync runs from cron.
_SYNC_BATCH_SIZE = 50
# Magento product types the product import supports.
_IMPORTABLE_PRODUCT_TYPES = frozenset({"simple", "virtual", "downloadable"})
# Error/skip lines kept in a sync report's notes.
_MAX_NOTE_LINES = 50


def _short_reason(exc, limit=300):
    """One-line failure reason for logs/reports; never dumps payloads."""
    if isinstance(exc, UserError):
        text = str(exc.args[0] if exc.args else exc)
    else:
        text = "%s: %s" % (type(exc).__name__, exc)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


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
        self.error_messages = []
        self.skip_messages = []

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
        for key in ("created", "updated", "errors", "skipped", "processed", "inventory_updated"):
            setattr(self, key, getattr(self, key) + int((counts or {}).get(key) or 0))
        self.error_messages.extend((counts or {}).get("error_messages") or [])
        self.skip_messages.extend((counts or {}).get("skip_messages") or [])

    def as_dict(self):
        return {
            "created": self.created,
            "updated": self.updated,
            "errors": self.errors,
            "skipped": self.skipped,
            "processed": self.processed,
            "inventory_updated": self.inventory_updated,
            "error_messages": list(self.error_messages),
            "skip_messages": list(self.skip_messages),
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

    def _action_open_related(self, model, field_name, name):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": "list,form",
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
            and self.env.context.get("ir_cron_progress_id")
            and self.env.context.get("cron_id")
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    def _magento_commit_batch(self, processed):
        """Commit progress during real cron runs only. Interactive runs never commit."""
        if self._magento_can_commit():
            self.env["ir.cron"]._commit_progress(processed)

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
        for instance in self.with_context(magento_cron=True):
            if not instance.active:
                continue
            try:
                with instance._magento_isolation():
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
            now_time = fields.Datetime.now()
            self._create_sync_report(
                "all", {"errors": 1}, now_time, now_time, source_action=source_action,
                notes="Auto sync is enabled but no scope is selected.",
            )
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
                            notes=None, per_type_counts=None):
        """Write an honest ``magento.sync.report``: real error and skip counts.

        ``total_records`` = records seen; ``success`` = created + updated;
        ``errors`` = record failures plus failed sub-steps and fetch errors.
        """
        self.ensure_one()
        counts = counts or {}
        created = int(counts.get("created") or 0)
        updated = int(counts.get("updated") or 0)
        errors = int(counts.get("errors") or 0)
        skipped = int(counts.get("skipped") or 0)
        is_cron = self._is_magento_cron()
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
        if skipped:
            note_lines.append("Skipped: %s" % skipped)
            note_lines.extend("  - %s" % msg for msg in (counts.get("skip_messages") or [])[:_MAX_NOTE_LINES])
        if errors:
            note_lines.append("Errors: %s" % errors)
            note_lines.extend("  - %s" % msg for msg in (counts.get("error_messages") or [])[:_MAX_NOTE_LINES])
        vals["notes"] = "\n".join(note_lines) or False
        return report_model.create(vals)

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

    def _sync_record(self, stats, kind, magento_id, func):
        """Run ``func`` for one Magento record inside a savepoint.

        ``func`` returns ``"created"``, ``"updated"``, ``("skipped", reason)`` or None.
        A failure rolls back only this record, is logged at WARNING with the Magento
        id and reason, and is counted in ``stats``.
        """
        stats.processed += 1
        try:
            with self.env.cr.savepoint():
                outcome = func()
        except Exception as exc:  # noqa: BLE001
            stats.error(kind, magento_id, exc, instance=self)
            return False
        if isinstance(outcome, tuple) and outcome and outcome[0] == "skipped":
            stats.skip(kind, magento_id, outcome[1], instance=self)
        elif outcome in ("created", "updated"):
            stats.add(outcome)
        return True

    def _fetch_list(self, stats, kind, func):
        """Fetch the list of Magento records; a failure is reported, not raised."""
        try:
            result = func()
        except Exception as exc:  # noqa: BLE001
            stats.error(kind, None, exc, instance=self, reason=self._format_magento_request_error(exc, self))
            return None
        return result

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
            api = MagentoAPI(instance)
            tree = instance._fetch_list(stats, "category", api.get_categories)
            if tree is None:
                continue
            for index, item in enumerate(instance._flatten_categories(tree), start=1):
                if item.get("id") is None:
                    continue
                instance._sync_record(
                    stats, "category", item["id"],
                    lambda item=item: instance._sync_category_record(cat_model, item),
                )
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_category_record(self, cat_model, item):
        self.ensure_one()
        cat = cat_model.search([
            ("instance_id", "=", self.id),
            ("magento_id", "=", str(item["id"])),
        ], limit=1)
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
            return "updated"
        vals.update({"instance_id": self.id, "magento_id": str(item["id"])})
        cat_model.create(vals)
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
        start_time = fields.Datetime.now()
        counts = self.sync_categories()
        self._create_sync_report("category", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Category", counts)

    def action_sync_attribute_sets(self):
        self.ensure_one()
        counts = self.sync_attribute_sets()
        return self._sync_notification("Attribute set", counts)

    @api.model
    def _supported_product_types(self):
        """Magento product types the importer handles. Configurable, bundle and grouped
        exist in the selection (for display/export) but are not imported yet (Phase 2)."""
        field = self.env["magento.product.map"]._fields["type_id"]
        return _IMPORTABLE_PRODUCT_TYPES & set(field.get_values(self.env))

    def sync_products(self):
        stats = _SyncStats("product")
        supported_types = self._supported_product_types()
        for instance in self:
            api = MagentoAPI(instance)
            products = instance._fetch_list(stats, "product", api.get_products)
            if products is None:
                continue
            for index, p in enumerate(products, start=1):
                sku = p.get("sku")
                if not sku:
                    continue
                instance._sync_record(
                    stats, "product", p.get("id") or sku,
                    lambda p=p: instance._sync_product_record(api, p, supported_types, stats),
                )
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_product_record(self, api, p, supported_types, stats):
        self.ensure_one()
        sku = p.get("sku")
        type_id = p.get("type_id")
        if type_id and type_id not in supported_types:
            return ("skipped", f"unsupported product type '{type_id}' (sku {sku})")

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

        product = mapping.odoo_product_id
        if not product:
            product = product_model.search([("default_code", "=", sku)], limit=1)
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

        values = mapping._apply_magento_data(product_data)
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
        stock_item = (product_data.get("extension_attributes") or {}).get("stock_item")
        if stock_item is not None:
            stats.inventory_updated += 1
        return outcome

    def action_sync_products(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_products()
        self._create_sync_report("product", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Product", counts)

    def sync_orders(self):
        stats = _SyncStats("order")
        for instance in self:
            api = MagentoAPI(instance)
            orders = instance._fetch_list(stats, "order", api.get_orders)
            if orders is None:
                continue
            for index, data in enumerate(orders, start=1):
                magento_id = data.get("entity_id") or data.get("id")
                if not magento_id:
                    continue
                order_box = []
                ok = instance._sync_record(
                    stats, "order", magento_id,
                    lambda data=data, magento_id=magento_id, box=order_box: instance._sync_order_record(
                        api, data, magento_id, box
                    ),
                )
                if ok and order_box:
                    instance._sync_order_followups(api, order_box[0], magento_id, stats)
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_order_record(self, api, data, magento_id, order_box):
        self.ensure_one()
        order_model = self.env["sale.order"].with_context(skip_magento_sync=True)
        # Instance-scoped lookup (Agent B's helper), done with sudo so a salesman-restricted
        # user cannot create a duplicate of an order they may not see; the order is then
        # used in the caller's environment, so writing it still respects access rights.
        found = order_model.sudo()._magento_find_order(self, magento_id)
        order = order_model.browse(found.ids)

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
        """
        self.ensure_one()
        steps = []
        if order.magento_invoice_state in ("partial", "invoiced"):
            steps.append(("invoice sync", lambda: order._magento_sync_invoices(api)))
        if order.magento_payment_state in ("partial", "paid"):
            steps.append(("payment sync", lambda: order._magento_apply_payments_from_magento()))
        steps.append(("credit memo sync", lambda: order._magento_sync_credit_memos(api)))
        steps.append(("shipment sync", lambda: order._magento_sync_shipments(api)))
        steps.append(("shipment push", lambda: order._magento_push_shipments()))
        for label, step in steps:
            try:
                with self.env.cr.savepoint():
                    step()
            except Exception as exc:  # noqa: BLE001
                stats.error("order", magento_id, exc, instance=self, step=label)

    def action_sync_orders(self):
        self.ensure_one()
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
            api = MagentoAPI(instance)
            customers = instance._fetch_list(stats, "customer", api.get_customers)
            if customers is None:
                continue
            for index, data in enumerate(customers, start=1):
                instance._sync_record(
                    stats, "customer", data.get("id") or data.get("email"),
                    lambda data=data: instance._sync_customer_record(customer_model, data),
                )
                if index % _SYNC_BATCH_SIZE == 0:
                    instance._magento_commit_batch(_SYNC_BATCH_SIZE)
            instance._mark_synced()
            instance._magento_commit_batch(0)
        return stats.as_dict()

    def _sync_customer_record(self, customer_model, data):
        self.ensure_one()
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
        values = customer_model._extract_magento_values(data, self.id)
        if customer:
            customer.write(values)
            return "updated"
        # List form: works whether or not magento.customer.create is model_create_multi.
        customer_model.create([values])
        return "created"

    def action_sync_customers(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_customers()
        self._create_sync_report("customer", counts, start_time, fields.Datetime.now())
        return self._sync_notification("Customer", counts)

    def sync_all_and_report(self):
        self.ensure_one()
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
