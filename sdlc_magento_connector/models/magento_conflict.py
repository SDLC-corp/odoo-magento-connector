"""Conflict detection (roadmap 3.7): orders changed in Magento after Odoo processed them.

Every order re-sync (scheduled import, "Pull from Magento", a single-record resync from the
Error Center, a queued job) ends in ``sale.order._magento_sync_order_lines``, which compares
the current Magento order with the order's import snapshot (``magento_import_snapshot``) and
keeps one ``magento.order.conflict`` row per (order, conflict type) open while they diverge.

Behaviour per type (the Business Central "orders changed after import" table):

=========================  ===========================  ==================================
Type                       Quotation (draft / sent)     Confirmed order
=========================  ===========================  ==================================
items / qty / price /      Magento wins: lines updated  Open. "Apply Magento changes"
totals changed             in place, row resolved_auto  (unlock + rewrite lines) or "Keep
                                                        Odoo". Delivered or invoiced: crit,
                                                        "Keep Odoo" only (credit note /
                                                        return).
address_changed            New address applied,         Open. "Apply new address" (shipping
                           resolved_auto                + invoice address, open pickings)
                                                        until delivered; then crit, keep.
customer_changed           Open (a customer is never    Open. Accept re-resolves the
                           re-assigned silently)        customer until delivered/invoiced.
payment_changed            resolved_auto                Open, accept / keep (acknowledge).
status_regressed           resolved_auto                Open (e.g. complete -> processing);
                                                        auto-resolves when the status moves
                                                        forward again.
canceled_after_fulfilment  -                            Open, crit, keep only. Mirrors
                                                        ``magento_cancel_conflict``.
edited_in_odoo             Open, warn (informational)   Open, warn (informational).
=========================  ===========================  ==================================

Every Magento status transition is kept in ``magento.order.status.event``: the order's
``status_histories`` (one event per history entry, so several status changes between two
syncs are never collapsed into the last one) plus the status seen by the sync when Magento
changed it without a history entry. ``Keep Odoo`` stores the acknowledged Magento version
(``magento_hash``): the same Magento change never re-opens the row, a newer one does.
"""
import hashlib
import json
import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

CONFLICT_TYPES = [
    ("items_changed", "Items changed"),
    ("qty_changed", "Quantity changed"),
    ("price_changed", "Price changed"),
    ("address_changed", "Address changed"),
    ("customer_changed", "Customer changed"),
    ("totals_changed", "Totals changed"),
    ("status_regressed", "Status regressed"),
    ("canceled_after_fulfilment", "Cancelled after fulfilment"),
    ("payment_changed", "Payment changed"),
    ("edited_in_odoo", "Edited in Odoo"),
]
CONFLICT_STATES = [
    ("open", "Open"),
    ("accepted", "Magento applied"),
    ("kept", "Odoo kept"),
    ("resolved_auto", "Resolved automatically"),
]
SEVERITIES = [("info", "Info"), ("warn", "Warning"), ("crit", "Critical")]

# Types fixed together by "Apply Magento changes" (one rewrite of the order lines).
LINE_TYPES = ("items_changed", "qty_changed", "price_changed", "totals_changed")
# Types a quotation takes from Magento automatically.
AUTO_TYPES = LINE_TYPES + ("address_changed", "payment_changed", "status_regressed")
# Snapshot sections refreshed when a type is resolved in Magento's favour.
TYPE_SECTIONS = {
    "items_changed": ("items", "totals", "odoo_lines"),
    "qty_changed": ("items", "totals", "odoo_lines"),
    "price_changed": ("items", "totals", "odoo_lines"),
    "totals_changed": ("items", "totals", "odoo_lines"),
    "address_changed": ("shipping_address", "billing_address"),
    "customer_changed": ("customer",),
    "payment_changed": ("payment_method",),
}
SNAPSHOT_VERSION = 1

# Default Magento order statuses in lifecycle order. ``holded`` (and custom statuses) have
# no rank: a move to or from them is never a regression on its own.
STATUS_RANK = {
    "new": 0, "pending": 0, "pending_payment": 0, "payment_review": 0, "fraud": 0,
    "processing": 1,
    "complete": 2,
    "closed": 3, "canceled": 3,
}
ADDRESS_FIELDS = (
    ("name", "Name"), ("company", "Company"), ("street", "Street"), ("city", "City"),
    ("postcode", "ZIP"), ("region", "State"), ("country", "Country"), ("telephone", "Phone"),
)


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(value):
    return " ".join(str(value or "").split())


def status_rank(status):
    return STATUS_RANK.get((status or "").strip().lower())


def is_regression(old, new):
    old_rank, new_rank = status_rank(old), status_rank(new)
    return old_rank is not None and new_rank is not None and new_rank < old_rank


def _fingerprint(value):
    raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _magento_datetime(value):
    if not value:
        return False
    try:
        return fields.Datetime.to_datetime(str(value)[:19])
    except (TypeError, ValueError):
        return False


