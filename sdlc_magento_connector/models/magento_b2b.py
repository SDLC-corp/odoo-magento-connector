"""Adobe Commerce B2B (roadmap 3.6): companies, shared catalogs, negotiable quotes.

Opt-in per instance (``b2b_enabled``) and only meaningful on Adobe Commerce with the B2B
modules installed. While it is off nothing here sends a request: the job methods return
at once, the order import hook does not even query, and the B2B menus are hidden.
**Detect B2B** probes ``GET /V1/company`` and reports whether B2B is present (Magento Open
Source answers 404).

Companies <-> contacts
    ``GET /V1/company`` -> ``res.partner`` companies bound by ``(magento_company_instance_id,
    magento_company_id)``: name, legal name, VAT / tax id, reseller id, e-mail, phone,
    address, status, customer group and (informational) the company credit of
    ``GET /V1/companyCredits/company/:id``. The company users of ``GET /V1/hierarchy/:id``
    (and the admin, ``super_user_id``) become child contacts, reusing the contacts already
    bound to those Magento customers. An Odoo company flagged "Sync to Magento" is created
    or updated with ``POST`` / ``PUT /V1/company``; Magento requires a company admin, an
    existing Magento customer: without one the export stops with a clear error.

Shared catalogs <-> pricelists
    ``GET /V1/sharedCatalog`` -> ``magento.shared.catalog``, each mapped to an Odoo pricelist.
    Import: the catalog's SKUs (``/products``) and the custom / tier prices of its customer
    group (``POST /V1/products/tier-prices-information``) become pricelist items owned by
    the catalog; its companies (``/companies``) get the catalog and the pricelist.
    Export: every mapped product a pricelist item applies to is assigned
    (``/assignProducts``) and gets a tier price for the catalog's customer group
    (``POST /V1/products/tier-prices``, quantity 1 = the catalog price); companies set on
    the catalog in Odoo are assigned (``/assignCompanies``).

Negotiable quotes <-> quotations (two-way)
    ``GET /V1/carts/:id`` (``extension_attributes.negotiable_quote``) -> a draft
    ``sale.order`` bound by ``magento_quote_id``: requested items, prices, the negotiated
    discount spread on the lines, status, expiration date, and the quote comments
    (``/negotiableQuote/:id/comments``) as chatter notes. **Send proposal** turns the Odoo
    prices into Magento's quote-level negotiated price (``PUT /V1/negotiableQuote/:id``:
    type 1 percentage when every line has the same discount, else type 3 proposed total)
    and submits it with a comment (``POST /V1/negotiableQuote/submitToCustomer``);
    **Decline** posts ``/V1/negotiableQuote/decline``. The buyer's changes come back on the
    next re-sync. When the buyer places the order, the regular order import links the
    Magento order to the quotation through ``quote_id`` and closes the quotation.

Payment terms
    Companies with a Magento credit limit get the instance's "Payment on Account" term
    (when they have none), and imported orders paid with ``companycredit`` get it too.

Everything that talks to Magento runs as a ``magento.job`` and records failures in the
Error Center (entities ``b2b_company`` / ``b2b_catalog`` / ``b2b_quote``).

Endpoints and payloads: https://developer.adobe.com/commerce/webapi/rest/b2b/integrations
and the pages cited in ``services/magento_api.py``. The local test store is Magento Open
Source 2.4.9 (no B2B): this is validated against Adobe's documented API with simulated
responses; it has not yet been tested against a live Adobe Commerce store.
"""
import html
import logging
from datetime import date

import requests
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_round

from ..services.magento_api import MagentoAPI, MagentoCapabilityError
from . import magento_sync_issue as _sync_issue_module
from .magento_job import classify_exception, exception_message

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
USER_GROUP = "sdlc_magento_connector.group_magento_user"
B2B_NOTICE = (
    "Requires Adobe Commerce with B2B. Validated against Adobe's documented API with simulated "
    "responses; not yet tested against a live Adobe Commerce store."
)
B2B_MENUS = (
    "sdlc_magento_connector.menu_magento_b2b",
    "sdlc_magento_connector.menu_magento_b2b_companies",
    "sdlc_magento_connector.menu_magento_b2b_shared_catalogs",
    "sdlc_magento_connector.menu_magento_b2b_quotes",
)
COMPANY_STATUSES = [("0", "Pending Approval"), ("1", "Approved"), ("2", "Rejected"), ("3", "Blocked")]
QUOTE_STATUSES = [
    ("created", "Created"),
    ("submitted_by_customer", "Submitted by Buyer"),
    ("processing_by_admin", "Seller Reviewing"),
    ("submitted_by_admin", "Sent to Buyer"),
    ("processing_by_customer", "Buyer Reviewing"),
    ("ordered", "Ordered"),
    ("expired", "Expired"),
    ("declined", "Declined"),
    ("closed", "Closed"),
]
# Statuses in which the seller may change the price and submit the quote back.
QUOTE_SELLER_TURN = frozenset({"created", "submitted_by_customer", "processing_by_admin"})
QUOTE_TERMINAL = frozenset({"ordered", "expired", "declined", "closed"})
NEGOTIATED_PRICE_TYPES = [("1", "Percentage Discount"), ("2", "Amount Discount"), ("3", "Proposed Total")]
B2B_ENTITIES = [("b2b_company", "B2B Company"), ("b2b_catalog", "Shared Catalog"), ("b2b_quote", "Negotiable Quote")]
# The Error Center keys issues by entity and maps an entity missing from its module-level
# ``ENTITIES`` list to "other" (``_record_failure``): register the B2B entities there too.
# (The field's selection is extended with ``selection_add`` below.)
for _key, _label in B2B_ENTITIES:
    if _key not in dict(_sync_issue_module.ENTITIES):
        _sync_issue_module.ENTITIES.append((_key, _label))
# Job methods replayed (not re-imported) by the Error Center retry.
B2B_REPLAYABLE = ("_magento_b2b_export_company", "_magento_b2b_export_catalog",
                  "_magento_b2b_send_proposal", "_magento_b2b_decline")
_CHUNK = 100
_NO_PUSH_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "mail_create_nolog": True,
                "tracking_disable": True}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chunks(items, size=_CHUNK):
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def _parse_date(value):
    """``2017-09-30`` / ``2017-09-30 12:00:00`` -> date, else False."""
    text = str(value or "").strip()[:10]
    if not text:
        return False
    try:
        return date.fromisoformat(text)
    except ValueError:
        return False


