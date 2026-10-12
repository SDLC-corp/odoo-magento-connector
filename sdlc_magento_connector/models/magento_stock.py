"""Stock push (roadmap 2.6).

What is pushed
    For every mapped simple product: the Odoo quantity on the instance's basis (on hand,
    free = on hand minus reserved, or forecast), summed over the instance's internal
    locations, minus an optional safety buffer, never below 0. With MSI, each
    ``magento.stock.source.map`` row sends the quantity of its own locations to its
    Magento ``source_code`` (all sources of a SKU in one request); without rows, the
    instance locations go to ``msi_source_code``. Sources are never unassigned.
    Kits (``mrp`` installed, phantom BoM) push min(floor(component qty / qty per kit)).
    A product Magento lets customers backorder stays in stock (status 1) at 0.

When it is pushed
    * Event-driven: every quant change (move done, inventory adjustment, reservation and
      unreservation for the free basis) and every forecast change (move confirmed or
      cancelled, forecast basis) inserts a ``magento.stock.dirty`` row per instance that
      maps the product, then enqueues ONE debounced job per instance (``stock-delta-<id>``,
      run 60 s later on channel ``stock-<id>``). The job pushes the dirty products in bulk
      chunks and deletes exactly the rows it read. Rows are append-only: a change
      committed while the job runs keeps its own row and is pushed by the next job.
    * Nightly: an inactive-by-default cron queues the full push of every instance with
      ``stock_push_enabled``.
    Nothing is sent to Magento inside the picking transaction: marking is one INSERT and
    at most one job per instance and transaction.
"""
import math
from collections import defaultdict
from datetime import timedelta

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from .magento_bulk import STATUS_COMPLETE, STATUS_OPEN

QTY_BASIS = [
    ("on_hand", "On Hand"),
    ("free", "Free (On Hand - Reserved)"),
    ("forecast", "Forecast"),
]
_BASIS_FIELD = {"on_hand": "qty_available", "free": "free_qty", "forecast": "virtual_available"}
# Which instance bases a stock event changes.
_EVENT_BASES = {
    "quantity": ["on_hand", "free", "forecast"],
    "reserved": ["free"],
    "forecast": ["forecast"],
}
BACKORDERS = [
    ("0", "No Backorders"),
    ("1", "Allow Qty Below 0"),
    ("2", "Allow Qty Below 0 and Notify Customer"),
]
# Stock push routes. MSI: SourceItemsSaveInterface::execute(sourceItems[]).
# Legacy: PUT /V1/products/:productSku/stockItems/:itemId
# (StockRegistryInterface::updateStockItemBySku); route parameters become ``byXxx``.
_STOCK_ROUTES = {
    "msi": ("post", "inventory/source-items"),
    "legacy": ("put", "products/byProductSku/stockItems/byItemId"),
}
STOCK_DELTA_DELAY = 60  # seconds: changes within this window share one push
_DELTA_ROW_LIMIT = 5000  # dirty rows per delta job; the rest goes to a follow-up job
_DIRTY_SEEN_KEY = "sdlc_magento_connector.stock_dirty_seen"
_DELTA_QUEUED_KEY = "sdlc_magento_connector.stock_delta_queued"
_SKIP_PUSH_CTX = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}


class MagentoStockSourceMap(models.Model):
    _name = "magento.stock.source.map"
    _description = "Magento MSI Source Mapping"
    _order = "instance_id, sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    source_code = fields.Char(
        string="Magento Source Code", required=True,
        help="Code of the Magento inventory source (Stores > Inventory > Sources).",
    )
    location_ids = fields.Many2many(
        "stock.location", "magento_stock_source_map_location_rel", "source_map_id", "location_id",
        string="Odoo Locations", required=True,
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Internal locations (children included) whose quantities are summed for this source.",
    )

    _instance_source_uniq = models.Constraint(
        "UNIQUE(instance_id, source_code)",
        "This Magento source is already mapped for this instance.",
    )

    @api.depends("source_code", "location_ids")
    def _compute_display_name(self):
        for row in self:
            row.display_name = "%s → %s" % (
                row.source_code or "?", ", ".join(row.location_ids.mapped("complete_name")) or "-")

    @api.constrains("source_code", "location_ids")
    def _check_source(self):
        for row in self:
            code = (row.source_code or "").strip()
            if not code or code != row.source_code or " " in code:
                raise ValidationError(_("A Magento source code cannot be empty or contain spaces."))
            if not row.location_ids:
                raise ValidationError(_("Map Magento source %s to at least one Odoo location.", code))
            if row.location_ids.filtered(lambda loc: loc.usage != "internal"):
                raise ValidationError(_("Magento source %s: only internal locations can be mapped.", code))


