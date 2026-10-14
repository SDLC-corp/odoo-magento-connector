"""Dry-run preview and Data Ownership guards (roadmap 3.8).

Dry-run
-------
``magento.dryrun`` reads records from Magento as a sync would, then runs the *real*
per-record import (``magento.instance._sync_<entity>_record``) for each one inside a
database SAVEPOINT that is always rolled back. Before the rollback it reads the Odoo
records the import touched; after it, it reads what they were before. The difference
is stored as a ``magento.dryrun.line`` (create / update / unchanged / skip / error, with
a field-level diff). Nothing of the import survives:

* the savepoint is rolled back (``_DryRunRollback``), and the ORM cache with it;
* post-commit callbacks registered by the import (immediate pushes, cron triggers)
  are restored to what they were before the record ran;
* ``ir.sequence`` hands out placeholders, so no document number is consumed;
* every Magento write is refused: ``MagentoAPI._request`` raises
  ``MagentoDryRunWriteBlocked`` for any non-GET call while ``DRY_RUN`` is set (and for
  any API built from an environment with ``magento_dry_run`` in its context), and the
  context also carries ``magento_skip_push``, so no push job is queued.

"Apply" queues the real import of the previewed references through the job queue.

Data Ownership
--------------
The import side is guarded in magento_instance.py. The push side is guarded here, by
overriding the queue entry points: ``_magento_enqueue_push`` / ``_magento_auto_push``
of the push mixin (products, categories, customers), the stock push
(``_magento_push_stock_maps``), the price push (``_magento_push_prices``) and the
order pushes on ``sale.order``. A blocked push is skipped with its reason, never
raised as an error, except for buttons a user clicks (they get a clear message).
"""
import itertools
import json
import logging
import re
from contextlib import contextmanager

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

from ..services.magento_api import DRY_RUN, MagentoAPI, MagentoDryRunWriteBlocked
from .magento_instance import OWNERSHIP_ENTITIES, _short_reason, _SyncStats
from . import orm_compat

_logger = logging.getLogger(__name__)

DRYRUN_ENTITIES = [
    ("product", "Products"),
    ("category", "Categories"),
    ("customer", "Customers"),
    ("order", "Orders"),
]
LINE_ACTIONS = [
    ("create", "Create"),
    ("update", "Update"),
    ("unchanged", "No change"),
    ("skip", "Skip"),
    ("error", "Error"),
]
_APPLY_ACTIONS = ("create", "update")

# Field-level diff: which fields of which model are compared.
_DIFF_TYPES = frozenset({
    "char", "text", "html", "integer", "float", "monetary", "boolean", "selection", "date",
    "datetime", "many2one", "many2many",
})
_DIFF_SKIP = frozenset({
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date",
    "magento_last_error", "last_stock_push_at", "last_pushed_qty", "company_id",
})
_DIFF_NOISE = re.compile(r"(hash|snapshot|payload|_json$|last_sync|synced_at|_seen$|^message_|^activity_)")
# Core Odoo models: only these fields (plus any ``magento_*`` field) are compared.
_DIFF_CURATED = {
    "product.product": {
        "name", "default_code", "barcode", "list_price", "standard_price", "type", "categ_id",
        "active", "sale_ok", "description_sale", "weight", "volume", "detailed_type",
    },
    "product.template": {
        "name", "default_code", "barcode", "list_price", "standard_price", "type", "categ_id",
        "active", "sale_ok", "description_sale", "weight", "volume", "detailed_type",
    },
    "res.partner": {
        "name", "email", "phone", "street", "street2", "city", "zip", "state_id", "country_id",
        "vat", "company_type", "parent_id", "active", "type",
    },
    "sale.order": {
        "name", "partner_id", "partner_invoice_id", "partner_shipping_id", "date_order", "state",
        "amount_untaxed", "amount_tax", "amount_total", "currency_id", "pricelist_id",
        "client_order_ref",
    },
}
_DIFF_VALUE_MAX = 500
_ORDER_LINES_KEY = "__order_lines__"

# Placeholders handed out by ir.sequence during a preview (never a real number).
_PREVIEW_SEQUENCE = itertools.count(1)


class _DryRunRollback(Exception):
    """Raised inside the preview savepoint to roll it back after reading the result."""

    def __init__(self, outcome, after, primary):
        super().__init__("dry-run rollback")
        self.outcome = outcome
        self.after = after
        self.primary = primary


def magento_dry_run_active(env):
    return bool(DRY_RUN.get() or env.context.get("magento_dry_run"))


@contextmanager
def _preserved_callbacks(cr):
    """Undo the post-commit callbacks (and their data) registered inside the block.

    A savepoint rollback discards the rows of the block, but not the Python callbacks
    it queued for after the commit (e.g. "run these push jobs now"). Data containers
    that existed before are restored in place: callbacks registered earlier keep their
    reference to them.
    """
    callbacks = cr.postcommit
    funcs = list(callbacks._funcs)
    saved = {}
    for key, value in callbacks.data.items():
        if isinstance(value, (set, list, dict)):
            saved[key] = (value, value.copy())
        else:
            saved[key] = (value, None)
    try:
        yield
    finally:
        callbacks._funcs.clear()
        callbacks._funcs.extend(funcs)
        for key in list(callbacks.data):
            if key not in saved:
                del callbacks.data[key]
        for key, (value, content) in saved.items():
            if content is not None:
                value.clear()
                if isinstance(value, dict):
                    value.update(content)
                elif isinstance(value, set):
                    value.update(content)
                else:
                    value.extend(content)
            callbacks.data[key] = value