class MagentoInstanceB2B(models.Model):
    _inherit = "magento.instance"

    b2b_enabled = fields.Boolean(
        string="Adobe Commerce B2B", default=False,
        help="Sync B2B companies, shared catalogs and negotiable quotes. Needs Adobe Commerce with "
             "the B2B modules (Magento Open Source has no B2B). Off: nothing B2B is requested.",
    )
    b2b_status = fields.Selection(
        [("unknown", "Not checked"), ("present", "B2B available"), ("absent", "B2B not available"),
         ("error", "Check failed")],
        string="B2B Detection", default="unknown", readonly=True, copy=False,
    )
    b2b_status_message = fields.Char(string="B2B Detection Result", readonly=True, copy=False)
    b2b_checked_at = fields.Datetime(string="B2B Checked", readonly=True, copy=False)
    b2b_credit_payment_term_id = fields.Many2one(
        "account.payment.term", string="Payment on Account Term", ondelete="set null",
        help="Payment term given to imported B2B companies that have a Magento credit limit (when "
             "they have none yet), and to imported orders paid with Payment on Account (companycredit).",
    )
    b2b_quotes_watermark = fields.Datetime(string="Quotes Synced Until", readonly=True, copy=False)
    b2b_company_count = fields.Integer(compute="_compute_b2b_counts")
    b2b_catalog_count = fields.Integer(compute="_compute_b2b_counts")
    b2b_quote_count = fields.Integer(compute="_compute_b2b_counts")

    def _compute_b2b_counts(self):
        partners = self.env["res.partner"].sudo()
        catalogs = self.env["magento.shared.catalog"].sudo()
        orders = self.env["sale.order"].sudo()
        for instance in self:
            if not instance.id:
                instance.b2b_company_count = instance.b2b_catalog_count = instance.b2b_quote_count = 0
                continue
            instance.b2b_company_count = partners.search_count([("magento_company_instance_id", "=", instance.id)])
            instance.b2b_catalog_count = catalogs.search_count([("instance_id", "=", instance.id)])
            instance.b2b_quote_count = orders.search_count([("magento_quote_instance_id", "=", instance.id)])

    # ------------------------------------------------------------------
    # Menus follow the toggle: hidden while no instance has B2B on
    # ------------------------------------------------------------------
    @api.model
    def _magento_b2b_sync_menus(self):
        enabled = bool(self.sudo().search_count([("b2b_enabled", "=", True)]))
        for xmlid in B2B_MENUS:
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            if menu and menu.sudo().active != enabled:
                menu.sudo().active = enabled
        return enabled

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if any(vals.get("b2b_enabled") for vals in vals_list):
            self._magento_b2b_sync_menus()
        return records

    def write(self, vals):
        result = super().write(vals)
        if "b2b_enabled" in vals or ("active" in vals and any(self.mapped("b2b_enabled"))):
            self._magento_b2b_sync_menus()
        return result

    def unlink(self):
        had_b2b = any(self.mapped("b2b_enabled"))
        result = super().unlink()
        if had_b2b:
            self._magento_b2b_sync_menus()
        return result

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def _magento_b2b_detect(self):
        """``(status, message)`` of one probe of ``GET /V1/company`` (no retries)."""
        self.ensure_one()
        api_client = MagentoAPI(self.sudo())
        api_client.MAX_RETRIES = 0
        try:
            result = api_client.probe_b2b()
        except MagentoCapabilityError as exc:
            return "error", exception_message(exc)
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            detail = self._magento_error_message(exc.response)[0] if exc.response is not None else ""
            if status == 404:
                return "absent", _(
                    "B2B not available: Magento answered HTTP 404 for /V1/company (%(detail)s). The "
                    "Adobe Commerce B2B modules are not installed; Magento Open Source has no B2B.",
                    detail=detail or "no such route")
            if status in (401, 403):
                return "error", _(
                    "Magento refused /V1/company (HTTP %(status)s: %(detail)s). B2B may be installed but "
                    "the integration lacks the Magento_Company resources.", status=status, detail=detail)
            return "error", _("B2B check failed: HTTP %(status)s %(detail)s", status=status, detail=detail)
        except (requests.exceptions.RequestException, UserError) as exc:
            return "error", self._format_magento_request_error(exc, self)
        total = result.get("total_count") if isinstance(result, dict) else None
        return "present", _("Adobe Commerce B2B detected (%s compan(ies) in Magento).", total if total is not None else "?")

    def action_b2b_detect(self):
        self.ensure_one()
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can run the B2B detection."))
        status, message = self._magento_b2b_detect()
        self.sudo().write({
            "b2b_status": status,
            "b2b_status_message": message[:500],
            "b2b_checked_at": fields.Datetime.now(),
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Detect B2B"),
                "message": message[:500],
                "type": {"present": "success", "absent": "warning"}.get(status, "danger"),
                "sticky": status != "present",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _magento_access_probes(self):
        probes = super()._magento_access_probes()
        if self.b2b_enabled:
            missing = "Adobe Commerce B2B modules are not installed."
            probes += [
                {"label": "B2B companies", "endpoint": "/company", "params": {"searchCriteria[pageSize]": 1},
                 "resource": "Magento_Company::index", "optional": True, "missing_detail": missing,
                 "unavailable": False},
                {"label": "B2B shared catalogs", "endpoint": "/sharedCatalog",
                 "params": {"searchCriteria[pageSize]": 1}, "resource": "Magento_SharedCatalog::list",
                 "optional": True, "missing_detail": missing, "unavailable": False},
                {"label": "B2B company credit", "endpoint": "/companyCredits",
                 "params": {"searchCriteria[pageSize]": 1}, "resource": "Magento_CompanyCredit::credit",
                 "optional": True, "missing_detail": missing, "unavailable": False},
            ]
        return probes

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------
    def _magento_b2b_require_enabled(self):
        for instance in self:
            if not instance.b2b_enabled:
                raise UserError(_("Turn on Adobe Commerce B2B on %s first (Adobe Commerce tab).", instance.name))

    def _magento_b2b_enqueue(self, records, method, description, args=(), identity_key=None):
        self.ensure_one()
        return self.env["magento.job"].enqueue(
            records, method, args=args, instance=self, channel="b2b-%s" % self.id,
            identity_key=identity_key, description=description,
        )

    def _magento_b2b_queued(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Adobe Commerce B2B"), "message": message, "type": "info", "sticky": False},
        }

    def _magento_b2b_failure(self, entity, ref, exc, *, payload=None, record=None, retry_transient=True):
        """Record a failure in the Error Center, or re-raise a transient one so the job
        queue retries it with backoff (never for a create: it may have been applied)."""
        severity = classify_exception(exc)[0]
        if retry_transient and severity == "transient" and self.env.context.get("magento_job_id"):
            raise exc
        return self.env["magento.sync.issue"]._record_failure(
            self, entity, ref, exc, payload=payload, record=record)

    def action_b2b_import_companies(self):
        self.ensure_one()
        self._magento_b2b_require_enabled()
        self._magento_b2b_enqueue(self, "_magento_b2b_import_companies", _("Import B2B companies"),
                                  identity_key="b2b-companies-%s" % self.id)
        return self._magento_b2b_queued(_("Company import queued."))

    def action_b2b_import_shared_catalogs(self):
        self.ensure_one()
        self._magento_b2b_require_enabled()
        self._magento_b2b_enqueue(self, "_magento_b2b_import_shared_catalogs", _("Import shared catalogs"),
                                  identity_key="b2b-catalogs-%s" % self.id)
        return self._magento_b2b_queued(_("Shared catalog import queued."))

    def action_b2b_import_quotes(self):
        self.ensure_one()
        self._magento_b2b_require_enabled()
        self._magento_b2b_enqueue(self, "_magento_b2b_import_quotes", _("Import negotiable quotes"),
                                  identity_key="b2b-quotes-%s" % self.id)
        return self._magento_b2b_queued(_("Negotiable quote import queued."))

    def _magento_b2b_open(self, xmlid, domain):
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["domain"] = domain
        return action

    def action_view_b2b_companies(self):
        self.ensure_one()
        return self._magento_b2b_open("sdlc_magento_connector.action_magento_b2b_company",
                                      [("magento_company_instance_id", "=", self.id)])

    def action_view_b2b_catalogs(self):
        self.ensure_one()
        action = self._magento_b2b_open("sdlc_magento_connector.action_magento_shared_catalog",
                                        [("instance_id", "=", self.id)])
        action["context"] = {"default_instance_id": self.id}
        return action

    def action_view_b2b_quotes(self):
        self.ensure_one()
        return self._magento_b2b_open("sdlc_magento_connector.action_magento_negotiable_quote",
                                      [("magento_quote_instance_id", "=", self.id)])

    # ------------------------------------------------------------------
    # Companies (job methods)
    # ------------------------------------------------------------------
    def _magento_b2b_partners(self):
        return self.env["res.partner"].sudo().with_context(**_NO_PUSH_CTX)

    def _magento_b2b_import_companies(self):
        """Job: import every Magento company of the instance."""
        self.ensure_one()
        if not self.b2b_enabled:
            return "B2B is disabled on this instance: nothing imported."
        api_client = MagentoAPI(self.sudo())
        done = failed = 0
        try:
            for data in api_client.get_companies(lazy=True):
                if not isinstance(data, dict) or data.get("id") in (None, ""):
                    continue
                ref = str(data["id"])
                try:
                    with self.env.cr.savepoint():
                        self._magento_b2b_import_company_data(api_client, data)
                except Exception as exc:  # noqa: BLE001 - one company never stops the others
                    failed += 1
                    self._magento_b2b_failure("b2b_company", ref, exc, payload=data, retry_transient=False)
                else:
                    done += 1
                    self.env["magento.sync.issue"]._record_success(self, "b2b_company", ref)
        except requests.exceptions.RequestException as exc:
            self._magento_b2b_failure("b2b_company", "list", exc)
            return "Company list failed: %s" % exception_message(exc)
        self.env["magento.sync.issue"]._record_success(self, "b2b_company", "list")
        return "%s compan(ies) imported, %s failed." % (done, failed)

    def _magento_b2b_import_company(self, company_id):
        """Job / retry: import one Magento company by id."""
        self.ensure_one()
        if not self.b2b_enabled:
            return False
        api_client = MagentoAPI(self.sudo())
        data = api_client.get_company(company_id)
        partner = self._magento_b2b_import_company_data(api_client, data)
        self.env["magento.sync.issue"]._record_success(self, "b2b_company", str(company_id))
        return partner

    def _magento_b2b_company_vals(self, data, credit):
        self.ensure_one()
        street = data.get("street") or []
        if isinstance(street, str):
            street = [street]
        street = [s for s in street if s]
        country = self.env["res.country"]
        code = (data.get("country_id") or "").strip().upper()
        if code:
            country = country.search([("code", "=", code)], limit=1)
        state = self.env["res.country.state"]
        region = (data.get("region") or "").strip() if isinstance(data.get("region"), str) else ""
        if country and region:
            state = state.search([("country_id", "=", country.id), "|", ("code", "=ilike", region),
                                  ("name", "=ilike", region)], limit=1)
        status = data.get("status")
        status = str(status) if str(status) in dict(COMPANY_STATUSES) else False
        name = (data.get("company_name") or data.get("legal_name") or "").strip() or "Magento company %s" % data["id"]
        try:
            group_id = int(data.get("customer_group_id") or 0)
        except (TypeError, ValueError):
            group_id = 0
        credit = credit if isinstance(credit, dict) else {}
        return {
            "name": name,
            "is_company": True,
            "email": (data.get("company_email") or "").strip() or False,
            "phone": (data.get("telephone") or "").strip() or False,
            "street": street[0] if street else False,
            "street2": " ".join(street[1:]) or False,
            "city": data.get("city") or False,
            "zip": data.get("postcode") or False,
            "country_id": country.id or False,
            "state_id": state.id or False,
            "magento_company_instance_id": self.id,
            "magento_company_id": str(data["id"]),
            "magento_company_legal_name": data.get("legal_name") or False,
            "magento_company_vat": data.get("vat_tax_id") or False,
            "magento_company_reseller_id": data.get("reseller_id") or False,
            "magento_company_status": status,
            "magento_company_group_id": group_id,
            "magento_company_credit_limit": _to_float(credit.get("credit_limit")),
            "magento_company_credit_balance": _to_float(credit.get("balance")),
            "magento_company_credit_available": _to_float(credit.get("available_limit")),
            "magento_company_credit_currency": credit.get("currency_code") or False,
        }

    def _magento_b2b_import_company_data(self, api_client, data):
        """Create or update the company contact of one Magento company JSON (idempotent)."""
        self.ensure_one()
        partners = self._magento_b2b_partners()
        company_id = str(data["id"])
        credit = {}
        try:
            credit = api_client.get_company_credit(company_id) or {}
        except requests.exceptions.RequestException as exc:  # informational only
            _logger.info("Magento company %s: credit not read: %s", company_id, exc)
        vals = self._magento_b2b_company_vals(data, credit)
        partner = partners.with_context(active_test=False).search([
            ("magento_company_instance_id", "=", self.id), ("magento_company_id", "=", company_id),
        ], limit=1)
        vat = (data.get("vat_tax_id") or "").strip()
        if not partner and vat:
            # An unbound Odoo company with the same VAT is the same company.
            partner = partners.search([("is_company", "=", True), ("vat", "=ilike", vat),
                                       ("magento_company_id", "=", False)], limit=1)
        if partner:
            changed = self.env["magento.customer"]._magento_changed_vals(partner, vals)
            if changed:
                partner.write(changed)
        else:
            partner = partners.create(vals)
        if vat and (partner.vat or "") != vat:
            try:
                with self.env.cr.savepoint():
                    partner.write({"vat": vat})
            except Exception as exc:  # noqa: BLE001 - an invalid VAT stays in magento_company_vat
                _logger.info("Magento company %s: VAT %r not copied: %s", company_id, vat, exc)
        self._magento_b2b_sync_company_users(api_client, partner, data)
        term = self.b2b_credit_payment_term_id
        if term and partner.magento_company_credit_limit > 0:
            scoped = partner.with_company(self.company_id)
            if not scoped.property_payment_term_id:
                scoped.write({"property_payment_term_id": term.id})
        return partner

    def _magento_b2b_sync_company_users(self, api_client, company, data):
        """Company admin and users (``GET /V1/hierarchy/:id``) as child contacts."""
        admin_id = str(data.get("super_user_id") or "")
        customer_ids = []
        try:
            for node in api_client.get_company_hierarchy(data["id"]):
                if isinstance(node, dict) and node.get("entity_type") == "customer" and node.get("entity_id"):
                    customer_ids.append(str(node["entity_id"]))
        except requests.exceptions.HTTPError as exc:
            _logger.info("Magento company %s: hierarchy not read (%s); admin only", data["id"], exc)
        if admin_id and admin_id not in customer_ids:
            customer_ids.insert(0, admin_id)
        admin = self.env["res.partner"]
        for customer_id in dict.fromkeys(customer_ids):
            contact = self._magento_b2b_company_contact(api_client, company, customer_id)
            if customer_id == admin_id:
                admin = contact
        if admin and company.magento_company_admin_id != admin:
            company.write({"magento_company_admin_id": admin.id})

    def _magento_b2b_company_contact(self, api_client, company, customer_id):
        partners = self._magento_b2b_partners()
        contact = partners.with_context(active_test=False).search([
            ("magento_instance_id", "=", self.id), ("magento_customer_id", "=", customer_id),
        ], limit=1)
        if contact:
            if contact != company and not contact.is_company and contact.parent_id != company:
                contact.write({"parent_id": company.id})
            return contact
        try:
            customer = api_client.get_customer(customer_id) or {}
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return partners.browse()
            raise
        email = self.env["sale.order"]._magento_normalize_email(customer.get("email"))
        name = " ".join(p for p in ((customer.get("firstname") or "").strip(),
                                    (customer.get("lastname") or "").strip()) if p) or email or customer_id
        if email:
            contact = partners.search([("parent_id", "=", company.id), ("email_normalized", "=", email),
                                       ("magento_customer_id", "=", False)], limit=1)
        bind = {"magento_instance_id": self.id, "magento_customer_id": customer_id}
        if contact:
            contact.write(bind)
            return contact
        return partners.create(dict(bind, name=name, email=email or False, parent_id=company.id, type="contact"))

    # ------------------------------------------------------------------
    # Shared catalogs (job methods)
    # ------------------------------------------------------------------
    def _magento_b2b_import_shared_catalogs(self):
        """Job: import the shared catalogs, then each one's products, prices and companies."""
        self.ensure_one()
        if not self.b2b_enabled:
            return "B2B is disabled on this instance: nothing imported."
        api_client = MagentoAPI(self.sudo())
        try:
            rows = api_client.get_shared_catalogs()
        except requests.exceptions.RequestException as exc:
            self._magento_b2b_failure("b2b_catalog", "list", exc)
            return "Shared catalog list failed: %s" % exception_message(exc)
        done = failed = 0
        for data in rows:
            if not isinstance(data, dict) or data.get("id") in (None, ""):
                continue
            ref = str(data["id"])
            try:
                with self.env.cr.savepoint():
                    catalog = self.env["magento.shared.catalog"]._magento_upsert(self, data)
                    catalog._magento_b2b_import_content(api_client)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self._magento_b2b_failure("b2b_catalog", ref, exc, payload=data, retry_transient=False)
            else:
                done += 1
                self.env["magento.sync.issue"]._record_success(self, "b2b_catalog", ref)
        self.env["magento.sync.issue"]._record_success(self, "b2b_catalog", "list")
        return "%s shared catalog(s) imported, %s failed." % (done, failed)

    # ------------------------------------------------------------------
    # Negotiable quotes (job methods)
    # ------------------------------------------------------------------
    def _magento_b2b_import_quotes(self):
        """Job: import the negotiable quotes updated since the watermark."""
        self.ensure_one()
        if not self.b2b_enabled:
            return "B2B is disabled on this instance: nothing imported."
        api_client = MagentoAPI(self.sudo())
        started = fields.Datetime.now()
        done = failed = 0
        try:
            for data in api_client.search_negotiable_quotes(self.b2b_quotes_watermark, lazy=True):
                ref = str(data.get("id") or "")
                try:
                    with self.env.cr.savepoint():
                        self._magento_b2b_import_quote_data(api_client, data)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self._magento_b2b_failure("b2b_quote", ref, exc, payload=data, retry_transient=False)
                else:
                    done += 1
                    self.env["magento.sync.issue"]._record_success(self, "b2b_quote", ref)
        except requests.exceptions.RequestException as exc:
            self._magento_b2b_failure("b2b_quote", "list", exc)
            return "Quote list failed: %s" % exception_message(exc)
        self.env["magento.sync.issue"]._record_success(self, "b2b_quote", "list")
        if not failed:
            self.sudo().write({"b2b_quotes_watermark": started})
        return "%s negotiable quote(s) imported, %s failed." % (done, failed)

    def _magento_b2b_import_quote(self, quote_id):
        """Job / retry: import (re-sync) one negotiable quote by id."""
        self.ensure_one()
        if not self.b2b_enabled:
            return False
        api_client = MagentoAPI(self.sudo())
        quotation = self._magento_b2b_import_quote_data(api_client, api_client.get_negotiable_quote(quote_id))
        self.env["magento.sync.issue"]._record_success(self, "b2b_quote", str(quote_id))
        return quotation

    def _magento_b2b_quote_partner(self, api_client, data):
        """Buyer contact of a quote: the contact bound to its Magento customer, else one
        created under the buyer's B2B company."""
        partners = self._magento_b2b_partners()
        customer = data.get("customer") or {}
        customer_id = str(customer.get("id") or data.get("customer_id") or "")
        if customer_id:
            partner = partners.with_context(active_test=False).search([
                ("magento_instance_id", "=", self.id), ("magento_customer_id", "=", customer_id)], limit=1)
            if partner:
                return partner
        email = self.env["sale.order"]._magento_normalize_email(customer.get("email") or data.get("customer_email"))
        company_attrs = ((customer.get("extension_attributes") or {}).get("company_attributes") or {})
        company = partners.browse()
        if company_attrs.get("company_id"):
            company = partners.search([("magento_company_instance_id", "=", self.id),
                                       ("magento_company_id", "=", str(company_attrs["company_id"]))], limit=1)
        if email:
            domain = [("email_normalized", "=", email)]
            if customer_id:
                domain += ["|", ("magento_customer_id", "=", False), ("magento_instance_id", "!=", self.id)]
            partner = partners.search(domain, limit=1)
            if partner:
                return partner
        name = " ".join(p for p in ((customer.get("firstname") or "").strip(),
                                    (customer.get("lastname") or "").strip()) if p) or email or "Magento buyer"
        vals = {"name": name, "email": email or False}
        if customer_id:
            vals.update(magento_instance_id=self.id, magento_customer_id=customer_id)
        if company:
            vals.update(parent_id=company.id, type="contact")
        return partners.create(vals)

    def _magento_b2b_import_quote_data(self, api_client, data):
        """Create or update the quotation of one negotiable quote (``GET /V1/carts/:id``)."""
        self.ensure_one()
        data = data if isinstance(data, dict) else {}
        negotiable = (data.get("extension_attributes") or {}).get("negotiable_quote") or {}
        quote_id = str(data.get("id") or negotiable.get("quote_id") or "")
        if not negotiable or not quote_id:
            raise UserError(_("Magento quote %s is not a negotiable quote.", quote_id or "?"))
        orders = self.env["sale.order"].sudo().with_context(**_NO_PUSH_CTX)
        quotation = orders.search([("magento_quote_instance_id", "=", self.id),
                                   ("magento_quote_id", "=", quote_id)], limit=1)
        status = negotiable.get("status") if negotiable.get("status") in dict(QUOTE_STATUSES) else False
        price_type = str(negotiable.get("negotiated_price_type") or "")
        expiration = _parse_date(negotiable.get("expiration_period"))
        header = {
            "magento_quote_instance_id": self.id,
            "magento_quote_id": quote_id,
            "magento_quote_name": negotiable.get("quote_name") or False,
            "magento_quote_status": status,
            "magento_quote_expiration": expiration,
            "magento_quote_negotiated_type": price_type if price_type in dict(NEGOTIATED_PRICE_TYPES) else False,
            "magento_quote_negotiated_value": _to_float(negotiable.get("negotiated_price_value")),
            "magento_quote_original_total": _to_float(negotiable.get("original_total_price")),
            "magento_quote_negotiated_total": _to_float(negotiable.get("negotiated_total_price")),
        }
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        if not quotation:
            partner = self._magento_b2b_quote_partner(api_client, data)
            quotation = orders.create(dict(
                header,
                partner_id=partner.id,
                company_id=self.company_id.id,
                origin=_("Magento quote %s", quote_id),
                client_order_ref=negotiable.get("quote_name") or False,
                validity_date=expiration or False,
            ))
            quotation._magento_b2b_sync_quote_lines(items, negotiable)
        elif quotation.state in ("draft", "sent"):
            changed = self.env["magento.customer"]._magento_changed_vals(
                quotation, dict(header, validity_date=expiration or quotation.validity_date))
            if changed:
                quotation.write(changed)
            if status not in QUOTE_TERMINAL:
                quotation._magento_b2b_sync_quote_lines(items, negotiable)
        else:
            changed = self.env["magento.customer"]._magento_changed_vals(
                quotation, {"magento_quote_status": header["magento_quote_status"]})
            if changed:
                quotation.write(changed)
        try:
            comments = api_client.get_negotiable_quote_comments(quote_id)
        except requests.exceptions.HTTPError as exc:
            _logger.info("Magento quote %s: comments not read: %s", quote_id, exc)
            comments = []
        quotation._magento_b2b_post_quote_comments(comments)
        if status in QUOTE_TERMINAL and quotation.state in ("draft", "sent"):
            quotation._action_cancel()
        return quotation

    # ------------------------------------------------------------------
    # Error Center single-record retry
    # ------------------------------------------------------------------
    def _magento_b2b_resync_one(self, entity, ref):
        self.ensure_one()
        if not self.b2b_enabled:
            return False
        if entity == "b2b_company" and ref == "list":
            return self._magento_b2b_import_companies()
        if entity == "b2b_catalog" and ref == "list":
            return self._magento_b2b_import_shared_catalogs()
        if entity == "b2b_quote" and ref == "list":
            return self._magento_b2b_import_quotes()
        if entity == "b2b_company":
            return self._magento_b2b_import_company(ref)
        if entity == "b2b_quote":
            return self._magento_b2b_import_quote(ref)
        if entity == "b2b_catalog":
            api_client = MagentoAPI(self.sudo())
            catalog = self.env["magento.shared.catalog"]._magento_upsert(self, api_client.get_shared_catalog(ref))
            catalog._magento_b2b_import_content(api_client)
            self.env["magento.sync.issue"]._record_success(self, "b2b_catalog", str(ref))
            return catalog
        return False


class ResPartnerB2B(models.Model):
    _inherit = "res.partner"

    magento_company_instance_id = fields.Many2one(
        "magento.instance", string="Magento B2B Instance", index=True, copy=False, ondelete="set null",
        help="Adobe Commerce instance this company is bound to (B2B).",
    )
    magento_company_id = fields.Char(string="Magento Company ID", index=True, copy=False)
    magento_b2b_export = fields.Boolean(
        string="Sync to Magento as B2B company", copy=False,
        help="Create or update this company in Adobe Commerce B2B with 'Export to Magento'.",
    )
    magento_company_admin_id = fields.Many2one(
        "res.partner", string="Company Admin", copy=False, ondelete="set null",
        domain="[('parent_id', '=', id)]",
        help="Magento requires a company admin: a contact linked to an existing Magento customer.",
    )
    magento_company_legal_name = fields.Char(string="Legal Name", copy=False)
    magento_company_vat = fields.Char(string="Magento VAT / Tax ID", copy=False)
    magento_company_reseller_id = fields.Char(string="Reseller ID", copy=False)
    magento_company_status = fields.Selection(COMPANY_STATUSES, string="Magento Company Status", copy=False)
    magento_company_group_id = fields.Integer(string="Magento Customer Group ID", copy=False)
    magento_company_credit_limit = fields.Float(string="Magento Credit Limit", copy=False, readonly=True)
    magento_company_credit_balance = fields.Float(string="Magento Credit Balance", copy=False, readonly=True)
    magento_company_credit_available = fields.Float(string="Magento Available Credit", copy=False, readonly=True)
    magento_company_credit_currency = fields.Char(string="Credit Currency", copy=False, readonly=True)
    magento_shared_catalog_id = fields.Many2one(
        "magento.shared.catalog", string="Shared Catalog", copy=False, ondelete="set null",
        domain="[('instance_id', '=', magento_company_instance_id)]",
    )

    _magento_company_uniq = models.Constraint(
        "unique(magento_company_instance_id, magento_company_id)",
        "A Magento company can only be linked to one contact per Magento instance.",
    )

    def _magento_b2b_company_payload(self):
        """``company`` body for ``POST`` / ``PUT /V1/company``. Raises a clear UserError
        when Magento would refuse it (no company admin, missing required fields)."""
        self.ensure_one()
        instance = self.magento_company_instance_id
        if not instance:
            raise UserError(_("Choose the Magento B2B instance of %s first.", self.display_name))
        if not self.is_company:
            raise UserError(_("Only companies can be exported as Magento B2B companies."))
        admin = self.magento_company_admin_id
        try:
            admin_customer = int(admin.magento_customer_id) if admin else 0
        except (TypeError, ValueError):
            admin_customer = 0
        if not admin or not admin_customer or admin.magento_instance_id != instance:
            raise UserError(_(
                "Magento needs a company admin to create a company. Set 'Company Admin' on %(company)s "
                "to a contact linked to a Magento customer of %(instance)s (import that customer first).",
                company=self.display_name, instance=instance.name))
        missing = [label for value, label in (
            (self.email, _("Email")), (self.street, _("Street")), (self.city, _("City")),
            (self.country_id, _("Country")), (self.zip, _("ZIP")), (self.phone, _("Phone")),
        ) if not value]
        if missing:
            raise UserError(_("Magento requires these company fields on %(company)s: %(fields)s.",
                              company=self.display_name, fields=", ".join(missing)))
        payload = {
            "company_name": self.name,
            "legal_name": self.magento_company_legal_name or self.name,
            "company_email": self.email,
            "vat_tax_id": self.vat or self.magento_company_vat or "",
            "reseller_id": self.magento_company_reseller_id or "",
            "street": [s for s in (self.street, self.street2) if s],
            "city": self.city,
            "country_id": self.country_id.code,
            "postcode": self.zip,
            "telephone": self.phone,
            "super_user_id": admin_customer,
        }
        if self.state_id:
            payload["region"] = self.state_id.name
        group = self.magento_shared_catalog_id.customer_group_id or self.magento_company_group_id
        if group or not self.magento_company_id:
            payload["customer_group_id"] = group or 1
        return payload

    def action_magento_b2b_export(self):
        for partner in self:
            partner.magento_company_instance_id._magento_b2b_require_enabled()
            partner._magento_b2b_company_payload()  # fail now, with the reason, not in the job
            partner.magento_company_instance_id._magento_b2b_enqueue(
                partner, "_magento_b2b_export_company", _("Export B2B company %s", partner.display_name),
                identity_key="b2b-company-export-%s" % partner.id,
            )
        return self.env["magento.instance"]._magento_b2b_queued(_("Company export queued."))

    def action_magento_b2b_pull(self):
        self.ensure_one()
        instance = self.magento_company_instance_id
        if not instance or not self.magento_company_id:
            raise UserError(_("This contact is not linked to a Magento company."))
        instance._magento_b2b_require_enabled()
        instance._magento_b2b_enqueue(instance, "_magento_b2b_import_company", _("Import B2B company %s",
                                      self.magento_company_id), args=(self.magento_company_id,),
                                      identity_key="b2b-company-%s-%s" % (instance.id, self.magento_company_id))
        return instance._magento_b2b_queued(_("Company re-sync queued."))

    def _magento_b2b_export_company(self):
        """Job: create (``POST /V1/company``) or update (``PUT /V1/company/:id``) each company."""
        issues = self.env["magento.sync.issue"]
        result = []
        for partner in self:
            instance = partner.magento_company_instance_id
            if not instance or not instance.b2b_enabled:
                continue
            ref = partner.magento_company_id or "odoo-partner-%s" % partner.id
            payload = None
            try:
                payload = partner._magento_b2b_company_payload()
                api_client = MagentoAPI(instance.sudo())
                if partner.magento_company_id:
                    api_client.update_company(partner.magento_company_id, payload)
                else:
                    created = api_client.create_company(payload) or {}
                    if not created.get("id"):
                        raise UserError(_("Magento did not return the id of the new company."))
                    status = str(created.get("status"))
                    partner.sudo().with_context(**_NO_PUSH_CTX).write({
                        "magento_company_id": str(created["id"]),
                        "magento_company_status": status if status in dict(COMPANY_STATUSES) else False,
                    })
            except (requests.exceptions.RequestException, UserError) as exc:
                instance._magento_b2b_failure("b2b_company", ref, exc, payload=payload, record=partner,
                                              retry_transient=bool(partner.magento_company_id))
                result.append("%s: failed" % partner.display_name)
                continue
            issues._record_success(instance, "b2b_company", ref)
            issues._record_success(instance, "b2b_company", "odoo-partner-%s" % partner.id)
            result.append("%s: exported as company %s" % (partner.display_name, partner.magento_company_id))
        return "; ".join(result) or False


class MagentoSharedCatalog(models.Model):
    _name = "magento.shared.catalog"
    _description = "Magento Shared Catalog (Adobe Commerce B2B)"
    _order = "instance_id, magento_catalog_id"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True,
                                 string="Odoo Company")
    magento_catalog_id = fields.Integer(string="Magento Catalog ID", required=True, readonly=True)
    description = fields.Text()
    catalog_type = fields.Selection([("0", "Custom"), ("1", "Public")], string="Type", readonly=True)
    customer_group_id = fields.Integer(string="Magento Customer Group ID", readonly=True)
    customer_group_rec_id = fields.Many2one("magento.customer.group", string="Customer Group",
                                            readonly=True, ondelete="set null")
    tax_class_id = fields.Integer(string="Tax Class ID", readonly=True)
    store_id = fields.Integer(string="Store ID", readonly=True)
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist", ondelete="set null",
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Import fills this pricelist with the catalog's custom prices; export pushes its prices "
             "to the catalog. Created on the first import when empty.",
    )
    product_skus = fields.Json(string="Assigned SKUs", readonly=True, copy=False)
    product_count = fields.Integer(string="Products", compute="_compute_counts")
    company_partner_ids = fields.One2many("res.partner", "magento_shared_catalog_id", string="Companies")
    company_partner_count = fields.Integer(string="Company Count", compute="_compute_counts")
    pricelist_item_count = fields.Integer(string="Price Rules", compute="_compute_counts")
    last_import_at = fields.Datetime(readonly=True, copy=False)
    last_export_at = fields.Datetime(readonly=True, copy=False)
    last_result = fields.Char(readonly=True, copy=False)
    b2b_notice = fields.Char(compute="_compute_b2b_notice")

    _magento_catalog_uniq = models.Constraint(
        "unique(instance_id, magento_catalog_id)",
        "This Magento shared catalog already exists for this instance.",
    )

    def _compute_b2b_notice(self):
        for catalog in self:
            catalog.b2b_notice = B2B_NOTICE

    def _compute_counts(self):
        Item = self.env["product.pricelist.item"].sudo()
        for catalog in self:
            catalog.product_count = len(catalog.product_skus or [])
            catalog.company_partner_count = len(catalog.company_partner_ids)
            catalog.pricelist_item_count = Item.search_count(
                [("magento_shared_catalog_id", "=", catalog.id)]) if catalog.id else 0

    @api.model
    def _magento_upsert(self, instance, data):
        if not isinstance(data, dict) or data.get("id") in (None, ""):
            raise UserError(_("Magento returned a shared catalog without id."))
        catalogs = self.sudo()
        catalog_type = str(data.get("type")) if str(data.get("type")) in ("0", "1") else False
        vals = {
            "name": (data.get("name") or "").strip() or "Shared catalog %s" % data["id"],
            "description": data.get("description") or False,
            "catalog_type": catalog_type,
            "customer_group_id": int(data.get("customer_group_id") or 0),
            "tax_class_id": int(data.get("tax_class_id") or 0),
            "store_id": int(data.get("store_id") or 0),
        }
        catalog = catalogs.search([("instance_id", "=", instance.id),
                                   ("magento_catalog_id", "=", int(data["id"]))], limit=1)
        if catalog:
            changed = self.env["magento.customer"]._magento_changed_vals(catalog, vals)
            if changed:
                catalog.write(changed)
        else:
            catalog = catalogs.create(dict(vals, instance_id=instance.id, magento_catalog_id=int(data["id"])))
        return catalog

    def _magento_b2b_group(self):
        """The ``magento.customer.group`` of the catalog (its code is the tier price group)."""
        self.ensure_one()
        group = self.customer_group_rec_id
        if not group or group.magento_group_id != self.customer_group_id:
            group = self.env["magento.customer.group"].sudo()._magento_get_group(
                self.instance_id, self.customer_group_id)
            if group and self.customer_group_rec_id != group:
                self.sudo().customer_group_rec_id = group
        if not group or not group.name:
            raise UserError(_("Customer group %s of the shared catalog is unknown.", self.customer_group_id))
        return group

    def _magento_b2b_ensure_pricelist(self):
        self.ensure_one()
        if not self.pricelist_id:
            company = self.instance_id.company_id
            self.sudo().pricelist_id = self.env["product.pricelist"].sudo().create({
                "name": _("Magento B2B: %s", self.name),
                "currency_id": company.currency_id.id,
                "company_id": company.id,
            })
        return self.pricelist_id

    def _magento_b2b_maps(self, skus=None):
        domain = [("instance_id", "=", self.instance_id.id), ("odoo_product_id", "!=", False),
                  ("sku", "!=", False), ("sku", "!=", "")]
        if skus is not None:
            domain.append(("sku", "in", list(skus)))
        return self.env["magento.product.map"].sudo().search(domain, order="id")

    def _magento_b2b_import_content(self, api_client):
        """Products, custom prices and companies of one shared catalog -> Odoo."""
        self.ensure_one()
        if not self.instance_id.b2b_enabled:
            return False
        skus = api_client.get_shared_catalog_products(self.magento_catalog_id)
        code = self._magento_b2b_group().name.lower()
        rows = []
        for chunk in _chunks(skus):
            rows += [row for row in (api_client.get_tier_prices(chunk) or [])
                     if isinstance(row, dict) and str(row.get("customer_group") or "").lower() == code]
        pricelist = self._magento_b2b_ensure_pricelist()
        products = {m.sku: m.odoo_product_id for m in self._magento_b2b_maps(skus)}
        desired, unmapped = {}, set()
        for row in rows:
            product = products.get(row.get("sku"))
            if not product:
                unmapped.add(row.get("sku"))
                continue
            qty = max(_to_float(row.get("quantity"), 1.0), 1.0)
            price = _to_float(row.get("price"))
            if (row.get("price_type") or "fixed") == "discount":
                vals = {"compute_price": "percentage", "percent_price": price}
            else:
                vals = {"compute_price": "fixed", "fixed_price": price}
            desired[(product.id, qty)] = vals
        self._magento_b2b_apply_items(pricelist, desired)
        company_ids = api_client.get_shared_catalog_companies(self.magento_catalog_id)
        partners = self.env["res.partner"].sudo().with_context(**_NO_PUSH_CTX).search([
            ("magento_company_instance_id", "=", self.instance_id.id), ("magento_company_id", "in", company_ids),
        ]) if company_ids else self.env["res.partner"]
        for partner in partners:
            if partner.magento_shared_catalog_id != self:
                partner.write({"magento_shared_catalog_id": self.id})
            scoped = partner.with_company(self.instance_id.company_id)
            if scoped.property_product_pricelist != pricelist:
                scoped.write({"property_product_pricelist": pricelist.id})
        message = _("%(skus)s SKU(s), %(prices)s price(s), %(companies)s compan(ies) imported",
                    skus=len(skus), prices=len(desired), companies=len(partners))
        if unmapped:
            message += _("; %s SKU(s) not mapped in Odoo", len(unmapped))
        self.sudo().write({"product_skus": skus, "last_import_at": fields.Datetime.now(), "last_result": message})
        return True

    def _magento_b2b_apply_items(self, pricelist, desired):
        """Make the pricelist items owned by this catalog match ``{(product_id, qty): vals}``."""
        Item = self.env["product.pricelist.item"].sudo()
        existing = Item.search([("pricelist_id", "=", pricelist.id), ("magento_shared_catalog_id", "=", self.id)])
        by_key = {}
        for item in existing:
            by_key.setdefault((item.product_id.id, item.min_quantity), item)
        stale = existing - Item.browse([i.id for i in by_key.values()])
        for (product_id, qty), vals in desired.items():
            full = dict(vals, applied_on="0_product_variant", product_id=product_id, min_quantity=qty)
            item = by_key.pop((product_id, qty), None)
            if item:
                changed = self.env["magento.customer"]._magento_changed_vals(item, full)
                if changed:
                    item.write(changed)
            else:
                Item.create(dict(full, pricelist_id=pricelist.id, magento_shared_catalog_id=self.id))
        (stale | Item.browse([i.id for i in by_key.values()])).unlink()

    def _magento_b2b_desired_prices(self):
        """``{(sku, qty): price}`` the pricelist gives the mapped products: quantity 1 where a
        pricelist item applies, plus every quantity break above 1."""
        self.ensure_one()
        pricelist = self.pricelist_id.sudo()
        maps = self._magento_b2b_maps()
        products = maps.mapped("odoo_product_id")
        if not products:
            return {}
        now = fields.Datetime.now()
        digits = self.env["decimal.precision"].precision_get("Product Price")
        Item = self.env["product.pricelist.item"].sudo()
        entries = {}
        base = pricelist._compute_price_rule(products, 1.0, date=now)
        for product_map in maps:
            price, rule_id = base[product_map.odoo_product_id.id]
            if rule_id:
                entries[(product_map.sku, 1.0)] = float_round(price, precision_digits=digits)
        tiers = pricelist._get_applicable_rules(products, now).filtered(lambda i: i.min_quantity > 1)
        for qty in sorted(set(tiers.mapped("min_quantity"))):
            prices = pricelist._compute_price_rule(products, qty, date=now)
            for product_map in maps:
                price, rule_id = prices[product_map.odoo_product_id.id]
                if rule_id and Item.browse(rule_id).min_quantity > 1:
                    entries[(product_map.sku, qty)] = float_round(price, precision_digits=digits)
        return entries

    def action_import(self):
        for catalog in self:
            catalog.instance_id._magento_b2b_require_enabled()
            catalog.instance_id._magento_b2b_enqueue(
                catalog.instance_id, "_magento_b2b_resync_one", _("Import shared catalog %s", catalog.name),
                args=("b2b_catalog", str(catalog.magento_catalog_id)),
                identity_key="b2b-catalog-import-%s" % catalog.id,
            )
        return self.env["magento.instance"]._magento_b2b_queued(_("Shared catalog import queued."))

    def action_export(self):
        for catalog in self:
            catalog.instance_id._magento_b2b_require_enabled()
            if not catalog.pricelist_id:
                raise UserError(_("Set the pricelist of shared catalog %s first.", catalog.name))
            catalog._magento_b2b_group()
            catalog.instance_id._magento_b2b_enqueue(
                catalog, "_magento_b2b_export_catalog", _("Export shared catalog %s", catalog.name),
                identity_key="b2b-catalog-export-%s" % catalog.id,
            )
        return self.env["magento.instance"]._magento_b2b_queued(_("Shared catalog export queued."))

    def action_view_pricelist_items(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Prices: %s", self.name),
            "res_model": "product.pricelist.item",
            "view_mode": "list,form",
            "domain": [("magento_shared_catalog_id", "=", self.id)],
        }

    def _magento_b2b_export_catalog(self):
        """Job: pricelist -> shared catalog (assigned products, tier prices of the catalog's
        customer group, assigned companies)."""
        result = []
        for catalog in self:
            instance = catalog.instance_id
            if not instance.b2b_enabled:
                continue
            ref = str(catalog.magento_catalog_id)
            try:
                if not catalog.pricelist_id:
                    raise UserError(_("Shared catalog %s has no pricelist.", catalog.name))
                code = catalog._magento_b2b_group().name
                api_client = MagentoAPI(instance.sudo())
                entries = catalog._magento_b2b_desired_prices()
                skus = sorted({sku for sku, _qty in entries})
                for chunk in _chunks(skus):
                    api_client.assign_shared_catalog_products(catalog.magento_catalog_id, chunk)
                prices = [{
                    "sku": sku, "price": price, "price_type": "fixed", "website_id": 0,
                    "customer_group": code, "quantity": qty,
                } for (sku, qty), price in sorted(entries.items())]
                failures = []
                for chunk in _chunks(prices):
                    failures += api_client.update_tier_prices(chunk)
                companies = catalog.company_partner_ids.filtered("magento_company_id")
                if companies:
                    api_client.assign_shared_catalog_companies(
                        catalog.magento_catalog_id, companies.mapped("magento_company_id"))
            except (requests.exceptions.RequestException, UserError) as exc:
                instance._magento_b2b_failure("b2b_catalog", ref, exc, record=catalog)
                result.append("%s: failed" % catalog.name)
                continue
            if failures:
                messages = [MagentoAPI.format_magento_message(f.get("message"), f.get("parameters"))
                            for f in failures]
                instance._magento_b2b_failure(
                    "b2b_catalog", ref, "Tier prices rejected: %s" % "; ".join(dict.fromkeys(messages)),
                    payload=failures, record=catalog)
            else:
                self.env["magento.sync.issue"]._record_success(instance, "b2b_catalog", ref)
            message = _("%(skus)s SKU(s), %(prices)s price(s) (%(failed)s rejected), %(companies)s compan(ies) exported",
                        skus=len(skus), prices=len(prices), failed=len(failures), companies=len(companies))
            catalog.sudo().write({"last_export_at": fields.Datetime.now(), "last_result": message})
            result.append("%s: %s" % (catalog.name, message))
        return "; ".join(result) or False


class ProductPricelistItemB2B(models.Model):
    _inherit = "product.pricelist.item"

    magento_shared_catalog_id = fields.Many2one(
        "magento.shared.catalog", string="Magento Shared Catalog", index="btree_not_null",
        ondelete="set null", copy=False,
        help="Set on the prices imported from an Adobe Commerce shared catalog: the next import "
             "updates or removes them.",
    )


class SaleOrderB2B(models.Model):
    _inherit = "sale.order"

    magento_quote_instance_id = fields.Many2one(
        "magento.instance", string="Magento Quote Instance", index=True, copy=False, readonly=True,
        ondelete="set null",
    )
    magento_quote_id = fields.Char(string="Magento Quote ID", index=True, copy=False, readonly=True)
    magento_quote_name = fields.Char(string="Quote Name", copy=False, readonly=True)
    magento_quote_status = fields.Selection(QUOTE_STATUSES, string="Negotiation Status", copy=False,
                                            readonly=True)
    magento_quote_expiration = fields.Date(string="Quote Expires", copy=False, readonly=True)
    magento_quote_negotiated_type = fields.Selection(NEGOTIATED_PRICE_TYPES, string="Negotiated Price Type",
                                                     copy=False, readonly=True)
    magento_quote_negotiated_value = fields.Float(string="Negotiated Value", copy=False, readonly=True)
    magento_quote_original_total = fields.Float(string="Magento Original Total", copy=False, readonly=True)
    magento_quote_negotiated_total = fields.Float(string="Magento Negotiated Total", copy=False, readonly=True)
    magento_quote_snapshot = fields.Json(copy=False, readonly=True,
                                         help="Items as Magento last sent them: {item_id: {sku, qty, price}}.")
    magento_quote_comment_ids = fields.Json(copy=False, readonly=True)
    magento_quote_proposal_sent_at = fields.Datetime(string="Proposal Sent", copy=False, readonly=True)
    magento_quote_seller_turn = fields.Boolean(compute="_compute_magento_quote_seller_turn")
    magento_source_quotation_id = fields.Many2one(
        "sale.order", string="Negotiated Quotation", copy=False, readonly=True, index="btree_not_null",
        ondelete="set null", help="Magento negotiable quote this order was placed from.",
    )
    magento_placed_order_ids = fields.One2many("sale.order", "magento_source_quotation_id",
                                               string="Placed Orders", readonly=True)

    _magento_quote_uniq = models.Constraint(
        "unique(magento_quote_instance_id, magento_quote_id)",
        "This Magento negotiable quote is already imported for this instance.",
    )

    @api.depends("magento_quote_id", "magento_quote_status", "state")
    def _compute_magento_quote_seller_turn(self):
        for order in self:
            order.magento_quote_seller_turn = bool(
                order.magento_quote_id and order.magento_quote_status in QUOTE_SELLER_TURN
                and order.state in ("draft", "sent")
            )

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _magento_b2b_quote_discount(negotiable, original):
        """Negotiated quote-level price as a line discount percentage."""
        value = _to_float(negotiable.get("negotiated_price_value"))
        price_type = str(negotiable.get("negotiated_price_type") or "")
        if price_type == "1":
            return value
        if not original:
            return 0.0
        if price_type == "2":
            return value / original * 100.0
        if price_type == "3":
            return (1.0 - value / original) * 100.0
        return 0.0

    def _magento_b2b_sync_quote_lines(self, items, negotiable):
        """Quote items -> quotation lines, matched by Magento item id; the negotiated price
        is spread on the lines as one discount so the Odoo total matches Magento's."""
        self.ensure_one()
        instance = self.magento_quote_instance_id
        skus = [str(item.get("sku") or "") for item in items]
        maps = self.env["magento.product.map"].sudo().search([
            ("instance_id", "=", instance.id), ("sku", "in", skus), ("odoo_product_id", "!=", False)])
        products = {m.sku: m.odoo_product_id for m in maps}
        original = sum(_to_float(i.get("price")) * _to_float(i.get("qty")) for i in items)
        discount = float_round(self._magento_b2b_quote_discount(negotiable, original), precision_digits=4)
        existing = self.order_line.filtered("magento_quote_item_id")
        commands, snapshot = [], {}
        for item in items:
            sku = str(item.get("sku") or "")
            product = products.get(sku) or self.env["product.product"].sudo().search(
                [("default_code", "=", sku)], limit=1)
            if not product:
                raise UserError(_("Magento quote %(quote)s: SKU %(sku)s is not mapped to an Odoo product. "
                                  "Import the product first.", quote=self.magento_quote_id, sku=sku))
            item_id = str(item.get("item_id") or "")
            qty, price = _to_float(item.get("qty")), _to_float(item.get("price"))
            vals = {
                "product_id": product.id,
                "product_uom_qty": qty,
                "price_unit": price,
                "discount": discount,
                "magento_quote_item_id": item_id,
            }
            line = existing.filtered(lambda l, i=item_id: l.magento_quote_item_id == i)[:1]
            if line:
                existing -= line
                if self.env["magento.customer"]._magento_changed_vals(line, vals):
                    # Write price and discount too: a new quantity alone would make Odoo
                    # recompute them from the pricelist.
                    commands.append((1, line.id, vals))
            else:
                commands.append((0, 0, dict(vals, name=item.get("name") or product.display_name)))
            snapshot[item_id] = {"sku": sku, "qty": qty, "price": price}
        commands += [(2, line.id) for line in existing]
        vals = {"magento_quote_snapshot": snapshot}
        if commands:
            vals["order_line"] = commands
        self.write(vals)

    def _magento_b2b_post_quote_comments(self, comments):
        """Magento quote comments not seen yet -> chatter notes (no e-mail)."""
        self.ensure_one()
        seen = set(self.magento_quote_comment_ids or [])
        authors = {2: _("Seller"), 3: _("Buyer")}
        posted = False
        for comment in sorted((c for c in comments if isinstance(c, dict)),
                              key=lambda c: (str(c.get("created_at") or ""), _to_float(c.get("entity_id")))):
            entity_id = comment.get("entity_id")
            if entity_id in (None, "") or entity_id in seen or comment.get("is_draft"):
                continue
            try:
                author = authors.get(int(comment.get("creator_type")), _("Magento"))
            except (TypeError, ValueError):
                author = _("Magento")
            text = html.unescape(str(comment.get("comment") or ""))
            body = Markup("<p><b>%s</b> %s%s</p><p>%s</p>") % (
                _("Magento quote comment from %s", author),
                Markup("<small class='text-muted'>(%s)</small>") % (comment.get("created_at") or ""),
                Markup(" <span class='badge text-bg-danger'>%s</span>") % _("Declined")
                if comment.get("is_decline") else "",
                text,
            )
            self.sudo().message_post(body=body, subtype_xmlid="mail.mt_note")
            seen.add(entity_id)
            posted = True
        if posted:
            self.sudo().with_context(**_NO_PUSH_CTX).write({"magento_quote_comment_ids": sorted(seen)})

    # ------------------------------------------------------------------
    # Odoo -> Magento: proposal / decline
    # ------------------------------------------------------------------
    def _magento_b2b_proposal_values(self):
        """``negotiable_quote`` attributes for ``PUT /V1/negotiableQuote/:id`` (may be empty).

        Magento's REST API only lets the seller set a quote-level negotiated price, so: when
        every line keeps Magento's price and has the same discount -> type 1 (percentage),
        otherwise type 3 (proposed total = the Odoo untaxed total). Quantities and items are
        the buyer's to change in Magento: a difference is refused here."""
        self.ensure_one()
        if not self.magento_quote_id:
            raise UserError(_("This quotation is not a Magento negotiable quote."))
        if self.magento_quote_status not in QUOTE_SELLER_TURN:
            raise UserError(_(
                "Magento only accepts a proposal while the quote waits for the seller (Created, Submitted "
                "by Buyer or Seller Reviewing). This quote is '%s': re-sync it first.",
                dict(QUOTE_STATUSES).get(self.magento_quote_status, self.magento_quote_status or "?")))
        snapshot = self.magento_quote_snapshot or {}
        lines = self.order_line.filtered(lambda l: not l.display_type)
        if lines.filtered(lambda l: not l.magento_quote_item_id) or len(lines) != len(snapshot):
            raise UserError(_("Items cannot be added or removed from Odoo: Magento's API lets the seller "
                              "propose a price only. The buyer changes items in Magento."))
        currency_digits = self.currency_id.decimal_places
        same_prices = True
        for line in lines:
            snap = snapshot.get(line.magento_quote_item_id)
            if not snap or float_compare(line.product_uom_qty, snap["qty"], precision_digits=4):
                raise UserError(_("The quantity of %s differs from Magento: quantities are the buyer's to "
                                  "change in Magento. Re-sync the quote.", line.product_id.display_name))
            if float_compare(line.price_unit, snap["price"], precision_digits=currency_digits):
                same_prices = False
        discounts = set(float_round(d, precision_digits=4) for d in lines.mapped("discount"))
        values = {}
        if same_prices and len(discounts) == 1:
            discount = discounts.pop()
            if discount or self.magento_quote_negotiated_type:
                values = {"negotiated_price_type": 1, "negotiated_price_value": discount}
        else:
            total = float_round(sum(lines.mapped("price_subtotal")), precision_digits=currency_digits)
            values = {"negotiated_price_type": 3, "negotiated_price_value": total}
        if self.validity_date:
            values["expiration_period"] = fields.Date.to_string(self.validity_date)
        return values

    def _magento_b2b_check_quote(self):
        self.ensure_one()
        instance = self.magento_quote_instance_id
        if not self.magento_quote_id or not instance:
            raise UserError(_("This quotation is not a Magento negotiable quote."))
        instance._magento_b2b_require_enabled()
        return instance

    def action_magento_quote_wizard(self):
        self.ensure_one()
        self._magento_b2b_check_quote()
        mode = self.env.context.get("magento_quote_mode") or "proposal"
        return {
            "type": "ir.actions.act_window",
            "name": _("Send Proposal to Magento") if mode == "proposal" else _("Decline Magento Quote"),
            "res_model": "magento.b2b.quote.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_order_id": self.id, "default_mode": mode},
        }

    def action_magento_quote_resync(self):
        self.ensure_one()
        instance = self._magento_b2b_check_quote()
        instance._magento_b2b_enqueue(
            instance, "_magento_b2b_import_quote", _("Re-sync negotiable quote %s", self.magento_quote_id),
            args=(self.magento_quote_id,), identity_key="b2b-quote-%s-%s" % (instance.id, self.magento_quote_id))
        return instance._magento_b2b_queued(_("Quote re-sync queued."))

    def _magento_b2b_quote_failure(self, exc, payload=None):
        instance = self.magento_quote_instance_id
        return instance._magento_b2b_failure("b2b_quote", self.magento_quote_id, exc, payload=payload, record=self)

    def _magento_b2b_send_proposal(self, comment=""):
        """Job: negotiated price (``PUT /V1/negotiableQuote/:id``) then
        ``POST /V1/negotiableQuote/submitToCustomer`` with the comment."""
        done = []
        for order in self:
            instance = order.magento_quote_instance_id
            if not instance or not instance.b2b_enabled:
                continue
            values = None
            try:
                values = order._magento_b2b_proposal_values()
                api_client = MagentoAPI(instance.sudo())
                if values:
                    api_client.update_negotiable_quote(order.magento_quote_id, values)
                api_client.submit_negotiable_quote(order.magento_quote_id, comment)
            except (requests.exceptions.RequestException, UserError) as exc:
                order._magento_b2b_quote_failure(exc, payload=values)
                continue
            order.sudo().with_context(**_NO_PUSH_CTX).write({
                "magento_quote_status": "submitted_by_admin",
                "magento_quote_proposal_sent_at": fields.Datetime.now(),
            })
            summary = {1: _("%(value)s%% discount"), 3: _("proposed total %(value)s")}.get(
                (values or {}).get("negotiated_price_type"), _("no price change"))
            order.sudo().message_post(
                body=Markup("<p>%s</p><p>%s</p>") % (
                    _("Proposal sent to Magento: %s.", summary % {"value": (values or {}).get("negotiated_price_value")}),
                    comment or ""),
                subtype_xmlid="mail.mt_note")
            self.env["magento.sync.issue"]._record_success(instance, "b2b_quote", order.magento_quote_id)
            done.append(order.magento_quote_id)
        return "Proposal sent for quote(s) %s" % ", ".join(done) if done else False

    def _magento_b2b_decline(self, reason=""):
        """Job: ``POST /V1/negotiableQuote/decline``; the quotation is cancelled."""
        done = []
        for order in self:
            instance = order.magento_quote_instance_id
            if not instance or not instance.b2b_enabled:
                continue
            try:
                if not (reason or "").strip():
                    raise UserError(_("Magento requires a reason to decline a quote."))
                MagentoAPI(instance.sudo()).decline_negotiable_quote(order.magento_quote_id, reason)
            except (requests.exceptions.RequestException, UserError) as exc:
                order._magento_b2b_quote_failure(exc, payload={"reason": reason})
                continue
            order.sudo().with_context(**_NO_PUSH_CTX).write({"magento_quote_status": "declined"})
            if order.state in ("draft", "sent"):
                order.sudo()._action_cancel()
            order.sudo().message_post(
                body=Markup("<p>%s</p><p>%s</p>") % (_("Quote declined in Magento."), reason),
                subtype_xmlid="mail.mt_note")
            self.env["magento.sync.issue"]._record_success(instance, "b2b_quote", order.magento_quote_id)
            done.append(order.magento_quote_id)
        return "Declined quote(s) %s" % ", ".join(done) if done else False

    # ------------------------------------------------------------------
    # Placed order -> quotation link (regular order import)
    # ------------------------------------------------------------------
    @api.model
    def _magento_prepare_order_vals(self, data, instance, order=None):
        vals = super()._magento_prepare_order_vals(data, instance, order=order)
        if not instance or not instance.b2b_enabled:
            return vals
        quote_id = str(data.get("quote_id") or "")
        if quote_id and not (order and order.magento_source_quotation_id):
            quotation = self.sudo().search([("magento_quote_instance_id", "=", instance.id),
                                            ("magento_quote_id", "=", quote_id)], limit=1)
            if quotation:
                vals["magento_source_quotation_id"] = quotation.id
        payment = data.get("payment") or {}
        term = instance.b2b_credit_payment_term_id
        if term and not order and isinstance(payment, dict) and payment.get("method") == "companycredit":
            vals["payment_term_id"] = term.id
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        linked = orders.filtered("magento_source_quotation_id")
        if linked:
            linked._magento_b2b_close_quotations()
        return orders

    def write(self, vals):
        result = super().write(vals)
        if vals.get("magento_source_quotation_id"):
            self._magento_b2b_close_quotations()
        return result

    def _magento_b2b_close_quotations(self):
        """The buyer placed the quote as an order: mark the quotation ordered and close it."""
        for order in self:
            quotation = order.magento_source_quotation_id.sudo()
            if not quotation or quotation == order:
                continue
            if quotation.magento_quote_status != "ordered":
                quotation.with_context(**_NO_PUSH_CTX).write({"magento_quote_status": "ordered"})
            if quotation.state in ("draft", "sent"):
                quotation._action_cancel()
                quotation.message_post(
                    body=Markup("<p>%s</p>") % _("Placed in Magento as order %s; this quotation is closed.",
                                                 order.magento_increment_id or order.name),
                    subtype_xmlid="mail.mt_note")


