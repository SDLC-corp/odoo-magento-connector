"""Adobe Commerce as a Cloud Service (ACCS, roadmap 3.9).

ACCS is Adobe's SaaS edition of Commerce. Compared with Magento Open Source and Adobe
Commerce PaaS (on-premise / "on Cloud infrastructure") its REST API differs in three ways
the connector handles:

* **URLs**: ``https://<server>.api.commerce.adobe.com/<tenant-id>/V1/<endpoint>``, no
  ``/rest``, no store code in the path; the store scope is the ``Store`` HTTP header.
  Async bulk is ``/<tenant-id>/V1/async/bulk/<route>``. ``MagentoAPI._request`` rewrites
  every PaaS path when ``platform == 'accs'`` (``services/magento_api.py``).
* **Auth**: only Adobe IMS OAuth 2 server-to-server credentials (``auth_method = 'ims'``,
  ``models/magento_instance_auth.py``, ``services/magento_auth.py``).
* **Fewer endpoints**: the guest and customer REST APIs are not available (Adobe points to
  GraphQL), nor the bulk form of ``POST /V1/customers``; ACCS runs no custom PHP module, so
  the free SDLC_OdooConnector companion module cannot be installed. Features that need
  them show "not available on ACCS" (``ACCS_CAPABILITIES``) and their API calls raise
  ``MagentoCapabilityError`` before any request.

Sources:
  https://developer.adobe.com/commerce/webapi/rest/  (SaaS section: URLs, Store header,
  "customer and guest REST APIs are not available")
  https://developer.adobe.com/commerce/services/cloud/guides/rest/authentication
  https://experienceleague.adobe.com/en/docs/commerce-learn/tutorials/getting-started/commerce-as-a-cloud-service/webapi-rest/customer
  https://developer.adobe.com/commerce/webapi/rest/use-rest/bulk-endpoints/

The local test store is Magento Open Source 2.4.9, not ACCS: everything here is validated
against Adobe's documented API with simulated responses only, never against a live tenant.
The default platform (``open_source_paas``) behaves exactly as before.
"""
from markupsafe import Markup
from requests.compat import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.magento_api import PLATFORM_ACCS
# The setup wizard model is defined in ``wizard/``, which the module imports after
# ``models/``. Import it first so its class exists before the ACCS inherit below.
from ..wizard import magento_setup_wizard  # noqa: F401

ACCS_NOTICE = (
    "Adobe Commerce as a Cloud Service support is validated against Adobe's documented API with "
    "simulated responses; it has not yet been tested against a live ACCS tenant."
)
ACCS_HOST_SUFFIX = ".api.commerce.adobe.com"

# (key, feature, available on ACCS, explanation)
ACCS_CAPABILITIES = (
    ("admin_rest", "Catalog, customers, orders, invoices, shipments, credit memos and stock (admin REST)",
     True, "Same /V1 endpoints, tenant URL and Store header."),
    ("async_bulk", "Async bulk API", True, "Sent to /<tenant>/V1/async/bulk/<route>."),
    ("guest_cart", "Export Odoo orders to Magento",
     False, "Uses the guest cart REST API (/V1/guest-carts), which ACCS does not offer (GraphQL only)."),
    ("customer_self", "Customer self-service REST (/V1/customers/me, /V1/carts/mine)",
     False, "The customer REST API is not available on ACCS."),
    ("bulk_customers", "Bulk / async customer creation",
     False, "ACCS does not support the bulk or async form of POST /V1/customers."),
    ("companion_module", "Real-time push through the free SDLC_OdooConnector Magento module",
     False, "ACCS runs no custom PHP modules; use scheduled sync (or Adobe I/O Events)."),
)


