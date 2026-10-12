"""Connect a Magento store: the onboarding stepper (roadmap 2.10, UI U6).

Six re-runnable steps, each with its own status (pending / ok / warning / error):

1. Store URL      normalise whatever the user pasted, check the REST API answers
                  (``GET /rest/V1/store/storeConfigs`` without credentials: 401 or 200);
                  an untrusted certificate is a warning with the Verify SSL toggle.
2. Credentials    the four OAuth 1.0a integration values (Bearer under "advanced");
                  creates or updates the instance and reuses ``action_diagnose_access``.
3. Stores         ``_magento_discover_stores`` when installed (else the store views read
                  straight from Magento); the chosen view sets website/store id and code.
4. Mappings       payment methods, carriers and tax rates seen in the last 50 orders (tax
                  rates from ``/V1/taxRates/search`` when allowed) and the customer groups,
                  pre-filled from the existing mappings; accepted lines become mappings.
5. Catalog check  up to 500 Magento SKUs against Odoo: duplicate default codes, barcode
                  collisions, SKUs that would create duplicates, product types, no price.
                  Informational only, never blocks.
6. Go live        schedule, background syncs, stock push, alert users, order workflows,
                  the known-issues checklist, "Run first sync now" and "Open Console".

Errors are shown on the step as text; a step never raises a traceback at the user.
"""
import logging
import re
import warnings
from itertools import islice

import requests
from markupsafe import Markup, escape
from requests.compat import urlparse, urlunparse
from urllib3.exceptions import InsecureRequestWarning

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare

from ..models.magento_customer import _DEFAULT_GROUP_NAMES
from ..models.magento_dashboard import MagentoDashboard as _BaseDashboard
from ..services.magento_api import MagentoAPI

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
WIZARD_ACTION = "sdlc_magento_connector.action_magento_setup_wizard"

STEPS = [
    ("url", "Store URL"),
    ("credentials", "Credentials"),
    ("stores", "Stores"),
    ("mappings", "Mappings"),
    ("catalog", "Catalog check"),
    ("golive", "Go live"),
]
STEP_KEYS = [key for key, _label in STEPS]
STATUSES = [("pending", "Pending"), ("ok", "OK"), ("warning", "Warning"), ("error", "Error")]
DONE = ("ok", "warning")
_STATUS_BADGE = {
    "pending": ("text-bg-secondary", "Pending"),
    "ok": ("text-bg-success", "OK"),
    "warning": ("text-bg-warning", "Warning"),
    "error": ("text-bg-danger", "Error"),
}
_STATUS_ALERT = {
    "pending": "alert-info",
    "ok": "alert-success",
    "warning": "alert-warning",
    "error": "alert-danger",
}
MAGENTO_ORANGE = "#eb5202"

REACH_TIMEOUT = 15
ORDER_SAMPLE = 50
CATALOG_SAMPLE = 500
# Custom attributes that usually carry a barcode on a Magento product.
BARCODE_ATTRIBUTES = ("barcode", "ean", "ean13", "upc", "gtin", "gtin13")
PRICED_TYPES = ("simple", "virtual", "downloadable")
ORDER_FIELDS = (
    "items[entity_id,increment_id,customer_group_id,shipping_description,payment[method,"
    "additional_information],extension_attributes[applied_taxes,shipping_assignments[shipping[method]]]],"
    "total_count"
)
PRODUCT_FIELDS = "items[id,sku,name,type_id,price,status,custom_attributes],total_count"
REQUIRED_RESOURCES_HINT = (
    "In Magento Admin open System > Extensions > Integrations, edit the integration, open the API "
    "tab and tick the missing resources, then Save and Reauthorize."
)
# Path segments that are never part of the Magento root URL.
_CUT_SEGMENTS = {"index.php", "rest", "soap", "graphql"}
_ADMIN_SEGMENT = re.compile(r"^(admin|backend)(_\w+)?$", re.IGNORECASE)
_STORE_DISCOVERY_NOTE = (
    "Store discovery available after update: the website and store view records are not "
    "installed yet, so the store views below are read straight from Magento."
)


def normalize_store_url(value, env=None):
    """Magento root URL from whatever the user pasted.

    ``shop.com``, ``http://shop.com/admin``, ``https://shop.com/rest/V1/``,
    ``https://shop.com/index.php`` (and sub-folder installs such as
    ``https://shop.com/magento/index.php/admin/dashboard``) all give the root URL.
    No scheme means https. Raises ``UserError`` with a readable message (translated
    when ``env`` is given).
    """
    def tr(message, *args):
        return env._(message, *args) if env is not None else (message % args if args else message)

    url = (value or "").strip()
    if not url:
        raise UserError(tr("Enter the address of the Magento store, e.g. shop.example.com."))
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UserError(tr("The store address must start with http:// or https://."))
    netloc = (parsed.netloc or "").strip().lower()
    if not netloc or " " in netloc or not parsed.hostname:
        raise UserError(tr("'%s' is not a valid store address.", value.strip()))
    kept = []
    for segment in (parsed.path or "").split("/"):
        if not segment:
            continue
        if segment.lower() in _CUT_SEGMENTS or _ADMIN_SEGMENT.match(segment):
            break
        kept.append(segment)
    path = "/" + "/".join(kept) if kept else ""
    return urlunparse((scheme, netloc, path, "", "", ""))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _http_status(exc):
    response = getattr(exc, "response", None)
    return response.status_code if response is not None else 0


