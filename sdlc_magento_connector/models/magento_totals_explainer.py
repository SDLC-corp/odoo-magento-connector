"""Totals explanation engine (roadmap 3.1).

Why does the Odoo total of a Magento order differ from Magento's ``grand_total``? Other
connectors add a "price difference" plug line or tell the merchant to mirror the Magento
configuration. This engine explains the difference instead, component by component, with the
evidence and the setting or record to fix.

Algorithm: a waterfall over a *model* of the order
---------------------------------------------------
1. The model starts as the Odoo order: one model line per order line (qty, unit price,
   discount %, taxes as percentages with their price-included flag) and the company tax
   rounding method. A small tax engine (percentage taxes, per line / global / per unit
   rounding) computes the model total; it reproduces Odoo's total for the taxes the
   connector creates.
2. Corrections are applied one at a time, each moving the model from "what Odoo did" to
   "what Magento did": extra lines, missing lines, lines edited in Odoo, tax-included
   prices, tax rates, discounts, tax after discount / discount tax compensation, FPT,
   shipping, Magento-only deductions, tax rounding. The change of the model total caused by
   a correction is one **component** (amount = its effect on Magento minus Odoo).
3. The **unexplained remainder** is ``difference - sum(components)``: it is always shown,
   even at zero. A remainder of at most one currency unit, while the corrected model
   matches the raw Magento grand total to a sub-cent, is shown as negligible currency
   rounding instead.

Every amount is rounded to the currency, so ``sum(components) + remainder == difference``
holds exactly (the remainder is computed from the difference, never the other way round).

The Magento order is read with ``GET /V1/orders/{id}`` (read-only); the payload is kept on
the explanation and reused when Magento cannot be reached.
"""
import json
import logging
from collections import OrderedDict, defaultdict

import requests
from markupsafe import Markup

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_round

from ..services.magento_api import MagentoAPI
from .magento_order_mapping import _format_percent
from .sql_compat import Constraint

_logger = logging.getLogger(__name__)

CATEGORIES = [
    ("missing_line", "Line missing in Odoo"),
    ("extra_line", "Line not in Magento"),
    ("edited_line", "Line edited in Odoo"),
    ("tax_mode", "Tax-included prices"),
    ("tax_rate", "Tax rate"),
    ("discount", "Discount"),
    ("discount_tax", "Discount tax compensation"),
    ("fpt", "FPT / WEEE"),
    ("shipping", "Shipping"),
    ("adjustment", "Magento-only deduction"),
    ("rounding", "Tax rounding"),
    ("currency_rounding", "Currency rounding"),
    ("unexplained", "Unexplained remainder"),
]
CATEGORY_ORDER = {code: idx for idx, (code, _label) in enumerate(CATEGORIES)}

STATES = [
    ("explained", "Explained"),
    ("partially", "Partially explained"),
    ("unexplained", "Unexplained"),
]

FIX_TYPES = [
    ("open_instance", "Open instance settings"),
    ("apply_rounding", "Apply rounding to company"),
    ("open_tax_map", "Open tax mapping"),
    ("open_order", "Open order"),
]

ROUNDING_LABELS = {
    "round_per_line": "per line",
    "round_globally": "globally",
    "unit": "per unit",
}

# Order-level Magento amounts deducted from the grand total that Odoo books as payments.
DEDUCTION_KEYS = (
    ("gift_cards_amount", "Gift cards"),
    ("customer_balance_amount", "Store credit"),
    ("reward_currency_amount", "Reward points"),
)


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _MLine:
    """One line of the order model. ``taxes``: [(key, percent, price_include, tax_id)]."""

    __slots__ = ("key", "role", "qty", "price", "disc", "taxes", "fixed_total", "label",
                 "sale_line_id")

    def __init__(self, key, role, qty=1.0, price=0.0, disc=0.0, taxes=None, fixed_total=None,
                 label="", sale_line_id=False):
        self.key = key
        self.role = role
        self.qty = qty
        self.price = price
        self.disc = disc
        self.taxes = list(taxes or [])
        self.fixed_total = fixed_total
        self.label = label
        self.sale_line_id = sale_line_id

    def incl(self):
        """True / False: price-included / excluded taxes; None: no tax."""
        if not self.taxes:
            return None
        return any(tax[2] for tax in self.taxes)

    def pcts(self):
        return sorted(round(tax[1], 4) for tax in self.taxes)

    def tax_signature(self):
        return sorted((round(tax[1], 4), bool(tax[2])) for tax in self.taxes)


