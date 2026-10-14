"""Order lifecycle actions (roadmap 2.7): cancel, hold and release hold, both ways.

Odoo -> Magento
    * Cancelling a Magento order in Odoo first reads the Magento order. It is cancelled
      there (``POST /V1/orders/{id}/cancel``) only while Magento allows it: state new,
      pending_payment or processing, and nothing invoiced. Otherwise the Odoo cancel is
      refused with the reason. An order Magento already cancelled is not posted again.
    * "Hold in Magento" / "Release Hold" (``/hold``, ``/unhold``) check the Magento state
      first, so a repeated click posts nothing.
    Every action leaves a chatter note.

Magento -> Odoo (order sync)
    * ``canceled``: the Odoo order is cancelled when that is safe (no posted invoice, no
      done delivery); otherwise it is flagged as a conflict for a person to resolve.
    * ``holded``: ``magento_on_hold`` drives the "On hold in Magento" badge.
    * Imported Magento shipments bring their tracking numbers onto the delivery.
"""
import logging

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Magento order states that accept cancel / hold (Magento's canCancel / canHold).
CANCELABLE_STATES = ("new", "pending_payment", "processing")
HOLDABLE_STATES = ("new", "pending_payment", "processing")
_NO_PUSH_CTX = {"skip_magento_sync": True, "magento_skip_push": True}


def _amount(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SaleOrder(models.Model):
    _inherit = "sale.order"

    magento_on_hold = fields.Boolean(
        string="On Hold in Magento",
        compute="_compute_magento_on_hold",
        store=True,
        index=True,
        help="The Magento order is on hold (state 'holded').",
    )
    magento_cancel_conflict = fields.Boolean(
        string="Cancelled in Magento Only",
        readonly=True,
        copy=False,
        help="Magento cancelled the order, but Odoo did not cancel it because it already has a "
             "posted invoice or a done delivery. Resolve it by hand (credit note, return).",
    )

    @api.depends("magento_state")
    def _compute_magento_on_hold(self):
        for order in self:
            order.magento_on_hold = (order.magento_state or "").lower() == "holded"

    # ------------------------------------------------------------------
    # Magento calls
    # ------------------------------------------------------------------
    def _magento_order_ref(self):
        self.ensure_one()
        return self.magento_increment_id or self.magento_order_id

    def _magento_remote_order(self, api):
        """Current Magento order (``GET /V1/orders/{id}``). Raises ``UserError``."""
        self.ensure_one()
        try:
            data = api.get_order(self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        if not isinstance(data, dict) or not data:
            raise UserError(_("Magento order %s could not be read.") % self._magento_order_ref())
        return data

    def _magento_store_remote_state(self, data):
        """Keep the Magento state/status of ``data`` on the order (no push)."""
        self.ensure_one()
        vals = {}
        for fname, key in (("magento_state", "state"), ("magento_status", "status")):
            value = (data or {}).get(key)
            if value and value != (self[fname] or ""):
                vals[fname] = value
        if vals:
            self.with_context(**_NO_PUSH_CTX).write(vals)

    def _magento_refresh_remote_state(self, api, expected_state=None):
        """Read the Magento state back after an action; never raises (the action is done)."""
        self.ensure_one()
        try:
            data = api.get_order(self.magento_order_id)
        except requests.exceptions.RequestException as exc:
            _logger.warning("Magento order %s could not be read back: %s", self.magento_order_id, exc)
            data = {"state": expected_state, "status": expected_state} if expected_state else {}
        if isinstance(data, dict):
            self._magento_store_remote_state(data)

    def _magento_order_action(self, api, action):
        """``POST /V1/orders/{id}/{action}`` (cancel, hold, unhold); Magento answers a boolean."""
        self.ensure_one()
        try:
            result = api._post("/rest/V1/orders/%s/%s" % (self.magento_order_id, action))
        except requests.exceptions.RequestException as exc:
            self._magento_raise_http_error(exc)
        if result is False:
            raise UserError(_(
                "Magento refused to %(action)s order %(order)s.",
                action=action, order=self._magento_order_ref(),
            ))
        return result

    def _magento_lifecycle_orders(self):
        orders = self.filtered(lambda o: o.magento_order_id and o.magento_instance_id)
        if not orders:
            raise UserError(_("This order is not linked to a Magento order."))
        return orders

    def _magento_note(self, message):
        self.ensure_one()
        self.message_post(body=Markup("<p>%s</p>") % escape(message))

    def _magento_action_notification(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento"),
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    # ------------------------------------------------------------------
    # Odoo -> Magento: cancel
    # ------------------------------------------------------------------
    def _magento_cancel_refusal(self, data):
        """Why Magento cannot cancel ``data`` (``None`` when it can, or already did)."""
        self.ensure_one()
        state = (data.get("state") or "").lower()
        ref = self._magento_order_ref()
        if state == "canceled":
            return None
        if state == "holded":
            return _(
                "Magento order %s is on hold. Release the hold in Magento before cancelling it.",
                ref,
            )
        if state not in CANCELABLE_STATES:
            return _(
                "Magento order %(ref)s is '%(state)s' and can no longer be cancelled there: Magento "
                "only cancels new, pending payment or processing orders. Refund it with a credit "
                "memo instead.",
                ref=ref, state=data.get("status") or state or _("unknown"),
            )
        if _amount(data.get("total_invoiced")) > 0:
            return _(
                "Magento order %s is already invoiced in Magento, so it cannot be cancelled there. "
                "Refund it with a credit memo instead.",
                ref,
            )
        return None

    def _action_cancel(self):
        ctx = self.env.context
        if ctx.get("skip_magento_sync") or ctx.get("magento_skip_push"):
            res = super()._action_cancel()
            self.filtered("magento_cancel_conflict").write({"magento_cancel_conflict": False})
            return res
        plan = []
        for order in self.filtered(
            lambda o: o.magento_order_id and o.magento_instance_id and o.state != "cancel"
        ):
            api = order._magento_api()
            data = order._magento_remote_order(api)
            refusal = order._magento_cancel_refusal(data)
            if refusal:
                raise UserError(refusal)
            plan.append((order, api, data))
        res = super()._action_cancel()
        self.filtered("magento_cancel_conflict").write({"magento_cancel_conflict": False})
        # Odoo first, Magento last: a Magento failure rolls the Odoo cancel back.
        for order, api, data in plan:
            if (data.get("state") or "").lower() == "canceled":
                order._magento_store_remote_state(data)
                order._magento_note(_("Already cancelled in Magento: nothing sent."))
                continue
            order._magento_order_action(api, "cancel")
            order._magento_refresh_remote_state(api, expected_state="canceled")
            order._magento_note(_(
                "Magento order %(ref)s cancelled from Odoo by %(user)s.",
                ref=order._magento_order_ref(), user=self.env.user.name,
            ))
        return res

    # ------------------------------------------------------------------
    # Odoo -> Magento: hold / release hold
    # ------------------------------------------------------------------
    def action_magento_hold(self):
        held = 0
        for order in self._magento_lifecycle_orders():
            api = order._magento_api()
            data = order._magento_remote_order(api)
            state = (data.get("state") or "").lower()
            if state == "holded":
                order._magento_store_remote_state(data)
                order._magento_note(_("Already on hold in Magento: nothing sent."))
                continue
            if state not in HOLDABLE_STATES:
                raise UserError(_(
                    "Magento order %(ref)s is '%(state)s' and cannot be put on hold: Magento only "
                    "holds new, pending payment or processing orders.",
                    ref=order._magento_order_ref(), state=data.get("status") or state,
                ))
            order._magento_order_action(api, "hold")
            order._magento_refresh_remote_state(api, expected_state="holded")
            order._magento_note(_(
                "Magento order %(ref)s put on hold from Odoo by %(user)s.",
                ref=order._magento_order_ref(), user=self.env.user.name,
            ))
            held += 1
        return self._magento_action_notification(
            _("Order put on hold in Magento.") if held else _("The order was already on hold in Magento.")
        )

    def action_magento_unhold(self):
        released = 0
        for order in self._magento_lifecycle_orders():
            api = order._magento_api()
            data = order._magento_remote_order(api)
            if (data.get("state") or "").lower() != "holded":
                order._magento_store_remote_state(data)
                order._magento_note(_("Not on hold in Magento: nothing sent."))
                continue
            order._magento_order_action(api, "unhold")
            order._magento_refresh_remote_state(api)
            order._magento_note(_(
                "Magento order %(ref)s released from hold from Odoo by %(user)s.",
                ref=order._magento_order_ref(), user=self.env.user.name,
            ))
            released += 1
        return self._magento_action_notification(
            _("Hold released in Magento.") if released else _("The order was not on hold in Magento.")
        )

    # ------------------------------------------------------------------
    # Magento -> Odoo: cancel
    # ------------------------------------------------------------------
    def _magento_cancel_blockers(self):
        """Reasons the Odoo order cannot simply be cancelled (posted invoice, done delivery)."""
        self.ensure_one()
        moves = self.invoice_ids | self.env["account.move"].search([
            ("magento_sale_order_id", "=", self.id),
        ])
        invoices = moves.filtered(lambda m: m.state == "posted" and m.move_type == "out_invoice")
        deliveries = self.picking_ids.filtered(
            lambda p: p.state == "done" and p.picking_type_code == "outgoing"
        )
        reasons = []
        if invoices:
            reasons.append(_("posted invoice %s") % ", ".join(invoices.mapped("name")))
        if deliveries:
            reasons.append(_("done delivery %s") % ", ".join(deliveries.mapped("name")))
        return reasons

    def _magento_reflect_magento_cancel(self):
        """Mirror a Magento cancellation: cancel when safe, otherwise flag a conflict."""
        for order in self:
            if order.state == "cancel":
                continue
            blockers = order._magento_cancel_blockers()
            if blockers:
                if not order.magento_cancel_conflict:
                    order.write({"magento_cancel_conflict": True})
                    order._magento_note(_(
                        "Magento cancelled this order, but it was not cancelled in Odoo because of "
                        "the %s. Resolve it by hand (credit note, return).",
                        " and ".join(blockers),
                    ))
                continue
            if order.state == "done":  # Odoo 16: locked
                order.action_unlock()
            order.with_context(**_NO_PUSH_CTX)._action_cancel()
            order._magento_note(_("Cancelled because the order was cancelled in Magento."))

    def _magento_wf_cancel(self):
        # Workflow "cancel" step: same safety rule as a Magento cancellation.
        self._magento_reflect_magento_cancel()

    # ------------------------------------------------------------------
    # Magento -> Odoo: shipment tracks
    # ------------------------------------------------------------------
    def _magento_apply_shipment(self, shipment_data):
        picking = super()._magento_apply_shipment(shipment_data)
        if picking:
            picking._magento_apply_magento_tracks(shipment_data.get("tracks"))
        return picking


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    def _magento_order_followup_steps(self, api, order):
        steps = super()._magento_order_followup_steps(api, order)
        if (order.magento_state or "").lower() == "canceled" and order.state != "cancel":
            steps = [("magento cancel", order._magento_reflect_magento_cancel)] + list(steps)
        return steps