class MagentoDryRun(models.Model):
    _name = "magento.dryrun"
    _description = "Magento Dry-run Preview"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    instance_id = fields.Many2one(
        "magento.instance", string="Instance", required=True, ondelete="cascade", index=True,
        default=lambda self: self.env["magento.instance"].search([("active", "=", True)], limit=1),
    )
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    entity = fields.Selection(DRYRUN_ENTITIES, required=True, default="product")
    limit = fields.Integer(
        default=50,
        help="Maximum number of Magento records previewed (1-500). Leave the references "
             "empty to preview the first records Magento returns (oldest change first).",
    )
    magento_refs = fields.Char(
        string="Magento References",
        help="Optional: preview only these records, separated by commas or spaces. "
             "SKUs for products, Magento ids for categories, customers and orders.",
    )
    updated_since = fields.Datetime(
        string="Changed Since",
        help="Optional: only records Magento changed after this time (not for categories).",
    )
    state = fields.Selection(
        [("draft", "Draft"), ("previewed", "Previewed"), ("applied", "Applied")],
        default="draft", required=True, readonly=True, copy=False,
    )
    previewed_at = fields.Datetime(readonly=True, copy=False)
    previewed_by = fields.Many2one("res.users", readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    line_ids = fields.One2many("magento.dryrun.line", "dryrun_id", string="Preview", copy=False)
    line_count = fields.Integer(compute="_compute_counts")
    create_count = fields.Integer(string="Creates", compute="_compute_counts")
    update_count = fields.Integer(string="Updates", compute="_compute_counts")
    unchanged_count = fields.Integer(string="No Change", compute="_compute_counts")
    skip_count = fields.Integer(string="Skips", compute="_compute_counts")
    error_count = fields.Integer(string="Errors", compute="_compute_counts")
    job_ids = fields.Many2many("magento.job", string="Apply Jobs", readonly=True, copy=False)
    job_count = fields.Integer(compute="_compute_counts")
    summary = fields.Char(readonly=True, copy=False)

    @api.depends("entity", "instance_id")
    def _compute_name(self):
        labels = dict(DRYRUN_ENTITIES)
        for run in self:
            run.name = _("%(entity)s preview (%(instance)s)",
                         entity=labels.get(run.entity, run.entity or ""),
                         instance=run.instance_id.name or "-")

    @api.depends("line_ids.action", "job_ids")
    def _compute_counts(self):
        rows = orm_compat.read_group(self.env["magento.dryrun.line"], 
            [("dryrun_id", "in", self.ids)], ["dryrun_id", "action"], ["__count"],
        )
        counts = {}
        for run, action, count in rows:
            counts.setdefault(run.id, {})[action] = count
        for run in self:
            per_action = counts.get(run.id, {})
            run.create_count = per_action.get("create", 0)
            run.update_count = per_action.get("update", 0)
            run.unchanged_count = per_action.get("unchanged", 0)
            run.skip_count = per_action.get("skip", 0)
            run.error_count = per_action.get("error", 0)
            run.line_count = sum(per_action.values())
            run.job_count = len(run.job_ids)

    @api.constrains("limit")
    def _check_limit(self):
        for run in self:
            if not 1 <= int(run.limit or 0) <= 500:
                raise UserError(_("The preview limit must be between 1 and 500."))

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _parsed_refs(self):
        self.ensure_one()
        refs = []
        for ref in re.split(r"[,\s;]+", self.magento_refs or ""):
            ref = ref.strip()
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    def _dr_instance(self):
        """The instance in the preview environment: reads allowed, writes and pushes not."""
        self.ensure_one()
        return self.instance_id.with_context(
            magento_dry_run=True,
            magento_skip_push=True,
            tracking_disable=True,
            mail_notrack=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
        )

    def _dr_fetch(self, instance, api):
        """``[(magento ref, label, row, full)]``: the Magento records to preview. ``full``
        tells the product import that ``row`` already is the complete product."""
        self.ensure_one()
        refs = self._parsed_refs()
        limit = max(1, min(500, int(self.limit or 50)))
        updated_since = self.updated_since or None
        rows = []
        if self.entity == "category":
            tree = api.get_categories() or {}
            items = instance._flatten_categories(tree)
            if refs:
                items = [item for item in items if str(item.get("id")) in refs]
            for item in items[:limit]:
                if item.get("id") is not None:
                    rows.append((str(item["id"]), item.get("name"), item, True))
            return rows
        if self.entity == "product":
            if refs:
                for sku in refs[:limit]:
                    data = api.get_product(sku) or {}
                    if data.get("sku"):
                        rows.append((data["sku"], data.get("name"), data, True))
            else:
                for row in api.get_products(updated_since, page_size=limit, limit=limit) or ():
                    if isinstance(row, dict) and row.get("sku"):
                        rows.append((row["sku"], row.get("name"), row, False))
            return rows
        if self.entity == "customer":
            records = ([api.get_customer(ref) for ref in refs[:limit]] if refs
                       else api.get_customers(updated_since, page_size=limit, limit=limit))
            for data in records or ():
                if isinstance(data, dict) and (data.get("id") or data.get("email")):
                    name = " ".join(filter(None, (data.get("firstname"), data.get("lastname"))))
                    rows.append((str(data.get("id") or data.get("email")), name or data.get("email"), data, True))
            return rows
        records = ([api.get_order(ref) for ref in refs[:limit]] if refs
                   else api.get_orders(updated_since, page_size=limit, limit=limit))
        for data in records or ():
            magento_id = isinstance(data, dict) and (data.get("entity_id") or data.get("id"))
            if magento_id:
                rows.append((str(magento_id), data.get("increment_id"), data, True))
        return rows

    def _dr_run(self, instance, api, row, full, box, stats):
        """The real per-record import of the entity (no result hook, no follow-ups)."""
        self.ensure_one()
        env = instance.env
        if self.entity == "category":
            cat_model = env["magento.category"].with_context(skip_magento_sync=True)
            return instance._sync_category_record(cat_model, row, box=box)
        if self.entity == "product":
            return instance._sync_product_record(
                api, row, instance._supported_product_types(), stats,
                product_data=row if full else None, box=box,
            )
        if self.entity == "customer":
            customer_model = env["magento.customer"].with_context(skip_magento_sync=True, active_test=False)
            return instance._sync_customer_record(customer_model, row, box=box)
        magento_id = row.get("entity_id") or row.get("id")
        return instance._sync_order_record(api, row, magento_id, box)

    @staticmethod
    def _dr_targets(box):
        """The Odoo records an import of this entity writes (primary record first)."""
        record = box[0] if box else None
        if not record:
            return []
        targets = [record]
        if record._name == "magento.product.map":
            if record.odoo_product_id:
                targets.append(record.odoo_product_id)
            elif record.odoo_template_id:
                targets.append(record.odoo_template_id)
        elif record._name == "magento.customer" and record.partner_id:
            targets.append(record.partner_id)
        return targets

    def action_preview(self):
        """Run the preview: one rolled-back import per Magento record, one line each."""
        self.ensure_one()
        if not self.instance_id.active:
            raise UserError(_("Instance %s is archived.", self.instance_id.name))
        instance = self._dr_instance()
        token = DRY_RUN.set(True)
        try:
            api = MagentoAPI(instance)
            try:
                rows = self._dr_fetch(instance, api)
            except Exception as exc:  # noqa: BLE001
                raise UserError(_(
                    "Could not read %(entity)s from Magento: %(error)s",
                    entity=dict(DRYRUN_ENTITIES)[self.entity].lower(),
                    error=instance._format_magento_request_error(exc, instance),
                )) from exc
            stats = _SyncStats(self.entity)
            line_vals = []
            for sequence, (ref, label, row, full) in enumerate(rows, start=1):
                vals = self._dr_preview_one(instance, api, ref, row, full, stats)
                vals.update(sequence=sequence, magento_ref=ref, name=vals.get("name") or label or ref)
                line_vals.append(vals)
        finally:
            DRY_RUN.reset(token)
        self.line_ids.unlink()
        self.write({
            "line_ids": [(0, 0, vals) for vals in line_vals],
            "state": "previewed",
            "previewed_at": fields.Datetime.now(),
            "previewed_by": self.env.uid,
            "applied_at": False,
            "summary": False,
        })
        self.summary = self._dr_summary()
        return True

    def _dr_preview_one(self, instance, api, ref, row, full, stats):
        """Preview one record: run its import in a savepoint, read, roll back, diff."""
        cr = self.env.cr
        transform_errors = []
        run_instance = instance.with_context(magento_transform_errors=transform_errors)
        result = None
        error = None
        with _preserved_callbacks(cr):
            try:
                with cr.savepoint():
                    box = []
                    outcome = self._dr_run(run_instance, api, row, full, box, stats)
                    self.env.flush_all()
                    targets = self._dr_targets(box)
                    after = {(rec._name, rec.id): self._dr_snapshot(rec) for rec in targets}
                    primary = (targets[0]._name, targets[0].id, targets[0].display_name) if targets else None
                    raise _DryRunRollback(outcome, after, primary)
            except _DryRunRollback as rollback:
                result = rollback
            except Exception as exc:  # noqa: BLE001 - one bad record never stops the preview
                error = exc
        # Rolled back: what still exists existed before the import ran.
        if error is not None:
            message = _short_reason(error)
            if isinstance(error, MagentoDryRunWriteBlocked):
                message = _("The import tried to write to Magento; the call was blocked: %s", message)
            return self._dr_line_vals("error", message=message, transform_errors=transform_errors)
        outcome = result.outcome
        if isinstance(outcome, tuple) and outcome and outcome[0] == "skipped":
            return self._dr_line_vals("skip", message=outcome[1], transform_errors=transform_errors)
        if outcome not in ("created", "updated"):
            return self._dr_line_vals("skip", message=_("Ignored by the import."),
                                      transform_errors=transform_errors)
        diff = []
        for (model, res_id), after in result.after.items():
            before_rec = self.env[model].browse(res_id).exists()
            before = self._dr_snapshot(before_rec) if before_rec else None
            diff.extend(self._dr_diff(model, before, after))
        action = "create" if outcome == "created" else ("update" if diff else "unchanged")
        vals = self._dr_line_vals(action, diff=diff, transform_errors=transform_errors)
        if result.primary:
            model, res_id, display_name = result.primary
            vals.update(target_model=model, target_name=display_name,
                        target_id=res_id if self.env[model].browse(res_id).exists() else 0)
        return vals

    @staticmethod
    def _dr_line_vals(action, *, message=None, diff=None, transform_errors=()):
        notes = [message] if message else []
        notes.extend(transform_errors or ())
        diff = diff or []
        return {
            "action": action,
            "message": "\n".join(notes) or False,
            "diff_json": json.dumps(diff, ensure_ascii=False) if diff else False,
            "change_count": len(diff),
            "changed_fields": ", ".join(dict.fromkeys(d["label"] for d in diff))[:1000] or False,
        }

    def _dr_summary(self):
        self.ensure_one()
        return _(
            "%(create)s to create, %(update)s to update, %(same)s unchanged, %(skip)s skipped, "
            "%(error)s errors",
            create=self.create_count, update=self.update_count, same=self.unchanged_count,
            skip=self.skip_count, error=self.error_count,
        )

    # ------------------------------------------------------------------
    # Snapshots and diff
    # ------------------------------------------------------------------
    @api.model
    def _dr_field_names(self, record):
        curated = _DIFF_CURATED.get(record._name)
        names = []
        for name, field in record._fields.items():
            if name in _DIFF_SKIP or field.type not in _DIFF_TYPES:
                continue
            if not (field.store or field.inherited) or field.groups:
                continue
            if _DIFF_NOISE.search(name):
                continue
            if curated is not None and name not in curated and not name.startswith("magento_"):
                continue
            names.append(name)
        return names

    @api.model
    def _dr_format(self, field, value):
        if field.type == "many2one":
            return value.display_name or "" if value else ""
        if field.type == "many2many":
            names = sorted(value.mapped("display_name"))
            text = ", ".join(names[:20])
            return text + (" (+%s)" % (len(names) - 20) if len(names) > 20 else "")
        if field.type == "boolean":
            return _("Yes") if value else _("No")
        if value is False or value is None:
            return ""
        if field.type == "selection":
            try:
                selection = dict(field._description_selection(self.env))
            except Exception:  # noqa: BLE001
                selection = {}
            return str(selection.get(value, value))
        if field.type in ("float", "monetary"):
            return ("%.6f" % value).rstrip("0").rstrip(".")
        if field.type == "html":
            return html2plaintext(value or "").strip()
        return str(value)

    @api.model
    def _dr_snapshot(self, record):
        """``{field: (label, display value, comparison key)}`` of the compared fields of
        ``record``. Relational fields compare by ids (a renamed related record is not a
        change of this one), the others by their displayed value."""
        record = record.with_context(active_test=False)
        snapshot = {}
        for name in self._dr_field_names(record):
            field = record._fields[name]
            try:
                value = record[name]
                display = self._dr_format(field, value)
            except Exception:  # noqa: BLE001 - access errors etc.: the field is not compared
                continue
            key = tuple(sorted(value.ids)) if field.type in ("many2one", "many2many") else display
            snapshot[name] = (field.string or name, display, key)
        if record._name == "sale.order":
            lines = record.order_line.filtered(lambda l: not l.display_type)
            text = "; ".join(
                "%s × %s @ %s" % (line.product_id.default_code or line.name or "",
                                  ("%.4f" % line.product_uom_qty).rstrip("0").rstrip("."),
                                  ("%.4f" % line.price_unit).rstrip("0").rstrip("."))
                for line in lines
            )
            snapshot[_ORDER_LINES_KEY] = (_("Order lines"), text, text)
        return snapshot

    @api.model
    def _dr_diff(self, model, before, after):
        """Field-level changes ``[{model, field, label, old, new}]``. A new record lists
        its non-empty values (old is empty)."""
        description = self.env[model]._description
        changes = []
        for name, (label, new, new_key) in after.items():
            old, old_key = "", None
            if before is not None and name in before:
                _label, old, old_key = before[name]
            if before is not None and old_key == new_key:
                continue
            if before is None and new in ("", _("No")):
                continue
            changes.append({
                "model": model,
                "model_label": description,
                "field": name,
                "label": label,
                "old": old[:_DIFF_VALUE_MAX] if before is not None else None,
                "new": new[:_DIFF_VALUE_MAX],
            })
        return changes

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------
    def action_apply(self):
        """Queue the real import of every record the preview would create or update."""
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.action in _APPLY_ACTIONS)
        if not lines:
            raise UserError(_("Nothing to apply: the preview has nothing to create or update."))
        return lines.action_apply_lines()

    def action_apply_full_sync(self):
        """Queue the regular (paged) sync of the whole entity."""
        self.ensure_one()
        entity = {"product": "products", "category": "categories", "customer": "customers",
                  "order": "orders"}[self.entity]
        queued = self.instance_id._magento_enqueue_sync([entity])
        jobs = self.env["magento.job"].browse([job.id for _entity, job, _created in queued])
        self.write({"job_ids": [(4, job.id) for job in jobs], "state": "applied",
                    "applied_at": fields.Datetime.now()})
        return self._dr_notify(_("Full %(entity)s sync queued (job %(jobs)s).",
                                 entity=dict(DRYRUN_ENTITIES)[self.entity].lower(),
                                 jobs=", ".join(str(j.id) for j in jobs)))

    @api.model
    def _dr_notify(self, message, kind="info"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Dry-run Preview"), "message": message, "type": kind,
                       "sticky": False, "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def action_view_lines(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_dryrun_line")
        action["domain"] = [("dryrun_id", "=", self.id)]
        context = {"default_dryrun_id": self.id}
        line_action = self.env.context.get("dryrun_action")
        if line_action:
            context["search_default_action_%s" % line_action] = 1
        action["context"] = context
        action["display_name"] = self.name
        return action

    def action_view_jobs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sdlc_magento_connector.action_magento_job")
        action["domain"] = [("id", "in", self.job_ids.ids)]
        action["context"] = {}
        return action