class _Ctx:
    """Everything the steps need: the order, its Magento payload and the inferences."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MagentoTotalsExplanation(models.Model):
    _name = "magento.totals.explanation"
    _description = "Magento Totals Explanation"
    _order = "date_explained desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    sale_order_id = fields.Many2one(
        "sale.order", string="Order", required=True, index=True, ondelete="cascade", readonly=True,
    )
    instance_id = fields.Many2one("magento.instance", readonly=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", readonly=True, index=True)
    currency_id = fields.Many2one("res.currency", readonly=True)
    magento_increment_id = fields.Char(string="Magento Order #", readonly=True)
    magento_grand_total = fields.Monetary(string="Magento Grand Total", readonly=True)
    odoo_total = fields.Monetary(string="Odoo Total", readonly=True)
    difference = fields.Monetary(
        readonly=True, help="Magento grand total minus Odoo total, rounded to the currency.",
    )
    explained_amount = fields.Monetary(
        string="Explained", readonly=True,
        help="Sum of the components with a known cause (negligible rounding included).",
    )
    unexplained_amount = fields.Monetary(
        string="Unexplained", readonly=True,
        help="Difference minus every explained component. Never hidden, even at zero.",
    )
    calibration_gap = fields.Monetary(
        readonly=True,
        help="Odoo total minus the explainer's own recomputation of the Odoo order. Not zero "
             "when the order uses taxes the explainer cannot recompute (fixed, code taxes...); "
             "that part stays in the unexplained remainder.",
    )
    state = fields.Selection(STATES, readonly=True, index=True, default="unexplained")
    matches = fields.Boolean(string="Totals Match", readonly=True,
                             help="The Odoo total equals the Magento grand total.")
    component_count = fields.Integer(readonly=True)
    source = fields.Selection(
        [("api", "Fetched from Magento"), ("stored", "Stored payload")],
        readonly=True, help="Where the Magento order came from.",
    )
    payload = fields.Text(string="Magento Order JSON", readonly=True)
    date_explained = fields.Datetime(string="Explained On", readonly=True)
    line_ids = fields.One2many("magento.totals.explanation.line", "explanation_id",
                               string="Components", readonly=True)
    summary_html = fields.Html(compute="_compute_summary_html", sanitize=False)

    _magento_explanation_order_uniq = Constraint(
        "unique(sale_order_id)", "An order has a single totals explanation.",
    )

    @api.depends("sale_order_id.name", "magento_increment_id")
    def _compute_name(self):
        for record in self:
            if record.magento_increment_id:
                record.name = "%s / #%s" % (record.sale_order_id.name or "", record.magento_increment_id)
            else:
                record.name = record.sale_order_id.name or ""

    @api.depends("line_ids.amount", "line_ids.category", "odoo_total", "magento_grand_total",
                 "difference", "currency_id")
    def _compute_summary_html(self):
        labels = dict(self.env["magento.totals.explanation.line"]._fields["category"]
                      ._description_selection(self.env))
        for record in self:
            currency = record.currency_id or self.env.company.currency_id
            subtotals = OrderedDict()
            for line in record.line_ids.sorted(lambda l: (CATEGORY_ORDER.get(l.category, 99), l.sequence)):
                subtotals[line.category] = subtotals.get(line.category, 0.0) + line.amount

            def money(amount, sign=False):
                text = "%.*f" % (currency.decimal_places, amount)
                if sign and amount > 0:
                    text = "+" + text
                return text

            cell = "padding:4px 10px;"
            num = cell + "text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;"
            rows = [Markup(
                "<tr style='color:#736963'><td style='%s'>%s</td><td style='%s'>%s</td></tr>"
            ) % (cell, _("Odoo total"), num, money(record.odoo_total))]
            for category, amount in subtotals.items():
                color = ""
                if category == "unexplained":
                    color = "color:#b42318;font-weight:600;" if not currency.is_zero(amount) \
                        else "color:#067647;"
                elif category == "currency_rounding":
                    color = "color:#736963;"
                rows.append(Markup("<tr><td style='%s%s'>%s</td><td style='%s%s'>%s</td></tr>") % (
                    cell, color, labels.get(category, category), num, color, money(amount, sign=True),
                ))
            rows.append(Markup(
                "<tr style='border-top:2px solid #41362f;font-weight:600'><td style='%s'>%s</td>"
                "<td style='%s'>%s</td></tr>"
            ) % (cell, _("Magento grand total"), num, money(record.magento_grand_total)))
            balanced = currency.compare_amounts(sum(subtotals.values()), record.difference) == 0
            footer = _("Components and remainder add up to the difference of %(diff)s.",
                       diff=money(record.difference)) if balanced else _("Not balanced.")
            record.summary_html = Markup(
                "<table class='table table-sm mb-1' style='max-width:560px;"
                "font-variant-numeric:tabular-nums'><tbody>%s</tbody></table>"
                "<div class='text-muted small'>%s</div>"
            ) % (Markup("").join(rows), footer)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _action_open(self):
        if len(self) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": _("Why does this differ?"),
                "res_model": self._name,
                "res_id": self.id,
                "view_mode": "form",
                "views": [(False, "form")],
            }
        return self._action_list()

    def _action_list(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Totals Explanations"),
            "res_model": self._name,
            "view_mode": "tree,form",
            "views": [(False, "tree"), (False, "form")],
            "domain": [("id", "in", self.ids)],
            "context": {"create": False},
        }

    def action_explain_again(self):
        explanations = self._explain_orders(self.sale_order_id)
        return explanations._action_open()

    def action_open_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": self.sale_order_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }

    def action_view_components(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Difference Components"),
            "res_model": "magento.totals.explanation.line",
            "view_mode": "tree",
            "domain": [("explanation_id", "in", self.ids)],
            "context": {"create": False, "group_by": ["category"]},
        }

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    @api.model
    def _explain_orders(self, orders):
        """Explain (or explain again) each Magento order; return the explanations."""
        result = self.browse()
        for order in orders:
            if not order.magento_order_id or not order.magento_instance_id:
                raise UserError(_("%s is not a Magento order: there is no Magento total to explain.",
                                  order.display_name))
            existing = self.search([("sale_order_id", "=", order.id)], limit=1)
            data, source = self._mx_fetch_payload(order, existing)
            vals = self._mx_explain(order, data)
            line_vals = vals.pop("lines")
            vals.update({
                "source": source,
                "payload": json.dumps(data, sort_keys=True, default=str),
                "date_explained": fields.Datetime.now(),
            })
            if existing:
                existing.line_ids.unlink()
                existing.write(dict(vals, line_ids=[Command.create(v) for v in line_vals]))
            else:
                existing = self.create(dict(vals, sale_order_id=order.id,
                                            line_ids=[Command.create(v) for v in line_vals]))
            result |= existing
        return result

    @api.model
    def _explain_orders_safe(self, orders):
        """Like ``_explain_orders`` but one failing order does not stop the others."""
        result = self.browse()
        failures = []
        for order in orders:
            try:
                with self.env.cr.savepoint():
                    result |= self._explain_orders(order)
            except Exception as exc:  # noqa: BLE001 - reported to the user, the batch goes on
                _logger.info("Magento totals explanation of %s failed: %s", order.display_name, exc)
                failures.append((order.display_name, str(exc)))
        return result, failures

    @api.model
    def _mx_fetch_payload(self, order, existing=None):
        """The Magento order JSON (read-only GET), or the stored payload when unreachable."""
        try:
            data = MagentoAPI(order.magento_instance_id.sudo()).get_order(order.magento_order_id)
            if isinstance(data, dict) and data:
                return data, "api"
            error = _("Magento returned no order data.")
        except (requests.exceptions.RequestException, UserError, ValueError) as exc:
            error = str(exc)
        if existing and existing.payload:
            try:
                return json.loads(existing.payload), "stored"
            except ValueError:
                pass
        raise UserError(_(
            "Could not read Magento order %(order)s to explain its total: %(error)s",
            order=order.magento_increment_id or order.magento_order_id, error=error,
        ))

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    @api.model
    def _mx_amounts(self, ctx, lines, mode):
        """Total and tax of the model lines with the given rounding method.

        Percentage taxes only. Price-included taxes never change a line total (Magento and
        Odoo both keep price x qty); rounding only moves tax-excluded tax amounts.
        """
        currency = ctx.currency
        base_rounded = 0.0
        base_raw = 0.0
        fixed = 0.0
        tax_per_line = {True: 0.0, False: 0.0}
        tax_per_unit = {True: 0.0, False: 0.0}
        tax_groups = defaultdict(float)
        for line in lines:
            if line.fixed_total is not None:
                fixed += line.fixed_total
                continue
            qty = line.qty or 0.0
            base = line.price * qty * (1.0 - (line.disc or 0.0) / 100.0)
            incl_pct = sum(tax[1] for tax in line.taxes if tax[2])
            excl_base = base / (1.0 + incl_pct / 100.0) if incl_pct else base
            base_rounded += currency.round(base)
            base_raw += base
            for key, pct, incl, _tax_id in line.taxes:
                raw = excl_base * pct / 100.0
                incl = bool(incl)
                tax_groups[(key, incl)] += raw
                tax_per_line[incl] += currency.round(raw)
                tax_per_unit[incl] += currency.round(raw / qty) * qty if qty else currency.round(raw)
        if mode == "round_per_line":
            bases, taxes = base_rounded, tax_per_line
        elif mode == "unit":
            bases, taxes = base_rounded, tax_per_unit
        else:
            bases = currency.round(base_raw)
            taxes = {True: 0.0, False: 0.0}
            for (_key, incl), raw in tax_groups.items():
                taxes[incl] += currency.round(raw)
        return {
            "total": currency.round(bases + taxes[False] + fixed),
            "tax": currency.round(taxes[True] + taxes[False]),
        }

    @api.model
    def _mx_total(self, ctx, model):
        return self._mx_amounts(ctx, model["lines"], model["mode"])["total"]

    @api.model
    def _mx_odoo_taxes(self, taxes):
        """[(key, pct, incl, tax_id)] of Odoo taxes, and whether all are percentages."""
        result = []
        supported = True
        flat = self.env["account.tax"]
        for tax in taxes:
            flat |= tax.children_tax_ids if tax.amount_type == "group" else tax
        for tax in flat:
            if tax.amount_type != "percent" or (tax.include_base_amount and len(flat) > 1):
                supported = False
            result.append((("odoo", tax.id), tax.amount, bool(tax.price_include), tax.id))
        return result, supported

    @api.model
    def _mx_odoo_model(self, ctx):
        lines = []
        for line in ctx.order.order_line.filtered(lambda l: not l.display_type):
            role = line.magento_line_role or ("product" if line.magento_item_id else "odoo")
            key = line.magento_line_key or line.magento_item_id or False
            taxes, supported = self._mx_odoo_taxes(line.tax_id)
            lines.append(_MLine(
                key=key, role=role, qty=line.product_uom_qty, price=line.price_unit,
                disc=line.discount, taxes=taxes,
                fixed_total=None if supported else line.price_total,
                label=line.name or line.product_id.display_name or "", sale_line_id=line.id,
            ))
        return {"lines": lines, "mode": ctx.company_mode}

    # -- Magento side -------------------------------------------------------
    @api.model
    def _mx_tax_details(self, data):
        extension = data.get("extension_attributes") or {}
        applied = [t for t in extension.get("applied_taxes") or [] if isinstance(t, dict)]
        by_item = {}
        shipping = None
        for entry in extension.get("item_applied_taxes") or []:
            if not isinstance(entry, dict):
                continue
            rates = [t for t in entry.get("applied_taxes") or [] if isinstance(t, dict)]
            kind = entry.get("type") or "product"
            item_id = entry.get("item_id") or entry.get("associated_item_id")
            if kind == "shipping":
                shipping = rates
            elif kind == "product" and item_id:
                by_item[str(item_id)] = rates
        return applied, by_item, shipping

    @api.model
    def _mx_rate_label(self, rate):
        return rate.get("title") or rate.get("code") or ""

    @api.model
    def _mx_match_rates(self, ctx, base, tax_amount):
        """Magento order-level rates whose percentages reproduce ``tax_amount`` on ``base``."""
        rates = [r for r in ctx.applied if _f(r.get("percent"))]
        candidates = [rates] + [[r] for r in rates] if rates else []
        for combo in candidates:
            pct = sum(_f(r.get("percent")) for r in combo)
            if ctx.currency.compare_amounts(base * pct / 100.0, tax_amount) == 0:
                return [(_f(r.get("percent")), self._mx_rate_label(r)) for r in combo]
        return [(round(tax_amount / base * 100.0, 4), "")] if base else []

    @api.model
    def _mx_item_rates(self, ctx, item):
        """[(percent, label)] Magento applied to an item."""
        explicit = ctx.by_item.get(str(item.get("item_id") or "")) \
            or ctx.by_item.get(str(item.get("_magento_parent_item_id") or ""))
        explicit = [r for r in explicit or [] if _f(r.get("percent"))]
        if explicit:
            return [(_f(r.get("percent")), self._mx_rate_label(r)) for r in explicit]
        if item.get("tax_percent") not in (None, ""):
            pct = _f(item.get("tax_percent"))
            if not pct:
                return []
            rates = [r for r in ctx.applied if _f(r.get("percent"))]
            if rates and abs(sum(_f(r.get("percent")) for r in rates) - pct) < 0.00005:
                return [(_f(r.get("percent")), self._mx_rate_label(r)) for r in rates]
            for rate in rates:
                if abs(_f(rate.get("percent")) - pct) < 0.00005:
                    return [(pct, self._mx_rate_label(rate))]
            return [(pct, "")]
        tax = _f(item.get("tax_amount"))
        if not tax or ctx.currency.is_zero(tax):
            return []
        return self._mx_match_rates(ctx, _f(item.get("row_total")), tax)

    @api.model
    def _mx_shipping_rates(self, ctx):
        explicit = [r for r in ctx.shipping_rates or [] if _f(r.get("percent"))]
        if explicit:
            return [(_f(r.get("percent")), self._mx_rate_label(r)) for r in explicit]
        tax = _f(ctx.data.get("shipping_tax_amount"))
        if not tax or ctx.currency.is_zero(tax):
            return []
        return self._mx_match_rates(ctx, _f(ctx.data.get("shipping_amount")), tax)

    @api.model
    def _mx_subcent(self, ctx, value):
        scaled = value * (10 ** ctx.currency.decimal_places)
        return abs(scaled - round(scaled)) > 1e-6

    @api.model
    def _mx_infer_prices_incl(self, ctx):
        """Magento 'Catalog Prices' mode from the payload: (bool, reason) or (None, reason).

        With tax-included catalog prices Magento derives the tax-excluded unit price, which
        then has sub-cent digits (9.99 incl 8.25% -> 9.2286) while the included price is
        round; tax-excluded catalogs show the opposite. A discount tax compensation only
        exists with tax-included prices.
        """
        incl = excl = 0
        example = ""
        for item in ctx.items.values():
            if not _f(item.get("tax_percent")) and not _f(item.get("tax_amount")):
                continue
            price = _f(item.get("price"))
            price_incl = _f(item.get("price_incl_tax"))
            if not price or not price_incl:
                continue
            price_sub = self._mx_subcent(ctx, price)
            incl_sub = self._mx_subcent(ctx, price_incl)
            if price_sub and not incl_sub:
                incl += 1
                example = example or _("item price %(excl)s excl. tax / %(incl)s incl. tax",
                                       excl=price, incl=price_incl)
            elif incl_sub and not price_sub:
                excl += 1
                example = example or _("item price %(excl)s excl. tax / %(incl)s incl. tax",
                                       excl=price, incl=price_incl)
        if not ctx.currency.is_zero(ctx.dtc_total):
            incl += 1
            example = example or _("discount tax compensation %s", self._mx_fmt(ctx, ctx.dtc_total))
        if incl > excl:
            return True, example
        if excl > incl:
            return False, example
        return None, ""

    @api.model
    def _mx_infer_shipping_incl(self, ctx):
        data = ctx.data
        if not _f(data.get("shipping_tax_amount")):
            return None
        amount = _f(data.get("shipping_amount"))
        incl_amount = _f(data.get("shipping_incl_tax"))
        if self._mx_subcent(ctx, amount) and not self._mx_subcent(ctx, incl_amount):
            return True
        if self._mx_subcent(ctx, incl_amount) and not self._mx_subcent(ctx, amount):
            return False
        return None

    @api.model
    def _mx_infer_after_discount(self, ctx):
        """Magento 'Apply Customer Tax' (before / after discount) from discounted items."""
        after = before = 0
        currency = ctx.currency
        for item in ctx.items.values():
            discount = abs(_f(item.get("discount_amount")))
            pct = _f(item.get("tax_percent"))
            if not discount or not pct or currency.is_zero(discount):
                continue
            tax = _f(item.get("tax_amount"))
            if ctx.incl:
                row = _f(item.get("row_total_incl_tax")) or _f(item.get("row_total")) + tax
                tax_after = (row - discount) * pct / (100.0 + pct)
                tax_before = row * pct / (100.0 + pct)
            else:
                row = _f(item.get("row_total"))
                tax_after = (row - discount) * pct / 100.0
                tax_before = row * pct / 100.0
            if currency.compare_amounts(tax_after, tax_before) == 0:
                continue
            if currency.compare_amounts(tax_after, tax) == 0:
                after += 1
            elif currency.compare_amounts(tax_before, tax) == 0:
                before += 1
        if after > before:
            return True
        if before > after:
            return False
        return None

    @api.model
    def _mx_fmt(self, ctx, amount):
        return "%.*f" % (ctx.currency.decimal_places, amount or 0.0)

    @api.model
    def _mx_qty(self, item):
        return _f(item.get("qty_ordered") or item.get("qty")) or 1.0

    @api.model
    def _mx_item_prices(self, ctx, item):
        """(tax-excluded, tax-included) unit prices, at the order line price precision."""
        qty = self._mx_qty(item)
        excl = _f(item.get("price"))
        if item.get("price_incl_tax") not in (None, ""):
            incl = _f(item.get("price_incl_tax"))
        else:
            incl = excl + _f(item.get("tax_amount")) / qty
        digits = ctx.price_digits
        return float_round(excl, precision_digits=digits), float_round(incl, precision_digits=digits)

    @api.model
    def _mx_magento_taxes(self, rates, incl):
        return [(("mg", round(pct, 4)), pct, bool(incl), False) for pct, _label in rates]

    @api.model
    def _mx_rates_text(self, rates):
        if not rates:
            return _("no tax")
        return ", ".join(
            ("%s %s%%" % (label, _format_percent(pct))).strip() for pct, label in rates
        )

    @api.model
    def _mx_taxes_text(self, ctx, taxes):
        if not taxes:
            return _("no tax")
        names = []
        for _key, pct, incl, tax_id in taxes:
            name = self.env["account.tax"].browse(tax_id).name if tax_id else _("Magento rate")
            names.append(_("%(name)s (%(pct)s%%, %(mode)s)", name=name, pct=_format_percent(pct),
                           mode=_("incl.") if incl else _("excl.")))
        return ", ".join(names)

    # -- fixes ----------------------------------------------------------------
    @api.model
    def _mx_fix_order(self, ctx, label=None):
        return {"fix_type": "open_order", "fix_label": label or _("Review the order lines"),
                "fix_res_ids": str(ctx.order.id)}

    @api.model
    def _mx_fix_instance(self, ctx, label):
        return {"fix_type": "open_instance", "fix_label": label,
                "fix_res_ids": str(ctx.instance.id)}

    @api.model
    def _mx_fix_tax_map(self, ctx, tax_ids=(), rates=(), incl=None, label=None):
        TaxMap = self.env["magento.tax.map"].sudo().with_context(active_test=False)
        rows = TaxMap.browse()
        tax_ids = [t for t in tax_ids if t]
        if tax_ids:
            rows = TaxMap.search([("instance_id", "=", ctx.instance.id), ("tax_id", "in", tax_ids)])
        if not rows and rates:
            for pct, _label in rates:
                domain = [("instance_id", "=", ctx.instance.id)]
                if incl is not None:
                    domain.append(("price_include", "=", bool(incl)))
                rows |= TaxMap.search(domain).filtered(
                    lambda m, pct=pct: float_compare(m.percent, pct, precision_digits=4) == 0)
        return {"fix_type": "open_tax_map", "fix_label": label or _("Check the tax mapping"),
                "fix_res_ids": ",".join(str(i) for i in rows.ids)}

    # -- the waterfall ----------------------------------------------------------
    @api.model
    def _mx_step(self, ctx, model, category, name, evidence, mutate, fix=None, sale_line_id=False,
                 magento_item_id=False):
        before = self._mx_total(ctx, model)
        mutate()
        after = self._mx_total(ctx, model)
        delta = ctx.currency.round(after - before)
        if ctx.currency.is_zero(delta):
            return 0.0
        vals = {
            "category": category,
            "name": name,
            "amount": delta,
            "evidence": evidence,
            "sale_line_id": sale_line_id or False,
            "magento_item_id": magento_item_id or False,
        }
        vals.update(fix or {})
        ctx.components.append(vals)
        return delta

    @api.model
    def _mx_lines(self, model, role=None, key=None):
        return [l for l in model["lines"]
                if (role is None or l.role == role) and (key is None or l.key == key)]

    @api.model
    def _mx_item_model_lines(self, ctx, key, item):
        """Model lines Magento's item stands for: product, discount, FPT."""
        excl_price, incl_price = self._mx_item_prices(ctx, item)
        rates = self._mx_item_rates(ctx, item)
        taxes = self._mx_magento_taxes(rates, ctx.incl)
        label = item.get("sku") or item.get("name") or key
        lines = [_MLine(key=key, role="product", qty=self._mx_qty(item),
                        price=incl_price if ctx.incl else excl_price, taxes=taxes, label=label)]
        discount = abs(_f(item.get("discount_amount")))
        if discount and not ctx.currency.is_zero(discount):
            lines.append(_MLine(key="discount:%s" % key, role="discount", price=-discount,
                                taxes=list(taxes) if ctx.after_discount else [], label=label))
        weee = _f(item.get("weee_tax_applied_row_amount"))
        if weee and not ctx.currency.is_zero(weee):
            lines.append(_MLine(key="fpt:%s" % key, role="fpt", price=weee, label=label))
        return lines

    @api.model
    def _mx_step_extra_lines(self, ctx, model):
        seen = set()
        for line in list(model["lines"]):
            if line.role == "shipping" or line.key == "discount:shipping":
                continue
            reason = None
            if line.role == "odoo":
                reason = _("The line was added in Odoo: it has no Magento item.")
            elif line.role == "product":
                if line.key not in ctx.items:
                    reason = _("Magento item %s is not in the Magento order.", line.key or "-")
                elif line.key in seen:
                    reason = _("Second Odoo line for Magento item %s.", line.key)
                seen.add(line.key)
            elif line.role in ("discount", "fpt"):
                parent = (line.key or "").split(":", 1)[-1]
                if parent not in ctx.items:
                    reason = _("Its Magento item %s is not in the Magento order.", parent or "-")
            if not reason:
                continue

            def remove(line=line):
                model["lines"].remove(line)

            self._mx_step(
                ctx, model, "extra_line", _("Odoo line: %s", line.label), reason, remove,
                fix=self._mx_fix_order(ctx, _("Remove or correct the Odoo line")),
                sale_line_id=line.sale_line_id,
            )

    @api.model
    def _mx_step_missing_lines(self, ctx, model):
        for key, item in ctx.items.items():
            if self._mx_lines(model, "product", key):
                continue
            new_lines = self._mx_item_model_lines(ctx, key, item)
            for role in ("discount", "fpt"):
                for line in self._mx_lines(model, role, "%s:%s" % (role, key)):
                    model["lines"].remove(line)

            def add(new_lines=new_lines):
                model["lines"].extend(new_lines)

            excl_price, incl_price = self._mx_item_prices(ctx, item)
            self._mx_step(
                ctx, model, "missing_line", _("Magento item: %s", item.get("sku") or key),
                _("Magento item %(sku)s (%(qty)s x %(price)s) has no line on the Odoo order.",
                  sku=item.get("sku") or key, qty=self._mx_qty(item),
                  price=self._mx_fmt(ctx, incl_price if ctx.incl else excl_price)),
                add, fix=self._mx_fix_order(ctx, _("Pull the order from Magento again")),
                magento_item_id=key,
            )

    @api.model
    def _mx_step_product_lines(self, ctx, model):
        """Lines edited in Odoo, tax-included prices, tax rates."""
        digits = ctx.price_digits
        instance = ctx.instance

        def close(a, b):
            return float_compare(a, b, precision_digits=digits) == 0

        for key, item in ctx.items.items():
            matches = self._mx_lines(model, "product", key)
            if not matches:
                continue
            line = matches[0]
            sku = item.get("sku") or key
            qty = self._mx_qty(item)
            excl_price, incl_price = self._mx_item_prices(ctx, item)
            line_incl = line.incl()
            # 1. Edited in Odoo: quantity or price no Magento price mode explains.
            mode_swap = False
            if line.fixed_total is None:
                qty_ok = float_compare(line.qty, qty, precision_digits=4) == 0
                if line_incl is None:
                    own = incl_price if close(line.price, incl_price) and not close(line.price, excl_price) \
                        else excl_price
                    other = own
                else:
                    own = incl_price if line_incl else excl_price
                    other = excl_price if line_incl else incl_price
                price_ok = close(line.price, own)
                mode_swap = qty_ok and not price_ok and line_incl is not None and close(line.price, other)
                if not (qty_ok and price_ok) and not mode_swap:
                    evidence = _(
                        "Odoo line: %(oqty)s x %(oprice)s. Magento item: %(mqty)s x %(mprice)s.",
                        oqty=line.qty, oprice=self._mx_fmt(ctx, line.price), mqty=qty,
                        mprice=self._mx_fmt(ctx, own))
                    if ctx.order.magento_exported_from_odoo:
                        evidence += " " + _("The order was created in Odoo, which keeps its own prices.")

                    def edit(line=line, qty=qty, own=own):
                        line.qty = qty
                        line.price = own

                    self._mx_step(ctx, model, "edited_line", _("Edited line: %s", sku), evidence, edit,
                                  fix=self._mx_fix_order(ctx, _("Restore the Magento quantity and price")),
                                  sale_line_id=line.sale_line_id, magento_item_id=key)
            # 2. Tax-included vs tax-excluded prices.
            line_incl = line.incl()
            if line.fixed_total is None and line_incl is not None and (line_incl != ctx.incl or mode_swap):
                target = ctx.incl
                evidence = _(
                    "Magento catalog prices are tax-%(mmode)s (%(why)s). The Odoo line uses %(price)s "
                    "with tax-%(lmode)s taxes.",
                    mmode=_("included") if target else _("excluded"),
                    why=ctx.incl_reason, price=self._mx_fmt(ctx, line.price),
                    lmode=_("included") if line_incl else _("excluded"))
                if bool(instance.prices_include_tax) != bool(target):
                    fix = self._mx_fix_instance(ctx, _("Set 'Catalog Prices Include Tax' to %s",
                                                       _("Yes") if target else _("No")))
                else:
                    fix = self._mx_fix_tax_map(ctx, tax_ids=[t[3] for t in line.taxes],
                                               label=_("Check the price mode of the mapped tax"))

                def switch(line=line, target=target, excl_price=excl_price, incl_price=incl_price):
                    line.taxes = [(k, p, target, t) for k, p, _i, t in line.taxes]
                    line.price = incl_price if target else excl_price

                self._mx_step(ctx, model, "tax_mode", _("Price mode: %s", sku), evidence, switch,
                              fix=fix, sale_line_id=line.sale_line_id, magento_item_id=key)
            # 3. Tax rate (wrong mapping, edited taxes).
            rates = self._mx_item_rates(ctx, item)
            magento_pcts = sorted(round(pct, 4) for pct, _label in rates)
            if line.fixed_total is not None or magento_pcts != line.pcts():
                incl = line.incl()
                if incl is None:
                    incl = ctx.incl
                new_taxes = self._mx_magento_taxes(rates, incl)
                old_taxes = list(line.taxes)
                evidence = _("Odoo applies %(odoo)s. Magento applied %(magento)s.",
                             odoo=self._mx_taxes_text(ctx, old_taxes), magento=self._mx_rates_text(rates))
                fix = self._mx_fix_tax_map(ctx, tax_ids=[t[3] for t in old_taxes], rates=rates,
                                           incl=incl, label=_("Map the Magento rate to the right Odoo tax"))
                had_taxes = line_incl is not None

                def retax(line=line, key=key, new_taxes=new_taxes, old_taxes=old_taxes, incl=incl,
                          had_taxes=had_taxes, excl_price=excl_price, incl_price=incl_price, qty=qty):
                    if line.fixed_total is not None:
                        line.fixed_total = None
                        line.qty = qty
                        line.price = incl_price if incl else excl_price
                    elif not had_taxes:
                        line.price = incl_price if incl else excl_price
                    line.taxes = list(new_taxes)
                    for discount in self._mx_lines(model, "discount", "discount:%s" % key):
                        if discount.tax_signature() == sorted((round(t[1], 4), bool(t[2])) for t in old_taxes):
                            discount.taxes = list(new_taxes)

                self._mx_step(ctx, model, "tax_rate", _("Tax rate: %s", sku), evidence, retax,
                              fix=fix, sale_line_id=line.sale_line_id, magento_item_id=key)

    @api.model
    def _mx_step_discounts(self, ctx, model):
        instance = ctx.instance
        currency = ctx.currency
        for key, item in ctx.items.items():
            matches = self._mx_lines(model, "product", key)
            if not matches:
                continue
            line = matches[0]
            sku = item.get("sku") or key
            magento = abs(_f(item.get("discount_amount")))
            discount_lines = self._mx_lines(model, "discount", "discount:%s" % key)
            line_part = line.price * line.qty * (line.disc or 0.0) / 100.0
            odoo = line_part - sum(d.price * d.qty for d in discount_lines)
            if currency.compare_amounts(odoo, magento) != 0:
                if discount_lines:
                    taxes = list(discount_lines[0].taxes)
                elif line.disc or instance.tax_after_discount:
                    taxes = list(line.taxes)
                else:
                    taxes = []

                def rediscount(line=line, discount_lines=discount_lines, magento=magento, key=key,
                               taxes=taxes, label=sku):
                    line.disc = 0.0
                    for discount in discount_lines:
                        model["lines"].remove(discount)
                    if magento and not currency.is_zero(magento):
                        model["lines"].append(_MLine(key="discount:%s" % key, role="discount",
                                                     price=-magento, taxes=taxes, label=label))

                self._mx_step(
                    ctx, model, "discount", _("Discount: %s", sku),
                    _("Magento discount on %(sku)s: %(magento)s. Odoo discount: %(odoo)s.",
                      sku=sku, magento=self._mx_fmt(ctx, magento), odoo=self._mx_fmt(ctx, odoo)),
                    rediscount, fix=self._mx_fix_order(ctx, _("Restore the Magento discount")),
                    sale_line_id=line.sale_line_id, magento_item_id=key,
                )
            # Tax on the discount: Magento's "Apply Customer Tax" and price mode.
            if not magento or currency.is_zero(magento):
                continue
            target = list(line.taxes) if ctx.after_discount else []
            target_sig = sorted((round(t[1], 4), bool(t[2])) for t in target)
            discount_lines = self._mx_lines(model, "discount", "discount:%s" % key)
            convert = bool(line.disc) and not ctx.after_discount
            if not convert and all(d.tax_signature() == target_sig for d in discount_lines):
                continue

            def retax(line=line, discount_lines=discount_lines, target=target, convert=convert,
                      key=key, label=sku):
                if convert:
                    amount = line.price * line.qty * line.disc / 100.0
                    line.disc = 0.0
                    model["lines"].append(_MLine(key="discount:%s" % key, role="discount",
                                                 price=-amount, taxes=[], label=label))
                for discount in discount_lines:
                    discount.taxes = list(target)

            if not currency.is_zero(ctx.dtc_total):
                name = _("Discount tax compensation: %s", sku)
                evidence = _(
                    "Magento took the discount off tax-included prices and booked a discount tax "
                    "compensation of %(dtc)s. Odoo taxes the discount line with %(odoo)s; Magento's "
                    "discount carries %(magento)s.",
                    dtc=self._mx_fmt(ctx, ctx.dtc_total),
                    odoo=self._mx_taxes_text(ctx, discount_lines[0].taxes if discount_lines else []),
                    magento=self._mx_taxes_text(ctx, target))
            else:
                name = _("Tax after discount: %s", sku)
                evidence = _(
                    "Magento taxes the price %(when)s the discount (%(why)s). Odoo taxes the "
                    "discount with %(odoo)s.",
                    when=_("after") if ctx.after_discount else _("before"), why=ctx.after_reason,
                    odoo=self._mx_taxes_text(ctx, discount_lines[0].taxes if discount_lines else line.taxes))
            if bool(instance.tax_after_discount) != bool(ctx.after_discount):
                fix = self._mx_fix_instance(ctx, _("Set 'Tax After Discount' to %s",
                                                   _("Yes") if ctx.after_discount else _("No")))
            elif bool(instance.prices_include_tax) != bool(ctx.incl):
                fix = self._mx_fix_instance(ctx, _("Set 'Catalog Prices Include Tax' to %s",
                                                   _("Yes") if ctx.incl else _("No")))
            else:
                fix = self._mx_fix_order(ctx, _("Pull the order from Magento again"))
            self._mx_step(ctx, model, "discount_tax", name, evidence, retax, fix=fix,
                          sale_line_id=line.sale_line_id, magento_item_id=key)

    @api.model
    def _mx_step_fpt(self, ctx, model):
        for key, item in ctx.items.items():
            if not self._mx_lines(model, "product", key):
                continue
            magento = _f(item.get("weee_tax_applied_row_amount"))
            fpt_lines = self._mx_lines(model, "fpt", "fpt:%s" % key)
            odoo = sum(l.price * l.qty for l in fpt_lines)
            if ctx.currency.compare_amounts(odoo, magento) == 0:
                continue
            sku = item.get("sku") or key

            def refpt(fpt_lines=fpt_lines, magento=magento, key=key, label=sku):
                for line in fpt_lines:
                    model["lines"].remove(line)
                if magento and not ctx.currency.is_zero(magento):
                    model["lines"].append(_MLine(key="fpt:%s" % key, role="fpt", price=magento,
                                                 label=label))

            self._mx_step(
                ctx, model, "fpt", _("FPT: %s", sku),
                _("Magento Fixed Product Tax (WEEE) on %(sku)s: %(magento)s. Odoo FPT lines: %(odoo)s.",
                  sku=sku, magento=self._mx_fmt(ctx, magento), odoo=self._mx_fmt(ctx, odoo)),
                refpt, fix=self._mx_fix_order(ctx, _("Pull the order again to import the FPT line")),
                sale_line_id=fpt_lines[0].sale_line_id if fpt_lines else False, magento_item_id=key,
            )

    @api.model
    def _mx_step_shipping(self, ctx, model):
        data = ctx.data
        currency = ctx.currency
        instance = ctx.instance
        excl_amount = _f(data.get("shipping_amount"))
        incl_amount = _f(data.get("shipping_incl_tax")) if data.get("shipping_incl_tax") not in (None, "") \
            else excl_amount + _f(data.get("shipping_tax_amount"))
        magento_price = incl_amount if ctx.ship_incl else excl_amount
        rates = self._mx_shipping_rates(ctx)
        magento_taxes = self._mx_magento_taxes(rates, ctx.ship_incl)
        lines = self._mx_lines(model, "shipping")
        charged = not currency.is_zero(excl_amount) or not currency.is_zero(incl_amount)
        # Amount.
        if not charged and lines:
            def drop(lines=lines):
                for line in lines:
                    model["lines"].remove(line)

            self._mx_step(ctx, model, "shipping", _("Shipping"),
                          _("Magento charged no shipping; Odoo has a shipping line of %s.",
                            self._mx_fmt(ctx, sum(l.price * l.qty for l in lines))),
                          drop, fix=self._mx_fix_order(ctx, _("Remove the shipping line")),
                          sale_line_id=lines[0].sale_line_id)
        elif charged and not lines:
            def add():
                model["lines"].append(_MLine(key="shipping", role="shipping", price=magento_price,
                                             taxes=list(magento_taxes), label=_("Shipping")))

            self._mx_step(ctx, model, "shipping", _("Shipping"),
                          _("Magento charged shipping %(amount)s (%(desc)s); Odoo has no shipping line.",
                            amount=self._mx_fmt(ctx, magento_price),
                            desc=data.get("shipping_description") or "-"),
                          add, fix=self._mx_fix_order(ctx, _("Pull the order from Magento again")))
        elif charged:
            line = lines[0]
            line_incl = line.incl()
            own = incl_amount if line_incl else excl_amount
            odoo = sum(l.price * l.qty for l in lines)
            if currency.compare_amounts(odoo, own) != 0:
                def reprice(line=line, own=own, lines=lines):
                    for other in lines[1:]:
                        model["lines"].remove(other)
                    line.qty = 1.0
                    line.price = own

                self._mx_step(ctx, model, "shipping", _("Shipping amount"),
                              _("Magento shipping: %(magento)s. Odoo shipping: %(odoo)s.",
                                magento=self._mx_fmt(ctx, own), odoo=self._mx_fmt(ctx, odoo)),
                              reprice, fix=self._mx_fix_order(ctx, _("Restore the Magento shipping amount")),
                              sale_line_id=line.sale_line_id)
            # Tax on shipping: rate and price mode.
            target_sig = sorted((round(t[1], 4), bool(t[2])) for t in magento_taxes)
            if line.tax_signature() != target_sig:
                pct_differs = line.pcts() != sorted(round(p, 4) for p, _l in rates)
                if not pct_differs and bool(instance.shipping_includes_tax) != bool(ctx.ship_incl):
                    fix = self._mx_fix_instance(ctx, _("Set 'Shipping Prices Include Tax' to %s",
                                                       _("Yes") if ctx.ship_incl else _("No")))
                elif pct_differs:
                    fix = self._mx_fix_tax_map(ctx, tax_ids=[t[3] for t in line.taxes], rates=rates,
                                               incl=ctx.ship_incl,
                                               label=_("Map the Magento shipping rate to the right Odoo tax"))
                else:
                    fix = self._mx_fix_order(ctx, _("Pull the order from Magento again"))

                def retax(line=line):
                    line.taxes = list(magento_taxes)
                    line.price = magento_price

                self._mx_step(ctx, model, "shipping", _("Shipping tax"),
                              _("Magento taxed shipping with %(magento)s (shipping tax %(tax)s). Odoo "
                                "applies %(odoo)s.",
                                magento=self._mx_rates_text(rates),
                                tax=self._mx_fmt(ctx, _f(data.get("shipping_tax_amount"))),
                                odoo=self._mx_taxes_text(ctx, line.taxes)),
                              retax, fix=fix, sale_line_id=line.sale_line_id)
        # Shipping discount.
        magento_discount = abs(_f(data.get("shipping_discount_amount")))
        discount_lines = self._mx_lines(model, "discount", "discount:shipping")
        odoo_discount = -sum(l.price * l.qty for l in discount_lines)
        if currency.compare_amounts(odoo_discount, magento_discount) != 0:
            shipping_lines = self._mx_lines(model, "shipping")
            taxes = list(shipping_lines[0].taxes) if shipping_lines and ctx.after_discount else []

            def rediscount(discount_lines=discount_lines, taxes=taxes):
                for line in discount_lines:
                    model["lines"].remove(line)
                if magento_discount and not currency.is_zero(magento_discount):
                    model["lines"].append(_MLine(key="discount:shipping", role="discount",
                                                 price=-magento_discount, taxes=taxes,
                                                 label=_("Shipping discount")))

            self._mx_step(ctx, model, "shipping", _("Shipping discount"),
                          _("Magento shipping discount: %(magento)s. Odoo: %(odoo)s.",
                            magento=self._mx_fmt(ctx, magento_discount),
                            odoo=self._mx_fmt(ctx, odoo_discount)),
                          rediscount, fix=self._mx_fix_order(ctx, _("Restore the shipping discount")))

    @api.model
    def _mx_step_order_amounts(self, ctx, model):
        """Order-level Magento amounts with no Odoo line: discount tax compensation that the
        lines do not already reproduce, gift cards, store credit, reward points. Applied only
        when they bring the model closer to Magento's grand total."""
        currency = ctx.currency
        extension = ctx.data.get("extension_attributes") or {}
        candidates = []
        if not currency.is_zero(ctx.dtc_total):
            candidates.append(("discount_tax", _("Discount tax compensation"), ctx.dtc_total, _(
                "Magento added a discount tax compensation of %s to the grand total (tax hidden in "
                "a discount on tax-included prices). Odoo has no such amount.",
                self._mx_fmt(ctx, ctx.dtc_total))))
        for key, label in DEDUCTION_KEYS:
            amount = abs(_f(ctx.data.get(key) or extension.get(key)))
            if amount and not currency.is_zero(amount):
                candidates.append(("adjustment", label, -amount, _(
                    "Magento deducted %(label)s of %(amount)s from the grand total. Odoo records it as "
                    "a payment, not an order line.", label=label, amount=self._mx_fmt(ctx, amount))))
        for category, name, amount, evidence in candidates:
            gap = currency.round(ctx.grand_total - self._mx_total(ctx, model))
            if abs(currency.round(gap - amount)) >= abs(gap):
                continue

            def add(amount=amount, name=name):
                model["lines"].append(_MLine(key="order:%s" % name, role="order", price=amount,
                                             label=name))

            fix = None
            if category == "discount_tax" and bool(ctx.instance.prices_include_tax) != bool(ctx.incl):
                fix = self._mx_fix_instance(ctx, _("Set 'Catalog Prices Include Tax' to %s",
                                                   _("Yes") if ctx.incl else _("No")))
            self._mx_step(ctx, model, category, name, evidence, add, fix=fix)

    @api.model
    def _mx_step_rounding(self, ctx, model):
        currency = ctx.currency
        magento_tax = _f(ctx.data.get("tax_amount"))
        modes = ("round_per_line", "round_globally", "unit")
        taxes = {mode: self._mx_amounts(ctx, model["lines"], mode)["tax"] for mode in modes}
        current = model["mode"]
        if currency.compare_amounts(taxes[current], magento_tax) == 0:
            return
        preference = [ctx.instance.tax_rounding_method, "round_per_line", "round_globally", "unit"]
        target = next((mode for mode in preference
                       if mode != current and currency.compare_amounts(taxes[mode], magento_tax) == 0), None)
        if not target:
            return
        evidence = _(
            "Magento charged tax %(magento)s. Recomputed: per line %(line)s, globally %(globally)s, "
            "per unit %(unit)s. Odoo (company setting) rounds %(odoo)s; Magento's figure is "
            "reproduced by rounding %(target)s.",
            magento=self._mx_fmt(ctx, magento_tax), line=self._mx_fmt(ctx, taxes["round_per_line"]),
            globally=self._mx_fmt(ctx, taxes["round_globally"]), unit=self._mx_fmt(ctx, taxes["unit"]),
            odoo=ROUNDING_LABELS.get(current, current), target=ROUNDING_LABELS[target])
        if target == "unit":
            fix = {"fix_type": False, "fix_label": _(
                "Odoo cannot round per unit: set Magento's 'Tax Calculation Method Based On' to "
                "Row Total or Total")}
        elif ctx.instance.tax_rounding_method != target:
            fix = self._mx_fix_instance(ctx, _("Set the instance Tax Rounding to '%s', then Apply to Company",
                                               ROUNDING_LABELS[target]))
        else:
            fix = {"fix_type": "apply_rounding", "fix_res_ids": str(ctx.instance.id),
                   "fix_label": _("Apply '%s' rounding to the company", ROUNDING_LABELS[target])}

        def switch(target=target):
            model["mode"] = target

        self._mx_step(ctx, model, "rounding", _("Tax rounding (%s)", ROUNDING_LABELS[target]),
                      evidence, switch, fix=fix)

    @api.model
    def _mx_price_digits(self):
        """Precision of order line unit prices: the field's digits when it has some (prices are
        then stored rounded), else Magento's 4 decimals (Odoo 19 keeps the full price)."""
        digits = self.env["sale.order.line"]._fields["price_unit"].get_digits(self.env)
        return digits[1] if digits else 4

    @api.model
    def _mx_context(self, order, data):
        instance = order.magento_instance_id
        currency = order.currency_id or order.company_id.currency_id or self.env.company.currency_id
        items = OrderedDict()
        for item in order._magento_effective_items(data.get("items")):
            key = str(item.get("item_id") or "") or "sku:%s" % (item.get("sku") or "")
            items[key] = item
        applied, by_item, shipping_rates = self._mx_tax_details(data)
        grand_total = data.get("grand_total")
        if grand_total in (None, ""):
            grand_total = data.get("base_grand_total")
        ctx = _Ctx(
            order=order, instance=instance, data=data, currency=currency, items=items,
            applied=applied, by_item=by_item, shipping_rates=shipping_rates,
            grand_total=_f(grand_total),
            price_digits=self._mx_price_digits(),
            company_mode=order.company_id.tax_calculation_rounding_method or "round_per_line",
            dtc_total=_f(data.get("discount_tax_compensation_amount"))
            + _f(data.get("shipping_discount_tax_compensation_amount")),
            components=[],
        )
        incl, reason = self._mx_infer_prices_incl(ctx)
        if incl is None:
            incl = bool(instance.prices_include_tax)
            reason = _("instance setting; the Magento order gives no clue")
        ctx.incl = incl
        ctx.incl_reason = reason
        ship_incl = self._mx_infer_shipping_incl(ctx)
        ctx.ship_incl = bool(instance.shipping_includes_tax) if ship_incl is None else ship_incl
        after = self._mx_infer_after_discount(ctx)
        if after is None:
            ctx.after_discount = bool(instance.tax_after_discount)
            ctx.after_reason = _("instance setting")
        else:
            ctx.after_discount = after
            ctx.after_reason = _("the item tax amounts match")
        return ctx

    @api.model
    def _mx_explain(self, order, data):
        """Explanation values (with ``lines``) of ``order`` against the Magento payload."""
        order.ensure_one()
        ctx = self._mx_context(order, data)
        currency = ctx.currency
        odoo_total = order.amount_total
        difference = currency.round(ctx.grand_total - odoo_total)

        model = self._mx_odoo_model(ctx)
        calibration = currency.round(odoo_total - self._mx_total(ctx, model))
        self._mx_step_extra_lines(ctx, model)
        self._mx_step_missing_lines(ctx, model)
        self._mx_step_product_lines(ctx, model)
        self._mx_step_discounts(ctx, model)
        self._mx_step_fpt(ctx, model)
        self._mx_step_shipping(ctx, model)
        self._mx_step_order_amounts(ctx, model)
        self._mx_step_rounding(ctx, model)

        components = ctx.components
        explained = currency.round(sum(c["amount"] for c in components))
        residual = currency.round(difference - explained)
        final_total = self._mx_total(ctx, model)
        raw_gap = ctx.grand_total - final_total
        if (not currency.is_zero(residual)
                and abs(residual) <= currency.rounding + 1e-9
                and abs(raw_gap) < currency.rounding - 1e-9):
            components.append({
                "category": "currency_rounding",
                "name": _("Currency rounding"),
                "amount": residual,
                "negligible": True,
                "evidence": _(
                    "Magento grand total %(raw)s, Odoo total %(odoo)s: the remaining %(amount)s comes "
                    "from rounding amounts to %(digits)s decimals. Negligible.",
                    raw=ctx.grand_total, odoo=self._mx_fmt(ctx, odoo_total),
                    amount=self._mx_fmt(ctx, residual), digits=currency.decimal_places),
            })
            explained = currency.round(explained + residual)
            residual = currency.round(difference - explained)
        elif currency.is_zero(difference) and abs(ctx.grand_total - odoo_total) > 1e-6:
            components.append({
                "category": "currency_rounding",
                "name": _("Currency rounding"),
                "amount": 0.0,
                "negligible": True,
                "evidence": _("Magento grand total %(raw)s differs from the Odoo total %(odoo)s by less "
                              "than the currency rounding. Negligible.",
                              raw=ctx.grand_total, odoo=self._mx_fmt(ctx, odoo_total)),
            })
        evidence = [_("Difference %(diff)s minus the explained components %(explained)s.",
                      diff=self._mx_fmt(ctx, difference), explained=self._mx_fmt(ctx, explained))]
        if not currency.is_zero(calibration):
            evidence.append(_(
                "Odoo's total is %(odoo)s while the explainer recomputes %(model)s from the same lines "
                "(taxes it cannot recompute, or Odoo-specific rounding).",
                odoo=self._mx_fmt(ctx, odoo_total), model=self._mx_fmt(ctx, odoo_total - calibration)))
        if not currency.is_zero(residual):
            evidence.append(_("No known cause reproduces this amount: compare the order with Magento."))
        components.append({
            "category": "unexplained",
            "name": _("Unexplained remainder"),
            "amount": residual,
            "evidence": " ".join(evidence),
        })
        known = [c for c in components if c["category"] != "unexplained" and not currency.is_zero(c["amount"])]
        if currency.is_zero(residual):
            state = "explained"
        elif known:
            state = "partially"
        else:
            state = "unexplained"
        for sequence, vals in enumerate(components, start=1):
            vals["sequence"] = sequence * 10
        return {
            "instance_id": ctx.instance.id,
            "company_id": order.company_id.id,
            "currency_id": currency.id,
            "magento_increment_id": order.magento_increment_id or ctx.data.get("increment_id") or False,
            "magento_grand_total": ctx.grand_total,
            "odoo_total": odoo_total,
            "difference": difference,
            "explained_amount": explained,
            "unexplained_amount": residual,
            "calibration_gap": calibration,
            "state": state,
            "matches": currency.is_zero(difference),
            "component_count": len(known),
            "lines": components,
        }


