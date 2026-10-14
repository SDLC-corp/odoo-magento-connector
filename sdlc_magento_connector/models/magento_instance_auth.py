"""Magento authentication settings (OAuth 1.0a / legacy Bearer / Adobe IMS) and access
diagnostics.

Adobe IMS (``auth_method = 'ims'``) is the only method Adobe Commerce as a Cloud Service
accepts: a server-to-server credential (client id + secret) exchanged at
``https://ims-na1.adobelogin.com/ims/token/v3`` for a bearer token, cached here with its
expiry and refreshed before it expires (and once on a 401). See
https://developer.adobe.com/commerce/services/cloud/guides/rest/authentication .
Validated against Adobe's documented payloads with simulated responses only; not yet
tested against a live ACCS tenant.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.sql import column_exists, create_column, table_exists

from ..services import magento_auth
from ..services.magento_api import MagentoAPI, MagentoCapabilityError
from . import sql_compat

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
# ``access_token`` is required on every instance; an IMS instance has no integration token,
# so it stores this marker there (the IMS bearer token lives in ``ims_access_token``).
IMS_ACCESS_TOKEN_PLACEHOLDER = "adobe-ims"

# (label, endpoint below /rest/V1, query params, Magento ACL resource, optional)
ACCESS_PROBES = (
    ("Stores / websites", "/store/websites", None, "Magento_Backend::store", False),
    ("Products", "/products", {"searchCriteria[pageSize]": 1}, "Magento_Catalog::products", False),
    ("Categories", "/categories", None, "Magento_Catalog::categories", False),
    ("Customers", "/customers/search", {"searchCriteria[pageSize]": 1}, "Magento_Customer::customer", False),
    ("Orders", "/orders", {"searchCriteria[pageSize]": 1}, "Magento_Sales::sales", False),
    ("Invoices", "/invoices", {"searchCriteria[pageSize]": 1}, "Magento_Sales::sales_invoice", False),
    ("Shipments", "/shipments", {"searchCriteria[pageSize]": 1}, "Magento_Sales::shipment", False),
    ("Credit memos", "/creditmemos", {"searchCriteria[pageSize]": 1}, "Magento_Sales::sales_creditmemo", False),
    ("Inventory sources (MSI)", "/inventory/sources", {"searchCriteria[pageSize]": 1},
     "Magento_InventoryApi::source", True),
)

BEARER_DISABLED_HINT = (
    "Magento rejected the Bearer token (\"The consumer isn't authorized\"). Since Magento 2.4.4 "
    "integration tokens are no longer accepted as Bearer tokens by default. Switch this instance "
    "to OAuth 1.0a (recommended) or, on Magento, enable "
    "Stores > Configuration > Services > OAuth > Consumer Settings > "
    "\"Allow OAuth Access Tokens to be used as standalone Bearer tokens\"."
)

_BADGES = {
    "ok": ("text-bg-success", "OK"),
    "missing": ("text-bg-danger", "Missing"),
    "not_installed": ("text-bg-warning", "Not installed"),
    "error": ("text-bg-danger", "Error"),
    "unavailable": ("text-bg-secondary", "Not on this platform"),
}


class MagentoInstanceAuth(models.Model):
    _inherit = "magento.instance"

    auth_method = fields.Selection(
        [
            ("oauth1", "OAuth 1.0a (recommended)"),
            ("bearer", "Bearer token (legacy)"),
            ("ims", "Adobe IMS (Commerce as a Cloud Service)"),
        ],
        string="Authentication",
        default="oauth1",
        help=(
            "OAuth 1.0a signs every request with the integration's four credentials and works on "
            "Magento 2.4.4+ out of the box. Bearer sends the access token alone; Magento 2.4.4+ "
            "rejects it unless 'Allow OAuth Access Tokens to be used as standalone Bearer tokens' "
            "is enabled. Adobe IMS (OAuth 2 server-to-server) is the only method Adobe Commerce "
            "as a Cloud Service accepts."
        ),
    )
    # Adobe IMS server-to-server credential (Adobe Developer Console). Secrets: managers only.
    ims_client_id = fields.Char(string="IMS Client ID", groups=MANAGER_GROUP, copy=False)
    ims_client_secret = fields.Char(string="IMS Client Secret", groups=MANAGER_GROUP, copy=False)
    ims_org_id = fields.Char(
        string="IMS Organization ID", groups=MANAGER_GROUP, copy=False,
        help="Organization ID of the Developer Console project (…@AdobeOrg). Kept for reference: "
             "Adobe's documented ACCS REST calls only send the bearer token.",
    )
    ims_scopes = fields.Char(
        string="IMS Scopes", default=magento_auth.IMS_DEFAULT_SCOPES, groups=MANAGER_GROUP,
        help="Comma-separated scopes requested with the token. commerce.accs is required.",
    )
    ims_token_url = fields.Char(
        string="IMS Token URL", default=magento_auth.IMS_TOKEN_URL, groups=MANAGER_GROUP,
    )
    ims_access_token = fields.Char(
        string="IMS Access Token", groups=MANAGER_GROUP, copy=False, readonly=True,
        help="Cached bearer token; fetched automatically and refreshed before it expires.",
    )
    ims_token_expires_at = fields.Datetime(
        string="IMS Token Expires", groups=MANAGER_GROUP, copy=False, readonly=True,
    )
    consumer_key = fields.Char(groups=MANAGER_GROUP, copy=False)
    consumer_secret = fields.Char(groups=MANAGER_GROUP, copy=False)
    access_token_secret = fields.Char(groups=MANAGER_GROUP, copy=False)
    oauth_signature_method = fields.Selection(
        [("sha256", "HMAC-SHA256"), ("sha1", "HMAC-SHA1")],
        string="Signature Method",
        default="sha256",
        groups=MANAGER_GROUP,
    )
    access_diagnostics = fields.Html(readonly=True, sanitize=True, copy=False)

    # ------------------------------------------------------------------
    # Upgrade: instances that existed before OAuth support keep Bearer.
    # ------------------------------------------------------------------
    def _auto_init(self):
        self._keep_existing_instances_on_bearer()
        return super()._auto_init()

    def _keep_existing_instances_on_bearer(self):
        """Create ``auth_method`` ourselves so the ORM does not back-fill the "oauth1"
        default on existing rows (they only have a Bearer token)."""
        cr = self.env.cr
        if table_exists(cr, self._table) and not column_exists(cr, self._table, "auth_method"):
            create_column(cr, self._table, "auth_method", "varchar")
            sql_compat.execute(cr, f'UPDATE "{self._table}" SET auth_method = %s', ["bearer"])
            _logger.info("magento_instance: %s existing instance(s) kept on Bearer auth", cr.rowcount)

    def init(self):
        super().init()
        sql_compat.execute(self.env.cr, 
            f'UPDATE "{self._table}" SET auth_method = %s WHERE auth_method IS NULL', ["bearer"]
        )

    _STRIPPED_SECRETS = ("consumer_key", "consumer_secret", "access_token_secret",
                         "ims_client_id", "ims_client_secret")
    # Changing one of these drops the cached IMS token.
    _IMS_CREDENTIAL_FIELDS = frozenset({"ims_client_id", "ims_client_secret", "ims_scopes",
                                        "ims_token_url", "auth_method"})

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Programmatic creates that only pass a token (pre-OAuth callers) stay on Bearer;
            # the form always sends auth_method, so new instances get OAuth 1.0a.
            if "auth_method" not in vals and vals.get("access_token") and not vals.get("consumer_key"):
                vals["auth_method"] = "bearer"
            if vals.get("auth_method") == "ims" and not (vals.get("access_token") or "").strip():
                vals["access_token"] = IMS_ACCESS_TOKEN_PLACEHOLDER
            for name in self._STRIPPED_SECRETS:
                if isinstance(vals.get(name), str):
                    vals[name] = vals[name].strip()
        return super().create(vals_list)

    def write(self, vals):
        for name in self._STRIPPED_SECRETS:
            if isinstance(vals.get(name), str):
                vals = dict(vals, **{name: vals[name].strip()})
        if "access_token" in vals and not (vals["access_token"] or "").strip() and (
            vals.get("auth_method") == "ims"
            or ("auth_method" not in vals and self and all(i.auth_method == "ims" for i in self))
        ):
            vals = dict(vals, access_token=IMS_ACCESS_TOKEN_PLACEHOLDER)
        elif vals.get("auth_method") == "ims" and "access_token" not in vals:
            vals = dict(vals, access_token=IMS_ACCESS_TOKEN_PLACEHOLDER)
        if self._IMS_CREDENTIAL_FIELDS.intersection(vals) and "ims_access_token" not in vals:
            vals = dict(vals, ims_access_token=False, ims_token_expires_at=False)
        return super().write(vals)

    @api.constrains("auth_method", "ims_client_id", "ims_client_secret")
    def _check_ims_credentials(self):
        for instance in self.sudo():
            if instance.auth_method != "ims":
                continue
            missing = [label for name, label in (("ims_client_id", "IMS Client ID"),
                                                 ("ims_client_secret", "IMS Client Secret"))
                       if not (instance[name] or "").strip()]
            if missing:
                raise ValidationError(_(
                    "Adobe IMS needs the server-to-server credential of your Adobe Developer Console "
                    "project. Missing on %(name)s: %(fields)s.",
                    name=instance.name, fields=", ".join(missing),
                ))

    # ------------------------------------------------------------------
    # Adobe IMS token cache (auth_method 'ims')
    # ------------------------------------------------------------------
    @staticmethod
    def _ims_epoch(value):
        """Naive UTC Datetime -> epoch seconds."""
        return value.replace(tzinfo=timezone.utc).timestamp() if value else 0.0

    def _magento_ims_token(self, force_refresh=False):
        """Bearer token for Adobe IMS: the cached one while it has more than
        ``IMS_REFRESH_MARGIN`` seconds left, else a new one from the IMS token endpoint
        (``grant_type=client_credentials``), cached with its ``expires_in``.

        ``force_refresh`` (used after a 401) always fetches a new token. Raises a
        ``UserError`` with Adobe's message when IMS refuses the credential."""
        self.ensure_one()
        instance = self.sudo()
        if instance.auth_method != "ims":
            return ""
        now = fields.Datetime.now()
        if (not force_refresh and instance.ims_access_token
                and magento_auth.ims_token_is_fresh(self._ims_epoch(instance.ims_token_expires_at),
                                                    now=self._ims_epoch(now))):
            return instance.ims_access_token
        try:
            token = magento_auth.fetch_ims_token(
                instance.ims_client_id, instance.ims_client_secret, instance.ims_scopes,
                token_url=instance.ims_token_url or magento_auth.IMS_TOKEN_URL,
                verify=bool(instance.verify_ssl), now=self._ims_epoch(now),
            )
        except magento_auth.ImsTokenError as exc:
            raise UserError(_("Adobe IMS token for %(name)s: %(error)s", name=instance.name, error=exc)) from exc
        expires_at = datetime.fromtimestamp(token["expires_at"], tz=timezone.utc).replace(tzinfo=None)
        instance.with_context(magento_ims_cache=True).write({
            "ims_access_token": token["access_token"],
            "ims_token_expires_at": expires_at.replace(microsecond=0),
        })
        _logger.info("Magento instance %s: Adobe IMS token fetched, valid until %s UTC",
                     instance.id, expires_at.replace(microsecond=0))
        return token["access_token"]

    def action_ims_fetch_token(self):
        """Fetch a fresh IMS token now (managers): checks the Developer Console credential."""
        self.ensure_one()
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can fetch the Adobe IMS token."))
        if self.auth_method != "ims":
            raise UserError(_("This instance does not use Adobe IMS authentication."))
        self._magento_ims_token(force_refresh=True)
        expires = self.sudo().ims_token_expires_at
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Adobe IMS"),
                "message": _("Token received, valid until %s UTC.", fields.Datetime.to_string(expires)),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    @api.constrains("auth_method", "consumer_key", "consumer_secret", "access_token", "access_token_secret")
    def _check_oauth_credentials(self):
        for instance in self.sudo():
            if instance.auth_method != "oauth1":
                continue
            missing = [
                label for name, label in (
                    ("consumer_key", "Consumer Key"),
                    ("consumer_secret", "Consumer Secret"),
                    ("access_token", "Access Token"),
                    ("access_token_secret", "Access Token Secret"),
                )
                if not (instance[name] or "").strip()
            ]
            if missing:
                raise ValidationError(_(
                    "OAuth 1.0a needs all four integration credentials. Missing on %(name)s: %(fields)s. "
                    "Find them in Magento Admin > System > Extensions > Integrations.",
                    name=instance.name, fields=", ".join(missing),
                ))

    # ------------------------------------------------------------------
    # Access diagnostics
    # ------------------------------------------------------------------
    @staticmethod
    def _magento_error_message(response):
        """Readable Magento error text, with %placeholders filled from ``parameters``."""
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:300], {}
        if not isinstance(body, dict):
            return str(body)[:300], {}
        message = str(body.get("message") or "")
        parameters = body.get("parameters") or {}
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                message = message.replace("%" + str(key), str(value))
        elif isinstance(parameters, list):
            for idx, value in enumerate(parameters, start=1):
                message = message.replace("%" + str(idx), str(value))
        return message[:300], parameters if isinstance(parameters, dict) else {}

    def _magento_access_probes(self):
        """Endpoints the diagnosis checks, as dicts: ``label``, ``endpoint`` (below
        ``/rest/V1``), ``params``, ``resource`` (Magento ACL), ``optional``,
        ``missing_detail`` (shown on a 404 of an optional endpoint) and ``unavailable``
        (a reason: the platform does not offer it, the row is shown but not probed).
        Extended by the ACCS and B2B modules of the connector."""
        self.ensure_one()
        return [
            {
                "label": label, "endpoint": endpoint, "params": params, "resource": resource,
                "optional": optional, "unavailable": False,
                "missing_detail": "Magento MSI (Inventory) modules are not installed." if optional else "",
            }
            for label, endpoint, params, resource, optional in ACCESS_PROBES
        ]

    def _run_access_probes(self):
        """Probe each read endpoint once (no retries). Returns (rows, connection_error)."""
        self.ensure_one()
        api = MagentoAPI(self)
        api.MAX_RETRIES = 0  # one shot per endpoint: a diagnosis must be quick
        rows = []
        if (self.auth_method or "bearer") == "ims":
            # Adobe IMS: the token is the first thing that can fail.
            try:
                self._magento_ims_token(force_refresh=True)
            except (UserError, requests.exceptions.RequestException) as exc:
                return rows, exc
        for probe in self._magento_access_probes():
            label, endpoint, params = probe["label"], probe["endpoint"], probe["params"]
            resource, optional = probe["resource"], probe["optional"]
            row = {"label": label, "resource": resource, "optional": optional, "detail": "", "http": None}
            if probe.get("unavailable"):
                row.update(status="unavailable", detail=probe["unavailable"])
                rows.append(row)
                continue
            try:
                api._request("get", "/rest/V1" + endpoint, params=dict(params) if params else None)
                row["status"] = "ok"
            except MagentoCapabilityError as exc:
                row.update(status="unavailable", detail=str(exc.args[0] if exc.args else exc))
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                status = response.status_code if response is not None else 0
                message, parameters = self._magento_error_message(response) if response is not None else ("", {})
                row["http"] = status
                row["detail"] = message
                if status in (401, 403):
                    row["status"] = "missing"
                    reported = parameters.get("resources") if isinstance(parameters, dict) else None
                    if reported:
                        row["resource"] = str(reported)
                elif status == 404 and optional:
                    row["status"] = "not_installed"
                    row["detail"] = probe.get("missing_detail") or "Not installed on this Magento."
                else:
                    row["status"] = "error"
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
                    requests.exceptions.InvalidURL) as exc:
                return rows, exc
            rows.append(row)
        return rows, None

    def _render_access_diagnostics(self, rows, hint=None, connection_message=None):
        checked = fields.Datetime.to_string(fields.Datetime.now())
        auth_label = dict(self._fields["auth_method"].selection).get(self.auth_method or "bearer")
        parts = [Markup('<p class="text-muted small mb-2">Checked %s with %s.</p>') % (checked, auth_label)]
        if connection_message:
            parts.append(Markup('<div class="alert alert-danger mb-2" role="alert">%s</div>') % connection_message)
        if hint:
            parts.append(Markup('<div class="alert alert-warning mb-2" role="alert">%s</div>') % hint)
        if rows:
            body = Markup()
            for row in rows:
                css, text = _BADGES[row["status"]]
                detail = row["detail"]
                if row["http"]:
                    detail = "HTTP %s%s" % (row["http"], (": " + detail) if detail else "")
                body += Markup(
                    '<tr><td>%s</td><td><code>%s</code></td>'
                    '<td><span class="badge %s">%s</span></td><td class="text-muted">%s</td></tr>'
                ) % (row["label"], row["resource"], css, text, detail)
            parts.append(Markup(
                '<table class="table table-sm align-middle mb-0"><thead><tr>'
                '<th>Endpoint</th><th>Magento API resource</th><th>Status</th><th>Details</th>'
                '</tr></thead><tbody>%s</tbody></table>'
            ) % body)
        return Markup().join(parts)

    def action_diagnose_access(self):
        self.ensure_one()
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can diagnose API access."))
        rows, connection_exc = self._run_access_probes()
        auth_method = self.auth_method or "bearer"

        hint = None
        if auth_method == "bearer" and any(
            row["http"] == 401 and "consumer isn't authorized" in (row["detail"] or "").lower()
            for row in rows
        ):
            hint = BEARER_DISABLED_HINT

        if connection_exc is not None:
            message = self._format_magento_request_error(connection_exc, self)
            state, notif_type = "error", "danger"
            summary = message
        else:
            missing = [row["resource"] for row in rows if row["status"] == "missing"]
            errors = [row for row in rows if row["status"] == "error"]
            if missing:
                state, notif_type = "error", "danger"
                message = "Missing Magento API resources: %s." % ", ".join(missing)
                if hint:
                    message += " " + hint
            elif errors:
                state, notif_type = "error", "warning"
                message = "Access check failed for: %s." % ", ".join(
                    "%s (HTTP %s)" % (row["label"], row["http"]) for row in errors)
            else:
                state, notif_type = "connected", "success"
                message = "All required Magento API resources are granted (%s)." % fields.Datetime.to_string(
                    fields.Datetime.now())
            summary = message

        html = self._render_access_diagnostics(
            rows, hint=hint, connection_message=summary if connection_exc is not None else None)
        self.sudo().write({
            "state": state,
            "last_connection_message": summary[:1000],
            "access_diagnostics": html,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento Access Diagnosis"),
                "message": summary[:500],
                "type": notif_type,
                "sticky": notif_type != "success",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }
