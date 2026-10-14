"""Order fidelity mappings (roadmap 2.4) and the order auto-workflow (roadmap 2.5).

* ``magento.tax.map``: Magento tax rate (code + percent + price mode) -> ``account.tax``.
  Unmapped rates are auto-created as sale taxes ("Magento <title> <pct>%") unless the
  instance says otherwise, in which case the order import fails with a clear issue and
  the unmapped rate is listed for the user to map.
* ``magento.payment.map``: Magento payment method code -> journal + payment method lines,
  used when registering Magento payments and refunds. Unmapped methods fall back to the
  instance default journal; they are listed (without journal) as they are met.
* ``magento.carrier.map``: Magento shipping method code -> ``delivery.carrier`` when the
  ``delivery`` module is installed (a Reference field, so no hard dependency); otherwise
  the code is just recorded.
* ``magento.order.workflow``: per-instance rules matched on the Magento state / status /
  payment method / paid flag, with the steps confirm, invoice, post, pay, deliver, lock
  (or cancel). Run by ``magento.instance._magento_order_followup_steps``.
"""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare
from .sql_compat import Constraint

_logger = logging.getLogger(__name__)

PUSH_SKIP_CTX = {"skip_magento_sync": True, "magento_skip_push": True, "skip_odoo_sync": True}

# kind -> (instance field, product name, default_code)
SERVICE_PRODUCTS = {
    "shipping": ("shipping_product_id", "Magento Shipping", "MAGENTO-SHIPPING"),
    "discount": ("discount_product_id", "Magento Discount", "MAGENTO-DISCOUNT"),
    "fpt": ("fpt_product_id", "Magento FPT", "MAGENTO-FPT"),
}

TAX_ROUNDING = [
    ("round_per_line", "Per line"),
    ("round_globally", "Globally"),
]


class MagentoUnmappedTax(UserError):
    """An order uses a Magento tax rate that is not mapped (auto-creation is off)."""

    def __init__(self, message, rate_vals):
        super().__init__(message)
        self.rate_vals = rate_vals


def _split_codes(value):
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


def _format_percent(percent):
    text = ("%.4f" % float(percent or 0.0)).rstrip("0").rstrip(".")
    return text or "0"


class MagentoTaxMap(models.Model):
    _name = "magento.tax.map"
    _description = "Magento Tax Mapping"
    _order = "instance_id, magento_code, percent"
    _rec_name = "magento_code"

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_code = fields.Char(
        string="Magento Rate Code",
        help="Magento tax rate code (applied_taxes.code), e.g. US-CA-*-Rate 1. Empty when Magento "
             "sent only a percentage.",
    )
    magento_title = fields.Char(string="Magento Title")
    percent = fields.Float(digits=(16, 4), required=True)
    price_include = fields.Boolean(
        string="Price Includes Tax",
        help="Rates are mapped separately for tax-included and tax-excluded catalog prices.",
    )
    tax_id = fields.Many2one(
        "account.tax", string="Odoo Tax", ondelete="restrict",
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]",
        help="Empty: the rate was met while automatic tax creation was off; orders using it "
             "fail until a tax is set.",
    )
    auto_created = fields.Boolean(readonly=True, help="The Odoo tax was created by the connector.")
    active = fields.Boolean(default=True)

    _magento_tax_map_uniq = Constraint(
        "unique(instance_id, magento_code, percent, price_include)",
        "This Magento tax rate is already mapped for this instance.",
    )

    @api.depends("magento_code", "magento_title", "percent")
    def _compute_display_name(self):
        for record in self:
            label = record.magento_title or record.magento_code or _("Magento tax")
            record.display_name = "%s (%s%%)" % (label, _format_percent(record.percent))


class MagentoPaymentMap(models.Model):
    _name = "magento.payment.map"
    _description = "Magento Payment Method Mapping"
    _order = "instance_id, magento_method"
    _rec_name = "magento_method"

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_method = fields.Char(string="Magento Method Code", required=True,
                                 help="e.g. checkmo, banktransfer, paypal_express")
    name = fields.Char(string="Magento Title")
    journal_id = fields.Many2one(
        "account.journal", string="Journal", ondelete="restrict",
        domain="[('type', 'in', ('bank', 'cash', 'credit')), ('company_id', '=', company_id)]",
        help="Empty: payments use the instance default payment journal.",
    )
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line", string="Incoming Method", ondelete="restrict",
        domain="[('journal_id', '=', journal_id), ('payment_type', '=', 'inbound')]",
    )
    refund_method_line_id = fields.Many2one(
        "account.payment.method.line", string="Refund Method", ondelete="restrict",
        domain="[('journal_id', '=', journal_id), ('payment_type', '=', 'outbound')]",
    )
    active = fields.Boolean(default=True)

    _magento_payment_map_uniq = Constraint(
        "unique(instance_id, magento_method)",
        "This Magento payment method is already mapped for this instance.",
    )


class MagentoCarrierMap(models.Model):
    _name = "magento.carrier.map"
    _description = "Magento Carrier Mapping"
    _order = "instance_id, magento_code"
    _rec_name = "magento_code"

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_code = fields.Char(string="Magento Shipping Method", required=True,
                               help="carrier_method code, e.g. flatrate_flatrate, ups_GND")
    name = fields.Char(string="Magento Description")
    carrier_ref = fields.Reference(
        selection="_selection_carrier_model", string="Odoo Carrier",
        help="Delivery method used on the sale order (needs the Delivery module).",
    )
    active = fields.Boolean(default=True)

    _magento_carrier_map_uniq = Constraint(
        "unique(instance_id, magento_code)",
        "This Magento shipping method is already mapped for this instance.",
    )

    @api.model
    def _selection_carrier_model(self):
        if "delivery.carrier" in self.env:
            return [("delivery.carrier", _("Delivery Method"))]
        return []

    def _carrier(self):
        self.ensure_one()
        ref = self.carrier_ref
        if ref and ref._name == "delivery.carrier":
            return ref.exists()
        return False