class SaleOrderLineB2B(models.Model):
    _inherit = "sale.order.line"

    magento_quote_item_id = fields.Char(string="Magento Quote Item ID", copy=False, readonly=True,
                                        index="btree_not_null")


class MagentoB2BQuoteWizard(models.TransientModel):
    _name = "magento.b2b.quote.wizard"
    _description = "Send a proposal for / decline a Magento negotiable quote"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade")
    mode = fields.Selection([("proposal", "Send Proposal"), ("decline", "Decline")], required=True,
                            default="proposal")
    comment = fields.Text(help="Shown to the buyer in Magento. Required to decline.")
    summary = fields.Text(compute="_compute_summary")

    @api.depends("order_id", "mode")
    def _compute_summary(self):
        for wizard in self:
            if wizard.mode != "proposal" or not wizard.order_id:
                wizard.summary = False
                continue
            try:
                values = wizard.order_id._magento_b2b_proposal_values()
            except UserError as exc:
                wizard.summary = str(exc.args[0] if exc.args else exc)
                continue
            kind = values.get("negotiated_price_type")
            if kind == 1:
                text = _("Magento receives a %s%% discount on the quote.", values["negotiated_price_value"])
            elif kind == 3:
                text = _("Magento receives a proposed total of %(total)s %(currency)s (untaxed).",
                         total=values["negotiated_price_value"], currency=wizard.order_id.currency_id.name)
            else:
                text = _("No price change: the quote is sent back as it is.")
            if values.get("expiration_period"):
                text += " " + _("Expires on %s.", values["expiration_period"])
            wizard.summary = text

    def action_confirm(self):
        self.ensure_one()
        order = self.order_id
        instance = order._magento_b2b_check_quote()
        if self.mode == "proposal":
            order._magento_b2b_proposal_values()
            instance._magento_b2b_enqueue(
                order, "_magento_b2b_send_proposal", _("Send proposal for quote %s", order.magento_quote_id),
                args=(self.comment or "",), identity_key="b2b-quote-proposal-%s" % order.id)
            message = _("Proposal queued: it reaches Magento within a minute.")
        else:
            if not (self.comment or "").strip():
                raise UserError(_("Magento requires a reason to decline a quote."))
            instance._magento_b2b_enqueue(
                order, "_magento_b2b_decline", _("Decline quote %s", order.magento_quote_id),
                args=(self.comment,), identity_key="b2b-quote-decline-%s" % order.id)
            message = _("Decline queued.")
        action = instance._magento_b2b_queued(message)
        action["params"]["next"] = {"type": "ir.actions.act_window_close"}
        return action