class MagentoStockDirty(models.Model):
    """Append-only queue of (instance, product) whose stock changed since the last push."""
    _name = "magento.stock.dirty"
    _description = "Magento Stock Change"
    _order = "id"
    _log_access = False

    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade",
                                  readonly=True)
    product_id = fields.Many2one("product.product", required=True, index=True, ondelete="cascade",
                                 readonly=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)

    _instance_id_idx = models.Index("(instance_id, id)")

    @api.model
    def _magento_mark(self, product_ids, event="quantity"):
        """Flag ``product_ids`` for every instance that maps them and pushes on a basis
        ``event`` changes; enqueue one debounced job per such instance. Returns the
        instance ids flagged. One INSERT; called from inside stock operations."""
        if self.env.context.get("magento_skip_stock_dirty"):
            return set()
        data = self.env.cr.precommit.data
        seen = data.setdefault(_DIRTY_SEEN_KEY, set())
        todo = sorted({int(pid) for pid in product_ids if pid} - {pid for pid, ev in seen if ev == event})
        if not todo:
            return set()
        seen.update((pid, event) for pid in todo)
        # The query reads these columns: write pending ORM changes first (no-op when clean).
        self.env["magento.instance"].flush_model(["stock_push_enabled", "active", "stock_qty_basis"])
        self.env["magento.product.map"].flush_model(["instance_id", "odoo_product_id", "type_id", "sku"])
        changed = SQL("SELECT unnest(%s::int[]) AS product_id", todo)
        if "mrp.bom" in self.env:
            self.env["mrp.bom"].flush_model(["product_id", "product_tmpl_id", "type", "active"])
            self.env["mrp.bom.line"].flush_model(["product_id", "bom_id"])
            # A component change changes the availability of the kits (phantom BoMs) using it.
            changed = SQL(
                """%s
                 UNION
                SELECT pp.id
                  FROM mrp_bom_line l
                  JOIN mrp_bom b ON b.id = l.bom_id AND b.type = 'phantom' AND b.active
                  JOIN product_product pp ON (pp.id = b.product_id
                       OR (b.product_id IS NULL AND pp.product_tmpl_id = b.product_tmpl_id))
                 WHERE l.product_id = ANY(%s::int[])""",
                changed, todo,
            )
        self.env.cr.execute(SQL(
            """
            WITH changed AS (%s)
            INSERT INTO magento_stock_dirty (instance_id, product_id, date)
            SELECT DISTINCT m.instance_id, m.odoo_product_id, %s
              FROM magento_product_map m
              JOIN changed c ON c.product_id = m.odoo_product_id
              JOIN magento_instance i ON i.id = m.instance_id
             WHERE i.stock_push_enabled
               AND i.active
               AND i.stock_qty_basis = ANY(%s)
               AND m.type_id = 'simple'
               AND COALESCE(m.sku, '') != ''
            RETURNING instance_id
            """,
            changed, fields.Datetime.now(), _EVENT_BASES.get(event, _EVENT_BASES["quantity"]),
        ))
        instance_ids = {row[0] for row in self.env.cr.fetchall()}
        if instance_ids:
            queued = data.setdefault(_DELTA_QUEUED_KEY, set())
            for instance in self.env["magento.instance"].sudo().browse(sorted(instance_ids - queued)):
                queued.add(instance.id)
                instance._magento_enqueue_stock_delta()
        return instance_ids


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    stock_push_enabled = fields.Boolean(
        string="Automatic Stock Push", default=False,
        help="Push the stock of changed products about a minute after each stock change "
             "(deliveries, receipts, inventory adjustments, reservations) and allow the "
             "nightly full reconcile. Off: stock is only pushed with 'Push Stock Now'.",
    )
    stock_qty_basis = fields.Selection(
        QTY_BASIS, string="Quantity Basis", default="free", required=True,
        help="On Hand: physical quantity. Free: on hand minus quantities reserved for "
             "transfers (avoids selling stock already promised). Forecast: on hand plus "
             "incoming minus outgoing confirmed moves.",
    )
    stock_location_ids = fields.Many2many(
        "stock.location", "magento_instance_stock_location_rel", "instance_id", "location_id",
        string="Stock Locations",
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Internal locations (children included) whose quantities are summed and pushed. "
             "Empty: the single Stock Location above.",
    )
    stock_safety_buffer = fields.Float(
        string="Safety Buffer", default=0.0, digits="Product Unit",
        help="Units held back from Magento: the pushed quantity is the Odoo quantity minus "
             "this buffer, never below 0.",
    )
    stock_source_map_ids = fields.One2many(
        "magento.stock.source.map", "instance_id", string="MSI Source Mapping",
        help="One Magento source per row, fed by its own Odoo locations. Empty: the "
             "locations above go to the single MSI source code.",
    )
    stock_map_count = fields.Integer(compute="_compute_stock_counts")
    stock_dirty_count = fields.Integer(string="Changes Waiting", compute="_compute_stock_counts")

    @api.constrains("stock_safety_buffer")
    def _check_stock_safety_buffer(self):
        for instance in self:
            if instance.stock_safety_buffer < 0:
                raise ValidationError(_("The stock safety buffer cannot be negative."))

    @api.constrains("stock_location_ids")
    def _check_stock_location_ids(self):
        for instance in self:
            if instance.stock_location_ids.filtered(lambda loc: loc.usage != "internal"):
                raise ValidationError(_("Magento stock locations must be internal locations."))

    def _compute_stock_counts(self):
        maps = self.env["magento.product.map"]._read_group(
            self._magento_stock_map_domain(), ["instance_id"], ["__count"])
        map_counts = {inst.id: n for inst, n in maps}
        dirty_counts = {}
        if self.ids:
            self.env["magento.stock.dirty"].flush_model()
            self.env.cr.execute(SQL(
                "SELECT instance_id, COUNT(DISTINCT product_id) FROM magento_stock_dirty "
                "WHERE instance_id = ANY(%s) GROUP BY instance_id", self.ids))
            dirty_counts = dict(self.env.cr.fetchall())
        for instance in self:
            instance.stock_map_count = map_counts.get(instance.id, 0)
            instance.stock_dirty_count = dirty_counts.get(instance.id, 0)

    def action_view_stock_maps(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_stock_push_maps")
        action["domain"] = self._magento_stock_map_domain(instance=self)
        action["context"] = {"default_instance_id": self.id}
        return action

    # ------------------------------------------------------------------
    # Scope: which maps, which locations
    # ------------------------------------------------------------------
    def _magento_stock_map_domain(self, instance=None):
        instances = instance or self
        return [
            ("instance_id", "in", instances.ids),
            ("sku", "!=", False),
            ("sku", "!=", ""),
            ("type_id", "=", "simple"),
            ("odoo_product_id", "!=", False),
        ]

    def _magento_stock_maps(self, product_ids=None):
        """Maps whose stock is pushed: simple products with a SKU and an Odoo product."""
        self.ensure_one()
        domain = self._magento_stock_map_domain(instance=self)
        if product_ids is not None:
            domain.append(("odoo_product_id", "in", list(product_ids)))
        return self.env["magento.product.map"].sudo().search(domain, order="id")

    def _magento_stock_location_sets(self):
        """``[(source_code, locations)]``: one entry per MSI source (``False`` in legacy mode)."""
        self.ensure_one()
        default = self.stock_location_ids or self._magento_stock_location()
        if self.stock_push_mode == "legacy":
            return [(False, default)]
        sources = self.stock_source_map_ids.filtered("active")
        if sources:
            return [(src.source_code, src.location_ids) for src in sources]
        return [(self.msi_source_code or "default", default)]

    # ------------------------------------------------------------------
    # Quantities
    # ------------------------------------------------------------------
    def _magento_kit_components(self, products):
        """``{kit product id: [(component, qty per kit)]}`` for products with a phantom BoM
        (``mrp`` installed); storable components only, quantities in the component UoM."""
        self.ensure_one()
        if "mrp.bom" not in self.env or not products:
            return {}
        boms = self.env["mrp.bom"].sudo()._bom_find(
            products, company_id=self.company_id.id, bom_type="phantom")
        result = {}
        for product in products:
            bom = boms.get(product)
            if not bom:
                continue
            _boms, lines = bom.explode(product, 1)
            needed = defaultdict(float)
            for line, line_data in lines:
                component = line.product_id
                if not component.is_storable or line.product_uom_id.is_zero(line_data["qty"]):
                    continue
                needed[component] += line.product_uom_id._compute_quantity(
                    line_data["qty"], component.uom_id, round=False, raise_if_failure=False)
            per_kit = [(comp, qty / (bom.product_qty or 1.0)) for comp, qty in needed.items() if qty > 0]
            if per_kit:
                result[product.id] = per_kit
        return result

    @staticmethod
    def _magento_kit_quantity(components, qty_by_product):
        """min over components of floor(component qty / qty per kit)."""
        ratios = [qty_by_product.get(comp.id, 0.0) / per_kit for comp, per_kit in components]
        return float(math.floor(min(ratios) + 1e-9)) if ratios else 0.0

    def _magento_stock_quantities(self, maps):
        """``{map id: {source_code: qty}}`` (``False`` key in legacy mode).

        Storable products and kits only: a consumable or service has no stock, and
        pushing 0 would mark it out of stock. Quantity = basis quantity summed over the
        locations, minus the safety buffer, never below 0.
        """
        self.ensure_one()
        location_sets = self._magento_stock_location_sets()
        if not location_sets or any(not locations for _code, locations in location_sets):
            raise UserError(_("Set a stock location on Magento instance %s first.", self.name))
        products = maps.odoo_product_id.with_company(self.company_id)
        kits = self._magento_kit_components(products)
        components = self.env["product.product"].browse(
            {comp.id for parts in kits.values() for comp, _qty in parts})
        field_name = _BASIS_FIELD.get(self.stock_qty_basis or "free", "free_qty")
        buffer = max(self.stock_safety_buffer or 0.0, 0.0)
        per_source = []
        for code, locations in location_sets:
            scoped = (products | components).with_company(self.company_id).with_context(
                location=locations.ids)
            per_source.append((code, {p.id: p[field_name] for p in scoped if p.is_storable}))
        result = {}
        for product_map in maps:
            product_id = product_map.odoo_product_id.id
            quantities = {}
            for code, qty_by_product in per_source:
                if product_id in kits:
                    qty = self._magento_kit_quantity(kits[product_id], qty_by_product)
                elif product_id in qty_by_product:
                    qty = qty_by_product[product_id]
                else:
                    break
                quantities[code] = max(float(qty or 0.0) - buffer, 0.0)
            else:
                result[product_map.id] = quantities
        return result

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------
    @staticmethod
    def _magento_stock_status(product_map, qty):
        """1 (in stock) when there is stock or Magento accepts backorders for the product."""
        backorders = (product_map.magento_backorders or "0") != "0"
        return 1 if (qty > 0 or backorders) else 0

    def _magento_stock_entry(self, product_map, quantities):
        """One bulk item for ``{source_code: qty}`` (a bare number: the default source).

        MSI: ``{"sourceItems": [{sku, source_code, quantity, status}, ...]}`` with every
        source of the SKU in one request, so each Magento operation maps to one SKU.
        Legacy: ``{"productSku", "itemId", "stockItem": {qty, is_in_stock}}``; Magento reads
        the SKU and stock item from the body (``itemId`` is only used for the synchronous
        URL; the stock item id stored at import, else 1)."""
        self.ensure_one()
        if not isinstance(quantities, dict):
            code = False if self.stock_push_mode == "legacy" else (self.msi_source_code or "default")
            quantities = {code: quantities}
        if self.stock_push_mode == "legacy":
            qty = float(sum(quantities.values()) or 0.0)
            payload = {
                "productSku": product_map.sku,
                "itemId": int(product_map.magento_stock_item_id or 1),
                "stockItem": {"qty": qty, "is_in_stock": bool(self._magento_stock_status(product_map, qty))},
            }
        else:
            payload = {"sourceItems": [{
                "sku": product_map.sku,
                "source_code": code or self.msi_source_code or "default",
                "quantity": float(qty or 0.0),
                "status": self._magento_stock_status(product_map, float(qty or 0.0)),
            } for code, qty in quantities.items()]}
        return {
            "payload": payload,
            "entity": "stock",
            "ref": product_map.sku,
            "res_model": product_map._name,
            "res_id": product_map.id,
        }

    @staticmethod
    def _magento_payload_qty(payload):
        payload = payload or {}
        if "sourceItems" in payload:
            return float(sum(float(item.get("quantity") or 0.0) for item in payload["sourceItems"] or ()))
        return float(((payload.get("stockItem") or {}).get("qty")) or 0.0)

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------
    def _magento_push_stock_maps(self, maps, description=None):
        """Push the stock of ``maps`` through the bulk API (or its synchronous fallback)."""
        self.ensure_one()
        quantities = self._magento_stock_quantities(maps) if maps else {}
        entries = [self._magento_stock_entry(m, quantities[m.id]) for m in maps if m.id in quantities]
        method, route = _STOCK_ROUTES[self.stock_push_mode or "msi"]
        ops = self.env["magento.bulk.operation"].sudo()._submit(
            self, "stock_update", method, route, entries,
            description=description or _("Stock push"),
        )
        self._magento_record_stock_push(ops)
        return {
            "items": len(entries),
            "operations": len(ops),
            "complete": sum(ops.mapped("complete_count")),
            "open": sum(ops.mapped("open_count")),
            "failed": sum(ops.mapped(lambda o: o.failed_count + o.retriable_count + o.rejected_count)),
        }

    def _magento_record_stock_push(self, ops):
        """Stamp ``last_pushed_qty`` / ``last_stock_push_at`` on the maps Magento accepted
        (complete, or queued for an async bulk). Failures are in the error center."""
        by_qty = defaultdict(list)
        for op in ops:
            for item in op._get_items():
                if item.get("res_model") != "magento.product.map" or not item.get("res_id"):
                    continue
                if item.get("status") not in (STATUS_COMPLETE, STATUS_OPEN):
                    continue
                by_qty[self._magento_payload_qty(item.get("payload"))].append(int(item["res_id"]))
        if not by_qty:
            return
        now = fields.Datetime.now()
        maps = self.env["magento.product.map"].sudo().with_context(**_SKIP_PUSH_CTX)
        for qty, map_ids in by_qty.items():
            maps.browse(map_ids).exists().write({"last_pushed_qty": qty, "last_stock_push_at": now})

    def _magento_dirty_rows(self, limit=None):
        """``[(row id, product id)]`` waiting for this instance, oldest first."""
        self.ensure_one()
        self.env["magento.stock.dirty"].flush_model()
        query = SQL("SELECT id, product_id FROM magento_stock_dirty WHERE instance_id = %s ORDER BY id",
                    self.id)
        if limit:
            query = SQL("%s LIMIT %s", query, limit)
        self.env.cr.execute(query)
        return self.env.cr.fetchall()

    def _magento_clear_dirty_rows(self, row_ids):
        if row_ids:
            self.env.cr.execute(SQL("DELETE FROM magento_stock_dirty WHERE id = ANY(%s)", list(row_ids)))
            self.env["magento.stock.dirty"].invalidate_model()
            # A later change in this transaction must flag its product again.
            self.env.cr.precommit.data.pop(_DIRTY_SEEN_KEY, None)

    def _magento_push_stock_bulk(self):
        """Push the stock of every mapped simple product (job body; nightly reconcile).
        Changes flagged before the quantities were read are covered by this push."""
        self.ensure_one()
        instance = self.sudo()
        rows = instance._magento_dirty_rows()
        result = instance._magento_push_stock_maps(instance._magento_stock_maps())
        instance._magento_clear_dirty_rows([row_id for row_id, _pid in rows])
        return result

    def _magento_push_stock_delta(self):
        """Push the products whose stock changed (debounced job body), then clear their flags."""
        self.ensure_one()
        instance = self.sudo()
        rows = instance._magento_dirty_rows(limit=_DELTA_ROW_LIMIT)
        if not rows:
            return {"items": 0, "products": 0}
        product_ids = {pid for _row_id, pid in rows}
        if instance.stock_push_enabled and instance.active:
            result = instance._magento_push_stock_maps(
                instance._magento_stock_maps(product_ids=product_ids),
                description=_("Stock push (changes)"),
            )
        else:
            result = {"items": 0, "skipped": "automatic stock push is off"}
        instance._magento_clear_dirty_rows([row_id for row_id, _pid in rows])
        result["products"] = len(product_ids)
        if len(rows) >= _DELTA_ROW_LIMIT:
            instance._magento_enqueue_stock_delta(delay=0)
        return result

    def _magento_enqueue_stock_delta(self, delay=STOCK_DELTA_DELAY):
        """Queue (or join) the debounced delta push of this instance.

        Two identity keys alternate so that a change made while a delta job runs still
        gets a pending job (the identity of a started job blocks a new one); both run on
        the instance's stock channel, one at a time.
        """
        self.ensure_one()
        keys = ("stock-delta-%s" % self.id, "stock-delta-%s-next" % self.id)
        Job = self.env["magento.job"].with_user(SUPERUSER_ID)
        active = Job.sudo().search([("identity_key", "in", keys), ("state", "in", ("pending", "started"))])
        pending = active.filtered(lambda job: job.state == "pending")
        if pending:
            return pending[:1]
        busy = set(active.mapped("identity_key"))
        free_keys = [key for key in keys if key not in busy]
        if not free_keys:
            return active[:1]
        eta = fields.Datetime.now() + timedelta(seconds=delay)
        job = Job.enqueue(
            self, "_magento_push_stock_delta",
            instance=self,
            channel="stock-%s" % self.id,
            identity_key=free_keys[0],
            eta=eta,
            description=_("Push changed stock to Magento (%s)", self.name),
        )
        cron = self.sudo().env.ref("sdlc_magento_connector.ir_cron_magento_job_runner",
                                   raise_if_not_found=False)
        if delay and cron and cron.active:
            cron._trigger(eta)
        return job

    def action_push_stock_bulk(self):
        """Queue the full stock push (one pending job per instance)."""
        self.ensure_one()
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can push stock to Magento."))
        location_sets = self._magento_stock_location_sets()
        if not location_sets or any(not locations for _code, locations in location_sets):
            raise UserError(_("Set a stock location on Magento instance %s first.", self.name))
        job = self.env["magento.job"].enqueue(
            self, "_magento_push_stock_bulk",
            instance=self,
            channel="stock-%s" % self.id,
            identity_key="stock-push-%s" % self.id,
            description=_("Push stock to Magento (%s)", self.name),
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stock push queued"),
                "message": _("Job %(job)s will push the stock of %(name)s to Magento.",
                             job=job.display_name, name=self.name),
                "type": "info",
                "sticky": False,
            },
        }

    @api.model
    def _cron_magento_stock_reconcile(self):
        """Nightly: queue the full stock push of every instance with automatic stock push on."""
        jobs = self.env["magento.job"]
        for instance in self.sudo().search([("stock_push_enabled", "=", True)]):
            jobs |= jobs.sudo().enqueue(
                instance, "_magento_push_stock_bulk",
                instance=instance,
                channel="stock-%s" % instance.id,
                identity_key="stock-push-%s" % instance.id,
                description=_("Nightly stock reconcile (%s)", instance.name),
            )
        return jobs