class MagentoOrderWorkflow(models.Model):
    _name = "magento.order.workflow"
    _description = "Magento Order Workflow"
    _order = "instance_id, sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    magento_states = fields.Char(
        string="Magento States",
        help="Comma-separated Magento order states (new, processing, complete, closed, "
             "canceled, holded...). Empty: any state.",
    )
    magento_statuses = fields.Char(
        string="Magento Statuses",
        help="Comma-separated Magento order status codes. Empty: any status.",
    )
    payment_methods = fields.Char(
        string="Payment Methods",
        help="Comma-separated Magento payment method codes (checkmo, paypal_express...). "
             "Empty: any method.",
    )
    payment_condition = fields.Selection(
        [("any", "Any"), ("paid", "Paid in Magento"), ("not_paid", "Not fully paid")],
        default="any", required=True,
    )
    # Steps, always run in this order.
    confirm_order = fields.Boolean(string="Confirm")
    create_invoice = fields.Boolean(
        string="Invoice",
        help="Reuse the invoices imported from Magento; only when Magento has none, create the "
             "invoice in Odoo (never pushed to Magento by the workflow).",
    )
    post_invoice = fields.Boolean(string="Post")
    register_payment = fields.Boolean(
        string="Pay",
        help="Register the amount Magento reports as paid (journal from the payment method "
             "mapping) and settle refunded credit notes.",
    )
    validate_delivery = fields.Boolean(
        string="Deliver",
        help="Import Magento shipments and validate the matching Odoo deliveries.",
    )
    lock_order = fields.Boolean(string="Lock")
    cancel_order = fields.Boolean(string="Cancel", help="Cancel the Odoo order.")

    @api.model
    def _magento_matches(self, rule, order):
        state = (order.magento_state or "").lower()
        status = (order.magento_status or "").lower()
        method = (order.magento_payment_method or "").lower()
        states = _split_codes(rule.magento_states)
        statuses = _split_codes(rule.magento_statuses)
        methods = _split_codes(rule.payment_methods)
        if states and state not in states:
            return False
        if statuses and status not in statuses:
            return False
        if methods and method not in methods:
            return False
        paid = order.magento_payment_state == "paid"
        if rule.payment_condition == "paid" and not paid:
            return False
        if rule.payment_condition == "not_paid" and paid:
            return False
        return True

    def _magento_step_labels(self):
        self.ensure_one()
        labels = []
        for fname in ("cancel_order", "confirm_order", "create_invoice", "post_invoice",
                      "register_payment", "validate_delivery", "lock_order"):
            if self[fname]:
                labels.append(self._fields[fname].string)
        return ", ".join(labels) or _("nothing")


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    shipping_product_id = fields.Many2one(
        "product.product", string="Shipping Product", domain="[('type', '=', 'service')]",
        help="Service product of the shipping line of imported orders. Created on first use.",
    )
    discount_product_id = fields.Many2one(
        "product.product", string="Discount Product", domain="[('type', '=', 'service')]",
        help="Service product of the discount lines (discounts that cannot be expressed as an "
             "exact line discount) and of credit memo adjustments. Created on first use.",
    )
    fpt_product_id = fields.Many2one(
        "product.product", string="FPT Product", domain="[('type', '=', 'service')]",
        help="Service product of the Fixed Product Tax (WEEE) lines. Created on first use.",
    )
    auto_create_taxes = fields.Boolean(
        string="Auto-create Taxes", default=True,
        help="Create a matching Odoo sale tax for an unmapped Magento tax rate. Off: the order "
             "import fails with an Error Center issue and the rate appears in Tax Mapping.",
    )
    prices_include_tax = fields.Boolean(
        string="Catalog Prices Include Tax",
        help="Mirror of Magento's 'Catalog Prices' tax setting: order lines then use Magento's "
             "tax-included prices and price-included taxes.",
    )
    shipping_includes_tax = fields.Boolean(
        string="Shipping Prices Include Tax",
        help="Mirror of Magento's 'Shipping Prices' tax setting.",
    )
    tax_after_discount = fields.Boolean(
        string="Tax After Discount", default=True,
        help="Mirror of Magento's 'Apply Customer Tax' setting (After Discount is Magento's "
             "default). Off: discounts are not taxed.",
    )
    tax_rounding_method = fields.Selection(
        TAX_ROUNDING, string="Tax Rounding", default="round_globally", required=True,
        help="Magento's tax calculation method: 'Total' rounds globally, 'Row' per line. "
             "Odoo rounds with the company setting; use 'Apply to Company' to align it.",
    )
    company_tax_rounding_method = fields.Selection(
        related="company_id.tax_calculation_rounding_method", string="Company Tax Rounding",
    )
    tax_rounding_differs = fields.Boolean(compute="_compute_tax_rounding_differs")
    tax_map_ids = fields.One2many("magento.tax.map", "instance_id", string="Tax Mapping")
    payment_map_ids = fields.One2many("magento.payment.map", "instance_id", string="Payment Methods")
    carrier_map_ids = fields.One2many("magento.carrier.map", "instance_id", string="Carriers")
    order_workflow_ids = fields.One2many("magento.order.workflow", "instance_id",
                                         string="Order Workflows")
    order_workflows_initialized = fields.Boolean(copy=False, readonly=True)

    @api.depends("tax_rounding_method", "company_id.tax_calculation_rounding_method")
    def _compute_tax_rounding_differs(self):
        for instance in self:
            company_method = instance.company_id.tax_calculation_rounding_method
            instance.tax_rounding_differs = bool(
                company_method and company_method != instance.tax_rounding_method
            )

    def action_apply_tax_rounding_to_company(self):
        for instance in self:
            instance.company_id.sudo().tax_calculation_rounding_method = instance.tax_rounding_method
        return True

    # ------------------------------------------------------------------
    # Default workflows
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._magento_create_default_order_workflows()
        return records

    @api.model
    def _magento_default_order_workflow_vals(self):
        full = dict(confirm_order=True, create_invoice=True, post_invoice=True, register_payment=True)
        return [
            dict(name=_("Pending: keep as quotation"), sequence=10, magento_states="new",
                 magento_statuses="pending"),
            dict(name=_("Processing and paid"), sequence=20, magento_states="processing",
                 payment_condition="paid", **full),
            dict(name=_("Complete"), sequence=30, magento_states="complete",
                 validate_delivery=True, **full),
            dict(name=_("Closed (refunded)"), sequence=40, magento_states="closed",
                 validate_delivery=True, **full),
            dict(name=_("Canceled"), sequence=50, magento_states="canceled", cancel_order=True),
        ]

    def _magento_create_default_order_workflows(self):
        Workflow = self.env["magento.order.workflow"].sudo()
        for instance in self.sudo():
            if instance.order_workflows_initialized:
                continue
            if not Workflow.with_context(active_test=False).search_count(
                    [("instance_id", "=", instance.id)]):
                Workflow.create([
                    dict(vals, instance_id=instance.id)
                    for vals in self._magento_default_order_workflow_vals()
                ])
            instance.order_workflows_initialized = True

    @api.model
    def _magento_init_default_order_workflows(self):
        """Data hook (module install/update): default rules for instances without any."""
        self.sudo().with_context(active_test=False).search([
            ("order_workflows_initialized", "=", False),
        ])._magento_create_default_order_workflows()

    def _magento_match_order_workflow(self, order):
        self.ensure_one()
        rules = self.env["magento.order.workflow"].sudo().search([("instance_id", "=", self.id)])
        for rule in rules:
            if rule._magento_matches(rule, order):
                return rule
        return rules.browse()

    # ------------------------------------------------------------------
    # Service products
    # ------------------------------------------------------------------
    def _magento_service_product(self, kind):
        self.ensure_one()
        field_name, name, code = SERVICE_PRODUCTS[kind]
        product = self.sudo()[field_name]
        if product:
            return product
        Product = self.env["product.product"].sudo().with_context(**PUSH_SKIP_CTX)
        company = self.company_id
        product = Product.with_context(active_test=False).search([
            ("default_code", "=", code),
            ("company_id", "in", (False, company.id)),
        ], limit=1)
        if not product:
            product = Product.create({
                "name": name,
                "default_code": code,
                "detailed_type": "service",
                "sale_ok": True,
                "purchase_ok": False,
                "list_price": 0.0,
                "taxes_id": [(5, 0, 0)],
                "invoice_policy": "order",
            })
        self.sudo().write({field_name: product.id})
        return product.with_env(self.env)

    # ------------------------------------------------------------------
    # Taxes
    # ------------------------------------------------------------------
    def _magento_tax_for_rate(self, rate, price_include, company=None):
        """``account.tax`` for one Magento rate dict (code, title, percent)."""
        self.ensure_one()
        company = company or self.company_id or self.env.company
        code = str(rate.get("code") or "").strip()
        title = str(rate.get("title") or code or "").strip()
        percent = round(float(rate.get("percent") or 0.0), 4)
        TaxMap = self.env["magento.tax.map"].sudo().with_context(active_test=False)
        mapping = TaxMap.search([
            ("instance_id", "=", self.id),
            ("magento_code", "=", code or False),
            ("price_include", "=", bool(price_include)),
        ]).filtered(lambda m: float_compare(m.percent, percent, precision_digits=4) == 0)[:1]
        if mapping and mapping.tax_id:
            return mapping.tax_id
        label = "%s %s%%" % (title or code or _("Tax"), _format_percent(percent))
        if not self.auto_create_taxes:
            # The order import is rolled back: the rate is listed for mapping by the
            # result hook (``_magento_on_record_result``), outside the rolled-back savepoint.
            raise MagentoUnmappedTax(_(
                "Magento tax rate '%(rate)s' is not mapped to an Odoo tax on instance %(instance)s. "
                "Map it in Magento > Configuration > Tax Mapping, or enable 'Auto-create Taxes'.",
                rate=label, instance=self.name,
            ), {
                "instance_id": self.id, "magento_code": code or False, "magento_title": title,
                "percent": percent, "price_include": bool(price_include),
            })
        tax = self._magento_find_or_create_tax(label, percent, price_include, company)
        if mapping:
            mapping.write({"tax_id": tax.id, "magento_title": title or mapping.magento_title})
        else:
            TaxMap.create({
                "instance_id": self.id, "magento_code": code or False, "magento_title": title,
                "percent": percent, "price_include": bool(price_include), "tax_id": tax.id,
                "auto_created": True,
            })
        return tax

    def _magento_find_or_create_tax(self, label, percent, price_include, company):
        Tax = self.env["account.tax"].sudo().with_context(active_test=False)
        name = "Magento %s" % label
        if price_include:
            name = "%s %s" % (name, _("(incl.)"))
        candidates = Tax.search([
            ("company_id", "=", company.id),
            ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"),
            ("name", "=", name),
        ])
        tax = candidates.filtered(
            lambda t: float_compare(t.amount, percent, precision_digits=4) == 0
            and bool(t.price_include) == bool(price_include)
        )[:1]
        if tax:
            if not tax.active:
                tax.active = True
            return tax
        vals = {
            "name": name,
            "amount_type": "percent",
            "amount": percent,
            "type_tax_use": "sale",
            "company_id": company.id,
            "price_include": bool(price_include),
            "description": "%s%%" % _format_percent(percent),
        }
        country = self._magento_tax_country(company, label)
        vals["country_id"] = country.id
        group = self._magento_tax_group(company, country, percent)
        if group:
            vals["tax_group_id"] = group.id
        return Tax.with_company(company).create(vals)

    def _magento_tax_country(self, company, label):
        """Country of an auto-created tax (required on Odoo 18 taxes): the company's
        fiscal country, else its country, else the ISO prefix of the Magento tax code
        (``US-MI-*-Rate 1``). Never guessed beyond that."""
        country = company.account_fiscal_country_id or company.country_id
        if country:
            return country
        match = re.match(r"\s*([A-Za-z]{2})-", label or "")
        if match:
            country = self.env["res.country"].search([("code", "=", match.group(1).upper())], limit=1)
        if not country:
            raise UserError(_(
                "Set the fiscal country of %(company)s: Odoo needs it to create the tax for "
                "Magento rate %(rate)s.", company=company.display_name, rate=label,
            ))
        return country

    @api.model
    def _magento_tax_group(self, company, country, percent):
        """The tax group "Tax <pct>%" of the company, created from the default sale
        tax group (same accounts) so the order totals show the real rate."""
        Group = self.env["account.tax.group"].sudo()
        name = "Tax %s%%" % _format_percent(percent)
        domain = [("company_id", "=", company.id), ("name", "=", name)]
        if country:
            domain.append(("country_id", "in", (country.id, False)))
        group = Group.search(domain, limit=1)
        if group:
            return group
        base = company.account_sale_tax_id.tax_group_id
        if base and base.company_id and base.company_id != company:
            base = Group.browse()
        if base:
            return base.copy({"name": name, "country_id": country.id if country else base.country_id.id})
        vals = {"name": name, "company_id": company.id}
        if country:
            vals["country_id"] = country.id
        return Group.create(vals)

    def _magento_on_record_result(self, entity, magento_ref, ok, exc=None, payload=None, record=None):
        if not ok and isinstance(exc, MagentoUnmappedTax):
            vals = exc.rate_vals
            TaxMap = self.env["magento.tax.map"].sudo().with_context(active_test=False)
            known = TaxMap.search([
                ("instance_id", "=", vals["instance_id"]),
                ("magento_code", "=", vals["magento_code"]),
                ("price_include", "=", vals["price_include"]),
            ]).filtered(lambda m: float_compare(m.percent, vals["percent"], precision_digits=4) == 0)
            if not known:
                TaxMap.create(vals)
        return super()._magento_on_record_result(
            entity, magento_ref, ok, exc=exc, payload=payload, record=record,
        )

    # ------------------------------------------------------------------
    # Payment methods / carriers
    # ------------------------------------------------------------------
    def _magento_note_payment_method(self, code, title=None):
        """The mapping row of ``code`` (created, without journal, when first met)."""
        self.ensure_one()
        code = (code or "").strip()
        if not code:
            return self.env["magento.payment.map"]
        PaymentMap = self.env["magento.payment.map"].sudo().with_context(active_test=False)
        mapping = PaymentMap.search([("instance_id", "=", self.id), ("magento_method", "=", code)],
                                    limit=1)
        if not mapping:
            mapping = PaymentMap.create({"instance_id": self.id, "magento_method": code,
                                         "name": title or code})
        elif title and not mapping.name:
            mapping.name = title
        return mapping

    def _magento_payment_config(self, method_code, company, outbound=False):
        """(journal, payment method line) to register a Magento payment (or refund)."""
        self.ensure_one()
        mapping = self.env["magento.payment.map"].sudo().search([
            ("instance_id", "=", self.id), ("magento_method", "=", method_code or ""),
        ], limit=1) if method_code else self.env["magento.payment.map"]
        journal = mapping.journal_id
        if journal and journal.company_id and journal.company_id != company:
            journal = self.env["account.journal"]
        method = self.env["account.payment.method.line"]
        if journal:
            method = mapping.refund_method_line_id if outbound else mapping.payment_method_line_id
        else:
            journal, method = self._get_magento_payment_config(company)
            if outbound:
                method = self.env["account.payment.method.line"]
        if not journal:
            return journal, method
        lines = journal.outbound_payment_method_line_ids if outbound \
            else journal.inbound_payment_method_line_ids
        if method and method not in lines:
            method = self.env["account.payment.method.line"]
        if not method:
            method = lines[:1]
        return journal, method

    def _magento_note_carrier(self, code, description=None):
        """The mapping row of a Magento shipping method (created when first met)."""
        self.ensure_one()
        code = (code or "").strip()
        if not code:
            return self.env["magento.carrier.map"]
        CarrierMap = self.env["magento.carrier.map"].sudo().with_context(active_test=False)
        mapping = CarrierMap.search([("instance_id", "=", self.id), ("magento_code", "=", code)],
                                    limit=1)
        if not mapping:
            mapping = CarrierMap.create({"instance_id": self.id, "magento_code": code,
                                         "name": description or code})
        return mapping

    # ------------------------------------------------------------------
    # Order follow-up steps (sync_orders hook)
    # ------------------------------------------------------------------
    def _magento_order_followup_steps(self, api, order):
        """Workflow-driven follow-up steps of an imported order.

        Documents Magento already has are always imported (invoices, credit memos); the
        matched workflow adds confirm / invoice / post / pay / deliver / lock (or cancel).
        Without a matching rule (or for orders created in Odoo) the Phase 1 steps run
        unchanged. Every step runs in its own savepoint (``_sync_order_followups``); the
        first failure skips the remaining steps and is reported to the Error Center
        through the order's result hook.
        """
        self.ensure_one()
        rule = self.env["magento.order.workflow"]
        if not order.magento_exported_from_odoo:
            rule = self._magento_match_order_workflow(order)
        if not rule:
            return super()._magento_order_followup_steps(api, order)
        else:
            if order.magento_workflow_id != rule:
                order.sudo().magento_workflow_id = rule.id
            invoiced = order.magento_invoice_state in ("partial", "invoiced")
            paid = order.magento_payment_state in ("partial", "paid")
            steps = []
            if rule.cancel_order:
                steps.append(("workflow: cancel", order._magento_wf_cancel))
            if rule.confirm_order:
                steps.append(("workflow: confirm", order._magento_wf_confirm))
            if invoiced:
                steps.append(("invoice sync", lambda: order._magento_sync_invoices(api)))
            if rule.create_invoice:
                steps.append(("workflow: invoice", order._magento_wf_create_invoice))
            if rule.post_invoice:
                steps.append(("workflow: post invoice", order._magento_wf_post_invoices))
            if rule.register_payment and paid:
                steps.append(("payment sync",
                              lambda: order._magento_apply_payments_from_magento(force=True)))
            steps.append(("credit memo sync", lambda: order._magento_sync_credit_memos(
                api, settle=rule.register_payment)))
            if rule.validate_delivery:
                steps.append(("workflow: deliver", lambda: order._magento_wf_validate_delivery(api)))
            steps.append(("shipment push", lambda: order._magento_push_shipments()))
            if rule.lock_order:
                steps.append(("workflow: lock", order._magento_wf_lock))
        return self._magento_stop_after_failure(steps)

    @api.model
    def _magento_stop_after_failure(self, steps):
        """Wrap the steps so that, once one has failed, the following ones are skipped."""
        failed = []

        def wrap(label, step):
            def run():
                if failed:
                    _logger.info("Magento order step '%s' skipped after '%s' failed.", label, failed[0])
                    return None
                try:
                    return step()
                except Exception:
                    failed.append(label)
                    raise
            return run

        return [(label, wrap(label, step)) for label, step in steps]