class MagentoTotalsExplanationLine(models.Model):
    _name = "magento.totals.explanation.line"
    _description = "Magento Totals Explanation Component"
    _order = "explanation_id, sequence, id"

    explanation_id = fields.Many2one("magento.totals.explanation", required=True, index=True,
                                     ondelete="cascade")
    sequence = fields.Integer(default=10)
    sale_order_id = fields.Many2one(related="explanation_id.sale_order_id", store=True, index=True)
    instance_id = fields.Many2one(related="explanation_id.instance_id", store=True)
    company_id = fields.Many2one(related="explanation_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="explanation_id.currency_id")
    category = fields.Selection(CATEGORIES, required=True, index=True)
    name = fields.Char(string="Component", required=True)
    amount = fields.Monetary(
        aggregator="sum",
        help="Effect on the difference (Magento minus Odoo): positive when Magento is higher.",
    )
    negligible = fields.Boolean()
    evidence = fields.Text()
    sale_line_id = fields.Many2one("sale.order.line", string="Order Line", ondelete="set null")
    magento_item_id = fields.Char(string="Magento Item")
    fix_type = fields.Selection(FIX_TYPES)
    fix_label = fields.Char(string="Suggested Fix")
    fix_res_ids = fields.Char(help="Ids of the records the suggested fix opens.")

    def _fix_ids(self):
        self.ensure_one()
        return [int(i) for i in (self.fix_res_ids or "").split(",") if i.strip().isdigit()]

    def action_apply_fix(self):
        self.ensure_one()
        ids = self._fix_ids()
        explanation = self.explanation_id
        if self.fix_type == "open_instance":
            return {
                "type": "ir.actions.act_window",
                "name": self.fix_label,
                "res_model": "magento.instance",
                "res_id": ids[0] if ids else explanation.instance_id.id,
                "view_mode": "form",
                "views": [(False, "form")],
            }
        if self.fix_type == "apply_rounding":
            if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
                raise AccessError(_("Only Magento managers can change the company tax rounding."))
            instance = self.env["magento.instance"].browse(ids[:1]) or explanation.instance_id
            instance.action_apply_tax_rounding_to_company()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Tax rounding"),
                    "message": _("%(company)s now rounds taxes like Magento (%(mode)s). New orders "
                                 "use it; explain this order again after re-importing it.",
                                 company=instance.company_id.name,
                                 mode=ROUNDING_LABELS.get(instance.tax_rounding_method, "")),
                    "type": "success",
                    "sticky": False,
                    "next": {"type": "ir.actions.client", "tag": "soft_reload"},
                },
            }
        if self.fix_type == "open_tax_map":
            domain = [("id", "in", ids)] if ids else [("instance_id", "=", explanation.instance_id.id)]
            return {
                "type": "ir.actions.act_window",
                "name": _("Tax Mapping"),
                "res_model": "magento.tax.map",
                "view_mode": "tree",
                "views": [(False, "tree")],
                "domain": domain,
                "context": {"active_test": False},
            }
        if self.fix_type == "open_order":
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.order",
                "res_id": ids[0] if ids else explanation.sale_order_id.id,
                "view_mode": "form",
                "views": [(False, "form")],
            }
        raise UserError(self.fix_label or _("There is no automatic fix for this component."))