class MagentoSetupWizard(models.TransientModel):
    _name = "magento.setup.wizard"
    _description = "Connect a Magento Store"

    instance_id = fields.Many2one("magento.instance", string="Instance", ondelete="cascade", readonly=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    current_step = fields.Selection(STEPS, default="url", required=True)
    url_status = fields.Selection(STATUSES, default="pending", required=True)
    credentials_status = fields.Selection(STATUSES, default="pending", required=True)
    stores_status = fields.Selection(STATUSES, default="pending", required=True)
    mappings_status = fields.Selection(STATUSES, default="pending", required=True)
    catalog_status = fields.Selection(STATUSES, default="pending", required=True)
    golive_status = fields.Selection(STATUSES, default="pending", required=True)
    url_message = fields.Text(readonly=True)
    credentials_message = fields.Text(readonly=True)
    stores_message = fields.Text(readonly=True)
    mappings_message = fields.Text(readonly=True)
    catalog_message = fields.Text(readonly=True)
    golive_message = fields.Text(readonly=True)
    step_header = fields.Html(compute="_compute_step_header", sanitize=False)
    step_alert = fields.Html(compute="_compute_step_header", sanitize=False)
    progress = fields.Integer(compute="_compute_step_header")
    is_first_step = fields.Boolean(compute="_compute_step_header")
    is_last_step = fields.Boolean(compute="_compute_step_header")

    # ---- 1. Store URL ----
    name = fields.Char(string="Store Name", help="How this store is called in Odoo.")
    url_input = fields.Char(
        string="Store Address",
        help="The shop address: shop.example.com, the admin URL or a REST URL all work.",
    )
    base_url = fields.Char(string="Magento Root URL", readonly=True)
    verify_ssl = fields.Boolean(string="Verify SSL", default=True)
    ssl_problem = fields.Boolean(readonly=True)

    # ---- 2. Credentials ----
    show_advanced = fields.Boolean(string="Advanced options")
    auth_method = fields.Selection(
        [("oauth1", "OAuth 1.0a (recommended)"), ("bearer", "Bearer token (legacy)")],
        string="Authentication", default="oauth1", required=True,
    )
    consumer_key = fields.Char()
    consumer_secret = fields.Char()
    access_token = fields.Char()
    access_token_secret = fields.Char()
    diagnostics_html = fields.Html(related="instance_id.access_diagnostics", string="Access check")

    # ---- 3. Stores ----
    discovery_available = fields.Boolean(compute="_compute_discovery_available")
    store_line_ids = fields.One2many("magento.setup.wizard.store", "wizard_id", string="Store Views")
    store_choice_id = fields.Many2one("magento.setup.wizard.store", string="Store View to Sync")
    website_id = fields.Integer(string="Magento Website ID", readonly=True)
    store_id = fields.Integer(string="Magento Store ID", readonly=True)
    store_code = fields.Char(string="Magento Store Code", readonly=True)

    # ---- 4. Mappings ----
    default_journal_id = fields.Many2one(
        "account.journal", string="Default Payment Journal",
        domain="[('type', 'in', ('bank', 'cash', 'credit')), ('company_id', '=', company_id)]",
        help="Journal of the payment methods without a journal of their own.",
    )
    payment_line_ids = fields.One2many(
        "magento.setup.wizard.mapping", "wizard_id", domain=[("kind", "=", "payment")],
        string="Payment Methods")
    carrier_line_ids = fields.One2many(
        "magento.setup.wizard.mapping", "wizard_id", domain=[("kind", "=", "carrier")],
        string="Shipping Methods")
    tax_line_ids = fields.One2many(
        "magento.setup.wizard.mapping", "wizard_id", domain=[("kind", "=", "tax")], string="Tax Rates")
    group_line_ids = fields.One2many(
        "magento.setup.wizard.mapping", "wizard_id", domain=[("kind", "=", "group")],
        string="Customer Groups")
    orders_sampled = fields.Integer(readonly=True)
    delivery_installed = fields.Boolean(compute="_compute_delivery_installed")

    # ---- 5. Catalog check ----
    check_line_ids = fields.One2many("magento.setup.wizard.check", "wizard_id", string="Findings")
    catalog_sampled = fields.Integer(string="SKUs Checked", readonly=True)
    catalog_total = fields.Integer(string="Magento Products", readonly=True)
    catalog_error_count = fields.Integer(string="Errors", readonly=True)
    catalog_warning_count = fields.Integer(string="Warnings", readonly=True)
    catalog_new_count = fields.Integer(string="New in Odoo", readonly=True)

    # ---- 6. Go live ----
    auto_sync_enabled = fields.Boolean(string="Automatic Sync")
    auto_sync_frequency = fields.Selection(
        selection=lambda self: self.env["magento.instance"]._fields["auto_sync_frequency"].selection,
        string="Frequency", default="hourly", required=True,
    )
    auto_sync_hour_interval = fields.Selection(
        selection=lambda self: self.env["magento.instance"]._fields["auto_sync_hour_interval"].selection,
        string="Every", default="1",
    )
    auto_sync_hour = fields.Integer(string="At Hour", default=1)
    auto_sync_products = fields.Boolean(string="Products", default=True)
    auto_sync_customers = fields.Boolean(string="Customers", default=True)
    auto_sync_categories = fields.Boolean(string="Categories", default=True)
    auto_sync_orders = fields.Boolean(string="Orders", default=True)
    run_syncs_in_background = fields.Boolean(string="Run Syncs in Background", default=True)
    stock_push_enabled = fields.Boolean(string="Automatic Stock Push")
    stock_qty_basis = fields.Selection(
        selection=lambda self: self.env["magento.instance"]._fields["stock_qty_basis"].selection,
        string="Quantity Basis", default="free", required=True,
    )
    alert_user_ids = fields.Many2many(
        "res.users", "magento_setup_wizard_alert_user_rel", "wizard_id", "user_id",
        string="Alert Users", domain="[('share', '=', False)]",
    )
    workflow_summary = fields.Text(compute="_compute_workflow_summary")
    issue_line_ids = fields.One2many("magento.setup.wizard.issue", "wizard_id", string="Known Issues")
    first_sync_queued = fields.Boolean(readonly=True)

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------
    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can run the store setup wizard."))

    @api.model
    def default_get(self, fields_list):
        self._check_manager()
        vals = super().default_get(fields_list)
        ctx = self.env.context
        instance_id = vals.get("instance_id")
        if not instance_id and ctx.get("active_model") == "magento.instance" and ctx.get("active_id"):
            instance_id = ctx["active_id"]
        instance = self.env["magento.instance"].browse(instance_id).exists() if instance_id else None
        if instance:
            vals.update(self._resume_vals(instance))
        else:
            vals.setdefault("alert_user_ids", [(6, 0, self.env.user.ids)])
            journal = self._default_payment_journal(self.env.company)
            if journal:
                vals.setdefault("default_journal_id", journal.id)
        return {key: value for key, value in vals.items() if key in fields_list}

    @api.model_create_multi
    def create(self, vals_list):
        self._check_manager()
        return super().create(vals_list)

    @api.model
    def _default_payment_journal(self, company):
        return self.env["account.journal"].search([
            ("type", "=", "bank"), ("company_id", "=", company.id),
        ], limit=1)

    @api.model
    def _resume_vals(self, instance):
        """Wizard values of an existing instance: what is configured counts as done."""
        inst = instance.sudo()
        connected = inst.state == "connected"
        vals = {
            "instance_id": inst.id,
            "company_id": inst.company_id.id,
            "name": inst.name,
            "url_input": inst.base_url,
            "base_url": inst.base_url,
            "verify_ssl": inst.verify_ssl,
            "auth_method": inst.auth_method or "bearer",
            "show_advanced": (inst.auth_method or "bearer") != "oauth1",
            "access_token": inst.access_token,
            "consumer_key": inst.consumer_key,
            "consumer_secret": inst.consumer_secret,
            "access_token_secret": inst.access_token_secret,
            "website_id": inst.website_id,
            "store_id": inst.store_id,
            "store_code": inst.store_code,
            "default_journal_id": (inst.payment_journal_id or self._default_payment_journal(inst.company_id)).id,
            "auto_sync_enabled": inst.auto_sync_enabled,
            "auto_sync_frequency": inst.auto_sync_frequency,
            "auto_sync_hour_interval": inst.auto_sync_hour_interval,
            "auto_sync_hour": inst.auto_sync_hour,
            "auto_sync_products": inst.auto_sync_products,
            "auto_sync_customers": inst.auto_sync_customers,
            "auto_sync_categories": inst.auto_sync_categories,
            "auto_sync_orders": inst.auto_sync_orders,
            "run_syncs_in_background": inst.run_syncs_in_background,
            "stock_push_enabled": inst.stock_push_enabled,
            "stock_qty_basis": inst.stock_qty_basis,
            "alert_user_ids": [(6, 0, (inst.alert_user_ids or self.env.user).ids)],
            "url_status": "ok" if connected else "pending",
            "credentials_status": "ok" if connected else ("error" if inst.state == "error" else "pending"),
            "credentials_message": inst.last_connection_message or False,
            "stores_status": "ok" if (inst.store_code or inst.store_id or inst.website_id) else "pending",
            "mappings_status": "ok" if (inst.payment_map_ids or inst.tax_map_ids or inst.carrier_map_ids)
            else "pending",
        }
        if connected:
            vals["url_message"] = _("Configured address: %s", inst.base_url)
        if vals["stores_status"] == "ok":
            vals["stores_message"] = _("Syncing website %(website)s, store %(store)s (%(code)s).",
                                       website=inst.website_id, store=inst.store_id,
                                       code=inst.store_code or "-")
        if vals["mappings_status"] == "ok":
            vals["mappings_message"] = _("Mappings exist; re-run the proposals to add new methods.")
        vals["current_step"] = next(
            (key for key in STEP_KEYS[:4] if vals.get("%s_status" % key) != "ok"), "catalog")
        return vals

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("current_step", *("%s_status" % key for key in STEP_KEYS),
                 *("%s_message" % key for key in STEP_KEYS))
    def _compute_step_header(self):
        for wizard in self:
            items = Markup()
            done = 0
            for index, (key, label) in enumerate(STEPS, start=1):
                status = wizard["%s_status" % key] or "pending"
                done += status in DONE
                css, text = _STATUS_BADGE[status]
                current = key == wizard.current_step
                pill_style = "box-shadow: inset 0 -3px 0 %s; font-weight: 600;" % MAGENTO_ORANGE if current else ""
                dot_style = (
                    "width: 24px; height: 24px; font-size: 12px; font-weight: 700; "
                    + ("background: %s; color: #fff;" % MAGENTO_ORANGE if current
                       else "background: #e9ecef; color: #41362f;")
                )
                items += Markup(
                    '<li class="d-flex align-items-center gap-2 px-3 py-2 border rounded-3 bg-white" '
                    'style="%s"%s>'
                    '<span class="d-inline-flex align-items-center justify-content-center rounded-circle" '
                    'style="%s" aria-hidden="true">%s</span>'
                    '<span>%s</span><span class="badge %s">%s</span></li>'
                ) % (pill_style, Markup(' aria-current="step"') if current else Markup(),
                     dot_style, index, label, css, text)
            wizard.step_header = Markup(
                '<ol class="list-unstyled d-flex flex-wrap gap-2 mb-0" aria-label="Setup steps">%s</ol>'
            ) % items
            wizard.progress = round(100.0 * done / len(STEPS))
            status = wizard["%s_status" % wizard.current_step] or "pending"
            message = wizard["%s_message" % wizard.current_step]
            if message:
                lines = Markup("<br/>").join(escape(line) for line in message.splitlines() if line.strip())
                wizard.step_alert = Markup('<div class="alert %s mb-0" role="%s">%s</div>') % (
                    _STATUS_ALERT[status], "alert" if status == "error" else "status", lines)
            else:
                wizard.step_alert = False
            wizard.is_first_step = wizard.current_step == STEP_KEYS[0]
            wizard.is_last_step = wizard.current_step == STEP_KEYS[-1]

    @api.depends("instance_id")
    def _compute_discovery_available(self):
        available = self._discovery_available()
        for wizard in self:
            wizard.discovery_available = available

    def _compute_delivery_installed(self):
        installed = "delivery.carrier" in self.env
        for wizard in self:
            wizard.delivery_installed = installed

    @api.depends("instance_id")
    def _compute_workflow_summary(self):
        for wizard in self:
            rules = wizard.instance_id.sudo().order_workflow_ids
            if rules:
                wizard.workflow_summary = "\n".join(
                    "%s: %s" % (rule.name, rule._magento_step_labels()) for rule in rules)
            else:
                wizard.workflow_summary = _("The default order workflows are created when you save.")

    @api.depends("current_step")
    def _compute_display_name(self):
        for wizard in self:
            wizard.display_name = _("Connect a Magento store")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _set_status(self, step, status, message=None):
        self.write({"%s_status" % step: status, "%s_message" % step: message or False})

    def action_next(self):
        self.ensure_one()
        self._check_manager()
        step = self.current_step
        if step == "url" and (self.url_status not in DONE or self._url_changed()):
            self.action_check_url()
            if self.url_status not in DONE:
                return False
        elif step == "credentials" and (self.credentials_status not in DONE or not self.instance_id):
            self.action_validate_credentials()
            if self.credentials_status not in DONE:
                return False
        elif step == "mappings" and self.mappings_status != "ok" and (
                self.payment_line_ids or self.carrier_line_ids or self.tax_line_ids or self.group_line_ids):
            self.action_apply_mappings()
        index = STEP_KEYS.index(step)
        if index < len(STEP_KEYS) - 1:
            self._goto(STEP_KEYS[index + 1])
        return False

    def action_back(self):
        self.ensure_one()
        index = STEP_KEYS.index(self.current_step)
        if index:
            self._goto(STEP_KEYS[index - 1])
        return False

    def action_goto_step(self):
        self.ensure_one()
        step = self.env.context.get("setup_step")
        if step in STEP_KEYS:
            self._goto(step)
        return False

    def _goto(self, step):
        self._check_manager()
        self.current_step = step
        if step == "stores" and self.instance_id and self.stores_status == "pending" and not self.store_line_ids:
            self.action_discover_stores()
        elif step == "mappings" and self.instance_id and self.mappings_status == "pending" and not (
                self.payment_line_ids or self.carrier_line_ids or self.tax_line_ids or self.group_line_ids):
            self.action_propose_mappings()
        elif step == "golive":
            self._build_issues()

    def _require_instance(self, step):
        if self.instance_id:
            return True
        self._set_status(step, "error", _("Connect the store first: complete step 2 (Credentials)."))
        return False

    def _url_changed(self):
        try:
            return normalize_store_url(self.url_input, self.env) != (self.base_url or "")
        except UserError:
            return True

    # ------------------------------------------------------------------
    # 1. Store URL
    # ------------------------------------------------------------------
    def action_check_url(self):
        self.ensure_one()
        self._check_manager()
        try:
            base_url = normalize_store_url(self.url_input, self.env)
        except UserError as exc:
            self.write({"url_status": "error", "url_message": str(exc.args[0]), "base_url": False})
            return False
        status, message, final_url, ssl_problem = self._probe_store(base_url, self.verify_ssl)
        if final_url and final_url != base_url:
            message = _("Magento redirected %(old)s to %(new)s; the wizard uses the new address.",
                        old=base_url, new=final_url) + "\n" + message
            base_url = final_url
        vals = {
            "base_url": base_url,
            "url_status": status,
            "url_message": message,
            "ssl_problem": ssl_problem,
        }
        if not self.name:
            vals["name"] = urlparse(base_url).hostname
        self.write(vals)
        if self.instance_id and status in DONE:
            self.instance_id.write({"base_url": base_url, "verify_ssl": self.verify_ssl})
        return False

    @api.model
    def _store_get(self, url, verify):
        with warnings.catch_warnings():
            # verify=False is the user's explicit choice; the step already says so.
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return requests.get(url, timeout=REACH_TIMEOUT, verify=verify,
                                headers={"Accept": "application/json"})

    @api.model
    def _probe_store(self, base_url, verify):
        """(status, message, final root URL, ssl problem) of the unauthenticated REST probe."""
        url = base_url + "/rest/V1/store/storeConfigs"
        host = urlparse(base_url).hostname
        ssl_problem = False
        try:
            response = self._store_get(url, verify)
        except requests.exceptions.SSLError as exc:
            ssl_problem = True
            try:
                response = self._store_get(url, False)
            except requests.exceptions.RequestException:
                return "error", _(
                    "The SSL certificate of %(host)s is not trusted and the store does not answer "
                    "without verification either: %(error)s", host=host, error=exc), None, True
        except requests.exceptions.Timeout:
            return "error", _("%(host)s did not answer within %(seconds)s seconds. Check the address and "
                              "that the store is online.", host=host, seconds=REACH_TIMEOUT), None, False
        except requests.exceptions.RequestException as exc:
            return "error", _(
                "Cannot reach %(host)s: %(error)s. Check the address, and that the Odoo server can "
                "resolve and reach it (firewall, VPN, DNS).", host=host, error=exc), None, False

        final_url = None
        reached = urlparse(response.url or url)
        if reached.netloc and (reached.scheme, reached.netloc.lower()) != (
                urlparse(base_url).scheme, urlparse(base_url).netloc):
            try:
                final_url = normalize_store_url(response.url, self.env)
            except UserError:
                final_url = None
        status_code = response.status_code
        is_json = "json" in (response.headers.get("Content-Type") or "").lower()
        if status_code in (200, 401) and (is_json or status_code == 401):
            message = _("The Magento REST API answers at %(url)s (HTTP %(code)s without credentials, "
                        "as expected).", url=final_url or base_url, code=status_code)
            if ssl_problem:
                return "warning", message + "\n" + _(
                    "The SSL certificate of %s is not trusted (self-signed or incomplete chain). "
                    "Untick Verify SSL only for a test or internal store, or install the CA certificate "
                    "on the Odoo server, then check again.", host), final_url, True
            if not verify and (final_url or base_url).startswith("https"):
                message += "\n" + _("SSL verification is off: use this for test stores only.")
            return "ok", message, final_url, False
        if status_code == 404:
            return "error", _(
                "%(url)s answered but has no Magento REST API (HTTP 404). Use the shop's home page "
                "address; the REST API may also be blocked by the web server.",
                url=base_url), final_url, ssl_problem
        if status_code == 200:
            return "warning", _(
                "%(url)s answered, but not like the Magento REST API (no JSON). Check this is the "
                "Magento store address.", url=base_url), final_url, ssl_problem
        if status_code == 403:
            return "warning", _(
                "%(url)s refused the REST API (HTTP 403): a firewall or web application filter may "
                "block requests from Odoo.", url=base_url), final_url, ssl_problem
        return "error", _("%(url)s answered HTTP %(code)s. Check the store is online.",
                          url=base_url, code=status_code), final_url, ssl_problem

    # ------------------------------------------------------------------
    # 2. Credentials
    # ------------------------------------------------------------------
    def _credential_vals(self):
        vals = {"auth_method": self.auth_method}
        for name in ("access_token", "consumer_key", "consumer_secret", "access_token_secret"):
            vals[name] = (self[name] or "").strip()
        return vals

    def action_validate_credentials(self):
        self.ensure_one()
        self._check_manager()
        if not self.base_url:
            self._set_status("credentials", "error", _("Check the store address first (step 1)."))
            return False
        creds = self._credential_vals()
        labels = [("consumer_key", _("Consumer Key")), ("consumer_secret", _("Consumer Secret")),
                  ("access_token", _("Access Token")), ("access_token_secret", _("Access Token Secret"))]
        if self.auth_method == "bearer":
            labels = [("access_token", _("Access Token"))]
        missing = [label for name, label in labels if not creds[name]]
        if missing:
            self._set_status("credentials", "error", _("Fill in: %s.", ", ".join(missing)))
            return False
        vals = dict(creds, base_url=self.base_url, verify_ssl=self.verify_ssl,
                    name=self.name or urlparse(self.base_url).hostname)
        try:
            with self.env.cr.savepoint():
                if self.instance_id:
                    self.instance_id.write(vals)
                else:
                    vals["company_id"] = self.company_id.id
                    self.instance_id = self.env["magento.instance"].create(vals)
        except (UserError, ValidationError) as exc:
            self._set_status("credentials", "error", str(exc.args[0] if exc.args else exc))
            return False
        instance = self.instance_id
        instance.action_diagnose_access()
        if instance.state == "connected":
            message = instance.last_connection_message or _("All required resources are granted.")
            self._set_status("credentials", "ok", message)
            if self.url_status != "ok" and not self.ssl_problem:
                self._set_status("url", "ok", _("The Magento REST API answers at %s.", self.base_url))
        else:
            message = instance.last_connection_message or _("Magento refused the credentials.")
            if "Missing Magento API resources" in message:
                message += "\n" + REQUIRED_RESOURCES_HINT
            self._set_status("credentials", "error", message)
        return False

    # ------------------------------------------------------------------
    # 3. Stores
    # ------------------------------------------------------------------
    @api.model
    def _discovery_available(self):
        return hasattr(self.env["magento.instance"], "_magento_discover_stores")

    def action_discover_stores(self):
        self.ensure_one()
        self._check_manager()
        if not self._require_instance("stores"):
            return False
        instance = self.instance_id
        notes = []
        rows = []
        if self._discovery_available():
            try:
                with self.env.cr.savepoint():
                    instance._magento_discover_stores()
                rows = self._stores_from_models(instance)
            except Exception as exc:  # noqa: BLE001 - shown on the step
                notes.append(_("Store discovery failed: %s", instance._format_magento_request_error(exc, instance)))
        else:
            notes.append(_STORE_DISCOVERY_NOTE)
        if not rows:
            try:
                rows = self._stores_from_api(instance)
            except Exception as exc:  # noqa: BLE001 - shown on the step
                notes.append(_("Store views could not be read: %s",
                               instance._format_magento_request_error(exc, instance)))
                self.store_line_ids.unlink()
                self._set_status("stores", "error", "\n".join(notes))
                return False
        self.store_choice_id = False
        self.store_line_ids.unlink()
        lines = self.env["magento.setup.wizard.store"].create([dict(row, wizard_id=self.id) for row in rows])
        if not lines:
            notes.append(_("Magento returned no store view."))
            self._set_status("stores", "error", "\n".join(notes))
            return False
        websites = len(set(lines.mapped("website_magento_id")))
        notes.insert(0, _("Found %(views)s store view(s) on %(websites)s website(s).",
                          views=len(lines), websites=websites))
        current = lines.filtered(lambda line: instance.store_code and line.store_code == instance.store_code) \
            or lines.filtered(lambda line: instance.store_id and line.store_magento_id == instance.store_id)
        if not current and len(lines) == 1:
            current = lines
        if current:
            self._apply_store(current[:1], notes)
        else:
            notes.append(_("Pick the store view to sync with 'Use this store'."))
            self._set_status("stores", "warning", "\n".join(notes))
        return False

    def _apply_store(self, line, notes=None):
        notes = list(notes or [])
        vals = {"website_id": line.website_magento_id, "store_id": line.store_magento_id,
                "store_code": line.store_code}
        self.instance_id.write(vals)
        self.write(dict(vals, store_choice_id=line.id))
        notes.append(_("Syncing %(store)s (code %(code)s) on website %(website)s.",
                       store=line.store_name, code=line.store_code, website=line.website_name or line.website_magento_id))
        self._set_status("stores", "ok", "\n".join(notes))

    @api.model
    def _stores_from_api(self, instance):
        api_client = MagentoAPI(instance)
        api_client.MAX_RETRIES = 0
        websites = {(_as_int(site.get("id"))): site for site in (api_client._get("/rest/V1/store/websites") or [])
                    if isinstance(site, dict)}
        rows = []
        for view in api_client._get("/rest/V1/store/storeViews") or []:
            if not isinstance(view, dict):
                continue
            store_id = _as_int(view.get("id"))
            website_id = _as_int(view.get("website_id"))
            if not store_id or (view.get("code") or "") == "admin":
                continue
            website = websites.get(website_id) or {}
            rows.append({
                "website_magento_id": website_id,
                "website_code": website.get("code") or False,
                "website_name": website.get("name") or False,
                "store_magento_id": store_id,
                "store_code": view.get("code") or False,
                "store_name": view.get("name") or view.get("code") or False,
                "is_active": bool(_as_int(view.get("is_active", 1))),
            })
        return rows

    @api.model
    def _stores_from_models(self, instance):
        """Store views recorded by ``_magento_discover_stores`` (field names read defensively)."""
        if "magento.store.view" not in self.env:
            return []
        View = self.env["magento.store.view"].sudo().with_context(active_test=False)
        if "instance_id" not in View._fields:
            return []

        def first(record, names):
            for name in names:
                if record and name in record._fields:
                    value = record[name]
                    if isinstance(value, models.BaseModel):
                        continue
                    if value not in (False, None, ""):
                        return value
            return False

        rows = []
        for view in View.search([("instance_id", "=", instance.id)]):
            website = view["website_id"] if "website_id" in view._fields \
                and view._fields["website_id"].type == "many2one" else None
            website_magento_id = first(website, ("magento_id", "magento_website_id", "website_id", "external_id")) \
                if website else first(view, ("magento_website_id", "website_id"))
            rows.append({
                "website_magento_id": _as_int(website_magento_id),
                "website_code": (first(website, ("code", "website_code")) if website else False) or False,
                "website_name": (first(website, ("name",)) if website else False) or False,
                "store_magento_id": _as_int(first(view, ("magento_id", "magento_store_id", "store_id",
                                                          "external_id"))),
                "store_code": first(view, ("code", "store_code", "magento_code")) or False,
                "store_name": first(view, ("name", "code")) or False,
                "is_active": bool(first(view, ("is_active", "active")) if any(
                    name in view._fields for name in ("is_active", "active")) else True),
            })
        return [row for row in rows if row["store_magento_id"] and row["store_code"] != "admin"]

    # ------------------------------------------------------------------
    # 4. Mappings
    # ------------------------------------------------------------------
    def action_propose_mappings(self):
        self.ensure_one()
        self._check_manager()
        if not self._require_instance("mappings"):
            return False
        instance = self.instance_id
        api_client = MagentoAPI(instance)
        api_client.MAX_RETRIES = 0
        notes, errors = [], []

        orders = []
        try:
            response = api_client._get("/rest/V1/orders", params=api_client.build_search_params(
                sort=(("created_at", "DESC"),), page_size=ORDER_SAMPLE, current_page=1, fields=ORDER_FIELDS))
            orders = [order for order in (response or {}).get("items") or [] if isinstance(order, dict)]
        except Exception as exc:  # noqa: BLE001 - shown on the step
            errors.append(_("Recent orders could not be read: %s",
                            instance._format_magento_request_error(exc, instance)))
        payments, carriers, order_taxes, order_groups = self._scan_orders(orders)
        if not orders and not errors:
            notes.append(_("Magento has no order yet: payment methods and carriers are added "
                           "automatically as orders are imported."))
        elif orders:
            notes.append(_("Read the %s most recent order(s).", len(orders)))

        taxes, tax_source = self._fetch_tax_rates(api_client, order_taxes, notes)
        groups = self._fetch_customer_groups(api_client, order_groups, notes)

        self.orders_sampled = len(orders)
        self.env["magento.setup.wizard.mapping"].search([("wizard_id", "=", self.id)]).unlink()
        lines = (self._payment_proposals(payments) + self._carrier_proposals(carriers)
                 + self._tax_proposals(taxes) + self._group_proposals(groups))
        self.env["magento.setup.wizard.mapping"].create([dict(line, wizard_id=self.id) for line in lines])
        if tax_source:
            notes.append(tax_source)
        notes.append(_("Review the proposals (untick a line to skip it), then click Create mappings."))
        status = "error" if errors and not lines else ("warning" if errors else "pending")
        self._set_status("mappings", status, "\n".join(errors + notes))
        return False

    @api.model
    def _scan_orders(self, orders):
        payments, carriers, taxes, groups = {}, {}, {}, {}
        for order in orders:
            payment = order.get("payment") or {}
            method = (payment.get("method") or "").strip()
            if method:
                info = payment.get("additional_information") or []
                title = next((item for item in info if isinstance(item, str) and item.strip()), "") \
                    if isinstance(info, list) else ""
                entry = payments.setdefault(method, {"title": title or method, "count": 0})
                entry["count"] += 1
            ext = order.get("extension_attributes") or {}
            shipping_method = (order.get("shipping_method") or "").strip()
            if not shipping_method:
                for assignment in ext.get("shipping_assignments") or []:
                    shipping_method = (((assignment or {}).get("shipping") or {}).get("method") or "").strip()
                    if shipping_method:
                        break
            if shipping_method:
                entry = carriers.setdefault(shipping_method, {
                    "title": order.get("shipping_description") or shipping_method, "count": 0})
                entry["count"] += 1
            for tax in ext.get("applied_taxes") or []:
                if not isinstance(tax, dict):
                    continue
                percent = round(float(tax.get("percent") or 0.0), 4)
                code = (tax.get("code") or "").strip()
                entry = taxes.setdefault((code, percent), {"title": tax.get("title") or code, "count": 0})
                entry["count"] += 1
            if order.get("customer_group_id") is not None:
                group_id = _as_int(order.get("customer_group_id"))
                groups[group_id] = groups.get(group_id, 0) + 1
        return payments, carriers, taxes, groups

    @api.model
    def _fetch_tax_rates(self, api_client, order_taxes, notes):
        """{(code, percent): {title, count}}: Magento tax rates, else those of recent orders."""
        try:
            response = api_client._get("/rest/V1/taxRates/search", params=api_client.build_search_params(
                page_size=300, current_page=1))
            rates = {}
            for rate in (response or {}).get("items") or []:
                if not isinstance(rate, dict):
                    continue
                code = (rate.get("code") or "").strip()
                percent = round(float(rate.get("rate") or 0.0), 4)
                count = order_taxes.get((code, percent), {}).get("count", 0)
                rates[(code, percent)] = {"title": code, "count": count}
            for key, value in order_taxes.items():
                rates.setdefault(key, value)
            return rates, _("Tax rates read from Magento (Stores > Tax Zones and Rates).")
        except Exception as exc:  # noqa: BLE001 - fall back to the orders
            reason = _("not allowed for this integration") if _http_status(exc) in (401, 403) \
                else str(exc)[:200]
            return order_taxes, _("Tax rates proposed from recent orders (tax rate list %s).", reason)

    @api.model
    def _fetch_customer_groups(self, api_client, order_groups, notes):
        """[{id, code, tax_class_id, tax_class_name, from_magento, count}]."""
        try:
            response = api_client._get("/rest/V1/customerGroups/search", params=api_client.build_search_params(
                sort=(("id", "ASC"),), page_size=300, current_page=1))
            rows = [row for row in (response or {}).get("items") or [] if isinstance(row, dict)]
            return [dict(row, from_magento=True, count=order_groups.get(_as_int(row.get("id")), 0))
                    for row in rows if row.get("id") not in (None, "")]
        except Exception as exc:  # noqa: BLE001 - fall back to the orders
            reason = _("not allowed for this integration") if _http_status(exc) in (401, 403) \
                else str(exc)[:200]
            notes.append(_("Customer groups taken from recent orders (group list %s).", reason))
            return [{"id": group_id, "code": False, "from_magento": False, "count": count}
                    for group_id, count in sorted(order_groups.items())]

    def _payment_proposals(self, payments):
        instance = self.instance_id
        existing = self.env["magento.payment.map"].sudo().with_context(active_test=False).search(
            [("instance_id", "=", instance.id)])
        by_code = {row.magento_method: row for row in existing}
        lines = []
        for code in sorted(set(payments) | set(by_code)):
            row = by_code.get(code)
            seen = payments.get(code, {})
            lines.append({
                "kind": "payment",
                "code": code,
                "label": (row.name if row else None) or seen.get("title") or code,
                "seen_count": seen.get("count", 0),
                "existing": bool(row),
                "journal_id": (row.journal_id.id if row and row.journal_id else self.default_journal_id.id) or False,
            })
        return lines

    def _carrier_proposals(self, carriers):
        instance = self.instance_id
        existing = self.env["magento.carrier.map"].sudo().with_context(active_test=False).search(
            [("instance_id", "=", instance.id)])
        by_code = {row.magento_code: row for row in existing}
        lines = []
        for code in sorted(set(carriers) | set(by_code)):
            row = by_code.get(code)
            seen = carriers.get(code, {})
            ref = row.carrier_ref if row else None
            lines.append({
                "kind": "carrier",
                "code": code,
                "label": (row.name if row else None) or seen.get("title") or code,
                "seen_count": seen.get("count", 0),
                "existing": bool(row),
                "carrier_ref": "%s,%s" % (ref._name, ref.id) if ref else False,
            })
        return lines

    def _tax_proposals(self, taxes):
        instance = self.instance_id.sudo()
        price_include = bool(instance.prices_include_tax)
        TaxMap = self.env["magento.tax.map"].sudo().with_context(active_test=False)
        Tax = self.env["account.tax"].with_context(active_test=False)
        lines = []
        for (code, percent), info in sorted(taxes.items()):
            mapping = TaxMap.search([
                ("instance_id", "=", instance.id), ("magento_code", "=", code or False),
                ("price_include", "=", price_include),
            ]).filtered(lambda m: float_compare(m.percent, percent, precision_digits=4) == 0)[:1]
            tax = mapping.tax_id
            if not tax:
                tax = Tax.search([
                    ("company_id", "=", instance.company_id.id), ("type_tax_use", "=", "sale"),
                    ("amount_type", "=", "percent"), ("active", "=", True),
                ]).filtered(lambda t: float_compare(t.amount, percent, precision_digits=4) == 0
                            and bool(t.price_include) == price_include)[:1]
            lines.append({
                "kind": "tax",
                "code": code or False,
                "label": info.get("title") or code or False,
                "percent": percent,
                "price_include": price_include,
                "seen_count": info.get("count", 0),
                "existing": bool(mapping),
                "tax_id": tax.id or False,
            })
        return lines

    def _group_proposals(self, groups):
        instance = self.instance_id
        existing = {row.magento_group_id: row for row in self.env["magento.customer.group"].sudo().search(
            [("instance_id", "=", instance.id)])}
        lines = []
        for group in groups:
            group_id = _as_int(group.get("id"))
            row = existing.get(group_id)
            lines.append({
                "kind": "group",
                "magento_ref": group_id,
                "code": group.get("code") or (row.name if row else False)
                or _DEFAULT_GROUP_NAMES.get(group_id) or _("Group %s", group_id),
                "label": group.get("tax_class_name") or False,
                "tax_class_id": _as_int(group.get("tax_class_id")),
                "from_magento": bool(group.get("from_magento")),
                "seen_count": group.get("count", 0),
                "existing": bool(row),
                "pricelist_id": row.pricelist_id.id if row and row.pricelist_id else False,
            })
        return lines

    def action_apply_mappings(self):
        self.ensure_one()
        self._check_manager()
        if not self._require_instance("mappings"):
            return False
        instance = self.instance_id
        created = updated = 0
        skipped_taxes = []
        try:
            with self.env.cr.savepoint():
                created, updated, skipped_taxes = self._apply_mapping_lines(instance)
        except (UserError, ValidationError) as exc:
            self._set_status("mappings", "error", _("Mappings not created: %s", exc.args[0] if exc.args else exc))
            return False
        notes = [_("Created %(created)s and updated %(updated)s mapping(s).", created=created, updated=updated)]
        status = "ok"
        if skipped_taxes:
            if instance.sudo().auto_create_taxes:
                notes.append(_("No Odoo tax chosen for %s: the connector creates one on the first order.",
                               ", ".join(skipped_taxes)))
            else:
                status = "warning"
                notes.append(_("No Odoo tax chosen for %s, and automatic tax creation is off: orders "
                               "using these rates fail until they are mapped.", ", ".join(skipped_taxes)))
        self._set_status("mappings", status, "\n".join(notes))
        return False

    def _apply_mapping_lines(self, instance):
        created = updated = 0
        skipped_taxes = []
        PaymentMap = self.env["magento.payment.map"].sudo().with_context(active_test=False)
        CarrierMap = self.env["magento.carrier.map"].sudo().with_context(active_test=False)
        TaxMap = self.env["magento.tax.map"].sudo().with_context(active_test=False)
        Group = self.env["magento.customer.group"].sudo()
        for line in self.payment_line_ids.filtered("accept"):
            row = PaymentMap.search([("instance_id", "=", instance.id), ("magento_method", "=", line.code)], limit=1)
            if row:
                if line.journal_id and row.journal_id != line.journal_id:
                    row.write({"journal_id": line.journal_id.id, "payment_method_line_id": False,
                               "refund_method_line_id": False})
                    updated += 1
            else:
                PaymentMap.create({"instance_id": instance.id, "magento_method": line.code,
                                   "name": line.label or line.code, "journal_id": line.journal_id.id or False})
                created += 1
            line.existing = True
        for line in self.carrier_line_ids.filtered("accept"):
            row = CarrierMap.search([("instance_id", "=", instance.id), ("magento_code", "=", line.code)], limit=1)
            ref = "%s,%s" % (line.carrier_ref._name, line.carrier_ref.id) if line.carrier_ref else False
            if row:
                if ref and row.carrier_ref != line.carrier_ref:
                    row.carrier_ref = ref
                    updated += 1
            else:
                CarrierMap.create({"instance_id": instance.id, "magento_code": line.code,
                                   "name": line.label or line.code, "carrier_ref": ref})
                created += 1
            line.existing = True
        for line in self.tax_line_ids.filtered("accept"):
            row = TaxMap.search([
                ("instance_id", "=", instance.id), ("magento_code", "=", line.code or False),
                ("price_include", "=", line.price_include),
            ]).filtered(lambda m: float_compare(m.percent, line.percent, precision_digits=4) == 0)[:1]
            if not line.tax_id:
                skipped_taxes.append("%s (%s%%)" % (line.label or line.code or "-", line.percent))
                continue
            if row:
                if row.tax_id != line.tax_id:
                    row.write({"tax_id": line.tax_id.id, "auto_created": False})
                    updated += 1
            else:
                TaxMap.create({"instance_id": instance.id, "magento_code": line.code or False,
                               "magento_title": line.label or line.code, "percent": line.percent,
                               "price_include": line.price_include, "tax_id": line.tax_id.id})
                created += 1
            line.existing = True
        group_lines = self.group_line_ids.filtered("accept")
        from_magento = group_lines.filtered("from_magento")
        if from_magento:
            before = set(Group.search([("instance_id", "=", instance.id)]).mapped("magento_group_id"))
            Group._magento_upsert_groups(instance, [
                {"id": line.magento_ref, "code": line.code, "tax_class_id": line.tax_class_id,
                 "tax_class_name": line.label} for line in from_magento])
            created += len(set(from_magento.mapped("magento_ref")) - before)
        for line in group_lines:
            group = Group.search([("instance_id", "=", instance.id), ("magento_group_id", "=", line.magento_ref)],
                                 limit=1)
            if not group:
                group = Group.create({"instance_id": instance.id, "magento_group_id": line.magento_ref,
                                      "name": line.code, "from_magento": False})
                group._magento_ensure_tag()
                created += 1
            if line.pricelist_id and group.pricelist_id != line.pricelist_id:
                group.pricelist_id = line.pricelist_id
                updated += 1
            line.existing = True
        if self.default_journal_id and not instance.sudo().payment_journal_id:
            instance.write({"payment_journal_id": self.default_journal_id.id})
        return created, updated, skipped_taxes

    # ------------------------------------------------------------------
    # 5. Catalog check
    # ------------------------------------------------------------------
    def action_check_catalog(self):
        self.ensure_one()
        self._check_manager()
        if not self._require_instance("catalog"):
            return False
        instance = self.instance_id
        api_client = MagentoAPI(instance)
        api_client.MAX_RETRIES = 1
        stats = {}
        try:
            items = list(islice(api_client.iter_search(
                lambda prefix: "%s/products" % prefix, fields=PRODUCT_FIELDS, page_size=100,
                sort=(("entity_id", "ASC"),), id_key="id", stats=stats,
            ), CATALOG_SAMPLE))
        except Exception as exc:  # noqa: BLE001 - shown on the step
            self._set_status("catalog", "error", _("Magento products could not be read: %s",
                                                   instance._format_magento_request_error(exc, instance)))
            return False
        findings, new_count = self._catalog_findings([item for item in items if isinstance(item, dict)])
        self.check_line_ids.unlink()
        lines = self.env["magento.setup.wizard.check"].create([dict(row, wizard_id=self.id) for row in findings])
        errors = len(lines.filtered(lambda line: line.severity == "error"))
        warnings_count = len(lines.filtered(lambda line: line.severity == "warning"))
        total = stats.get("total_count") or len(items)
        self.write({
            "catalog_sampled": len(items),
            "catalog_total": total,
            "catalog_error_count": errors,
            "catalog_warning_count": warnings_count,
        })
        summary = _("Checked %(sampled)s of %(total)s Magento product(s): %(errors)s error(s), "
                    "%(warnings)s warning(s).", sampled=len(items), total=total, errors=errors,
                    warnings=warnings_count)
        if errors or warnings_count:
            summary += "\n" + _("Nothing is blocked: fix what matters before the first product sync.")
        self.catalog_new_count = new_count
        self._set_status("catalog", "warning" if errors or warnings_count else "ok", summary)
        return False

    @api.model
    def _catalog_findings(self, items):
        """(finding rows, count of SKUs new to Odoo) for the sampled products.

        Rows are dicts (severity, category, sku, product_id, message); the SKUs that simply do
        not exist in Odoo yet are summed up in one info row, so real findings stay visible.
        """
        rows = []
        Product = self.env["product.product"].with_context(active_test=False)
        skus = [str(item.get("sku") or "").strip() for item in items]
        skus = [sku for sku in skus if sku]

        exact = {}
        for product in Product.search([("default_code", "in", skus)]) if skus else Product:
            exact.setdefault(product.default_code, Product)
            exact[product.default_code] |= product

        # Odoo default codes that equal a Magento SKU once case and spaces are ignored.
        loose = {}
        if skus:
            self.env.cr.execute(
                "SELECT id FROM product_product WHERE default_code IS NOT NULL "
                "AND lower(btrim(default_code)) = ANY(%s)", [list({sku.lower() for sku in skus})])
            for product in Product.search([("id", "in", [row[0] for row in self.env.cr.fetchall()])]):
                loose.setdefault(product.default_code.strip().lower(), Product)
                loose[product.default_code.strip().lower()] |= product

        by_barcode = {}
        barcodes = set(skus)
        for item in items:
            barcodes.update(self._item_barcodes(item).values())
        if barcodes:
            for product in Product.search([("barcode", "in", list(barcodes))]):
                by_barcode[product.barcode] = product

        magento_keys = {}
        type_counts = {}
        new_skus = []
        for item in items:
            sku = str(item.get("sku") or "").strip()
            if not sku:
                continue
            type_id = item.get("type_id") or "simple"
            type_counts[type_id] = type_counts.get(type_id, 0) + 1
            magento_keys.setdefault(sku.lower(), []).append(sku)
            matches = exact.get(sku, Product)
            if len(matches) > 1:
                rows.append(self._finding("error", "duplicate_sku", sku, matches[0], _(
                    "%(count)s Odoo products share the default code %(sku)s (%(names)s): the import "
                    "links the first one only.", count=len(matches), sku=sku,
                    names=", ".join(matches.mapped("display_name")[:5]))))
            elif not matches:
                near = loose.get(sku.lower(), Product)
                if near:
                    rows.append(self._finding("warning", "would_duplicate", sku, near[0], _(
                        "No Odoo product has the default code %(sku)s, but %(product)s has %(code)s: the "
                        "import would create a second product. Align the codes first.",
                        sku=sku, product=near[0].display_name, code=near[0].default_code)))
                else:
                    new_skus.append(sku)
            holder = by_barcode.get(sku)
            if holder and holder.default_code != sku:
                rows.append(self._finding("warning", "barcode", sku, holder, _(
                    "%(product)s uses %(sku)s as its barcode: scanning it opens that product, not the "
                    "Magento one.", product=holder.display_name, sku=sku)))
            for attribute, barcode in self._item_barcodes(item).items():
                holder = by_barcode.get(barcode)
                if holder and holder.default_code != sku:
                    rows.append(self._finding("error", "barcode", sku, holder, _(
                        "The %(attribute)s %(barcode)s of this Magento product is already the barcode of "
                        "%(product)s: Odoo barcodes are unique, so the import cannot set it.",
                        attribute=attribute, barcode=barcode, product=holder.display_name)))
            price = item.get("price")
            if type_id in PRICED_TYPES and (price in (None, "") or not float(price or 0.0)):
                rows.append(self._finding("warning", "no_price", sku, matches[:1] or None, _(
                    "No price in Magento: the Odoo product would be sold at 0.")))
        for key, variants in magento_keys.items():
            distinct = sorted(set(variants))
            if len(distinct) > 1:
                rows.append(self._finding("error", "magento_duplicate", distinct[0], None, _(
                    "Magento SKUs %s differ only by case or spaces: they would be imported as one "
                    "product code.", ", ".join(distinct))))
        labels = {
            "configurable": _("configurable product(s): imported as product templates with variants"),
            "bundle": _("bundle product(s): imported with their bundle components"),
            "grouped": _("grouped product(s): imported with their linked products"),
        }
        for type_id, label in labels.items():
            if type_counts.get(type_id):
                rows.append(self._finding("info", "type", False, None, "%s %s" % (type_counts[type_id], label)))
        if new_skus:
            preview = ", ".join(new_skus[:5]) + (", ..." if len(new_skus) > 5 else "")
            rows.append(self._finding("info", "new", False, None, _(
                "%(count)s SKU(s) have no Odoo product yet: the first product sync creates them (%(skus)s).",
                count=len(new_skus), skus=preview)))
        unsupported = sorted(set(type_counts) - set(PRICED_TYPES) - set(labels))
        for type_id in unsupported:
            rows.append(self._finding("warning", "type", False, None, _(
                "%(count)s product(s) of type %(type)s are not imported.", count=type_counts[type_id],
                type=type_id)))
        order = {"error": 0, "warning": 1, "info": 2}
        rows.sort(key=lambda row: (order[row["severity"]], row["category"], row["sku"] or ""))
        return rows, len(new_skus)

    @staticmethod
    def _item_barcodes(item):
        found = {}
        for attribute in item.get("custom_attributes") or []:
            if not isinstance(attribute, dict):
                continue
            code = (attribute.get("attribute_code") or "").lower()
            value = attribute.get("value")
            if code in BARCODE_ATTRIBUTES and isinstance(value, (str, int)) and str(value).strip():
                found[code] = str(value).strip()
        return found

    @staticmethod
    def _finding(severity, category, sku, product, message):
        return {
            "severity": severity,
            "category": category,
            "sku": sku or False,
            "product_id": product.id if product else False,
            "message": message,
        }

    # ------------------------------------------------------------------
    # 6. Go live
    # ------------------------------------------------------------------
    def _build_issues(self):
        self.issue_line_ids.unlink()
        rows = []
        labels = dict(STEPS)
        for step in STEP_KEYS[:-1]:
            status = self["%s_status" % step]
            if status == "ok":
                continue
            message = (self["%s_message" % step] or "").splitlines()
            detail = message[0] if message else _("Not run yet.")
            severity = {"error": "error", "warning": "warning"}.get(status) or (
                "info" if step == "catalog" else "warning")
            rows.append({"step": step, "severity": severity, "message": "%s: %s" % (labels[step], detail)})
        unmapped = self.tax_line_ids.filtered(lambda line: line.accept and not line.tax_id)
        if unmapped and self.instance_id and not self.instance_id.sudo().auto_create_taxes:
            rows.append({"step": "mappings", "severity": "warning", "message": _(
                "%s tax rate(s) have no Odoo tax and automatic tax creation is off.", len(unmapped))})
        if self.instance_id and not (self.default_journal_id or self.instance_id.sudo().payment_journal_id):
            rows.append({"step": "mappings", "severity": "warning", "message": _(
                "No default payment journal: Magento payments cannot be registered.")})
        if not self.alert_user_ids:
            rows.append({"step": "golive", "severity": "info", "message": _(
                "Nobody is alerted on authentication errors and permanent sync failures.")})
        self.env["magento.setup.wizard.issue"].create([dict(row, wizard_id=self.id) for row in rows])

    def action_refresh_issues(self):
        self.ensure_one()
        self._check_manager()
        self._build_issues()
        return False

    def _golive_vals(self):
        return {
            "auto_sync_enabled": self.auto_sync_enabled,
            "auto_sync_frequency": self.auto_sync_frequency,
            "auto_sync_hour_interval": self.auto_sync_hour_interval or "1",
            "auto_sync_hour": self.auto_sync_hour,
            "auto_sync_products": self.auto_sync_products,
            "auto_sync_customers": self.auto_sync_customers,
            "auto_sync_categories": self.auto_sync_categories,
            "auto_sync_orders": self.auto_sync_orders,
            "run_syncs_in_background": self.run_syncs_in_background,
            "stock_push_enabled": self.stock_push_enabled,
            "stock_qty_basis": self.stock_qty_basis,
            "alert_user_ids": [(6, 0, self.alert_user_ids.ids)],
        }

    def action_apply_golive(self):
        self.ensure_one()
        self._check_manager()
        if not self._require_instance("golive"):
            return False
        instance = self.instance_id
        try:
            with self.env.cr.savepoint():
                instance.write(self._golive_vals())
                instance.sudo()._magento_create_default_order_workflows()
        except (UserError, ValidationError) as exc:
            self._set_status("golive", "error", str(exc.args[0] if exc.args else exc))
            return False
        message = _("Settings saved on %s.", instance.name)
        if self.auto_sync_enabled:
            message += " " + _("Automatic sync is on.")
        self._set_status("golive", "ok", message)
        self._build_issues()
        return False

    def action_run_first_sync(self):
        """Save the go-live settings and queue categories, products, customers, orders."""
        self.ensure_one()
        self.action_apply_golive()
        if self.golive_status == "error":
            return False
        instance = self.instance_id.with_context(magento_sync_queue=True)
        messages = []
        for method in ("action_sync_categories", "action_sync_products", "action_sync_customers",
                       "action_sync_orders"):
            try:
                result = getattr(instance, method)() or {}
            except Exception as exc:  # noqa: BLE001 - shown on the step
                messages.append(instance._format_magento_request_error(exc, instance))
                continue
            message = ((result.get("params") or {}).get("message") or "").strip()
            if message:
                messages.append(message)
        self.first_sync_queued = True
        self._set_status("golive", "ok", "\n".join(
            [_("First sync started: categories, products, customers, then orders.")] + messages))
        return False

    def action_open_console(self):
        self.ensure_one()
        if self.instance_id and self.golive_status != "ok":
            self.action_apply_golive()
        return {"type": "ir.actions.client", "tag": "sdlc_magento_dashboard", "name": _("Magento Console")}


class MagentoSetupWizardStore(models.TransientModel):
    _name = "magento.setup.wizard.store"
    _description = "Magento Setup Wizard: Store View"
    _order = "website_magento_id, store_magento_id"
    _rec_name = "store_name"

    wizard_id = fields.Many2one("magento.setup.wizard", required=True, ondelete="cascade")
    website_magento_id = fields.Integer(string="Website ID")
    website_code = fields.Char(string="Website Code")
    website_name = fields.Char(string="Website")
    store_magento_id = fields.Integer(string="Store ID")
    store_code = fields.Char(string="Store Code")
    store_name = fields.Char(string="Store View")
    is_active = fields.Boolean(string="Active", default=True)
    is_selected = fields.Boolean(string="Selected", compute="_compute_is_selected")

    @api.depends("wizard_id.store_choice_id")
    def _compute_is_selected(self):
        for line in self:
            line.is_selected = line.wizard_id.store_choice_id == line

    def action_select(self):
        self.ensure_one()
        wizard = self.wizard_id
        wizard._check_manager()
        notes = [_("Store view chosen.")]
        if not wizard._discovery_available():
            notes.append(_STORE_DISCOVERY_NOTE)
        wizard._apply_store(self, notes)
        return False


class MagentoSetupWizardMapping(models.TransientModel):
    _name = "magento.setup.wizard.mapping"
    _description = "Magento Setup Wizard: Mapping Proposal"
    _order = "kind, seen_count desc, code"
    _rec_name = "code"

    wizard_id = fields.Many2one("magento.setup.wizard", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="wizard_id.company_id")
    kind = fields.Selection(
        [("payment", "Payment Method"), ("carrier", "Shipping Method"), ("tax", "Tax Rate"),
         ("group", "Customer Group")], required=True)
    accept = fields.Boolean(string="Map", default=True)
    code = fields.Char(string="Magento Code")
    label = fields.Char(string="Magento Title")
    magento_ref = fields.Integer(string="Magento ID")
    seen_count = fields.Integer(string="Recent Orders")
    existing = fields.Boolean(string="Already Mapped", readonly=True)
    journal_id = fields.Many2one(
        "account.journal", string="Journal",
        domain="[('type', 'in', ('bank', 'cash', 'credit')), ('company_id', '=', company_id)]")
    carrier_ref = fields.Reference(selection="_selection_carrier_model", string="Odoo Carrier")
    percent = fields.Float(digits=(16, 4))
    price_include = fields.Boolean(string="Tax Included")
    tax_id = fields.Many2one(
        "account.tax", string="Odoo Tax",
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]")
    tax_class_id = fields.Integer()
    from_magento = fields.Boolean()
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist",
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")

    @api.model
    def _selection_carrier_model(self):
        return self.env["magento.carrier.map"]._selection_carrier_model()


class MagentoSetupWizardCheck(models.TransientModel):
    _name = "magento.setup.wizard.check"
    _description = "Magento Setup Wizard: Catalog Finding"
    _order = "id"

    wizard_id = fields.Many2one("magento.setup.wizard", required=True, ondelete="cascade")
    severity = fields.Selection([("error", "Error"), ("warning", "Warning"), ("info", "Info")], required=True)
    category = fields.Selection([
        ("duplicate_sku", "Duplicate default code"),
        ("magento_duplicate", "Duplicate SKU in Magento"),
        ("would_duplicate", "Would create a duplicate"),
        ("barcode", "Barcode collision"),
        ("no_price", "No price"),
        ("type", "Product types"),
        ("new", "New product"),
    ], required=True)
    sku = fields.Char(string="Magento SKU")
    product_id = fields.Many2one("product.product", string="Odoo Product")
    message = fields.Char(required=True)


class MagentoSetupWizardIssue(models.TransientModel):
    _name = "magento.setup.wizard.issue"
    _description = "Magento Setup Wizard: Known Issue"
    _order = "id"

    wizard_id = fields.Many2one("magento.setup.wizard", required=True, ondelete="cascade")
    step = fields.Selection(STEPS, required=True)
    severity = fields.Selection([("error", "Error"), ("warning", "Warning"), ("info", "Info")], required=True)
    message = fields.Char(required=True)

    def action_fix(self):
        self.ensure_one()
        self.wizard_id.with_context(setup_step=self.step).action_goto_step()
        return False


class MagentoDashboard(models.AbstractModel):
    """Console navigation: a "Connect a store" link for administrators."""
    _inherit = "magento.dashboard"

    NAV_LINKS = [
        link for entry in _BaseDashboard.NAV_LINKS
        for link in (
            ((WIZARD_ACTION, "Connect a store", "Setup wizard: URL, credentials, stores, mappings, "
              "catalog check, go live", "fa-magic", "Configuration & logs"), entry)
            if entry[0] == "sdlc_magento_connector.action_magento_instance" else (entry,)
        )
    ]