class MagentoSyncIssueB2B(models.Model):
    _inherit = "magento.sync.issue"

    entity = fields.Selection(selection_add=B2B_ENTITIES,
                              ondelete={key: "set default" for key, _label in B2B_ENTITIES})

    def _enqueue_retry_job(self, jobs):
        """B2B issues: replay the failed export / proposal job, else re-import the record."""
        self.ensure_one()
        if self.entity not in dict(B2B_ENTITIES):
            return super()._enqueue_retry_job(jobs)
        failed = self.job_id.sudo()
        if failed and failed.method_name in B2B_REPLAYABLE and failed.model_name in self.env:
            records = self.env[failed.model_name].browse(failed.res_ids or []).exists()
            if not records:
                return jobs.browse()
            return jobs.enqueue(
                records, failed.method_name, args=tuple(failed.args or ()), kwargs=failed.kwargs or None,
                instance=self.instance_id, channel=failed.channel, priority=failed.priority,
                identity_key=failed.identity_key or "retry-%s" % self.trace_key,
                description=_("Retry %(what)s", what=self.display_label),
            )
        ref = self.magento_ref or ""
        if not ref or ref.startswith("odoo-"):
            return jobs.browse()
        return jobs.enqueue(
            self.instance_id, "_magento_b2b_resync_one", args=(self.entity, ref), instance=self.instance_id,
            channel="b2b-%s" % self.instance_id.id, identity_key="retry-%s" % self.trace_key,
            description=_("Retry %(what)s", what=self.display_label),
        )