class MagentoOrderConflict(models.Model):
    _name = "magento.order.conflict"
    _description = "Magento Order Conflict"
    _order = "state_rank, severity_rank desc, detected_at desc, id desc"

    order_id = fields.Many2one("sale.order", required=True, index=True, ondelete="cascade",
                               readonly=True)
    instance_id = fields.Many2one(related="order_id.magento_instance_id", store=True, index=True,
                                  string="Instance")
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    magento_increment_id = fields.Char(related="order_id.magento_increment_id", store=True,
                                       string="Magento Order")
    partner_id = fields.Many2one(related="order_id.partner_id", string="Customer")
    order_state = fields.Selection(related="order_id.state", string="Order Status")
    conflict_type = fields.Selection(CONFLICT_TYPES, string="Type", required=True, index=True,
                                     readonly=True)
    severity = fields.Selection(SEVERITIES, required=True, default="warn", readonly=True)
    severity_rank = fields.Integer(compute="_compute_ranks", store=True)
    state = fields.Selection(CONFLICT_STATES, required=True, default="open", index=True,
                             readonly=True)
    state_rank = fields.Integer(compute="_compute_ranks", store=True)
    detected_at = fields.Datetime(default=fields.Datetime.now, readonly=True,
                                  help="First time this difference was seen.")
    magento_updated_at = fields.Datetime("Magento Updated At", readonly=True,
                                         help="updated_at of the Magento order last compared.")
    summary = fields.Char(readonly=True)
    diff = fields.Json(readonly=True,
                       help="Rows {label, before (Odoo import snapshot), after (Magento now), "
                            "odoo (Odoo now)}.")
    diff_html = fields.Html("Differences", compute="_compute_diff_html", sanitize=False)
    magento_hash = fields.Char(readonly=True, copy=False,
                               help="Fingerprint of the Magento version compared. 'Keep Odoo' "
                                    "acknowledges it: the same Magento change never re-opens.")
    magento_data = fields.Json(readonly=True, copy=False,
                               help="Last Magento order payload compared (used by Apply).")
    can_accept = fields.Boolean("Magento Can Be Applied", readonly=True)
    guidance = fields.Text(compute="_compute_guidance")
    resolved_by = fields.Many2one("res.users", readonly=True, ondelete="set null",
                                  help="Empty on a resolved conflict: resolved automatically.")
    resolved_date = fields.Datetime(readonly=True)

    _open_type_uniq = models.UniqueIndex(
        "(order_id, conflict_type) WHERE state = 'open'",
        "This order already has an open conflict of this type.",
    )

    @api.depends("severity", "state")
    def _compute_ranks(self):
        sev = {"info": 0, "warn": 1, "crit": 2}
        for row in self:
            row.severity_rank = sev.get(row.severity, 0)
            row.state_rank = 0 if row.state == "open" else 1

    @api.depends("order_id.name", "conflict_type")
    def _compute_display_name(self):
        labels = dict(self._fields["conflict_type"]._description_selection(self.env))
        for row in self:
            row.display_name = "%s · %s" % (row.order_id.name or "", labels.get(row.conflict_type, ""))

    @api.depends("diff", "conflict_type")
    def _compute_diff_html(self):
        for row in self:
            rows = (row.diff or {}).get("rows") or []
            if not rows:
                row.diff_html = False
                continue
            with_odoo = any(r.get("odoo") not in (None, "") for r in rows)
            head = Markup("<th>%s</th><th>%s</th><th>%s</th>") % (
                _("Field"), _("Odoo (at import)"), _("Magento (now)"))
            if with_odoo:
                head += Markup("<th>%s</th>") % _("Odoo (now)")
            body = Markup("")
            for r in rows:
                cells = Markup("<td class='mg-diff__label'>%s</td><td class='mg-diff__before'>%s</td>"
                               "<td class='mg-diff__after fw-semibold' style='background: rgba(235, 82, 2, .07);'>%s</td>") % (
                    r.get("label") or "", r.get("before") if r.get("before") not in (None, "") else "—",
                    r.get("after") if r.get("after") not in (None, "") else "—")
                if with_odoo:
                    cells += Markup("<td class='mg-diff__odoo'>%s</td>") % (
                        r.get("odoo") if r.get("odoo") not in (None, "") else "—")
                body += Markup("<tr>%s</tr>") % cells
            row.diff_html = Markup(
                "<table class='table table-sm mg-diff' style='font-variant-numeric: tabular-nums;'>"
                "<thead><tr>%s</tr></thead><tbody>%s</tbody></table>") % (head, body)

    @api.depends("conflict_type", "can_accept", "state", "severity")
    def _compute_guidance(self):
        for row in self:
            row.guidance = row._guidance_text()

    def _guidance_text(self):
        self.ensure_one()
        ctype = self.conflict_type
        if ctype in LINE_TYPES:
            if self.can_accept:
                return _("Magento changed the order after it was confirmed in Odoo. 'Apply Magento "
                         "changes' unlocks the order and rewrites its lines from Magento (a removed "
                         "item gets quantity 0); 'Keep Odoo' leaves the order as it is.")
            return _("The order is already delivered or invoiced in Odoo, so its lines are not "
                     "changed. Keep the Odoo version and settle the difference by hand: a credit "
                     "note for an amount or quantity no longer due, a return for goods already "
                     "shipped, a new order for added items.")
        if ctype == "address_changed":
            if self.can_accept:
                return _("'Apply new address' sets the new delivery (and invoice) address on the "
                         "order and on its open deliveries.")
            return _("The goods have already left (or the invoice is posted) with the previous "
                     "address. Keep Odoo and contact the carrier or the customer if needed.")
        if ctype == "customer_changed":
            if self.can_accept:
                return _("Magento now has another customer on this order. Accepting re-assigns the "
                         "Odoo order to that customer; the connector never does it on its own.")
            return _("The order is delivered or invoiced: keep the Odoo customer and correct the "
                     "documents by hand if needed.")
        if ctype == "status_regressed":
            return _("Magento moved the order back to an earlier status. Check the status history "
                     "tab of the order: every transition is listed in order. The row resolves "
                     "itself when the status moves forward again.")
        if ctype == "canceled_after_fulfilment":
            return _("Magento cancelled an order that Odoo has already delivered or invoiced, so "
                     "Odoo did not cancel it. Issue a credit note for the invoice and a return for "
                     "the delivery, then cancel the order in Odoo.")
        if ctype == "payment_changed":
            return _("The Magento payment method changed. Check the payment journal of any "
                     "payment registered in Odoo.")
        if ctype == "edited_in_odoo":
            return _("Lines were added or changed in Odoo after the import and differ from "
                     "Magento. Informational: Magento is not changed by the connector.")
        return ""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _check_conflict_access(self):
        self.check_access("write")
        self.order_id.check_access("write")

    def _notify(self, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento conflicts"),
                "message": message,
                "type": kind,
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_accept(self):
        """Apply Magento's version (bulk): rows that cannot be applied are skipped."""
        self._check_conflict_access()
        rows = self.filtered(lambda r: r.state == "open" and r.can_accept)
        if not rows:
            raise UserError(_("None of the selected conflicts can take the Magento version: keep "
                              "the Odoo version instead (see the guidance on each conflict)."))
        for order in rows.order_id:
            order._magento_conflict_accept(rows.filtered(lambda r: r.order_id == order))
        skipped = len(self.filtered(lambda r: r.state == "open"))
        message = _("%s conflict(s) resolved with the Magento version.", len(rows))
        if skipped:
            message += " " + _("%s conflict(s) skipped: Keep Odoo is the only option.", skipped)
        return self._notify(message, "warning" if skipped else "success")

    def action_keep(self):
        """Keep the Odoo version (bulk): acknowledges this Magento version."""
        self._check_conflict_access()
        rows = self.filtered(lambda r: r.state == "open")
        if not rows:
            raise UserError(_("The selected conflicts are already resolved."))
        for order in rows.order_id:
            order._magento_conflict_keep(rows.filtered(lambda r: r.order_id == order))
        return self._notify(_("%s conflict(s) resolved: the Odoo version is kept.", len(rows)))

    def action_open_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": self.order_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MagentoOrderStatusEvent(models.Model):
    _name = "magento.order.status.event"
    _description = "Magento Order Status Event"
    _order = "order_id, sequence desc, id desc"
    _rec_name = "to_status"

    order_id = fields.Many2one("sale.order", required=True, index=True, ondelete="cascade",
                               readonly=True)
    instance_id = fields.Many2one(related="order_id.magento_instance_id", store=True,
                                  string="Instance")
    company_id = fields.Many2one(related="order_id.company_id", store=True, index=True)
    sequence = fields.Integer(readonly=True, help="Order in which the transitions happened.")
    event_date = fields.Datetime("Date", readonly=True,
                                 help="Magento time of the change (history entry or updated_at).")
    from_status = fields.Char("From", readonly=True)
    to_status = fields.Char("To", readonly=True)
    magento_state = fields.Char("Magento State", readonly=True)
    source = fields.Selection(
        [("import", "Import"), ("history", "Magento history"), ("sync", "Sync"),
         ("odoo", "Odoo action")],
        readonly=True, required=True, default="sync",
    )
    is_transition = fields.Boolean(readonly=True, help="The status changed (not a comment only).")
    is_regression = fields.Boolean("Regression", readonly=True)
    comment = fields.Text(readonly=True)
    magento_history_id = fields.Char(readonly=True, index="btree_not_null")

    _history_uniq = models.UniqueIndex(
        "(order_id, magento_history_id) WHERE magento_history_id IS NOT NULL",
        "This Magento status history entry is already recorded.",
    )


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    @api.model
    def _magento_conflict_address(self, address):
        address = address or {}
        street = address.get("street") or []
        if isinstance(street, str):
            street = [street]
        region = address.get("region") or ""
        if isinstance(region, dict):
            region = region.get("region") or region.get("region_code") or ""
        name = " ".join(p for p in (_norm(address.get("firstname")), _norm(address.get("lastname"))) if p)
        return {
            "name": name,
            "company": _norm(address.get("company")),
            "street": ", ".join(_norm(s) for s in street if _norm(s)),
            "city": _norm(address.get("city")),
            "postcode": _norm(address.get("postcode")),
            "region": _norm(region or address.get("region_code")),
            "country": _norm(address.get("country_id")),
            "telephone": _norm(address.get("telephone")),
        }

    @api.model
    def _magento_conflict_address_key(self, address_view):
        return _fingerprint({k: (v or "").lower() for k, v in (address_view or {}).items()})

    def _magento_conflict_items_view(self, data):
        """{key: {item_id, sku, name, qty, price, product_id}} of the Magento product items."""
        self.ensure_one()
        instance = self.magento_instance_id
        view = {}
        for item in self._magento_effective_items((data or {}).get("items")):
            qty = _to_float(item.get("qty_ordered") or item.get("qty")) or 1.0
            item_id = str(item.get("item_id") or "")
            key = item_id or "sku:%s" % (item.get("sku") or "")
            product, _map = self._magento_find_item_product(item, instance)
            view[key] = {
                "item_id": item_id,
                "sku": item.get("sku") or "",
                "name": item.get("name") or item.get("sku") or "",
                "qty": qty,
                "price": round(self._magento_item_unit_price(item, instance, qty), 6),
                "product_id": product.id or False,
            }
        return view

    def _magento_conflict_odoo_lines(self):
        """({key: {product_id, sku, name, qty, price}} of the Magento product lines,
        [{line_id, ...}] of the lines without a Magento key (added in Odoo))."""
        self.ensure_one()
        keyed, extra = {}, []
        for line in self.order_line:
            if line.display_type or line.is_downpayment:
                continue
            if (line.magento_line_role or "product") != "product":
                continue
            values = {
                "product_id": line.product_id.id or False,
                "sku": line.magento_sku or line.product_id.default_code or "",
                "name": line.product_id.display_name or line.name or "",
                "qty": line.product_uom_qty,
                "price": line.price_unit,
            }
            key = self._magento_line_key_of(line)
            if key:
                keyed.setdefault(key, values)
            else:
                extra.append(dict(values, line_id=line.id))
        return keyed, extra

    def _magento_conflict_snapshot_values(self, data):
        """Every section of the import snapshot, from the Magento order ``data``."""
        self.ensure_one()
        data = data or {}
        billing = self._magento_extract_billing_address(data)
        shipping = self._magento_extract_shipping_address(data) or billing
        shipping_view = self._magento_conflict_address(shipping)
        billing_view = self._magento_conflict_address(billing)
        payment = data.get("payment") or {}
        is_guest = self._magento_is_guest(data)
        keyed, extra = self._magento_conflict_odoo_lines()
        grand_total = data.get("grand_total")
        if grand_total in (None, ""):
            grand_total = data.get("base_grand_total")
        return {
            "version": SNAPSHOT_VERSION,
            "items": self._magento_conflict_items_view(data),
            "shipping_address": shipping_view,
            "shipping_address_hash": self._magento_conflict_address_key(shipping_view),
            "billing_address": billing_view,
            "billing_address_hash": self._magento_conflict_address_key(billing_view),
            "customer": {
                "email": self._magento_normalize_email(data.get("customer_email") or billing.get("email")),
                "customer_id": "" if is_guest else str(data.get("customer_id") or ""),
                "is_guest": bool(is_guest),
                "name": " ".join(p for p in (_norm(data.get("customer_firstname")),
                                             _norm(data.get("customer_lastname"))) if p),
            },
            "totals": {
                "grand_total": _to_float(grand_total),
                "subtotal": _to_float(data.get("subtotal")),
                "shipping_amount": _to_float(data.get("shipping_amount")),
                "discount_amount": _to_float(data.get("discount_amount")),
                "tax_amount": _to_float(data.get("tax_amount")),
            },
            "status": data.get("status") or "",
            "state": data.get("state") or "",
            "payment_method": (payment.get("method") or "") if isinstance(payment, dict) else "",
            "updated_at": data.get("updated_at") or "",
            "odoo_lines": keyed,
            "odoo_extra_ids": [e["line_id"] for e in extra],
            "taken_at": fields.Datetime.to_string(fields.Datetime.now()),
        }

    def _magento_conflict_store_snapshot(self, data, sections=None, keep=()):
        """Store the import snapshot (all sections, or only ``sections``), minus ``keep``."""
        self.ensure_one()
        fresh = self._magento_conflict_snapshot_values(data)
        snapshot = dict(self.magento_import_snapshot or {})
        if not snapshot:
            sections, keep = None, ()
        wanted = set(sections) if sections else set(fresh)
        if "shipping_address" in wanted:
            wanted.add("shipping_address_hash")
        if "billing_address" in wanted:
            wanted.add("billing_address_hash")
        if "odoo_extra_ids" in snapshot:
            # Lines added in Odoo after the import never become part of the baseline:
            # they stay "edited in Odoo" (until kept).
            wanted.discard("odoo_extra_ids")
        wanted -= set(keep)
        for key in wanted:
            snapshot[key] = fresh[key]
        snapshot["version"] = SNAPSHOT_VERSION
        snapshot["updated_at"] = fresh["updated_at"]
        snapshot["taken_at"] = fresh["taken_at"]
        self.sudo().with_context(skip_magento_sync=True, magento_skip_push=True).write(
            {"magento_import_snapshot": snapshot})
        return snapshot

    def _magento_conflict_legacy_snapshot(self, data):
        """Baseline of an order imported before snapshots existed: the Magento side is the
        current payload, except the items, taken from the Odoo lines (the Phase 0 compare)."""
        self.ensure_one()
        snapshot = self._magento_conflict_snapshot_values(data)
        keyed = snapshot["odoo_lines"]
        snapshot["items"] = {
            key: {"item_id": key if not key.startswith("sku:") else "", "sku": v["sku"],
                  "name": v["name"], "qty": v["qty"], "price": v["price"],
                  "product_id": v["product_id"]}
            for key, v in keyed.items()
        }
        snapshot["legacy"] = True
        self.sudo().with_context(skip_magento_sync=True, magento_skip_push=True).write(
            {"magento_import_snapshot": snapshot})
        return snapshot

    # ------------------------------------------------------------------
    # Status history
    # ------------------------------------------------------------------
    def _magento_record_status_events(self, data, source=None):
        """Record every Magento status transition of ``data`` (never only the last one)."""
        self.ensure_one()
        data = data or {}
        Event = self.env["magento.order.status.event"].sudo()
        events = Event.search([("order_id", "=", self.id)], order="sequence, id")
        known = set(e for e in events.mapped("magento_history_id") if e)
        last_status = events[-1:].to_status or ""
        sequence = events[-1:].sequence or 0

        def history_key(entry):
            try:
                entry_id = int(entry.get("entity_id") or 0)
            except (TypeError, ValueError):
                entry_id = 0
            return (str(entry.get("created_at") or ""), entry_id)

        histories = sorted(
            (h for h in data.get("status_histories") or [] if isinstance(h, dict) and h.get("status")),
            key=history_key,
        )
        vals_list = []
        for entry in histories:
            history_id = str(entry.get("entity_id") or "") or "t:%s:%s" % (
                entry.get("created_at") or "", entry.get("status"))
            if history_id in known:
                continue
            known.add(history_id)
            status = entry["status"]
            sequence += 1
            vals_list.append({
                "order_id": self.id,
                "sequence": sequence,
                "event_date": _magento_datetime(entry.get("created_at")) or fields.Datetime.now(),
                "from_status": last_status or False,
                "to_status": status,
                "source": "history",
                "is_transition": status != last_status,
                "is_regression": is_regression(last_status, status),
                "comment": entry.get("comment") or False,
                "magento_history_id": history_id,
            })
            last_status = status
        current = data.get("status") or ""
        if current and current != last_status:
            sequence += 1
            vals_list.append({
                "order_id": self.id,
                "sequence": sequence,
                "event_date": (fields.Datetime.now() if source == "odoo"
                               else _magento_datetime(data.get("updated_at")) or fields.Datetime.now()),
                "from_status": last_status or False,
                "to_status": current,
                "magento_state": data.get("state") or False,
                "source": source or ("import" if not events and not vals_list else "sync"),
                "is_transition": True,
                "is_regression": is_regression(last_status, current),
            })
        if vals_list:
            Event.create(vals_list)
        return len(vals_list)

    def _magento_store_remote_state(self, data):
        # Status changed by an Odoo action (hold, release, cancel): part of the history too.
        super()._magento_store_remote_state(data)
        for order in self:
            if (data or {}).get("status"):
                order._magento_record_status_events(
                    {"status": data.get("status"), "state": data.get("state")}, source="odoo")

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def _magento_conflict_fulfilment(self):
        """Why the order can no longer simply be rewritten: posted invoice, done delivery."""
        self.ensure_one()
        return self._magento_cancel_blockers()

    def _magento_conflict_detect(self, data, snapshot, types=None):
        """{type: detection} of the differences between ``data`` (Magento now) and
        ``snapshot`` that Odoo does not already reflect. A detection is a dict
        {rows, hash, severity, can_accept, summary}."""
        self.ensure_one()
        snapshot = snapshot or {}
        types = set(types or dict(CONFLICT_TYPES))
        currency = self.currency_id or self.env.company.currency_id
        fulfilled = bool(self._magento_conflict_fulfilment()) if self.state == "sale" else False
        exported = self.magento_exported_from_odoo
        now_items = self._magento_conflict_items_view(data)
        snap_items = snapshot.get("items") or {}
        odoo_lines, odoo_extra = self._magento_conflict_odoo_lines()
        detections = {}

        def qty_eq(a, b):
            return float_compare(_to_float(a), _to_float(b), precision_digits=4) == 0

        def price_eq(a, b):
            return currency.compare_amounts(_to_float(a), _to_float(b)) == 0

        def fmt_qty(value):
            return ("%g" % _to_float(value)) if value not in (None, "") else ""

        def fmt_price(value):
            return ("%.*f" % (currency.decimal_places, _to_float(value))) if value not in (None, "") else ""

        def label(key, view):
            return (view or {}).get("sku") or (view or {}).get("name") or key

        def add(ctype, rows, magento_side, severity="warn", can_accept=True, summary=None):
            if not rows or ctype not in types:
                return
            detections[ctype] = {
                "rows": rows,
                "hash": _fingerprint([ctype, magento_side]),
                "severity": "crit" if not can_accept and severity != "info" and ctype != "edited_in_odoo"
                            else severity,
                "can_accept": can_accept,
                "summary": summary or "; ".join(
                    "%s: %s → %s" % (r["label"], r.get("before") or "—", r.get("after") or "—")
                    for r in rows)[:250],
            }

        # -- lines ---------------------------------------------------------
        line_ok = self.state != "cancel"
        lines_accept = not fulfilled
        item_rows, qty_rows, price_rows = [], [], []
        if line_ok:
            for key, now in now_items.items():
                odoo = odoo_lines.get(key)
                snap = snap_items.get(key)
                if not snap:
                    if odoo and odoo["product_id"] == now["product_id"] and qty_eq(odoo["qty"], now["qty"]):
                        continue
                    item_rows.append({
                        "label": _("%s (added in Magento)", label(key, now)),
                        "before": "", "after": _("qty %s", fmt_qty(now["qty"])),
                        "odoo": _("qty %s", fmt_qty(odoo["qty"])) if odoo else "",
                    })
                    continue
                if (snap.get("sku") or "") != (now.get("sku") or "") or (
                        snap.get("product_id") and now.get("product_id")
                        and snap["product_id"] != now["product_id"]):
                    if not odoo or odoo["product_id"] != now["product_id"]:
                        item_rows.append({
                            "label": _("%s (product)", key),
                            "before": snap.get("sku") or snap.get("name"),
                            "after": now.get("sku") or now.get("name"),
                            "odoo": (odoo or {}).get("sku") or "",
                        })
                if not qty_eq(snap.get("qty"), now["qty"]) and (
                        not odoo or not qty_eq(odoo["qty"], now["qty"])):
                    qty_rows.append({
                        "label": _("%s quantity", label(key, now)),
                        "before": fmt_qty(snap.get("qty")), "after": fmt_qty(now["qty"]),
                        "odoo": fmt_qty(odoo["qty"]) if odoo else "",
                    })
                if not exported and not price_eq(snap.get("price"), now["price"]) and (
                        not odoo or not price_eq(odoo["price"], now["price"])):
                    price_rows.append({
                        "label": _("%s unit price", label(key, now)),
                        "before": fmt_price(snap.get("price")), "after": fmt_price(now["price"]),
                        "odoo": fmt_price(odoo["price"]) if odoo else "",
                    })
            for key, snap in snap_items.items():
                if key in now_items:
                    continue
                odoo = odoo_lines.get(key)
                if odoo and not qty_eq(odoo["qty"], 0.0):
                    item_rows.append({
                        "label": _("%s (removed in Magento)", label(key, snap)),
                        "before": _("qty %s", fmt_qty(snap.get("qty"))), "after": "",
                        "odoo": _("qty %s", fmt_qty(odoo["qty"])),
                    })
        add("items_changed", item_rows, {k: (v["sku"], v["product_id"]) for k, v in now_items.items()},
            can_accept=lines_accept)
        add("qty_changed", qty_rows, {k: v["qty"] for k, v in now_items.items()},
            can_accept=lines_accept)
        add("price_changed", price_rows, {k: v["price"] for k, v in now_items.items()},
            can_accept=lines_accept)

        # -- totals ----------------------------------------------------------
        snap_totals = snapshot.get("totals") or {}
        grand_total = data.get("grand_total")
        if grand_total in (None, ""):
            grand_total = data.get("base_grand_total")
        if (line_ok and not exported and grand_total not in (None, "") and "grand_total" in snap_totals
                and not price_eq(snap_totals.get("grand_total"), grand_total)
                and not price_eq(self.amount_total, grand_total)):
            rows = [{"label": _("Grand total"), "before": fmt_price(snap_totals.get("grand_total")),
                     "after": fmt_price(grand_total), "odoo": fmt_price(self.amount_total)}]
            for key, text in (("subtotal", _("Subtotal")), ("shipping_amount", _("Shipping")),
                              ("discount_amount", _("Discount")), ("tax_amount", _("Tax"))):
                if key in snap_totals and not price_eq(snap_totals[key], data.get(key)):
                    rows.append({"label": text, "before": fmt_price(snap_totals[key]),
                                 "after": fmt_price(data.get(key)), "odoo": ""})
            add("totals_changed", rows, _to_float(grand_total), can_accept=lines_accept)

        # -- addresses -------------------------------------------------------
        if line_ok and snapshot.get("shipping_address_hash"):
            billing = self._magento_extract_billing_address(data)
            shipping = self._magento_extract_shipping_address(data) or billing
            rows, magento_side = [], {}
            done_delivery = bool(self.picking_ids.filtered(
                lambda p: p.state == "done" and p.picking_type_code == "outgoing"))
            posted_invoice = bool(self._magento_get_customer_invoices()) if self.state == "sale" else False
            blocked = False
            for kind, address, partner, text in (
                    ("shipping_address", shipping, self.partner_shipping_id, _("Shipping")),
                    ("billing_address", billing, self.partner_invoice_id, _("Billing"))):
                now_view = self._magento_conflict_address(address)
                if self._magento_conflict_address_key(now_view) == snapshot.get(kind + "_hash"):
                    continue
                if not any(now_view.get(f) for f in ("street", "city", "postcode")):
                    continue
                if self._magento_conflict_partner_matches(partner, address):
                    continue
                before = snapshot.get(kind) or {}
                for fname, flabel in ADDRESS_FIELDS:
                    if (before.get(fname) or "").lower() != (now_view.get(fname) or "").lower():
                        rows.append({"label": "%s: %s" % (text, _(flabel)),
                                     "before": before.get(fname) or "", "after": now_view.get(fname) or ""})
                magento_side[kind] = now_view
                if (kind == "shipping_address" and done_delivery) or (
                        kind == "billing_address" and posted_invoice):
                    blocked = True
            add("address_changed", rows, magento_side, can_accept=not blocked)

        # -- customer --------------------------------------------------------
        snap_customer = snapshot.get("customer")
        if line_ok and snap_customer:
            billing = self._magento_extract_billing_address(data)
            is_guest = self._magento_is_guest(data)
            now_customer = {
                "email": self._magento_normalize_email(data.get("customer_email") or billing.get("email")),
                "customer_id": "" if is_guest else str(data.get("customer_id") or ""),
            }
            rows = []
            partner = self.partner_id.commercial_partner_id
            if (now_customer["email"] and now_customer["email"] != (snap_customer.get("email") or "")
                    and now_customer["email"] != self._magento_normalize_email(partner.email)):
                rows.append({"label": _("Customer email"), "before": snap_customer.get("email") or "",
                             "after": now_customer["email"], "odoo": partner.email or ""})
            if (now_customer["customer_id"] != (snap_customer.get("customer_id") or "")
                    and now_customer["customer_id"] != (partner.magento_customer_id or "")):
                rows.append({"label": _("Magento customer ID"),
                             "before": snap_customer.get("customer_id") or _("guest"),
                             "after": now_customer["customer_id"] or _("guest"),
                             "odoo": partner.magento_customer_id or ""})
            add("customer_changed", rows, now_customer, can_accept=not fulfilled)

        # -- payment ---------------------------------------------------------
        payment = data.get("payment") or {}
        method = (payment.get("method") or "") if isinstance(payment, dict) else ""
        if line_ok and "payment_method" in snapshot and method and method != (snapshot.get("payment_method") or ""):
            add("payment_changed", [{"label": _("Payment method"),
                                     "before": snapshot.get("payment_method") or "", "after": method}],
                method, can_accept=True)

        # -- status regression -------------------------------------------------
        if "status_regressed" in types:
            detection = self._magento_conflict_detect_regression(data, fulfilled)
            if detection:
                detections["status_regressed"] = detection

        # -- cancelled after fulfilment ----------------------------------------
        if ((data.get("state") or self.magento_state or "").lower() == "canceled"
                and self.state not in ("cancel", "draft", "sent")
                and (self.magento_cancel_conflict or fulfilled)):
            reasons = self._magento_conflict_fulfilment()
            add("canceled_after_fulfilment", [{
                "label": _("Magento status"), "before": snapshot.get("status") or "",
                "after": data.get("status") or "canceled",
                "odoo": ", ".join(str(r) for r in reasons) or _("confirmed"),
            }], "canceled", severity="crit", can_accept=False)

        # -- edited in Odoo ----------------------------------------------------
        baseline = snapshot.get("odoo_lines")
        if line_ok and baseline is not None and "edited_in_odoo" in types:
            rows = []
            for key, odoo in odoo_lines.items():
                base = baseline.get(key)
                if not base:
                    continue
                now = now_items.get(key)
                changed = (base.get("product_id") != odoo["product_id"]
                           or not qty_eq(base.get("qty"), odoo["qty"])
                           or (not exported and not price_eq(base.get("price"), odoo["price"])))
                if not changed:
                    continue
                if now and now["product_id"] == odoo["product_id"] and qty_eq(now["qty"], odoo["qty"]) \
                        and (exported or price_eq(now["price"], odoo["price"])):
                    continue
                rows.append({"label": _("%s (changed in Odoo)", label(key, odoo)),
                             "before": "%s × %s" % (fmt_qty(base.get("qty")), fmt_price(base.get("price"))),
                             "after": "%s × %s" % (fmt_qty(now["qty"]), fmt_price(now["price"])) if now else "",
                             "odoo": "%s × %s" % (fmt_qty(odoo["qty"]), fmt_price(odoo["price"]))})
            for key, base in baseline.items():
                if key not in odoo_lines and key in now_items:
                    rows.append({"label": _("%s (removed in Odoo)", label(key, base)),
                                 "before": fmt_qty(base.get("qty")), "after": fmt_qty(now_items[key]["qty"]),
                                 "odoo": ""})
            known_extra = set(snapshot.get("odoo_extra_ids") or [])
            for extra in odoo_extra:
                if extra["line_id"] in known_extra:
                    continue
                rows.append({"label": _("%s (added in Odoo)", extra["name"] or extra["sku"]),
                             "before": "", "after": "",
                             "odoo": "%s × %s" % (fmt_qty(extra["qty"]), fmt_price(extra["price"]))})
            add("edited_in_odoo", rows, [r["label"] + "|" + (r.get("odoo") or "") for r in rows],
                severity="warn", can_accept=False)
        return detections

    @api.model
    def _magento_conflict_partner_matches(self, partner, address):
        """True when the Odoo contact already carries the Magento ``address``."""
        if not partner:
            return False
        vals = self._magento_address_vals(address)
        vals["name"] = vals.get("name") or partner.name
        return self._magento_partner_address_key(partner) == self._magento_address_key(vals)

    def _magento_conflict_detect_regression(self, data, fulfilled=False):
        self.ensure_one()
        events = self.env["magento.order.status.event"].sudo().search(
            [("order_id", "=", self.id)], order="sequence, id")
        regressions = events.filtered("is_regression")
        if not regressions:
            return None
        last = regressions[-1]
        from_rank = status_rank(last.from_status)
        current = data.get("status") or self.magento_status or ""
        current_rank = status_rank(current)
        if current_rank is not None and current_rank >= from_rank:
            return None
        rows = [{"label": _("Magento status"), "before": last.from_status or "", "after": current,
                 "odoo": dict(self._fields["state"]._description_selection(self.env)).get(self.state)}]
        history = events.filtered(lambda e: e.sequence >= last.sequence and e.is_transition)
        if len(history) > 1:
            rows.append({"label": _("Transitions since"), "before": "",
                         "after": " → ".join([last.from_status or ""] + history.mapped("to_status"))})
        return {
            "rows": rows,
            "hash": _fingerprint(["status_regressed", last.id]),
            "severity": "crit" if fulfilled else "warn",
            "can_accept": True,
            "summary": _("Status %(old)s → %(new)s", old=last.from_status or "", new=current),
        }

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------
    def _magento_conflict_upsert(self, detections, data, auto_types=(), only_types=None):
        """Open / refresh / auto-resolve this order's conflict rows from ``detections``.

        ``auto_types``: detected types Magento already won (quotation): recorded resolved_auto.
        ``only_types``: limit the pass to these types (others are left untouched).
        Returns the newly opened rows.
        """
        self.ensure_one()
        Conflict = self.env["magento.order.conflict"].sudo()
        rows = Conflict.search([("order_id", "=", self.id)])
        open_rows = {r.conflict_type: r for r in rows if r.state == "open"}
        now = fields.Datetime.now()
        updated_at = _magento_datetime((data or {}).get("updated_at"))
        created = Conflict.browse()
        for ctype, _label in CONFLICT_TYPES:
            if only_types is not None and ctype not in only_types:
                continue
            detection = detections.get(ctype)
            row = open_rows.get(ctype)
            if not detection:
                if row:
                    row.write({"state": "resolved_auto", "resolved_date": now, "resolved_by": False,
                               "magento_data": False})
                continue
            vals = {
                "diff": {"rows": detection["rows"]},
                "summary": detection["summary"],
                "severity": detection["severity"],
                "can_accept": detection["can_accept"],
                "magento_hash": detection["hash"],
                "magento_updated_at": updated_at,
            }
            if ctype in auto_types:
                vals.update(state="resolved_auto", resolved_date=now, resolved_by=False,
                            severity="info", magento_data=False)
                if row:
                    row.write(vals)
                elif rows.filtered(lambda r: r.conflict_type == ctype and r.magento_hash == detection["hash"]):
                    continue  # this Magento change is already recorded
                else:
                    Conflict.create(dict(vals, order_id=self.id, conflict_type=ctype, detected_at=now))
                continue
            if row:
                vals["magento_data"] = data
                if row.magento_hash != detection["hash"] or row.diff != vals["diff"] \
                        or row.severity != vals["severity"] or row.can_accept != vals["can_accept"]:
                    row.write(vals)
                elif data and row.magento_data != data:
                    row.write({"magento_data": data, "magento_updated_at": updated_at})
                continue
            # This Magento version was already acknowledged (a status regression taken
            # automatically while the order was a quotation counts too: it is per event).
            ack_states = ("kept", "accepted", "resolved_auto") if ctype == "status_regressed" \
                else ("kept", "accepted")
            if rows.filtered(lambda r: r.conflict_type == ctype and r.state in ack_states
                             and r.magento_hash == detection["hash"]):
                continue
            created |= Conflict.create(dict(vals, order_id=self.id, conflict_type=ctype,
                                            detected_at=now, magento_data=data))
        notable = created.filtered(lambda r: r.conflict_type not in LINE_TYPES)
        if notable:
            labels = self._magento_conflict_type_labels()
            body = Markup("%s<ul>%s</ul>") % (
                _("Magento order conflict detected:"),
                Markup("").join(Markup("<li><b>%s</b>: %s</li>") % (
                    labels[r.conflict_type], r.summary or "") for r in notable),
            )
            self.message_post(body=body)
        self._magento_conflict_flag_refresh()
        return created

    def _magento_conflict_type_labels(self):
        field = self.env["magento.order.conflict"]._fields["conflict_type"]
        return dict(field._description_selection(self.env))

    def _magento_conflict_flag_refresh(self):
        # Stored flag: recomputed from the rows (also fixes orders flagged before 3.7).
        self.env.add_to_compute(self._fields["magento_sync_conflict"], self)
        self.invalidate_recordset(["magento_conflict_count"])

    def _magento_conflict_line_diffs(self):
        """Text lines of the open line conflicts (``magento_conflict_note``)."""
        self.ensure_one()
        rows = self.env["magento.order.conflict"].sudo().search([
            ("order_id", "=", self.id), ("state", "=", "open"), ("conflict_type", "in", LINE_TYPES),
        ], order="id")
        diffs = []
        for row in rows:
            for r in (row.diff or {}).get("rows") or []:
                diffs.append("%s: %s → %s" % (r.get("label"), r.get("before") or "—", r.get("after") or "—"))
        return diffs

    def _magento_conflict_run(self, data, items):
        """Conflict pass of one re-sync (called by ``_magento_sync_order_lines``)."""
        self.ensure_one()
        snapshot = self.magento_import_snapshot or {}
        if self.state in ("draft", "sent"):
            detections = self._magento_conflict_detect(
                data, snapshot, types=[t for t, _l in CONFLICT_TYPES if t != "edited_in_odoo"],
            ) if snapshot else {}
            self._magento_write_order_lines(items, data=data)
            address = detections.get("address_changed")
            if address and address["can_accept"]:
                self._magento_conflict_apply_address(data)
            keep = ("customer",) if "customer_changed" in detections else ()
            snapshot = self._magento_conflict_store_snapshot(data, keep=keep)
            detections.update(self._magento_conflict_detect(data, snapshot, types=["edited_in_odoo"]))
            self._magento_conflict_upsert(detections, data, auto_types=AUTO_TYPES)
            self._magento_set_conflict([])
            return
        if not snapshot:
            snapshot = self._magento_conflict_legacy_snapshot(data)
        detections = self._magento_conflict_detect(data, snapshot)
        self._magento_conflict_upsert(detections, data)
        self._magento_set_conflict(self._magento_conflict_line_diffs())

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def _magento_conflict_resolve_rows(self, rows, state):
        rows.sudo().write({
            "state": state,
            "resolved_by": self.env.uid,
            "resolved_date": fields.Datetime.now(),
            "magento_data": False,
        })

    def _magento_conflict_accept(self, rows):
        """Apply Magento's version for the open ``rows`` of this order."""
        self.ensure_one()
        rows = rows.filtered(lambda r: r.state == "open" and r.can_accept)
        if not rows:
            return
        data = next((r.magento_data for r in rows.sorted("id", reverse=True) if r.magento_data), None)
        types = set(rows.mapped("conflict_type"))
        needs_data = types & (set(LINE_TYPES) | {"address_changed", "customer_changed"})
        if needs_data and not data:
            raise UserError(_("The Magento version of order %s is no longer available: pull the "
                              "order from Magento again, then retry.", self.name))
        labels = self._magento_conflict_type_labels()
        notes = []
        sections = set()
        Conflict = self.env["magento.order.conflict"].sudo()
        if types & set(LINE_TYPES):
            if self._magento_conflict_fulfilment():
                raise UserError(_("Order %s is already delivered or invoiced: its lines cannot be "
                                  "rewritten. Keep the Odoo version and use a credit note or a "
                                  "return.", self.name))
            notes.append(self._magento_conflict_apply_lines(data))
            # One rewrite fixes every line conflict of the order.
            rows |= Conflict.search([("order_id", "=", self.id), ("state", "=", "open"),
                                     ("conflict_type", "in", LINE_TYPES)])
            sections.update(TYPE_SECTIONS["items_changed"])
        if "address_changed" in types:
            notes.append(self._magento_conflict_apply_address(data))
            sections.update(TYPE_SECTIONS["address_changed"])
        if "customer_changed" in types:
            notes.append(self._magento_conflict_apply_customer(data))
            sections.update(TYPE_SECTIONS["customer_changed"])
        if "payment_changed" in types:
            sections.update(TYPE_SECTIONS["payment_changed"])
            if data:
                notes.append(_("Magento payment method accepted."))
        if "status_regressed" in types:
            notes.append(_("Magento status '%s' accepted.", self.magento_status or ""))
        if sections and data:
            self._magento_conflict_store_snapshot(data, sections=sections)
        elif "payment_changed" in types:
            snapshot = dict(self.magento_import_snapshot or {})
            snapshot["payment_method"] = self.magento_payment_method or ""
            self.sudo().write({"magento_import_snapshot": snapshot})
        self._magento_conflict_resolve_rows(rows, "accepted")
        self._magento_set_conflict(self._magento_conflict_line_diffs())
        self._magento_conflict_flag_refresh()
        body = Markup("%s<ul>%s</ul>") % (
            _("Magento conflict resolved by %s: Magento version applied.", self.env.user.name),
            Markup("").join(Markup("<li>%s</li>") % escape(labels[t]) for t in sorted(set(rows.mapped("conflict_type")))),
        )
        for note in notes:
            if note:
                body += note if isinstance(note, Markup) else Markup("<p>%s</p>") % note
        self.message_post(body=body)

    def _magento_conflict_keep(self, rows):
        self.ensure_one()
        rows = rows.filtered(lambda r: r.state == "open")
        if not rows:
            return
        labels = self._magento_conflict_type_labels()
        self._magento_conflict_resolve_rows(rows, "kept")
        self._magento_set_conflict(self._magento_conflict_line_diffs())
        self._magento_conflict_flag_refresh()
        self.message_post(body=Markup("%s<ul>%s</ul>") % (
            _("Magento conflict resolved by %s: the Odoo version is kept (Magento unchanged).",
              self.env.user.name),
            Markup("").join(Markup("<li>%s: %s</li>") % (labels[r.conflict_type], r.summary or "")
                            for r in rows),
        ))

    def _magento_conflict_apply_lines(self, data):
        """Unlock the confirmed order and rewrite its lines from the Magento ``data``."""
        self.ensure_one()
        items = self._magento_effective_items(data.get("items"))
        if not items:
            raise UserError(_("The Magento order has no items to apply."))
        before = {line.id: (line.product_id.display_name, line.product_uom_qty, line.price_unit)
                  for line in self.order_line if not line.display_type}
        was_locked = self.locked
        if was_locked:
            self.action_unlock()
        order = self.with_context(skip_magento_sync=True, magento_skip_push=True,
                                  magento_keep_removed_lines=True)
        order._magento_write_order_lines(items, data=data)
        order._magento_update_total_difference(data)
        if was_locked:
            self.action_lock()
        changes = []
        for line in self.order_line.filtered(lambda l: not l.display_type):
            old = before.get(line.id)
            new = (line.product_id.display_name, line.product_uom_qty, line.price_unit)
            if old is None:
                changes.append(_("Added %(product)s × %(qty)s at %(price)s", product=new[0],
                                 qty=new[1], price=new[2]))
            elif old != new:
                changes.append(_("%(product)s: %(oq)s × %(op)s → %(nq)s × %(np)s", product=new[0],
                                 oq=old[1], op=old[2], nq=new[1], np=new[2]))
        intro = _("Order lines rewritten from Magento (order unlocked%s).",
                  _(" and locked again") if was_locked else "")
        if not changes:
            return Markup("<p>%s</p>") % intro
        return Markup("<p>%s</p><ul>%s</ul>") % (
            intro, Markup("").join(Markup("<li>%s</li>") % c for c in changes))

    def _magento_conflict_apply_address(self, data):
        """Set Magento's shipping / billing address on the order and its open deliveries."""
        self.ensure_one()
        partner = self.partner_id
        billing = self._magento_extract_billing_address(data)
        shipping = self._magento_extract_shipping_address(data) or billing
        vals = {}
        if shipping and not self._magento_conflict_partner_matches(self.partner_shipping_id, shipping):
            if self.picking_ids.filtered(lambda p: p.state == "done" and p.picking_type_code == "outgoing"):
                raise UserError(_("Order %s is already delivered: the new address cannot be applied.",
                                  self.name))
            delivery = self._magento_get_address_partner(partner, shipping, "delivery")
            if delivery != self.partner_shipping_id:
                vals["partner_shipping_id"] = delivery.id
        if billing and not self._magento_conflict_partner_matches(self.partner_invoice_id, billing):
            if self.state != "sale" or not self._magento_get_customer_invoices():
                invoice_partner = self._magento_get_address_partner(partner, billing, "invoice")
                if invoice_partner != self.partner_invoice_id:
                    vals["partner_invoice_id"] = invoice_partner.id
        if not vals:
            return None
        old = self.partner_shipping_id
        self.with_context(update_delivery_shipping_partner=True, skip_magento_sync=True,
                          magento_skip_push=True).write(vals)
        if "partner_shipping_id" in vals:
            pickings = self.picking_ids.filtered(
                lambda p: p.state not in ("done", "cancel") and p.picking_type_code == "outgoing"
                and p.partner_id != self.partner_shipping_id)
            if pickings:
                pickings.with_context(skip_magento_sync=True).write({"partner_id": self.partner_shipping_id.id})
            def where(partner):
                return ", ".join(p for p in (partner.street, partner.street2, partner.zip, partner.city) if p) \
                    or partner.display_name

            return Markup("<p>%s</p>") % _(
                "Delivery address changed from %(old)s to %(new)s (open deliveries updated).",
                old=where(old), new=where(self.partner_shipping_id))
        return Markup("<p>%s</p>") % _("Invoice address updated from Magento.")

    def _magento_conflict_apply_customer(self, data):
        self.ensure_one()
        partner, email = self._magento_resolve_customer(data, self.magento_instance_id)
        if partner == self.partner_id:
            return None
        old = self.partner_id
        billing = self._magento_extract_billing_address(data)
        shipping = self._magento_extract_shipping_address(data)
        invoice_partner = self._magento_get_address_partner(partner, billing, "invoice")
        shipping_partner = (self._magento_get_address_partner(partner, shipping, "delivery")
                            if shipping else invoice_partner)
        self.with_context(update_delivery_shipping_partner=True, skip_magento_sync=True,
                          magento_skip_push=True).write({
            "partner_id": partner.id,
            "partner_invoice_id": invoice_partner.id,
            "partner_shipping_id": shipping_partner.id,
            "magento_customer_email": email or self.magento_customer_email,
        })
        return Markup("<p>%s</p>") % _("Customer changed from %(old)s to %(new)s.",
                                       old=old.display_name, new=partner.display_name)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def action_view_magento_conflicts(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_order_conflict")
        action["domain"] = [("order_id", "=", self.id)]
        action["context"] = {"search_default_open": 1, "default_order_id": self.id}
        return action
