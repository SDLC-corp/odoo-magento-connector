"""Order reconciliation (roadmap 1.6): Magento vs Odoo order counts and totals per day.

A ``magento.reconciliation`` run compares one instance over a date range:

* **Magento side**: ``GET /V1/orders`` with ``created_at`` inside the range, paged with
  ``iter_search`` and a ``fields`` projection so each page stays small, sorted by
  ``entity_id`` (stable while orders are updated during the paging).
* **Odoo side**: ``sale.order`` of the instance with ``magento_order_id`` set, whose
  stored Magento creation date (``magento_created_at``; ``date_order`` when it is empty)
  falls in the range, plus every Odoo order matching a Magento order of the range.

Days are **UTC days**: Magento stores ``created_at`` in UTC and the import copies it
unchanged into ``magento_created_at``, so both sides are bucketed on the same clock.

One line per (day, Magento store_id, order currency). A matched order is bucketed by its
Magento values, so both counts of a line describe the same orders. An Odoo order not found
in Magento ("extra") is bucketed by its own date and currency; it joins the line of that
day and currency when exactly one store has one, otherwise a store-less line.

Totals are never converted: Magento ``grand_total`` (order currency) is compared with the
Odoo ``amount_total`` per order, within the currency rounding. Canceled orders (Magento
status ``canceled``; for Odoo-only orders the Odoo state ``cancel``) are counted and take
part in the missing check, but are left out of the totals and of the per-order total check
unless ``include_canceled`` is set.

Each order-level finding (missing in Odoo, total mismatch, extra in Odoo) is one
``magento.reconciliation.order`` row, so every figure on the run opens its records.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.modules import module as odoo_module
from odoo.tools import float_compare

from ..services.magento_api import MagentoAPI
from .magento_job import exception_message
from . import cron_compat

_logger = logging.getLogger(__name__)

# Ranges longer than this are always reconciled by a queued job.
_SYNC_MAX_DAYS = 7
_MAX_RANGE_DAYS = 366
_ORDERS_ENDPOINT = "/rest/V1/orders"
_ORDER_FIELDS = (
    "items[entity_id,increment_id,created_at,grand_total,order_currency_code,store_id,status],"
    "total_count"
)
_CANCELED_STATUSES = frozenset({"canceled", "cancelled"})
_MISSING_MESSAGE = "Missing in Odoo (reconciliation)"

RUN_STATES = [
    ("draft", "Draft"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]
LINE_STATUSES = [
    ("match", "Match"),
    ("missing", "Missing"),
    ("mismatch", "Mismatch"),
]
FINDING_KINDS = [
    ("missing", "Missing in Odoo"),
    ("mismatch", "Total mismatch"),
    ("extra", "Not in Magento"),
]


def _to_float(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ref(value):
    """Normalised Magento entity id: ``"105"`` for 105, "105" or "105.0"."""
    text = str(value if value not in (None, False) else "").strip()
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text


def _join_refs(refs, limit=200):
    refs = list(refs)
    text = ", ".join(refs[:limit])
    if len(refs) > limit:
        text += " ... (+%d)" % (len(refs) - limit)
    return text or False


class MagentoReconciliation(models.Model):
    _name = "magento.reconciliation"
    _description = "Magento Order Reconciliation"
    _order = "date_to desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    instance_id = fields.Many2one(
        "magento.instance", required=True, index=True, ondelete="cascade",
        default=lambda self: self.env["magento.instance"].search([], limit=1),
    )
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    date_from = fields.Date(
        required=True, default=lambda self: fields.Date.context_today(self) - timedelta(days=7),
        help="First Magento order creation day (UTC) included.",
    )
    date_to = fields.Date(
        required=True, default=lambda self: fields.Date.context_today(self) - timedelta(days=1),
        help="Last Magento order creation day (UTC) included.",
    )
    include_canceled = fields.Boolean(
        string="Include Canceled in Totals",
        help="Canceled orders are always counted and checked for presence in Odoo. When this is "
             "off they are left out of the totals and of the per-order total check.",
    )
    state = fields.Selection(RUN_STATES, default="draft", required=True, readonly=True, index=True)
    trigger = fields.Selection(
        [("manual", "Manual"), ("cron", "Scheduled")], default="manual", required=True, readonly=True,
    )
    date_run = fields.Datetime(string="Last Run", readonly=True)
    job_id = fields.Many2one("magento.job", string="Job", readonly=True, ondelete="set null")
    note = fields.Text(readonly=True)
    line_ids = fields.One2many("magento.reconciliation.line", "reconciliation_id", string="Days", readonly=True)
    finding_ids = fields.One2many("magento.reconciliation.order", "reconciliation_id", string="Findings", readonly=True)

    magento_count = fields.Integer(string="Magento Orders", readonly=True)
    odoo_count = fields.Integer(string="Odoo Orders", readonly=True)
    matched_count = fields.Integer(string="Matched", readonly=True)
    canceled_count = fields.Integer(string="Canceled", readonly=True)
    missing_count = fields.Integer(string="Missing in Odoo", readonly=True)
    mismatch_count = fields.Integer(string="Total Mismatches", readonly=True)
    extra_count = fields.Integer(string="Not in Magento", readonly=True)
    issue_count = fields.Integer(
        string="Finding Count", compute="_compute_issue_count",
        help="Missing, mismatched and extra orders.",
    )
    line_count = fields.Integer(compute="_compute_issue_count")

    @api.depends("instance_id.name", "date_from", "date_to")
    def _compute_name(self):
        for run in self:
            run.name = "%s: %s to %s" % (run.instance_id.name or "", run.date_from or "", run.date_to or "")

    @api.depends("missing_count", "mismatch_count", "extra_count", "line_ids")
    def _compute_issue_count(self):
        for run in self:
            run.issue_count = run.missing_count + run.mismatch_count + run.extra_count
            run.line_count = len(run.line_ids)

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for run in self:
            if run.date_from and run.date_to:
                if run.date_from > run.date_to:
                    raise ValidationError(_("The start date must be on or before the end date."))
                if (run.date_to - run.date_from).days + 1 > _MAX_RANGE_DAYS:
                    raise ValidationError(_("Reconcile at most %s days at a time.", _MAX_RANGE_DAYS))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _range_days(self):
        self.ensure_one()
        return (self.date_to - self.date_from).days + 1

    def _identity_key(self):
        self.ensure_one()
        return "recon-%s-%s-%s" % (self.instance_id.id, self.date_from, self.date_to)

    def action_run(self):
        """Reconcile now; ranges longer than a week go to the job queue instead."""
        for run in self:
            if run.state == "running":
                raise UserError(_("This reconciliation is already running."))
        if len(self) > 1 or any(run._range_days() > _SYNC_MAX_DAYS for run in self):
            return self.action_run_in_background()
        self._execute()
        return True

    def action_run_in_background(self):
        jobs = self.env["magento.job"]
        for run in self:
            job = jobs.enqueue(
                run, "_job_run",
                instance=run.instance_id,
                channel="reconciliation",
                identity_key=run._identity_key(),
                description=_("Reconcile %(name)s", name=run.name),
                max_attempts=3,
            )
            run.write({"state": "running", "job_id": job.id,
                       "note": _("Queued; the job queue runs it within a minute.")})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reconciliation"),
                "message": _("%s reconciliation(s) queued.", len(self)),
                "type": "info",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _job_run(self):
        """Job entry point: never raises, the run records its own failure."""
        self._execute()
        return {run.id: run.state for run in self}

    def action_reset(self):
        self.filtered(lambda r: r.state != "running").write({"state": "draft"})
        return True

    def action_reimport_missing(self):
        """Queue ``instance._magento_resync_one("order", id)`` per missing order (deduplicated)."""
        findings = self.finding_ids.filtered(lambda f: f.kind == "missing")
        if not findings:
            raise UserError(_("There are no missing orders to re-import."))
        queued = findings._enqueue_reimport()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reconciliation"),
                "message": _("%s re-import job(s) queued.", queued),
                "type": "info",
                "sticky": False,
            },
        }

    def _findings_action(self, kind, name):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": "magento.reconciliation.order",
            "view_mode": "list,form",
            "domain": [("reconciliation_id", "=", self.id), ("kind", "=", kind)],
            "context": {"create": False},
        }

    def action_view_missing(self):
        return self._findings_action("missing", _("Missing in Odoo"))

    def action_view_findings(self):
        self.ensure_one()
        action = self._findings_action("missing", _("Findings"))
        action["domain"] = [("reconciliation_id", "=", self.id)]
        action["context"] = {"create": False, "search_default_group_kind": 1}
        return action

    def _orders_action(self, kinds, name):
        self.ensure_one()
        orders = self.finding_ids.filtered(lambda f: f.kind in kinds).sale_order_id
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": "sale.order",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": [("id", "in", orders.ids)],
            "context": {"create": False},
        }

    def action_view_mismatch_orders(self):
        return self._orders_action(("mismatch",), _("Mismatched Orders"))

    def action_view_extra_orders(self):
        return self._orders_action(("extra",), _("Orders Not in Magento"))

    def action_view_lines(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Reconciliation by Currency"),
            "res_model": "magento.reconciliation.line",
            "view_mode": "list",
            "domain": [("reconciliation_id", "=", self.id)],
            "context": {"create": False, "search_default_group_currency": 1},
        }

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    def _execute(self):
        """Run every reconciliation of ``self``; a failure marks that run failed."""
        for run in self:
            try:
                with self.env.cr.savepoint():
                    run._compare()
            except Exception as exc:  # noqa: BLE001 - the run records its own failure
                _logger.info("Magento reconciliation %s failed: %s", run.id, exc)
                run.write({
                    "state": "failed",
                    "date_run": fields.Datetime.now(),
                    "note": _("Reconciliation failed: %s", exception_message(exc) or repr(exc)),
                })
        return True

    def _utc_bounds(self):
        start = datetime.combine(self.date_from, time.min)
        end = datetime.combine(self.date_to, time.min) + timedelta(days=1)
        return start, end

    def _fetch_magento_orders(self):
        """Magento orders created in the range: ``{entity_id: item}``."""
        self.ensure_one()
        start, end = self._utc_bounds()
        api_client = MagentoAPI(self.instance_id)
        filters = [
            ("created_at", start.strftime("%Y-%m-%d %H:%M:%S"), "gteq"),
            ("created_at", (end - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), "lteq"),
        ]
        page_size = self.instance_id._get_sync_page_size() if hasattr(
            self.instance_id, "_get_sync_page_size") else MagentoAPI.DEFAULT_PAGE_SIZE
        orders = {}
        for item in api_client.iter_search(
            _ORDERS_ENDPOINT, filters=filters, sort=(("entity_id", "ASC"),),
            page_size=page_size, fields=_ORDER_FIELDS, id_key="entity_id",
        ):
            if not isinstance(item, dict):
                continue
            ref = _ref(item.get("entity_id"))
            if ref:
                orders[ref] = item
        return orders

    def _fetch_odoo_orders(self, magento_refs):
        """Odoo orders of the instance created in the range or matching a Magento order."""
        self.ensure_one()
        start, end = self._utc_bounds()
        orders = self.env["sale.order"].sudo()
        base = [("magento_instance_id", "=", self.instance_id.id), ("magento_order_id", "!=", False)]
        in_range = base + [
            "|",
            "&", ("magento_created_at", ">=", start), ("magento_created_at", "<", end),
            "&", "&", ("magento_created_at", "=", False), ("date_order", ">=", start), ("date_order", "<", end),
        ]
        found = orders.search(in_range)
        refs = list(magento_refs)
        for idx in range(0, len(refs), 1000):
            found |= orders.search(base + [("magento_order_id", "in", refs[idx:idx + 1000])])
        return found

    def _currency(self, code, cache):
        code = (code or "").strip().upper()
        if code not in cache:
            cache[code] = self.env["res.currency"].with_context(active_test=False).search(
                [("name", "=", code)], limit=1) if code else self.env["res.currency"]
        return cache[code]

    @staticmethod
    def _amounts_differ(currency, a, b):
        if currency:
            return currency.compare_amounts(a, b) != 0
        return float_compare(a, b, precision_digits=2) != 0

    def _compare(self):
        self.ensure_one()
        self.write({"state": "running", "note": False})
        # Internal cleanup of the previous result: operators may re-run but not delete.
        self.finding_ids.sudo().unlink()
        self.line_ids.sudo().unlink()

        magento = self._fetch_magento_orders()
        odoo_orders = self._fetch_odoo_orders(magento.keys())
        by_ref = {}
        for order in odoo_orders:
            by_ref.setdefault(_ref(order.magento_order_id), order)

        currencies = {}
        include_canceled = self.include_canceled
        buckets = defaultdict(lambda: {
            "magento_count": 0, "odoo_count": 0, "canceled_count": 0,
            "magento_total": 0.0, "odoo_total": 0.0,
            "missing": [], "mismatch": [], "extra": [],
        })
        findings = []
        counts = defaultdict(int)

        for ref in sorted(magento, key=lambda r: (len(r), r)):
            item = magento[ref]
            created = fields.Datetime.to_datetime(item.get("created_at")) if item.get("created_at") else False
            day = created.date() if created else self.date_from
            store = int(_to_float(item.get("store_id")))
            currency = self._currency(item.get("order_currency_code"), currencies)
            key = (day, store, currency.id or False, (item.get("order_currency_code") or "").upper())
            bucket = buckets[key]
            canceled = (item.get("status") or "").lower() in _CANCELED_STATUSES
            in_totals = include_canceled or not canceled
            grand_total = _to_float(item.get("grand_total"))
            bucket["magento_count"] += 1
            counts["magento"] += 1
            if canceled:
                bucket["canceled_count"] += 1
                counts["canceled"] += 1
            if in_totals:
                bucket["magento_total"] += grand_total
            order = by_ref.pop(ref, None)
            finding = {
                "key": key, "magento_order_id": ref,
                "increment_id": item.get("increment_id") or False,
                "magento_created_at": created or False,
                "store_id": store, "currency_id": currency.id or False,
                "magento_total": grand_total, "magento_status": item.get("status") or False,
                "canceled": canceled,
            }
            if not order:
                bucket["missing"].append(ref)
                counts["missing"] += 1
                findings.append(dict(finding, kind="missing"))
                continue
            bucket["odoo_count"] += 1
            counts["odoo"] += 1
            counts["matched"] += 1
            if in_totals:
                bucket["odoo_total"] += order.amount_total
            same_currency = not currency or order.currency_id == currency
            if in_totals and (not same_currency or self._amounts_differ(
                    currency or order.currency_id, grand_total, order.amount_total)):
                bucket["mismatch"].append(ref)
                counts["mismatch"] += 1
                findings.append(dict(
                    finding, kind="mismatch", sale_order_id=order.id, odoo_total=order.amount_total,
                    odoo_currency_id=order.currency_id.id,
                ))

        # Odoo orders that Magento did not return for the range.
        stores_by_day = defaultdict(set)
        for day, store, currency_id, code in buckets:
            stores_by_day[(day, currency_id, code)].add(store)
        for ref, order in sorted(by_ref.items()):
            created = order.magento_created_at or order.date_order
            day = created.date() if created else self.date_from
            code = (order.magento_currency or order.currency_id.name or "").upper()
            currency = self._currency(code, currencies) or order.currency_id
            stores = stores_by_day.get((day, currency.id or False, code), set())
            store = next(iter(stores)) if len(stores) == 1 else 0
            key = (day, store, currency.id or False, code)
            bucket = buckets[key]
            canceled = order.state == "cancel" or (order.magento_status or "").lower() in _CANCELED_STATUSES
            bucket["odoo_count"] += 1
            counts["odoo"] += 1
            if canceled:
                bucket["canceled_count"] += 1
                counts["canceled"] += 1
            if include_canceled or not canceled:
                bucket["odoo_total"] += order.amount_total
            bucket["extra"].append(order.name)
            counts["extra"] += 1
            findings.append({
                "key": key, "kind": "extra", "magento_order_id": ref,
                "increment_id": order.magento_increment_id or False,
                "magento_created_at": order.magento_created_at or False,
                "store_id": store, "currency_id": order.currency_id.id or False,
                "odoo_total": order.amount_total, "sale_order_id": order.id,
                "magento_status": order.magento_status or False, "canceled": canceled,
            })

        Line = self.env["magento.reconciliation.line"]
        line_by_key = {}
        for key in sorted(buckets, key=lambda k: (k[0], k[1], k[3])):
            day, store, currency_id, code = key
            data = buckets[key]
            line_by_key[key] = Line.create({
                "reconciliation_id": self.id,
                "day": day,
                "store_id": store,
                "currency_id": currency_id,
                "currency_code": code or False,
                "magento_count": data["magento_count"],
                "odoo_count": data["odoo_count"],
                "canceled_count": data["canceled_count"],
                "magento_total": data["magento_total"],
                "odoo_total": data["odoo_total"],
                "missing_count": len(data["missing"]),
                "mismatch_count": len(data["mismatch"]),
                "extra_count": len(data["extra"]),
                "missing_magento_ids": _join_refs(data["missing"]),
                "mismatch_magento_ids": _join_refs(data["mismatch"]),
                "extra_odoo_ids": _join_refs(data["extra"]),
            })
        rows = []
        for finding in findings:
            vals = dict(finding)
            key = vals.pop("key")
            vals.pop("odoo_currency_id", None)
            vals.update(reconciliation_id=self.id, line_id=line_by_key[key].id)
            rows.append(vals)
        self.env["magento.reconciliation.order"].create(rows)

        summary = _(
            "%(magento)s Magento and %(odoo)s Odoo orders: %(missing)s missing in Odoo, "
            "%(mismatch)s total mismatches, %(extra)s not in Magento.",
            magento=counts["magento"], odoo=counts["odoo"], missing=counts["missing"],
            mismatch=counts["mismatch"], extra=counts["extra"],
        )
        self.write({
            "state": "done",
            "date_run": fields.Datetime.now(),
            "magento_count": counts["magento"],
            "odoo_count": counts["odoo"],
            "matched_count": counts["matched"],
            "canceled_count": counts["canceled"],
            "missing_count": counts["missing"],
            "mismatch_count": counts["mismatch"],
            "extra_count": counts["extra"],
            "note": summary,
        })
        return True

    # ------------------------------------------------------------------
    # Error Center + cron
    # ------------------------------------------------------------------
    def _record_missing_issues(self):
        """One transient Error Center issue per missing order, so it can be retried there."""
        issues = self.env["magento.sync.issue"]
        recorded = issues
        for finding in self.finding_ids.filtered(lambda f: f.kind == "missing"):
            recorded |= issues._record_failure(
                finding.reconciliation_id.instance_id, "order", finding.magento_order_id,
                _MISSING_MESSAGE, severity="transient",
                payload={
                    "entity_id": finding.magento_order_id,
                    "increment_id": finding.increment_id,
                    "created_at": fields.Datetime.to_string(finding.magento_created_at)
                    if finding.magento_created_at else None,
                    "grand_total": finding.magento_total,
                    "source": "reconciliation %s" % finding.reconciliation_id.id,
                },
            )
        return recorded

    @api.model
    def _cron_can_commit(self):
        in_test_mode = getattr(self.env.registry, "in_test_mode", None)
        return bool(
            self.env.context.get("ir_cron_progress_id")
            and self.env.context.get("cron_id")
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    @api.model
    def _cron_reconcile_yesterday(self):
        """Reconcile the last ``reconcile_lookback_days`` UTC days (up to yesterday) of every
        active instance with ``reconcile_daily``; missing orders go to the Error Center."""
        today = fields.Datetime.now().date()
        date_to = today - timedelta(days=1)
        runs = self.browse()
        instances = self.env["magento.instance"].sudo().search([("reconcile_daily", "=", True)])
        for instance in instances:
            lookback = max(1, instance.reconcile_lookback_days or 1)
            run = self.sudo().with_company(instance.company_id).create({
                "instance_id": instance.id,
                "date_from": date_to - timedelta(days=lookback - 1),
                "date_to": date_to,
                "trigger": "cron",
            })
            run._execute()
            if run.state == "done":
                run._record_missing_issues()
            runs |= run
            if self._cron_can_commit():
                cron_compat.commit_progress(self.env, 1)
        return runs


class MagentoReconciliationLine(models.Model):
    _name = "magento.reconciliation.line"
    _description = "Magento Reconciliation Day"
    _order = "day, store_id, currency_code, id"

    reconciliation_id = fields.Many2one(
        "magento.reconciliation", required=True, index=True, ondelete="cascade",
    )
    instance_id = fields.Many2one(related="reconciliation_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="reconciliation_id.company_id", store=True, index=True)
    day = fields.Date(required=True, help="Magento order creation day (UTC).")
    store_id = fields.Integer(string="Store", help="Magento store_id; 0 when unknown (orders only in Odoo).")
    currency_id = fields.Many2one("res.currency", string="Currency")
    currency_code = fields.Char(string="Currency Code")
    magento_count = fields.Integer(string="Magento", aggregator="sum")
    odoo_count = fields.Integer(string="Odoo", aggregator="sum")
    canceled_count = fields.Integer(string="Canceled", aggregator="sum")
    magento_total = fields.Monetary(string="Magento Total", currency_field="currency_id", aggregator="sum")
    odoo_total = fields.Monetary(string="Odoo Total", currency_field="currency_id", aggregator="sum")
    difference = fields.Monetary(
        compute="_compute_difference", store=True, currency_field="currency_id", aggregator="sum",
        help="Magento total minus Odoo total.",
    )
    missing_count = fields.Integer(string="Missing", aggregator="sum")
    mismatch_count = fields.Integer(string="Mismatch", aggregator="sum")
    extra_count = fields.Integer(string="Not in Magento", aggregator="sum")
    missing_magento_ids = fields.Text(string="Missing Magento IDs",
                                      help="Magento entity_ids with no Odoo order.")
    mismatch_magento_ids = fields.Text(string="Mismatched Magento IDs",
                                       help="Magento entity_ids whose Odoo total differs.")
    extra_odoo_ids = fields.Text(string="Odoo Orders Not in Magento",
                                 help="Odoo Magento orders that Magento did not return for this range.")
    status = fields.Selection(LINE_STATUSES, compute="_compute_status", store=True, index=True)

    @api.depends("magento_total", "odoo_total")
    def _compute_difference(self):
        for line in self:
            line.difference = line.magento_total - line.odoo_total

    @api.depends("missing_count", "mismatch_count", "extra_count", "magento_count", "odoo_count",
                 "magento_total", "odoo_total", "currency_id")
    def _compute_status(self):
        for line in self:
            if line.missing_count:
                line.status = "missing"
            elif (line.mismatch_count or line.extra_count or line.magento_count != line.odoo_count
                  or MagentoReconciliation._amounts_differ(line.currency_id, line.magento_total, line.odoo_total)):
                line.status = "mismatch"
            else:
                line.status = "match"

    def action_view_findings(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Findings"),
            "res_model": "magento.reconciliation.order",
            "view_mode": "list,form",
            "domain": [("line_id", "=", self.id)],
            "context": {"create": False},
        }


class MagentoReconciliationOrder(models.Model):
    _name = "magento.reconciliation.order"
    _description = "Magento Reconciliation Finding"
    _order = "kind, magento_created_at, id"
    _rec_name = "magento_order_id"

    reconciliation_id = fields.Many2one(
        "magento.reconciliation", required=True, index=True, ondelete="cascade",
    )
    line_id = fields.Many2one("magento.reconciliation.line", index=True, ondelete="cascade")
    instance_id = fields.Many2one(related="reconciliation_id.instance_id", store=True, index=True)
    company_id = fields.Many2one(related="reconciliation_id.company_id", store=True, index=True)
    kind = fields.Selection(FINDING_KINDS, required=True, index=True)
    magento_order_id = fields.Char(string="Magento ID", required=True)
    increment_id = fields.Char(string="Magento Order #")
    magento_created_at = fields.Datetime(string="Created in Magento")
    store_id = fields.Integer(string="Store")
    magento_status = fields.Char()
    canceled = fields.Boolean()
    currency_id = fields.Many2one("res.currency")
    magento_total = fields.Monetary(currency_field="currency_id")
    odoo_total = fields.Monetary(currency_field="currency_id")
    difference = fields.Monetary(compute="_compute_difference", store=True, currency_field="currency_id")
    sale_order_id = fields.Many2one("sale.order", string="Odoo Order", ondelete="set null")
    job_id = fields.Many2one("magento.job", string="Re-import Job", readonly=True, ondelete="set null")
    job_state = fields.Selection(related="job_id.state", string="Re-import")

    @api.depends("magento_total", "odoo_total", "kind")
    def _compute_difference(self):
        for row in self:
            row.difference = row.magento_total - row.odoo_total if row.kind == "mismatch" else 0.0

    def _enqueue_reimport(self):
        jobs = self.env["magento.job"]
        queued = 0
        for row in self.filtered(lambda r: r.kind in ("missing", "mismatch")):
            instance = row.reconciliation_id.instance_id
            job = jobs.enqueue(
                instance, "_magento_resync_one",
                args=("order", row.magento_order_id),
                instance=instance,
                channel="orders",
                identity_key="retry-%s-order-%s" % (instance.id, row.magento_order_id),
                description=_("Re-import Magento order %s", row.increment_id or row.magento_order_id),
            )
            row.job_id = job.id
            queued += 1
        return queued

    def action_reimport(self):
        queued = self._enqueue_reimport()
        if not queued:
            raise UserError(_("Only missing or mismatched orders can be re-imported."))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reconciliation"),
                "message": _("%s re-import job(s) queued.", queued),
                "type": "info",
                "sticky": False,
            },
        }

    def action_open_order(self):
        self.ensure_one()
        if not self.sale_order_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": self.sale_order_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    reconcile_daily = fields.Boolean(
        string="Reconcile Orders Daily",
        help="Each night, compare Magento and Odoo orders of the last days; orders missing in "
             "Odoo are listed in the Error Center, ready to retry.",
    )
    reconcile_lookback_days = fields.Integer(
        string="Reconcile Last (days)", default=1,
        help="Days, up to yesterday (UTC), that the nightly reconciliation compares.",
    )

    @api.constrains("reconcile_lookback_days")
    def _check_reconcile_lookback_days(self):
        for instance in self:
            if not 1 <= instance.reconcile_lookback_days <= 31:
                raise ValidationError(_("The reconciliation look-back must be between 1 and 31 days."))