class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    magento_backorders = fields.Selection(
        BACKORDERS, string="Magento Backorders", readonly=True, copy=False,
        help="extension_attributes.stock_item.backorders read at import. When backorders are "
             "allowed the stock push keeps the product in stock at quantity 0.",
    )
    last_pushed_qty = fields.Float(string="Last Pushed Qty", readonly=True, copy=False,
                                   digits="Product Unit")
    last_stock_push_at = fields.Datetime(string="Last Stock Push", readonly=True, copy=False)
    stock_push_pending = fields.Boolean(
        string="Stock Change Waiting", compute="_compute_stock_push_pending",
        search="_search_stock_push_pending",
        help="The Odoo stock changed since the last push; the next delta push sends it.",
    )

    def _compute_stock_push_pending(self):
        pending = set()
        if self.ids:
            self.env["magento.stock.dirty"].flush_model()
            self.env.cr.execute(SQL(
                """SELECT DISTINCT m.id FROM magento_product_map m
                     JOIN magento_stock_dirty d
                       ON d.instance_id = m.instance_id AND d.product_id = m.odoo_product_id
                    WHERE m.id = ANY(%s)""", self.ids))
            pending = {row[0] for row in self.env.cr.fetchall()}
        for product_map in self:
            product_map.stock_push_pending = product_map.id in pending

    def _search_stock_push_pending(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            return NotImplemented
        self.env["magento.stock.dirty"].flush_model()
        self.env.cr.execute(SQL(
            """SELECT DISTINCT m.id FROM magento_product_map m
                 JOIN magento_stock_dirty d
                   ON d.instance_id = m.instance_id AND d.product_id = m.odoo_product_id"""))
        ids = [row[0] for row in self.env.cr.fetchall()]
        positive = (operator == "=") == value
        return [("id", "in" if positive else "not in", ids)]

    def _apply_magento_data(self, data):
        values = super()._apply_magento_data(data)
        stock_item = ((data or {}).get("extension_attributes") or {}).get("stock_item") or {}
        backorders = stock_item.get("backorders")
        if backorders is not None and str(backorders) in dict(BACKORDERS):
            values["magento_backorders"] = str(backorders)
        return values

    def _build_magento_payload(self):
        """With automatic stock push on, the stock push owns Magento's quantity: a product
        update must not send back the quantity last imported from Magento."""
        payload = super()._build_magento_payload()
        if self.instance_id.stock_push_enabled:
            extension = (payload.get("product") or {}).get("extension_attributes")
            if isinstance(extension, dict):
                extension.pop("stock_item", None)
                if not extension:
                    payload["product"].pop("extension_attributes", None)
        return payload


class StockQuant(models.Model):
    _inherit = "stock.quant"

    @api.model
    def _update_available_quantity(self, product_id, location_id, *args, **kwargs):
        result = super()._update_available_quantity(product_id, location_id, *args, **kwargs)
        quantity = kwargs.get("quantity", args[0] if args else False)
        if product_id and location_id and location_id.usage == "internal":
            self.env["magento.stock.dirty"]._magento_mark(
                product_id.ids, "quantity" if quantity else "reserved")
        return result


class StockMove(models.Model):
    _inherit = "stock.move"

    def _action_confirm(self, *args, **kwargs):
        product_ids = self.product_id.ids  # read first: merged moves are deleted
        moves = super()._action_confirm(*args, **kwargs)
        self.env["magento.stock.dirty"]._magento_mark(product_ids + moves.product_id.ids, "forecast")
        return moves

    def _action_cancel(self):
        result = super()._action_cancel()
        self.env["magento.stock.dirty"]._magento_mark(self.product_id.ids, "forecast")
        return result