class MagentoInstanceAccs(models.Model):
    _inherit = "magento.instance"

    platform = fields.Selection(
        [("open_source_paas", "Magento Open Source / Adobe Commerce (PaaS, on-premise)"),
         ("accs", "Adobe Commerce as a Cloud Service (SaaS)")],
        string="Platform", default="open_source_paas", required=True,
        help="Adobe Commerce as a Cloud Service uses tenant URLs without /rest, Adobe IMS "
             "authentication and a smaller REST API.",
    )
    accs_tenant_id = fields.Char(
        string="Tenant ID", copy=False,
        help="Last part of the ACCS REST endpoint, e.g. AbCYab34cdEfGHiJ27123 in "
             "https://na1-sandbox.api.commerce.adobe.com/AbCYab34cdEfGHiJ27123.",
    )
    accs_capability_html = fields.Html(
        string="Platform Capabilities", compute="_compute_accs_capability_html", sanitize=False,
    )

    @api.depends("platform")
    def _compute_accs_capability_html(self):
        for instance in self:
            if instance.platform != PLATFORM_ACCS:
                instance.accs_capability_html = False
                continue
            body = Markup()
            for _key, label, available, note in ACCS_CAPABILITIES:
                badge = (Markup('<span class="badge text-bg-success">Available</span>') if available
                         else Markup('<span class="badge text-bg-secondary">Not available on ACCS</span>'))
                body += Markup('<tr><td>%s</td><td>%s</td><td class="text-muted">%s</td></tr>') % (
                    label, badge, note)
            instance.accs_capability_html = Markup(
                '<table class="table table-sm align-middle mb-0"><thead><tr>'
                '<th>Feature</th><th>Status</th><th>Why</th></tr></thead><tbody>%s</tbody></table>'
            ) % body

    @api.constrains("platform", "auth_method", "accs_tenant_id")
    def _check_accs_platform(self):
        for instance in self:
            if instance.platform == PLATFORM_ACCS:
                if instance.auth_method != "ims":
                    raise ValidationError(_(
                        "Adobe Commerce as a Cloud Service only accepts Adobe IMS authentication: "
                        "set Authentication to Adobe IMS on %s.", instance.name))
                if not (instance.accs_tenant_id or "").strip():
                    raise ValidationError(_(
                        "Adobe Commerce as a Cloud Service needs the tenant ID on %s.", instance.name))
            elif instance.auth_method == "ims":
                raise ValidationError(_(
                    "Adobe IMS authentication is for Adobe Commerce as a Cloud Service: set the "
                    "platform of %s to ACCS, or use OAuth 1.0a.", instance.name))

    @api.onchange("platform")
    def _onchange_platform_accs(self):
        if self.platform == PLATFORM_ACCS:
            self.auth_method = "ims"
        elif self.auth_method == "ims":
            self.auth_method = "oauth1"

    def write(self, vals):
        if "accs_tenant_id" in vals and isinstance(vals["accs_tenant_id"], str):
            vals = dict(vals, accs_tenant_id=vals["accs_tenant_id"].strip().strip("/"))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if isinstance(vals.get("accs_tenant_id"), str):
                vals["accs_tenant_id"] = vals["accs_tenant_id"].strip().strip("/")
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------
    def _magento_capability(self, key):
        """``(available, message)`` of feature ``key`` on this instance's platform."""
        self.ensure_one()
        if self.platform != PLATFORM_ACCS:
            return True, ""
        for cap_key, label, available, note in ACCS_CAPABILITIES:
            if cap_key == key:
                if available:
                    return True, ""
                return False, _("%(feature)s is not available on Adobe Commerce as a Cloud Service: %(why)s",
                                feature=label, why=note)
        return True, ""

    def _magento_require_capability(self, key):
        self.ensure_one()
        available, message = self._magento_capability(key)
        if not available:
            raise UserError(message)

    def _magento_access_probes(self):
        probes = super()._magento_access_probes()
        if self.platform == PLATFORM_ACCS:
            for key, label, available, note in ACCS_CAPABILITIES:
                if not available and key in ("guest_cart", "customer_self"):
                    probes.append({
                        "label": label, "endpoint": "", "params": None, "resource": "-",
                        "optional": True, "missing_detail": "", "unavailable": note,
                    })
        return probes


class SaleOrderAccs(models.Model):
    _inherit = "sale.order"

    def action_create_magento_order(self):
        """Order export uses Magento's guest cart REST API, absent on ACCS: say so up front."""
        for order in self:
            instance = order.magento_instance_id
            if instance and instance.platform == PLATFORM_ACCS:
                instance._magento_require_capability("guest_cart")
        return super().action_create_magento_order()


class MagentoSetupWizardAccs(models.TransientModel):
    _inherit = "magento.setup.wizard"

    @api.model
    def _probe_store(self, base_url, verify):
        """The wizard connects Open Source / PaaS stores (OAuth 1.0a integrations). An ACCS
        REST host gets a clear pointer instead of a misleading /rest probe failure."""
        host = (urlparse(base_url).hostname or "").lower()
        if host.endswith(ACCS_HOST_SUFFIX):
            message = _(
                "This is an Adobe Commerce as a Cloud Service endpoint. ACCS uses Adobe IMS "
                "credentials and tenant URLs, which this wizard does not set up: create the "
                "instance in Configuration > Instances, choose Platform 'Adobe Commerce as a Cloud "
                "Service', enter the tenant ID and the IMS client ID / secret, then use Diagnose "
                "Access. %s", ACCS_NOTICE,
            )
            return "error", message, None, False
        return super()._probe_store(base_url, verify)