class MagentoDryRunLine(models.Model):
    _name = "magento.dryrun.line"
    _description = "Magento Dry-run Preview Row"
    _order = "dryrun_id desc, sequence, id"
    _rec_name = "name"

    dryrun_id = fields.Many2one("magento.dryrun", required=True, ondelete="cascade", index=True)
    instance_id = fields.Many2one(related="dryrun_id.instance_id", store=True)
    company_id = fields.Many2one(related="dryrun_id.company_id", store=True)
    entity = fields.Selection(related="dryrun_id.entity", store=True)
    sequence = fields.Integer(default=10)
    magento_ref = fields.Char(string="Magento Reference", required=True)
    name = fields.Char(string="Record")
    action = fields.Selection(LINE_ACTIONS, required=True, index=True)
    target_model = fields.Char(string="Target Model")
    target_id = fields.Integer(string="Target ID")
    target_name = fields.Char(string="Odoo Record")
    change_count = fields.Integer(string="Changes")
    changed_fields = fields.Char(string="Changed Fields")
    diff_json = fields.Text(string="Diff (JSON)")
    diff_html = fields.Html(string="Diff", compute="_compute_diff_html", sanitize=False)
    message = fields.Text()
    job_id = fields.Many2one("magento.job", string="Apply Job", readonly=True, ondelete="set null")
    job_state = fields.Selection(related="job_id.state", string="Apply State")

    @api.depends("diff_json", "action")
    def _compute_diff_html(self):
        for line in self:
            line.diff_html = line._diff_table()

    def _diff_rows(self):
        try:
            rows = json.loads(self.diff_json or "[]")
        except ValueError:
            rows = []
        return rows if isinstance(rows, list) else []

    def _diff_table(self):
        rows = self._diff_rows()
        if not rows:
            return False
        parts = [Markup('<table class="table table-sm o_mg_diff mb-0"><thead><tr>'
                        '<th>Record</th><th>Field</th><th>Before</th><th>After</th>'
                        '</tr></thead><tbody>')]
        for row in rows:
            old = row.get("old")
            parts.append(Markup(
                '<tr><td class="text-muted">%s</td><td class="fw-semibold">%s</td>'
                '<td class="text-muted">%s</td><td>%s</td></tr>'
            ) % (
                row.get("model_label") or row.get("model") or "",
                row.get("label") or row.get("field") or "",
                _("(new record)") if old is None else (old or Markup("&#8212;")),
                row.get("new") or Markup("&#8212;"),
            ))
        parts.append(Markup("</tbody></table>"))
        return Markup("").join(parts)

    def action_apply_lines(self):
        """Queue ``_magento_resync_one`` per line (one sequential channel per instance, in
        preview order, so parent categories are imported before their children)."""
        jobs = self.env["magento.job"]
        lines = self.filtered(lambda l: l.action in _APPLY_ACTIONS and l.magento_ref)
        if not lines:
            raise UserError(_("Only rows to create or update can be applied."))
        for line in lines.sorted(lambda l: (l.dryrun_id.id, l.sequence, l.id)):
            instance = line.instance_id
            job = jobs.enqueue(
                instance, "_magento_resync_one", args=(line.entity, line.magento_ref),
                instance=instance,
                channel="magento.%s.dryrun" % instance.id,
                priority=8,
                identity_key="dryrun-%s-%s-%s" % (instance.id, line.entity, line.magento_ref),
                description=_("Apply preview: %(entity)s %(ref)s", entity=line.entity, ref=line.magento_ref),
            )
            line.job_id = job
        runs = lines.dryrun_id
        for run in runs:
            run_jobs = lines.filtered(lambda l, run=run: l.dryrun_id == run).job_id
            run.write({"job_ids": [(4, job.id) for job in run_jobs], "state": "applied",
                       "applied_at": fields.Datetime.now()})
        return self.env["magento.dryrun"]._dr_notify(
            _("%s import job(s) queued; follow them in the Job Queue.", len(lines.job_id)))

    def action_open_target(self):
        self.ensure_one()
        if not self.target_model or not self.target_id:
            raise UserError(_("This row has no Odoo record (yet)."))
        return {
            "type": "ir.actions.act_window",
            "res_model": self.target_model,
            "res_id": self.target_id,
            "view_mode": "form",
        }


