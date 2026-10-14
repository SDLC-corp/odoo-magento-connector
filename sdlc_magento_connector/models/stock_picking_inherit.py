import logging
import re

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..services.magento_api import MagentoAPI
from .sql_compat import Constraint


_logger = logging.getLogger(__name__)

# Magento carriers with real tracking; any other carrier code is sent as ``custom``.
TRACKABLE_CARRIERS = ("ups", "usps", "fedex", "dhl")
_TRACKING_SPLIT = re.compile(r"[,;\n]+")


def normalize_tracking_number(number):
    return re.sub(r"\s+", "", str(number or "")).upper()


def split_tracking_numbers(*values):
    """Tracking numbers found in ``values`` (comma/semicolon/newline separated),
    stripped and de-duplicated, first occurrence order kept."""
    seen = set()
    numbers = []
    for value in values:
        if not value:
            continue
        for part in _TRACKING_SPLIT.split(str(value)):
            number = part.strip()
            key = normalize_tracking_number(number)
            if key and key not in seen:
                seen.add(key)
                numbers.append(number)
    return numbers


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class StockPicking(models.Model):
    _inherit = "stock.picking"

    magento_sale_order_id = fields.Many2one(
        "sale.order",
        string="Magento Order",
        related="sale_id",
        store=True,
        readonly=True,
    )
    magento_order_id = fields.Char(
        related="sale_id.magento_order_id",
        store=True,
        readonly=True,
    )
    magento_instance_id = fields.Many2one(
        "magento.instance",
        string="Magento Instance",
        related="sale_id.magento_instance_id",
        store=True,
        readonly=True,
        index=True,
    )
    magento_increment_id = fields.Char(
        string="Order #",
        related="sale_id.magento_increment_id",
        store=True,
        readonly=True,
    )
    magento_sale_order_name = fields.Char(
        related="sale_id.name",
        store=True,
        readonly=True,
    )
    magento_sale_order_date = fields.Datetime(
        string="Order Date",
        compute="_compute_magento_sale_order_date",
        store=True,
        readonly=True,
    )
    magento_ship_to_name = fields.Char(
        string="Ship-to Name",
        compute="_compute_magento_ship_to_name",
        store=True,
        readonly=True,
    )
    magento_total_qty = fields.Float(
        string="Total Quantity",
        compute="_compute_magento_total_qty",
        store=True,
        readonly=True,
        digits="Product Unit",
    )
    magento_ship_date = fields.Datetime(
        string="Ship Date",
        compute="_compute_magento_ship_date",
        store=True,
        readonly=True,
    )
    magento_customer_email = fields.Char(
        string="Customer Email",
        related="partner_id.email",
        store=True,
        readonly=True,
    )
    magento_customer_phone = fields.Char(
        string="Customer Phone",
        compute="_compute_magento_customer_phone",
        store=True,
        readonly=True,
    )
    magento_shipment_id = fields.Char(
        string="Magento Shipment ID",
        readonly=True,
        copy=False,
        index=True,
    )
    magento_shipment_increment_id = fields.Char(
        string="Magento Shipment #",
        readonly=True,
        copy=False,
    )
    magento_tracking_ref = fields.Char(
        string="Magento Tracking Numbers",
        copy=False,
        help="Tracking numbers of this delivery in Magento (comma separated). Numbers added "
             "after the shipment was sent are pushed to the Magento shipment. With the "
             "Delivery module, the transfer's Tracking Reference is sent as well.",
    )
    magento_source_code = fields.Char(
        string="Magento Source",
        readonly=True,
        copy=False,
        help="MSI source the Magento shipment was deducted from.",
    )

    _magento_shipment_uniq = Constraint(
        "unique(magento_instance_id, magento_shipment_id)",
        "This Magento shipment is already linked to another transfer of the same instance.",
    )

    @api.depends("sale_id.magento_created_at", "sale_id.date_order")
    def _compute_magento_sale_order_date(self):
        for picking in self:
            order = picking.sale_id
            picking.magento_sale_order_date = (
                order.magento_created_at or order.date_order or False
            )

    @api.depends("sale_id.partner_shipping_id.name")
    def _compute_magento_ship_to_name(self):
        for picking in self:
            partner = picking.sale_id.partner_shipping_id
            picking.magento_ship_to_name = partner.name if partner else False

    @api.depends(
        "move_ids.product_uom_qty",
        "move_ids.quantity",
        "move_ids.state",
        "state",
    )
    def _compute_magento_total_qty(self):
        for picking in self:
            qty = 0.0
            for move in picking.move_ids:
                if move.state == "cancel":
                    continue
                if move.quantity:
                    qty += move.quantity
                else:
                    qty += move.product_uom_qty
            picking.magento_total_qty = qty

    @api.depends("date_done", "scheduled_date")
    def _compute_magento_ship_date(self):
        for picking in self:
            picking.magento_ship_date = picking.date_done or picking.scheduled_date or False

    @api.depends("partner_id.phone")
    def _compute_magento_customer_phone(self):
        for picking in self:
            partner = picking.partner_id
            picking.magento_customer_phone = partner.phone if partner else False

    def _magento_prepare_shipment_items(self):
        self.ensure_one()
        items = {}
        for move in self.move_ids:
            if move.state == "cancel":
                continue
            sale_line = move.sale_line_id
            if not sale_line or not sale_line.magento_item_id:
                continue
            qty = float(move.quantity or 0.0)
            if qty <= 0:
                qty = float(move.product_uom_qty or 0.0)
            if qty <= 0:
                continue
            if move.product_uom and sale_line.product_uom and move.product_uom != sale_line.product_uom:
                qty = move.product_uom._compute_quantity(
                    qty, sale_line.product_uom, rounding_method="HALF-UP"
                )
            # Configurable children are shipped through their parent item in Magento.
            key = str(sale_line._magento_push_item_id())
            items[key] = items.get(key, 0.0) + qty

        payload_items = []
        for item_id, qty in items.items():
            if qty <= 0:
                continue
            try:
                order_item_id = int(item_id)
            except (TypeError, ValueError):
                order_item_id = item_id
            payload_items.append({
                "order_item_id": order_item_id,
                "qty": qty,
            })
        return payload_items

    # ------------------------------------------------------------------
    # Tracking numbers and carrier (2.7). ``carrier_id`` / ``carrier_tracking_ref``
    # come from the optional ``stock_delivery`` module: never assume they exist.
    # ------------------------------------------------------------------
    def _magento_delivery_fields(self):
        """Names of the ``stock_delivery`` fields present on transfers (module optional)."""
        return {name for name in ("carrier_id", "carrier_tracking_ref") if name in self._fields}

    def _magento_tracking_numbers(self):
        """Tracking numbers of the transfer: the delivery Tracking Reference (when the
        Delivery module is installed) plus the connector's own tracking field."""
        self.ensure_one()
        refs = []
        if "carrier_tracking_ref" in self._magento_delivery_fields():
            refs.append(self["carrier_tracking_ref"])
        refs.append(self.magento_tracking_ref)
        return split_tracking_numbers(*refs)

    def _magento_odoo_carrier(self):
        self.ensure_one()
        if "carrier_id" in self._magento_delivery_fields():
            return self["carrier_id"]
        return False

    def _magento_order_carrier(self):
        """Delivery carrier mapped to the order's Magento shipping method, if any."""
        self.ensure_one()
        order = self.sale_id
        if not order.magento_instance_id or not order.magento_shipping_method:
            return False
        mapping = self.env["magento.carrier.map"].sudo().search([
            ("instance_id", "=", order.magento_instance_id.id),
            ("magento_code", "=", order.magento_shipping_method),
        ], limit=1)
        return mapping._carrier() if mapping else False

    def _magento_track_carrier(self):
        """``(carrier_code, title)`` sent with the tracks.

        The transfer's delivery carrier is reverse-mapped through ``magento.carrier.map``
        (its Magento method code gives the carrier code, its description the title).
        Without a delivery carrier, the order's own Magento shipping method row is used.
        Carrier codes Magento cannot track (flatrate, tablerate...) become ``custom``.
        """
        self.ensure_one()
        order = self.sale_id
        instance = order.magento_instance_id
        CarrierMap = self.env["magento.carrier.map"].sudo()
        carrier = self._magento_odoo_carrier()
        mapping = CarrierMap
        if instance and carrier:
            mapping = CarrierMap.search([
                ("instance_id", "=", instance.id),
                ("carrier_ref", "=", "%s,%s" % (carrier._name, carrier.id)),
            ], limit=1)
        elif instance and order.magento_shipping_method:
            mapping = CarrierMap.search([
                ("instance_id", "=", instance.id),
                ("magento_code", "=", order.magento_shipping_method),
            ], limit=1)
        code = ""
        if mapping:
            code = (mapping.magento_code or "").split("_", 1)[0].lower()
        elif carrier:
            code = (getattr(carrier, "delivery_type", "") or "").lower()
        if code not in TRACKABLE_CARRIERS:
            code = "custom"
        described = mapping.name if mapping and mapping.name != mapping.magento_code else ""
        title = (
            described
            or (carrier.name if carrier else "")
            or order.magento_shipping_description
            or (mapping.name if mapping else "")
            or _("Tracking")
        )
        return code, title

    def _magento_prepare_shipment_tracks(self, numbers=None):
        """``tracks`` of the Magento shipment payload (one per tracking number)."""
        self.ensure_one()
        numbers = self._magento_tracking_numbers() if numbers is None else numbers
        if not numbers:
            return []
        code, title = self._magento_track_carrier()
        return [{"track_number": number, "title": title, "carrier_code": code} for number in numbers]

    def _magento_shipment_source_code(self):
        """MSI source the goods left from, or ``False`` when the instance is not in MSI mode.

        The source mapped (``magento.stock.source.map``) to the locations the goods were
        taken from wins; otherwise the instance's MSI source code.
        """
        self.ensure_one()
        instance = self.sale_id.magento_instance_id.sudo()
        if not instance or instance.stock_push_mode != "msi":
            return False
        locations = self.move_line_ids.location_id | self.location_id
        for source in instance.stock_source_map_ids.filtered("active"):
            paths = [loc.parent_path for loc in source.location_ids if loc.parent_path]
            for location in locations:
                if location.parent_path and any(location.parent_path.startswith(p) for p in paths):
                    return source.source_code
        return instance.msi_source_code or "default"

    def _magento_record_tracks(self, numbers):
        """Remember the tracking numbers Magento has (connector field only, no push)."""
        self.ensure_one()
        merged = split_tracking_numbers(self.magento_tracking_ref, *numbers)
        text = ", ".join(merged) or False
        if text != (self.magento_tracking_ref or False):
            self.with_context(skip_magento_sync=True).write({"magento_tracking_ref": text})

    def _magento_apply_magento_tracks(self, tracks):
        """Import the tracks of a Magento shipment onto the transfer (idempotent)."""
        self.ensure_one()
        numbers = split_tracking_numbers(*[
            str(track.get("track_number"))
            for track in tracks or []
            if isinstance(track, dict) and track.get("track_number")
        ])
        vals = {}
        if numbers:
            merged = split_tracking_numbers(self.magento_tracking_ref, *numbers)
            if ", ".join(merged) != (self.magento_tracking_ref or ""):
                vals["magento_tracking_ref"] = ", ".join(merged)
        delivery_fields = self._magento_delivery_fields()
        if numbers and "carrier_tracking_ref" in delivery_fields:
            current = split_tracking_numbers(self["carrier_tracking_ref"])
            merged = split_tracking_numbers(self["carrier_tracking_ref"], *numbers)
            if merged != current:
                vals["carrier_tracking_ref"] = ",".join(merged)
        if "carrier_id" in delivery_fields and not self["carrier_id"]:
            carrier = self._magento_order_carrier()
            if carrier:
                vals["carrier_id"] = carrier.id
        if vals:
            self.with_context(skip_magento_sync=True).write(vals)
        return bool(vals)

    def _magento_push_missing_tracks(self, shipment_data=None, api=None):
        """Add to the linked Magento shipment the tracking numbers it does not have yet.

        ``POST /V1/shipment/track`` once per missing number; tracks Magento already has
        are never posted again. Returns the number of tracks added. Raises ``UserError``.
        """
        self.ensure_one()
        order = self.sale_id
        if not self.magento_shipment_id or not order.magento_order_id or not order.magento_instance_id:
            return 0
        numbers = self._magento_tracking_numbers()
        if not numbers:
            return 0
        order._magento_lock_push_row(self)
        api = api or MagentoAPI(order.magento_instance_id.sudo())

        def known_numbers(data):
            return {
                normalize_tracking_number(track.get("track_number"))
                for track in (data or {}).get("tracks") or []
                if isinstance(track, dict) and track.get("track_number")
            }

        try:
            if not isinstance(shipment_data, dict) or "tracks" not in shipment_data:
                shipment_data = api.get_shipment(self.magento_shipment_id) or {}
        except requests.exceptions.RequestException as exc:
            order._magento_raise_http_error(exc)
        known = known_numbers(shipment_data)
        missing = [n for n in numbers if normalize_tracking_number(n) not in known]
        code, title = self._magento_track_carrier()
        added = []
        for number in missing:
            entity = {
                "order_id": _as_int(order.magento_order_id),
                "parent_id": _as_int(self.magento_shipment_id),
                "track_number": number,
                "title": title,
                "carrier_code": code,
            }
            try:
                api._post("/rest/V1/shipment/track", {"entity": entity})
            except requests.exceptions.RequestException as exc:
                if not order._magento_is_ambiguous_push_error(exc):
                    order._magento_raise_http_error(exc)
                # The POST may have been applied: look before raising (never post twice).
                try:
                    applied = normalize_tracking_number(number) in known_numbers(
                        api.get_shipment(self.magento_shipment_id))
                except requests.exceptions.RequestException:
                    applied = False
                if not applied:
                    raise UserError(_(
                        "Magento did not confirm tracking number %(number)s for shipment %(ship)s "
                        "(%(error)s). Retrying is safe.",
                        number=number,
                        ship=self.magento_shipment_increment_id or self.magento_shipment_id,
                        error=order._magento_http_error_message(exc),
                    )) from exc
            added.append(number)
        self._magento_record_tracks(numbers)
        if added:
            self.message_post(body=_(
                "Tracking added to Magento shipment %(ship)s: %(numbers)s",
                ship=self.magento_shipment_increment_id or self.magento_shipment_id,
                numbers=", ".join(added),
            ))
        return len(added)

    # ------------------------------------------------------------------
    # Shipment export
    # ------------------------------------------------------------------
    def _magento_prepare_shipment_payload(self):
        """``POST /V1/order/{id}/ship`` body: shipped quantities, tracks, MSI source."""
        self.ensure_one()
        items = self._magento_prepare_shipment_items()
        if not items:
            return False
        payload = {"items": items, "notify": False}
        tracks = self._magento_prepare_shipment_tracks()
        if tracks:
            payload["tracks"] = tracks
        source_code = self._magento_shipment_source_code()
        if source_code:
            payload["arguments"] = {"extension_attributes": {"source_code": source_code}}
        return payload

    def _magento_create_shipment(self):
        self.ensure_one()
        if self.magento_shipment_id or self.env.context.get("skip_magento_sync"):
            return False
        if self.picking_type_code != "outgoing" or self.state != "done":
            return False
        order = self.sale_id
        if not order or not order.magento_order_id or not order.magento_instance_id:
            return False
        if order.magento_status in ("complete", "closed", "canceled"):
            _logger.info(
                "Skipping Magento shipment creation for order %s (status=%s).",
                order.name,
                order.magento_status,
            )
            return False

        payload = self._magento_prepare_shipment_payload()
        if not payload:
            _logger.info(
                "Skipping Magento shipment for picking %s: no shippable Magento items.",
                self.name,
            )
            return False

        # Idempotent: links an existing matching Magento shipment instead of posting twice.
        shipment_id, shipment_data = order._magento_push_document("shipment", self, payload)
        if not shipment_id:
            return False
        linked_existing = bool(shipment_data)

        increment_id = ""
        if shipment_data:
            increment_id = shipment_data.get("increment_id") or ""
        else:
            try:
                shipment_data = MagentoAPI(order.magento_instance_id.sudo()).get_shipment(shipment_id)
                if isinstance(shipment_data, dict):
                    increment_id = shipment_data.get("increment_id") or ""
            except requests.exceptions.RequestException as exc:
                _logger.warning("Magento shipment %s could not be read back: %s", shipment_id, exc)

        vals = {
            "magento_shipment_id": str(shipment_id),
            "magento_shipment_increment_id": increment_id,
        }
        arguments = payload.get("arguments") or {}
        source_code = (arguments.get("extension_attributes") or {}).get("source_code")
        if source_code:
            vals["magento_source_code"] = source_code
        self.with_context(skip_magento_sync=True).write(vals)
        numbers = [track["track_number"] for track in payload.get("tracks") or []]
        if linked_existing and numbers:
            # The Magento shipment came from an earlier unconfirmed attempt (or was made
            # by hand): add only the tracks it lacks.
            self._magento_push_missing_tracks(shipment_data=shipment_data)
        elif numbers:
            self._magento_record_tracks(numbers)
        return True

    def _magento_sync_shipment_after_done(self):
        if self.env.context.get("skip_magento_sync"):
            return
        for picking in self:
            if picking.magento_shipment_id:
                continue
            if picking.picking_type_code != "outgoing" or picking.state != "done":
                continue
            order = picking.sale_id
            if not order or not order.magento_order_id:
                continue
            # User-triggered (delivery validation): never block the validation, report
            # the failure on the transfer instead.
            try:
                picking._magento_create_shipment()
            except (UserError, requests.exceptions.RequestException) as exc:
                picking.message_post(
                    body=_("Magento shipment sync failed: %s") % exc
                )

    def _action_done(self):
        res = super()._action_done()
        self._magento_sync_shipment_after_done()
        return res

    def write(self, vals):
        res = super().write(vals)
        if (
            not self.env.context.get("skip_magento_sync")
            and {"carrier_tracking_ref", "magento_tracking_ref"} & set(vals)
        ):
            # Tracking entered after the shipment reached Magento: add the missing tracks.
            # Never blocks the save; a failure is reported on the transfer.
            for picking in self.filtered(
                lambda p: p.magento_shipment_id and p.state == "done" and p.picking_type_code == "outgoing"
            ):
                try:
                    picking._magento_push_missing_tracks()
                except (UserError, requests.exceptions.RequestException) as exc:
                    picking.message_post(body=_("Magento tracking sync failed: %s") % exc)
        return res

    def action_push_magento_tracking(self):
        self.ensure_one()
        if not self.magento_shipment_id:
            raise UserError(_("Push the shipment to Magento first."))
        if not self._magento_tracking_numbers():
            raise UserError(_("Enter a tracking number first."))
        added = self._magento_push_missing_tracks()
        message = (
            _("%s tracking number(s) added to the Magento shipment.") % added
            if added else _("Magento already has every tracking number of this delivery.")
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento"),
                "message": message,
                "type": "success" if added else "info",
                "sticky": False,
            },
        }

    def action_push_shipment_to_magento(self):
        self.ensure_one()
        if self.picking_type_code != "outgoing":
            raise UserError(_("Only outgoing deliveries can be pushed to Magento."))
        if self.state != "done":
            raise UserError(_("Please validate the delivery before pushing to Magento."))
        if not self.magento_order_id:
            raise UserError(_("This delivery is not linked to a Magento order."))
        if self.magento_shipment_id:
            raise UserError(_("This delivery is already linked to a Magento shipment."))
        if not self._magento_create_shipment():
            raise UserError(_(
                "Nothing was pushed: the Magento order is already complete/closed or this "
                "delivery has no Magento order items."
            ))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento"),
                "message": _("Shipment pushed to Magento."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_open_magento_shipment(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Shipment",
            "res_model": "stock.picking",
            "view_mode": "form",
            "res_id": self.id,
            "target": "current",
        }