class SaleOrder(models.Model):
    _inherit = "sale.order"

    magento_explanation_id = fields.Many2one(
        "magento.totals.explanation", string="Totals Explanation",
        compute="_compute_magento_explanation", groups="sdlc_magento_connector.group_magento_user",
    )
    magento_explanation_state = fields.Selection(
        STATES, string="Totals Explanation State", compute="_compute_magento_explanation",
        groups="sdlc_magento_connector.group_magento_user",
    )

    def _compute_magento_explanation(self):
        ids = [i for i in self.ids if isinstance(i, int)]
        found = {}
        if ids:
            for explanation in self.env["magento.totals.explanation"].sudo().search(
                    [("sale_order_id", "in", ids)]):
                found[explanation.sale_order_id.id] = explanation
        for order in self:
            explanation = found.get(order.id)
            order.magento_explanation_id = explanation.id if explanation else False
            order.magento_explanation_state = explanation.state if explanation else False

    def action_magento_explain_totals(self):
        """'Why does this differ?': explain the order against Magento and open the result."""
        explanations = self.env["magento.totals.explanation"]._explain_orders(self)
        return explanations._action_open()

    def action_view_magento_explanation(self):
        self.ensure_one()
        explanation = self.env["magento.totals.explanation"].search(
            [("sale_order_id", "=", self.id)], limit=1)
        if not explanation:
            return self.action_magento_explain_totals()
        return explanation._action_open()