# ---------------------------------------------------------------------------
# Instance: previews, stock push guard
# ---------------------------------------------------------------------------
class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    dryrun_count = fields.Integer(string="Previews", compute="_compute_dryrun_count")

    def _compute_dryrun_count(self):
        rows = orm_compat.read_group(self.env["magento.dryrun"], 
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"])
        counts = {instance.id: count for instance, count in rows}
        for instance in self:
            instance.dryrun_count = counts.get(instance.id, 0)

    def action_open_dryrun(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Dry-run Preview"),
            "res_model": "magento.dryrun",
            "view_mode": "form",
            "target": "current",
            "context": {"default_instance_id": self.id},
        }

    def action_view_dryruns(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sdlc_magento_connector.action_magento_dryrun")
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"default_instance_id": self.id}
        return action

    def _magento_push_stock_maps(self, maps, description=None):
        """Data Ownership: no stock push unless stock is Odoo -> Magento or two-way;
        create-only pushes only the products whose stock was never pushed."""
        self.ensure_one()
        reason = self._magento_export_block_reason("stock")
        if reason:
            _logger.info("Magento stock push skipped (instance %s): %s", self.name, reason)
            return {"items": 0, "operations": 0, "complete": 0, "open": 0, "failed": 0,
                    "skipped": len(maps or ()), "skip_reason": reason}
        skipped = 0
        if maps and self._magento_export_block_reason("stock", existing=True):
            fresh = maps.filtered(lambda m: not m.last_stock_push_at)
            skipped = len(maps) - len(fresh)
            maps = fresh
        result = super()._magento_push_stock_maps(maps, description=description)
        if skipped:
            result["skipped"] = skipped
            result["skip_reason"] = self._magento_export_block_reason("stock", existing=True)
        return result

    def action_push_stock_bulk(self):
        reason = self._magento_export_block_reason("stock") if len(self) == 1 else None
        if reason:
            raise UserError(reason)
        return super().action_push_stock_bulk()


# ---------------------------------------------------------------------------
# Automatic pushes of products, categories and customers
# ---------------------------------------------------------------------------
class MagentoPushErrorMixin(models.AbstractModel):
    _inherit = "magento.push.error.mixin"

    def _magento_ownership_entity(self):
        entity = self._magento_push_entity
        return entity if entity in dict(OWNERSHIP_ENTITIES) else None

    def _magento_push_block_reason(self):
        """Why Data Ownership forbids pushing this record, or None."""
        self.ensure_one()
        entity = self._magento_ownership_entity()
        if not entity or not self.instance_id:
            return None
        existing = bool("magento_id" in self._fields and self.magento_id)
        return self.instance_id._magento_export_block_reason(entity, existing=existing)

    def _magento_enqueue_push(self, flags=()):
        reason = self._magento_push_block_reason()
        if reason:
            _logger.info("Magento push of %s not queued: %s", self.display_name, reason)
            return self.env["magento.job"]
        return super()._magento_enqueue_push(flags)

    def _magento_auto_push(self, flags=()):
        allowed = self.browse()
        skipped = []
        for record in self:
            reason = record._magento_push_block_reason()
            if reason:
                skipped.append("%s: %s" % (record.display_name, reason))
            else:
                allowed |= record
        result = super(MagentoPushErrorMixin, allowed)._magento_auto_push(flags) if allowed else True
        if skipped:
            _logger.info("Magento push skipped: %s", "; ".join(skipped))
            return _("Skipped %(count)s push(es): %(reasons)s", count=len(skipped),
                     reasons="; ".join(skipped)[:1500])
        return result


class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    def _apply_magento_data(self, data):
        """Data Ownership of prices and stock on import: drop what Odoo owns.

        ``magento_import_new`` (set by the product import) says whether the import
        creates the product: create-only ownership then still takes the first values."""
        values = super()._apply_magento_data(data)
        instance = self[:1].instance_id
        if not instance:
            return values
        existing = not self.env.context.get("magento_import_new")
        if instance._magento_import_block_reason("price", existing=existing):
            values.pop("price", None)
        if instance._magento_import_block_reason("stock", existing=existing):
            for key in ("quantity", "stock_status", "magento_backorders"):
                values.pop(key, None)
        return values

    def _build_magento_payload(self):
        """Data Ownership of prices and stock on export: an existing Magento product keeps
        the price (stock) Magento owns. A new product still gets its first price."""
        payload = super()._build_magento_payload()
        instance = self.instance_id
        product = payload.get("product") if isinstance(payload, dict) else None
        if not instance or not isinstance(product, dict):
            return payload
        existing = bool(self.magento_id)
        if existing and instance._magento_export_block_reason("price", existing=True):
            product.pop("price", None)
        if instance._magento_export_block_reason("stock", existing=existing):
            extension = product.get("extension_attributes")
            if isinstance(extension, dict):
                extension.pop("stock_item", None)
                if not extension:
                    product.pop("extension_attributes", None)
        return payload


class MagentoPriceRule(models.Model):
    _inherit = "magento.price.rule"

    def _magento_push_prices(self, remove_all=False, force=False):
        """Data Ownership: price rules push only when prices are Odoo -> Magento or two-way;
        create-only pushes a rule's prices once (its first push) and never again."""
        summaries = {}
        allowed = self.browse()
        for rule in self:
            existing = bool(rule.last_push_at or rule.push_state_ids)
            reason = rule.instance_id._magento_export_block_reason("price", existing=existing)
            if reason:
                summaries[rule.id] = _("skipped: %s", reason)
                rule.sudo().write({"last_push_summary": _("Skipped: %s", reason)})
            else:
                allowed |= rule
        if allowed:
            summaries.update(super(MagentoPriceRule, allowed)._magento_push_prices(
                remove_all=remove_all, force=force))
        return summaries


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _magento_order_push_block_reason(self, existing=None):
        self.ensure_one()
        instance = self.magento_instance_id
        if not instance:
            return None
        return instance._magento_export_block_reason("order", existing=existing)

    def action_create_magento_order(self):
        self.ensure_one()
        instance = self.magento_instance_id or self._magento_resolve_order_instance()
        reason = instance._magento_export_block_reason("order") if instance else None
        if reason:
            raise UserError(reason)
        return super().action_create_magento_order()

    def _magento_push_status(self, status, comment, api=None):
        reason = self._magento_order_push_block_reason(existing=True)
        if reason:
            raise UserError(reason)
        return super()._magento_push_status(status, comment, api=api)

    def _magento_push_document(self, kind, record, payload, amount=None, currency=None):
        reason = self._magento_order_push_block_reason()
        if reason:
            raise UserError(reason)
        return super()._magento_push_document(kind, record, payload, amount=amount, currency=currency)

    def _magento_push_shipments(self):
        """Sync follow-up step: skipped silently (not an error) when Odoo may not push."""
        allowed = self.filtered(lambda order: not order._magento_order_push_block_reason())
        if allowed:
            return super(SaleOrder, allowed)._magento_push_shipments()
        return None


class IrSequence(models.Model):
    _inherit = "ir.sequence"

    def _next_do(self):
        # A preview must not consume real document numbers (standard sequences are not
        # transactional: a rolled-back preview would leave gaps).
        if self.implementation == "standard" and magento_dry_run_active(self.env):
            return "PREVIEW-%s" % next(_PREVIEW_SEQUENCE)
        return super()._next_do()


class IrSequenceDateRange(models.Model):
    _inherit = "ir.sequence.date_range"

    def _next(self):
        if self.sequence_id.implementation == "standard" and magento_dry_run_active(self.env):
            return "PREVIEW-%s" % next(_PREVIEW_SEQUENCE)
        return super()._next()
