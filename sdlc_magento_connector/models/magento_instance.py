import calendar
import logging
from datetime import datetime, timedelta

import requests
from requests.compat import urlparse, urlunparse

from odoo import api, models, fields
from odoo.exceptions import ValidationError, UserError
from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)


class MagentoInstance(models.Model):
    _name = "magento.instance"
    _description = "Magento Instance"

    _AUTO_SYNC_TRIGGER_FIELDS = {
        "auto_sync_enabled",
        "auto_sync_frequency",
        "auto_sync_hour",
        "auto_sync_minute",
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
    access_token = fields.Char(required=True)
    active = fields.Boolean(default=True)
    verify_ssl = fields.Boolean(default=True)
    website_id = fields.Integer(string="Magento Website ID")
    store_id = fields.Integer(string="Magento Store ID")
    store_code = fields.Char(string="Magento Store Code")
    customer_default_password = fields.Char(string="Magento User Password")
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

    @api.model
    def _build_monthly_datetime(self, year, month, day, hour):
        last_day = calendar.monthrange(year, month)[1]
        safe_day = min(day, last_day)
        return datetime(year, month, safe_day, hour, 0, 0)

    def _compute_auto_sync_nextcall(self):
        self.ensure_one()
        now_dt = fields.Datetime.to_datetime(fields.Datetime.now())
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
            return next_dt

        if frequency == "daily":
            next_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_dt <= now_dt:
                next_dt += timedelta(days=1)
            return next_dt

        if frequency == "weekly":
            target_weekday = self._normalized_auto_sync_weekday()
            next_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta_days = (target_weekday - next_dt.weekday()) % 7
            next_dt += timedelta(days=delta_days)
            if next_dt <= now_dt:
                next_dt += timedelta(days=7)
            return next_dt

        day = self._normalized_auto_sync_month_day()
        year = now_dt.year
        month = now_dt.month
        next_dt = self._build_monthly_datetime(year, month, day, hour)
        if next_dt <= now_dt:
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            next_dt = self._build_monthly_datetime(year, month, day, hour)
        return next_dt

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

    def cron_run_auto_sync(self):
        for instance in self:
            try:
                instance._sync_selected_auto_and_report()
            except Exception:
                _logger.exception("Magento auto sync failed for instance %s", instance.name)
        return True

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

    def _sync_selected_auto_and_report(self):
        self.ensure_one()
        scope = self._get_auto_sync_scope()
        if not any(scope.values()):
            now_time = fields.Datetime.now()
            self.env["magento.sync.report"].create({
                "instance_id": self.id,
                "sync_type": "all",
                "mode": "cron",
                "source_action": "cron_auto_scope",
                "total_records": 0,
                "success": 0,
                "errors": 1,
                "start_time": now_time,
                "end_time": now_time,
                "notes": "Auto sync is enabled but no scope is selected.",
            })
            return False

        sync_steps = [
            ("category", self.sync_categories, "categories_created", "categories_updated"),
            ("product", self.sync_products, "products_created", "products_updated"),
            ("order", self.sync_orders, "orders_created", "orders_updated"),
            ("customer", self.sync_customers, "customers_created", "customers_updated"),
        ]

        for sync_type, sync_method, created_key, updated_key in sync_steps:
            if not scope.get(sync_type):
                continue

            start_time = fields.Datetime.now()
            counts = {"created": 0, "updated": 0, "inventory_updated": 0}
            error_note = False

            try:
                result = sync_method()
                if isinstance(result, dict):
                    counts.update(result)
            except Exception as exc:
                if isinstance(exc, UserError):
                    _logger.warning("Auto sync %s failed for %s: %s", sync_type, self.name, exc)
                else:
                    _logger.exception("Auto sync %s failed for %s", sync_type, self.name)
                error_note = "%s sync failed: %s" % (sync_type.capitalize(), exc)

            end_time = fields.Datetime.now()
            created_count = int(counts.get("created") or 0)
            updated_count = int(counts.get("updated") or 0)
            total_records = created_count + updated_count

            report_vals = {
                "instance_id": self.id,
                "sync_type": sync_type,
                "mode": "cron",
                "source_action": "cron_auto_scope",
                "total_records": total_records,
                "success": 0 if error_note else total_records,
                "errors": 1 if error_note else 0,
                "start_time": start_time,
                "end_time": end_time,
                created_key: created_count,
                updated_key: updated_count,
                "notes": error_note or False,
            }
            if sync_type == "product":
                report_vals["inventory_updated"] = int(counts.get("inventory_updated") or 0)

            self.env["magento.sync.report"].create(report_vals)
        return True

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
    # SYNC METHODS
    # =====================
    def _raise_magento_request_user_error(self, exc, instance):
        base_url = (getattr(instance, "base_url", "") or "").strip()
        if isinstance(exc, requests.exceptions.HTTPError):
            if exc.response is not None:
                status = exc.response.status_code
                body = (exc.response.text or "").strip()
                if len(body) > 1200:
                    body = body[:1200] + "..."
                extra = ""
                if status in (401, 403):
                    extra = " Check the Magento access token permissions for REST API access."
                elif status == 404:
                    extra = (
                        " Verify base URL and REST endpoint availability "
                        "(e.g. /rest/V1/store/websites). Base URL must be Magento root "
                        "(do not include /rest or /rest/V1)."
                    )
                raise UserError(
                    f"Magento HTTP {status}: {body}.{extra}"
                ) from exc
            raise UserError(f"Magento HTTP error: {exc}") from exc
        if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema, requests.exceptions.InvalidURL)):
            raise UserError(
                f"Invalid Magento base URL '{base_url}'. Use a full URL like https://magento.example.com"
            ) from exc
        if isinstance(exc, requests.exceptions.SSLError):
            raise UserError(
                f"Magento SSL verification failed for '{base_url}': {exc}. "
                "For local/self-signed certs, disable 'Verify SSL' on the instance."
            ) from exc
        if isinstance(exc, requests.exceptions.Timeout):
            raise UserError(
                f"Magento request timed out for '{base_url}'. Check server availability."
            ) from exc
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
            raise UserError(
                f"Magento connection failed for '{base_url}'. "
                f"Details: {exc}. Check host, port, protocol (http/https), and if Magento is running."
                f"{loopback_hint}"
            ) from exc
        raise UserError(f"Magento API error: {exc}") from exc

    def sync_categories(self):
        total_created = 0
        total_updated = 0
        for instance in self:
            created = 0
            updated = 0
            api = MagentoAPI(instance)
            try:
                tree = api.get_categories()
            except requests.exceptions.RequestException as exc:
                self._raise_magento_request_user_error(exc, instance)

            flat = instance._flatten_categories(tree)
            cat_model = self.env["magento.category"]

            for item in flat:
                if item.get("id") is None:
                    continue

                cat = cat_model.search([
                    ("instance_id", "=", instance.id),
                    ("magento_id", "=", str(item["id"])),
                ], limit=1)

                parent = False
                if item.get("parent_id"):
                    parent = cat_model.search([
                        ("instance_id", "=", instance.id),
                        ("magento_id", "=", str(item["parent_id"])),
                    ], limit=1)

                vals = {
                    "name": item.get("name"),
                    "parent_id": parent.id if parent else False,
                }
                mapped_vals = self.env["magento.field.mapping"].sudo().apply_inbound_mapping_to_vals(
                    mapping_type="category",
                    instance=instance,
                    source_payload=item,
                )
                if mapped_vals:
                    vals.update(mapped_vals)

                if cat:
                    cat.with_context(skip_magento_sync=True).write(vals)
                    updated += 1
                else:
                    vals.update({
                        "instance_id": instance.id,
                        "magento_id": str(item["id"]),
                    })
                    cat_model.with_context(skip_magento_sync=True).create(vals)
                    created += 1
            total_created += created
            total_updated += updated
        return {
            "created": total_created,
            "updated": total_updated,
        }

    def sync_attribute_sets(self):
        total_created = 0
        total_updated = 0
        attr_model = self.env["magento.attribute.set"]
        for instance in self:
            created = 0
            updated = 0
            api = MagentoAPI(instance)
            try:
                sets = api.get_attribute_sets()
            except requests.exceptions.RequestException as exc:
                self._raise_magento_request_user_error(exc, instance)

            for item in sets:
                set_id = item.get("attribute_set_id") or item.get("id")
                if set_id is None:
                    continue
                name = (
                    item.get("attribute_set_name")
                    or item.get("name")
                    or f"Attribute Set {set_id}"
                )
                attr_set = attr_model.search([
                    ("instance_id", "=", instance.id),
                    ("magento_id", "=", str(set_id)),
                ], limit=1)
                vals = {
                    "name": name,
                    "instance_id": instance.id,
                    "magento_id": str(set_id),
                    "active": True,
                }
                if attr_set:
                    attr_set.with_context(skip_magento_sync=True).write(vals)
                    updated += 1
                else:
                    attr_model.with_context(skip_magento_sync=True).create(vals)
                    created += 1
            total_created += created
            total_updated += updated
        return {
            "created": total_created,
            "updated": total_updated,
        }

    def action_sync_categories(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_categories()
        end_time = fields.Datetime.now()
        total_records = counts.get("created", 0) + counts.get("updated", 0)
        self.env["magento.sync.report"].create({
            "instance_id": self.id,
            "sync_type": "category",
            "mode": "manual",
            "source_action": "manual",
            "total_records": total_records,
            "success": total_records,
            "errors": 0,
            "start_time": start_time,
            "end_time": end_time,
            "categories_created": counts.get("created", 0),
            "categories_updated": counts.get("updated", 0),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Category sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_attribute_sets(self):
        self.ensure_one()
        counts = self.sync_attribute_sets()
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

    def sync_products(self):
        total_created = 0
        total_updated = 0
        total_inventory = 0
        for instance in self:
            created = 0
            updated = 0
            inventory_updated = 0
            api = MagentoAPI(instance)
            try:
                products = api.get_products()
            except requests.exceptions.RequestException as exc:
                self._raise_magento_request_user_error(exc, instance)

            map_model = self.env["magento.product.map"]

            for p in products:
                sku = p.get("sku")
                if not sku:
                    continue

                try:
                    product_data = api.get_product(sku)
                except requests.exceptions.RequestException:
                    continue

                product = self.env["product.product"].search(
                    [("default_code", "=", sku)], limit=1
                )

                if not product:
                    product = self.env["product.product"].create({
                        "name": p.get("name") or sku,
                        "default_code": sku,
                    })

                mapping = False
                magento_id = p.get("id")
                if magento_id is not None:
                    mapping = map_model.search([
                        ("instance_id", "=", instance.id),
                        ("magento_id", "=", str(magento_id)),
                    ], limit=1)
                if not mapping:
                    mapping = map_model.search([
                        ("instance_id", "=", instance.id),
                        ("sku", "=", sku),
                    ], limit=1)
                if not mapping:
                    mapping = map_model.search([
                        ("instance_id", "=", instance.id),
                        ("odoo_product_id", "=", product.id),
                    ], limit=1)

                if not mapping:
                    mapping = map_model.create({
                        "instance_id": instance.id,
                        "magento_id": str(magento_id) if magento_id is not None else False,
                        "sku": sku,
                        "name": p.get("name"),
                        "odoo_product_id": product.id,
                    })
                    created += 1
                else:
                    updated += 1

                values = mapping._apply_magento_data(product_data)
                values["odoo_product_id"] = product.id
                media_entries = product_data.get("media_gallery_entries") or []
                if media_entries:
                    entry_id = media_entries[0].get("id")
                    if entry_id:
                        try:
                            media = api.get_product_media(sku, entry_id)
                            content = (media or {}).get("content") or {}
                            if content.get("base64_encoded_data"):
                                values["image_1920"] = content["base64_encoded_data"]
                        except requests.exceptions.RequestException:
                            pass
                mapping.with_context(skip_magento_sync=True).write(values)
                stock_item = (product_data.get("extension_attributes") or {}).get("stock_item")
                if stock_item is not None:
                    inventory_updated += 1
            total_created += created
            total_updated += updated
            total_inventory += inventory_updated
        return {
            "created": total_created,
            "updated": total_updated,
            "inventory_updated": total_inventory,
        }

    def action_sync_products(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_products()
        end_time = fields.Datetime.now()
        total_records = counts.get("created", 0) + counts.get("updated", 0)
        self.env["magento.sync.report"].create({
            "instance_id": self.id,
            "sync_type": "product",
            "mode": "manual",
            "source_action": "manual",
            "total_records": total_records,
            "success": total_records,
            "errors": 0,
            "start_time": start_time,
            "end_time": end_time,
            "products_created": counts.get("created", 0),
            "products_updated": counts.get("updated", 0),
            "inventory_updated": counts.get("inventory_updated", 0),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Product sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    def sync_orders(self):
        total_created = 0
        total_updated = 0
        for instance in self:
            created = 0
            updated = 0
            api = MagentoAPI(instance)
            try:
                orders = api.get_orders()
            except requests.exceptions.RequestException as exc:
                self._raise_magento_request_user_error(exc, instance)

            order_model = self.env["sale.order"].with_context(skip_magento_sync=True)

            for data in orders:
                magento_id = data.get("entity_id") or data.get("id")
                if not magento_id:
                    continue

                order = order_model.search([
                    ("magento_order_id", "=", str(magento_id)),
                ], limit=1)

                values = order_model._magento_prepare_order_vals(data, instance)
                if values.get("magento_invoice_state") == "not_invoiced":
                    try:
                        invoices = api.get_invoices(order_id=magento_id)
                    except requests.exceptions.RequestException as exc:
                        _logger.warning(
                            "Magento invoice lookup failed for order %s: %s",
                            magento_id,
                            exc,
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
                    if (
                        order.user_id
                        and order.create_uid
                        and order.user_id.id == order.create_uid.id
                    ):
                        values.setdefault("user_id", False)
                    order.write(values)
                    order._magento_sync_order_lines(data)
                    updated += 1
                else:
                    # Sale's default record rule for salesmen is:
                    #   user_id = current user OR user_id is False
                    # Sync jobs typically run under cron/admin; keep Magento orders
                    # unassigned so Sales users can still see them in the UI.
                    values.setdefault("user_id", False)
                    order = order_model.create(values)
                    order._magento_sync_order_lines(data)
                    created += 1
                if order.magento_invoice_state in ("partial", "invoiced"):
                    try:
                        order._magento_sync_invoices(api)
                    except Exception as exc:
                        _logger.warning(
                            "Magento invoice sync failed for order %s: %s",
                            order.name,
                            exc,
                        )
                if order.magento_payment_state in ("partial", "paid"):
                    try:
                        order._magento_apply_payments_from_magento()
                    except Exception as exc:
                        _logger.warning(
                            "Magento payment sync failed for order %s: %s",
                            order.name,
                            exc,
                        )
                try:
                    order._magento_sync_credit_memos(api)
                except Exception as exc:
                    _logger.warning(
                        "Magento credit memo sync failed for order %s: %s",
                        order.name,
                        exc,
                    )
                try:
                    order._magento_sync_shipments(api)
                except Exception as exc:
                    _logger.warning(
                        "Magento shipment sync failed for order %s: %s",
                        order.name,
                        exc,
                    )
                try:
                    order._magento_push_shipments()
                except Exception as exc:
                    _logger.warning(
                        "Magento shipment push failed for order %s: %s",
                        order.name,
                        exc,
                    )
            total_created += created
            total_updated += updated
        return {
            "created": total_created,
            "updated": total_updated,
        }

    def action_sync_orders(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_orders()
        end_time = fields.Datetime.now()
        total_records = counts.get("created", 0) + counts.get("updated", 0)
        self.env["magento.sync.report"].create({
            "instance_id": self.id,
            "sync_type": "order",
            "mode": "manual",
            "source_action": "manual",
            "total_records": total_records,
            "success": total_records,
            "errors": 0,
            "start_time": start_time,
            "end_time": end_time,
            "orders_created": counts.get("created", 0),
            "orders_updated": counts.get("updated", 0),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Order sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    def sync_customers(self):
        total_created = 0
        total_updated = 0
        customer_model = self.env["magento.customer"]
        for instance in self:
            created = 0
            updated = 0
            api = MagentoAPI(instance)
            try:
                customers = api.get_customers()
            except requests.exceptions.RequestException as exc:
                self._raise_magento_request_user_error(exc, instance)
            for data in customers:
                magento_id = str(data.get("id") or "")
                email = data.get("email") or ""
                customer = False
                if magento_id:
                    customer = customer_model.search([
                        ("instance_id", "=", instance.id),
                        ("magento_id", "=", magento_id),
                    ], limit=1)
                if not customer and email:
                    customer = customer_model.search([
                        ("instance_id", "=", instance.id),
                        ("email", "=", email),
                    ], limit=1)
                values = customer_model._extract_magento_values(data, instance.id)
                if customer:
                    customer.with_context(skip_magento_sync=True).write(values)
                    updated += 1
                else:
                    customer_model.with_context(skip_magento_sync=True).create(values)
                    created += 1
            total_created += created
            total_updated += updated
        return {
            "created": total_created,
            "updated": total_updated,
        }

    def action_sync_customers(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        counts = self.sync_customers()
        end_time = fields.Datetime.now()
        total_records = counts.get("created", 0) + counts.get("updated", 0)
        self.env["magento.sync.report"].create({
            "instance_id": self.id,
            "sync_type": "customer",
            "mode": "manual",
            "source_action": "manual",
            "total_records": total_records,
            "success": total_records,
            "errors": 0,
            "start_time": start_time,
            "end_time": end_time,
            "customers_created": counts.get("created", 0),
            "customers_updated": counts.get("updated", 0),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Customer sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    def sync_all_and_report(self):
        self.ensure_one()
        start_time = fields.Datetime.now()
        cat_counts = self.sync_categories()
        prod_counts = self.sync_products()
        order_counts = self.sync_orders()
        cust_counts = self.sync_customers()
        end_time = fields.Datetime.now()
        total_records = (
            cat_counts.get("created", 0)
            + cat_counts.get("updated", 0)
            + prod_counts.get("created", 0)
            + prod_counts.get("updated", 0)
            + order_counts.get("created", 0)
            + order_counts.get("updated", 0)
            + cust_counts.get("created", 0)
            + cust_counts.get("updated", 0)
        )
        report_vals = {
            "instance_id": self.id,
            "sync_type": "all",
            "mode": "cron" if self.env.context.get("magento_auto_sync_cron") else "manual",
            "source_action": "cron" if self.env.context.get("magento_auto_sync_cron") else "manual",
            "total_records": total_records,
            "success": total_records,
            "errors": 0,
            "start_time": start_time,
            "end_time": end_time,
            "categories_created": cat_counts.get("created", 0),
            "categories_updated": cat_counts.get("updated", 0),
            "products_created": prod_counts.get("created", 0),
            "products_updated": prod_counts.get("updated", 0),
            "orders_created": order_counts.get("created", 0),
            "orders_updated": order_counts.get("updated", 0),
            "customers_created": cust_counts.get("created", 0),
            "customers_updated": cust_counts.get("updated", 0),
            "inventory_updated": prod_counts.get("inventory_updated", 0),
        }
        self.env["magento.sync.report"].create(report_vals)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Sync",
                "message": f"Sync complete. Total: {total_records}",
                "type": "success",
                "sticky": False,
            },
        }

    # =====================
    # DASHBOARD DATA
    # =====================
    @api.model
    def get_dashboard_data(self, instance_id=False, days=8):
        try:
            days = int(days or 8)
        except (TypeError, ValueError):
            days = 8
        days = max(7, min(days, 90))

        instance_model = self.env["magento.instance"].sudo()
        instances = instance_model.search([("active", "=", True)], order="name asc")

        selected_instance_id = False
        if instance_id:
            try:
                selected_instance_id = int(instance_id)
            except (TypeError, ValueError):
                selected_instance_id = False
        if selected_instance_id and not instances.filtered(lambda r: r.id == selected_instance_id):
            selected_instance_id = False

        order_model = self.env["sale.order"].sudo()
        customer_model = self.env["magento.customer"].sudo()
        product_model = self.env["magento.product.map"].sudo()
        sync_model = self.env["magento.sync.report"].sudo()

        order_domain = [("magento_order_id", "!=", False)]
        customer_domain = []
        product_domain = []
        sync_domain = []
        if selected_instance_id:
            order_domain.append(("magento_instance_id", "=", selected_instance_id))
            customer_domain.append(("instance_id", "=", selected_instance_id))
            product_domain.append(("instance_id", "=", selected_instance_id))
            sync_domain.append(("instance_id", "=", selected_instance_id))

        today = fields.Date.context_today(self)
        start_date = today - timedelta(days=days - 1)
        prev_end_date = start_date - timedelta(days=1)
        prev_start_date = prev_end_date - timedelta(days=days - 1)

        def _dt(date_obj):
            return datetime(date_obj.year, date_obj.month, date_obj.day)

        current_start_dt = fields.Datetime.to_string(_dt(start_date))
        current_end_dt = fields.Datetime.to_string(_dt(today + timedelta(days=1)))
        prev_start_dt = fields.Datetime.to_string(_dt(prev_start_date))
        prev_end_dt = fields.Datetime.to_string(_dt(start_date))

        def _sum_amount(model, domain, field_name):
            data = model.read_group(domain, [f"{field_name}:sum"], [])
            if not data:
                return 0.0
            return float(data[0].get(f"{field_name}_sum") or 0.0)

        def _pct_change(current, previous):
            if previous:
                return round(((current - previous) / previous) * 100.0, 2)
            if current:
                return 100.0
            return 0.0

        total_sales = _sum_amount(order_model, order_domain, "amount_total")
        if not total_sales:
            total_sales = _sum_amount(order_model, order_domain, "magento_total_invoiced")
        if not total_sales:
            total_sales = _sum_amount(order_model, order_domain, "magento_total_paid")

        pending_domain = order_domain + [
            ("state", "!=", "cancel"),
            ("magento_status", "not in", ["closed", "canceled", "cancelled"]),
        ]
        pending_sales = _sum_amount(order_model, pending_domain, "amount_total")
        if not pending_sales:
            pending_sales = _sum_amount(order_model, pending_domain, "magento_total_invoiced")
        if not pending_sales:
            pending_sales = _sum_amount(order_model, pending_domain, "magento_total_paid")
        total_orders = order_model.search_count(order_domain)
        total_customers = customer_model.search_count(customer_domain)
        total_products = product_model.search_count(product_domain)

        current_order_domain = order_domain + [
            ("date_order", ">=", current_start_dt),
            ("date_order", "<", current_end_dt),
        ]
        prev_order_domain = order_domain + [
            ("date_order", ">=", prev_start_dt),
            ("date_order", "<", prev_end_dt),
        ]
        current_sales = _sum_amount(order_model, current_order_domain, "amount_total")
        prev_sales = _sum_amount(order_model, prev_order_domain, "amount_total")
        current_orders = order_model.search_count(current_order_domain)
        prev_orders = order_model.search_count(prev_order_domain)

        current_customer_domain = customer_domain + [
            ("create_date", ">=", current_start_dt),
            ("create_date", "<", current_end_dt),
        ]
        prev_customer_domain = customer_domain + [
            ("create_date", ">=", prev_start_dt),
            ("create_date", "<", prev_end_dt),
        ]
        current_customers = customer_model.search_count(current_customer_domain)
        prev_customers = customer_model.search_count(prev_customer_domain)

        current_product_domain = product_domain + [
            ("create_date", ">=", current_start_dt),
            ("create_date", "<", current_end_dt),
        ]
        prev_product_domain = product_domain + [
            ("create_date", ">=", prev_start_dt),
            ("create_date", "<", prev_end_dt),
        ]
        current_products = product_model.search_count(current_product_domain)
        prev_products = product_model.search_count(prev_product_domain)

        grouped = order_model.read_group(
            current_order_domain,
            ["amount_total:sum"],
            ["date_order:day"],
            lazy=False,
            orderby="date_order:day asc",
        )

        def _parse_group_day(row):
            range_data = row.get("__range") or {}
            if isinstance(range_data, dict):
                for range_key in ("date_order:day", "date_order"):
                    date_range = range_data.get(range_key) or {}
                    date_from = date_range.get("from") if isinstance(date_range, dict) else False
                    if not date_from:
                        continue
                    try:
                        dt_value = fields.Datetime.to_datetime(date_from)
                        if dt_value:
                            return dt_value.date()
                    except Exception:
                        pass
                    try:
                        day_value = fields.Date.to_date(str(date_from)[:10])
                        if day_value:
                            return day_value
                    except Exception:
                        pass

            grouped_key = row.get("date_order:day") or row.get("date_order")
            if not grouped_key:
                return False
            if isinstance(grouped_key, datetime):
                return grouped_key.date()
            if hasattr(grouped_key, "year") and hasattr(grouped_key, "month") and hasattr(grouped_key, "day"):
                return grouped_key

            grouped_value = str(grouped_key).strip()
            if not grouped_value:
                return False

            for parser in (fields.Datetime.to_datetime, fields.Date.to_date):
                try:
                    parsed = parser(grouped_value)
                except Exception:
                    parsed = False
                if not parsed:
                    continue
                if isinstance(parsed, datetime):
                    return parsed.date()
                return parsed

            try:
                parsed = fields.Date.to_date(grouped_value[:10])
                if parsed:
                    return parsed
            except Exception:
                pass

            for date_format in ("%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
                try:
                    return datetime.strptime(grouped_value, date_format).date()
                except Exception:
                    continue

            _logger.warning("Unable to parse grouped date key for Magento dashboard: %s", grouped_value)
            return False

        grouped_map = {}
        for row in grouped:
            day = _parse_group_day(row)
            if not day:
                continue
            grouped_map[day] = {
                "sales": float(row.get("amount_total_sum") or 0.0),
                "orders": int(row.get("__count") or 0),
            }

        sales_series = []
        current = start_date
        while current <= today:
            day_data = grouped_map.get(current, {"sales": 0.0, "orders": 0})
            sales_series.append(
                {
                    "date": fields.Date.to_string(current),
                    "sales": round(day_data["sales"], 2),
                    "orders": day_data["orders"],
                }
            )
            current += timedelta(days=1)

        status_groups = order_model.read_group(
            order_domain,
            [],
            ["magento_status"],
            lazy=False,
        )
        status_rows = []
        status_total = 0
        for row in status_groups:
            count = int(row.get("__count") or 0)
            if not count:
                continue
            status_total += count
            status_rows.append(
                {
                    "label": row.get("magento_status") or "unknown",
                    "count": count,
                }
            )
        status_rows.sort(key=lambda item: item["count"], reverse=True)
        status_rows = status_rows[:6]
        for item in status_rows:
            item["share"] = round((item["count"] * 100.0 / status_total), 2) if status_total else 0.0

        category_sql = """
            SELECT c.name, COUNT(rel.product_id) AS product_count
              FROM magento_category c
              JOIN magento_product_category_rel rel ON rel.category_id = c.id
              JOIN magento_product_map p ON p.id = rel.product_id
             {where_clause}
             GROUP BY c.id, c.name
             ORDER BY product_count DESC, c.name ASC
             LIMIT 5
        """
        where_clause = ""
        params = []
        if selected_instance_id:
            where_clause = "WHERE p.instance_id = %s"
            params.append(selected_instance_id)
        self.env.cr.execute(category_sql.format(where_clause=where_clause), params)
        top_categories = []
        for name, count in self.env.cr.fetchall():
            top_categories.append(
                {
                    "name": name or "Uncategorized",
                    "value": int(count or 0),
                }
            )
        categories_total = sum(item["value"] for item in top_categories)
        for item in top_categories:
            item["share"] = round((item["value"] * 100.0 / categories_total), 2) if categories_total else 0.0

        country_groups = customer_model.read_group(
            customer_domain + [("country_id", "!=", False)],
            ["country_id"],
            ["country_id"],
            lazy=False,
        )
        country_rows = []
        for row in country_groups:
            count = int(row.get("country_id_count") or 0)
            if not count:
                continue
            country = row.get("country_id")
            country_rows.append(
                {
                    "name": country[1] if country else "Unknown",
                    "count": count,
                    "share": round((count * 100.0 / total_customers), 2) if total_customers else 0.0,
                }
            )
        country_rows.sort(key=lambda item: item["count"], reverse=True)
        country_rows = country_rows[:6]

        delivered_orders = order_model.search_count(
            order_domain + [("delivery_status", "=", "full")]
        )
        not_delivered_orders = order_model.search_count(
            order_domain + [("delivery_status", "!=", "full")]
        )
        avg_order_value = round(total_sales / total_orders, 2) if total_orders else 0.0
        delivered_rate = round((delivered_orders * 100.0 / total_orders), 2) if total_orders else 0.0

        month_start = today.replace(day=1)
        prev_month_end = month_start - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        month_start_dt = fields.Datetime.to_string(_dt(month_start))
        month_end_dt = fields.Datetime.to_string(_dt(today + timedelta(days=1)))
        prev_month_start_dt = fields.Datetime.to_string(_dt(prev_month_start))
        prev_month_end_dt = fields.Datetime.to_string(_dt(month_start))

        month_sales = _sum_amount(
            order_model,
            order_domain + [("date_order", ">=", month_start_dt), ("date_order", "<", month_end_dt)],
            "amount_total",
        )
        prev_month_sales = _sum_amount(
            order_model,
            order_domain + [("date_order", ">=", prev_month_start_dt), ("date_order", "<", prev_month_end_dt)],
            "amount_total",
        )
        if prev_month_sales > 0:
            target_sales = prev_month_sales
        elif month_sales > 0:
            target_sales = month_sales * 1.15
        else:
            target_sales = 1.0
        target_progress = round(min(100.0, (month_sales * 100.0 / target_sales)), 2) if target_sales else 0.0

        manual_syncs = sync_model.search_count(sync_domain + [("mode", "=", "manual")])
        cron_syncs = sync_model.search_count(sync_domain + [("mode", "=", "cron")])
        sync_errors = _sum_amount(sync_model, sync_domain, "errors")
        sync_success = _sum_amount(sync_model, sync_domain, "success")
        last_sync = sync_model.search(sync_domain, limit=1, order="synced_at desc, id desc")

        currency = self.env.company.currency_id.name or "USD"
        return {
            "instances": [{"id": rec.id, "name": rec.name} for rec in instances],
            "selected_instance_id": selected_instance_id or False,
            "period_days": days,
            "generated_at": fields.Datetime.to_string(fields.Datetime.now()),
            "currency": currency,
            "kpis": {
                "total_sales": round(total_sales, 2),
                "pending_amount": round(pending_sales, 2),
                "total_orders": total_orders,
                "total_customers": total_customers,
                "total_products": total_products,
                "delivered_orders": delivered_orders,
                "not_delivered_orders": not_delivered_orders,
                "avg_order_value": avg_order_value,
                "delivered_rate": delivered_rate,
                "sales_change": _pct_change(current_sales, prev_sales),
                "orders_change": _pct_change(current_orders, prev_orders),
                "customers_change": _pct_change(current_customers, prev_customers),
                "products_change": _pct_change(current_products, prev_products),
            },
            "sales_series": sales_series,
            "monthly_target": {
                "current": round(month_sales, 2),
                "target": round(target_sales, 2),
                "progress": target_progress,
                "delta": _pct_change(month_sales, prev_month_sales),
            },
            "top_categories": top_categories,
            "top_countries": country_rows,
            "order_status": status_rows,
            "sync_health": {
                "manual_runs": manual_syncs,
                "cron_runs": cron_syncs,
                "success_records": int(sync_success),
                "error_records": int(sync_errors),
                "last_sync_at": fields.Datetime.to_string(last_sync.synced_at) if last_sync else False,
            },
        }

    # =====================
    # TEST CONNECTION
    # =====================
    def action_test_connection(self):
        self.ensure_one()
        base_url = self._normalize_base_url(self.base_url)
        access_token = (self.access_token or "").strip()
        if not access_token:
            raise UserError("Magento access token is missing on this instance.")

        updates = {}
        if base_url != (self.base_url or ""):
            updates["base_url"] = base_url
        if access_token != (self.access_token or ""):
            updates["access_token"] = access_token
        if updates:
            self.write(updates)

        api = MagentoAPI(self)

        try:
            api.get_websites()  # light endpoint
        except requests.exceptions.RequestException as exc:
            self._raise_magento_request_user_error(exc, self)
        except Exception as exc:
            raise UserError(f"Connection failed: {exc}") from exc

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Magento Connection",
                "message": "Connection successful.",
                "type": "success",
                "sticky": False,
            },
        }