class MagentoReconciliationOrder(models.Model):
    _inherit = "magento.reconciliation.order"

    explanation_id = fields.Many2one("magento.totals.explanation", compute="_compute_explanation",
                                     string="Explanation")
    explanation_state = fields.Selection(STATES, compute="_compute_explanation", string="Explained")
    explanation_unexplained = fields.Monetary(compute="_compute_explanation", string="Unexplained",
                                              currency_field="currency_id")

    @api.depends("sale_order_id")
    def _compute_explanation(self):
        orders = self.sale_order_id
        found = {}
        if orders:
            for explanation in self.env["magento.totals.explanation"].search(
                    [("sale_order_id", "in", orders.ids)]):
                found[explanation.sale_order_id.id] = explanation
        for row in self:
            explanation = found.get(row.sale_order_id.id) if row.kind == "mismatch" else None
            row.explanation_id = explanation.id if explanation else False
            row.explanation_state = explanation.state if explanation else False
            row.explanation_unexplained = explanation.unexplained_amount if explanation else 0.0

    def action_explain(self):
        rows = self.filtered(lambda r: r.kind == "mismatch" and r.sale_order_id)
        if not rows:
            raise UserError(_("Only total mismatches with an Odoo order can be explained."))
        explanations = self.env["magento.totals.explanation"]._explain_orders(rows.sale_order_id)
        return explanations._action_open()


class MagentoReconciliation(models.Model):
    _inherit = "magento.reconciliation"

    explanation_count = fields.Integer(compute="_compute_explanation_count",
                                       string="Explanations")

    def _compute_explanation_count(self):
        Explanation = self.env["magento.totals.explanation"]
        for run in self:
            orders = run.finding_ids.filtered(lambda f: f.kind == "mismatch").sale_order_id
            run.explanation_count = Explanation.search_count(
                [("sale_order_id", "in", orders.ids)]) if orders else 0

    def action_explain_all(self):
        """Explain every total mismatch of the run(s); one failing order does not stop the rest."""
        rows = self.finding_ids.filtered(lambda f: f.kind == "mismatch" and f.sale_order_id)
        if not rows:
            raise UserError(_("There are no total mismatches to explain."))
        explanations, failures = self.env["magento.totals.explanation"]._explain_orders_safe(
            rows.sale_order_id)
        action = explanations._action_list() if explanations else False
        if not failures:
            return action
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Totals explanations"),
                "message": _("%(done)s order(s) explained, %(failed)s failed: %(errors)s",
                             done=len(explanations), failed=len(failures),
                             errors="; ".join("%s: %s" % failure for failure in failures[:5])),
                "type": "warning",
                "sticky": True,
                "next": action or {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_view_explanations(self):
        self.ensure_one()
        orders = self.finding_ids.filtered(lambda f: f.kind == "mismatch").sale_order_id
        return {
            "type": "ir.actions.act_window",
            "name": _("Totals Explanations"),
            "res_model": "magento.totals.explanation",
            "view_mode": "tree,form",
            "views": [(False, "tree"), (False, "form")],
            "domain": [("sale_order_id", "in", orders.ids)],
            "context": {"create": False},
        }
