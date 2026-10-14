"""Bulk / async transport (roadmap 1.9).

Mass writes (stock, mapped product fields...) go through Magento's asynchronous bulk API
(``/rest/<store>/async/bulk/V1/...``) in chunks; each accepted chunk becomes a
``magento.bulk.operation`` that a cron polls (``/V1/bulk/<uuid>/detailed-status``) and
maps item by item into the error center:

* 1 complete             -> issue resolved (``_record_success``)
* 2 failed, retriable    -> transient issue
* 3 failed / 5 rejected  -> permanent issue with Magento's ``result_message``
* 4 open                 -> polled again; after ``_max_poll_attempts`` polls the operation fails

Instances without the RabbitMQ / cron consumers that bulk needs keep ``use_async_bulk``
off: the same items are then sent one by one to the synchronous endpoints and their
results are recorded at once, through the same model.
"""
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.modules import module as odoo_module

from ..services.magento_api import MagentoAPI
from . import cron_compat

_logger = logging.getLogger(__name__)

STATUS_COMPLETE = MagentoAPI.BULK_STATUS_COMPLETE
STATUS_RETRIABLE = MagentoAPI.BULK_STATUS_RETRIABLY_FAILED
STATUS_FAILED = MagentoAPI.BULK_STATUS_FAILED
STATUS_OPEN = MagentoAPI.BULK_STATUS_OPEN
STATUS_REJECTED = MagentoAPI.BULK_STATUS_REJECTED
_FINAL_STATUSES = (STATUS_COMPLETE, STATUS_RETRIABLE, STATUS_FAILED, STATUS_REJECTED)

OPERATIONS = [
    ("product_update", "Product Update"),
    ("stock_update", "Stock Update"),
    ("price_update", "Price Update"),
    ("other", "Other"),
]
BULK_STATES = [
    ("accepted", "Accepted"),
    ("partial", "Partially Failed"),
    ("done", "Done"),
    ("failed", "Failed"),
    ("rejected", "Rejected"),
]
# Polled every 2 minutes: 30 polls ~ one hour before giving up.
_DEFAULT_MAX_POLLS = 30
_MAX_POLLS_PARAM = "sdlc_magento_connector.bulk_max_polls"
_MAX_JSON_CHARS = 200000
_POLL_BATCH = 200

def _dumps(value):
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= _MAX_JSON_CHARS else text[:_MAX_JSON_CHARS]


class MagentoBulkOperation(models.Model):
    _name = "magento.bulk.operation"
    _description = "Magento Bulk Operation"
    _order = "id desc"

    name = fields.Char(required=True, readonly=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True, ondelete="cascade",
                                  readonly=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True, readonly=True)
    operation = fields.Selection(OPERATIONS, required=True, default="other", readonly=True)
    transport = fields.Selection(
        [("async", "Async Bulk"), ("sync", "Synchronous")], required=True, default="async", readonly=True,
        help="Async Bulk: queued by Magento and polled. Synchronous: sent one by one, "
             "results recorded at once (instance option 'Use Async Bulk API' off).",
    )
    method = fields.Char(readonly=True)
    route = fields.Char(readonly=True)
    bulk_uuid = fields.Char(string="Bulk UUID", index=True, readonly=True, copy=False)
    state = fields.Selection(BULK_STATES, default="accepted", required=True, index=True, readonly=True)
    item_count = fields.Integer(string="Items", readonly=True)
    complete_count = fields.Integer(string="Complete", readonly=True)
    retriable_count = fields.Integer(string="Retriable", readonly=True)
    failed_count = fields.Integer(string="Failed", readonly=True)
    open_count = fields.Integer(string="Open", readonly=True)
    rejected_count = fields.Integer(string="Rejected", readonly=True)
    items_json = fields.Text(string="Items (JSON)", readonly=True,
                             help="Request bodies with their Odoo record, error-center reference and status.")
    result_json = fields.Text(string="Magento Result (JSON)", readonly=True)
    message = fields.Text(readonly=True)
    last_polled = fields.Datetime(readonly=True)
    poll_attempts = fields.Integer(readonly=True)
    date_done = fields.Datetime(string="Finished", readonly=True)
    items_pretty = fields.Text(string="Item Details", compute="_compute_pretty")
    result_pretty = fields.Text(string="Magento Result Details", compute="_compute_pretty")

    @api.depends("items_json", "result_json")
    def _compute_pretty(self):
        for op in self:
            for src, dst in (("items_json", "items_pretty"), ("result_json", "result_pretty")):
                text = op[src] or ""
                try:
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                except (TypeError, ValueError):
                    pass
                op[dst] = text or False

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    def _get_items(self):
        self.ensure_one()
        try:
            items = json.loads(self.items_json or "[]")
        except ValueError:
            items = []
        return items if isinstance(items, list) else []

    def _item_record(self, item):
        model, res_id = item.get("res_model"), item.get("res_id")
        if model and res_id and model in self.env:
            return self.env[model].sudo().browse(int(res_id)).exists()
        return None

    @api.model
    def _max_poll_attempts(self):
        value = self.env["ir.config_parameter"].sudo().get_param(_MAX_POLLS_PARAM)
        try:
            return max(1, int(value)) if value else _DEFAULT_MAX_POLLS
        except (TypeError, ValueError):
            return _DEFAULT_MAX_POLLS

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------
    @api.model
    def _submit(self, instance, operation, method, route, entries, *, store_code=None, description=None):
        """Send ``entries`` through ``MagentoAPI.async_bulk``; return the bulk operations.

        Each entry is a dict: ``payload`` (one request body), ``entity`` and ``ref`` (the
        error-center key: ``product``/``stock`` + SKU...), optional ``res_model``/``res_id``
        (the Odoo record). One operation is created per chunk. Synchronous results
        (``use_async_bulk`` off) and refused chunks are recorded immediately.
        """
        instance.ensure_one()
        ops = self.sudo().browse()
        entries = [dict(e) for e in entries or ()]
        if not entries:
            return ops
        api_client = MagentoAPI(instance.sudo())
        results = api_client.async_bulk(
            method, route, [e["payload"] for e in entries], store_code=store_code,
        )
        label = description or dict(OPERATIONS).get(operation, operation)
        offset = 0
        for result in results:
            size = len(result.get("items") or ())
            chunk = entries[offset:offset + size]
            offset += size
            items = [{
                "entity": e.get("entity") or "other",
                "ref": str(e.get("ref") or ""),
                "res_model": e.get("res_model") or False,
                "res_id": e.get("res_id") or False,
                "payload": e["payload"],
                "status": None,
                "message": "",
            } for e in chunk]
            op = ops.create({
                "name": "%s (%s)" % (label, instance.name),
                "instance_id": instance.id,
                "operation": operation,
                "transport": "sync" if result.get("sync") else "async",
                "method": (method or "").upper(),
                "route": route,
                "bulk_uuid": result.get("bulk_uuid") or False,
                "item_count": len(items),
                "open_count": len(items),
                "items_json": _dumps(items),
            })
            op._apply_submit_result(result)
            ops |= op
        return ops

    def _apply_submit_result(self, result):
        self.ensure_one()
        items = self._get_items()
        error = result.get("error")
        if error is not None:
            status = MagentoAPI.bulk_status_for_exception(error)
            message = MagentoAPI.bulk_error_message(error)
            self.message = message
            self._apply_item_results({idx: (status, message) for idx in range(len(items))})
            return
        if result.get("sync"):
            self.result_json = _dumps(result.get("request_items") or [])
            self._apply_item_results(self._results_by_index(result.get("request_items") or [], len(items)))
            return
        # Async: Magento accepted (or refused) each request into its queue.
        self.result_json = _dumps({k: result.get(k) for k in ("bulk_uuid", "request_items", "errors")})
        if not self.bulk_uuid:
            message = _("Magento returned no bulk UUID: the request was not queued.")
            self.message = message
            self._apply_item_results({idx: (STATUS_FAILED, message) for idx in range(len(items))})
            return
        results = {}
        request_items = result.get("request_items") or []
        for pos, req in enumerate(request_items):
            if not isinstance(req, dict):
                continue
            idx = self._as_int(req.get("id"), pos)
            if str(req.get("status") or "accepted").lower() == "accepted":
                results[idx] = (STATUS_OPEN, "")
            else:
                results[idx] = (STATUS_REJECTED, req.get("error_message") or req.get("result_message")
                                or _("Rejected by Magento when queued (%s).", req.get("status")))
        # Items Magento did not list stay open; the detailed status settles them.
        for idx in range(len(items)):
            results.setdefault(idx, (STATUS_OPEN, ""))
        self._apply_item_results(results)

    @staticmethod
    def _as_int(value, default=None):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _results_by_index(self, operations, count):
        """``{index: (status, message)}`` from Magento's operation list.

        Uses ``operation_key`` (2.4+) when every operation has one; otherwise the
        operations are matched by position, ordered by ``id`` (created in request order).
        """
        ops = [op for op in operations or () if isinstance(op, dict)]
        keys = [self._as_int(op.get("operation_key")) for op in ops]
        if ops and all(key is not None for key in keys):
            indexed = list(zip(keys, ops))
        else:
            ordered = sorted(enumerate(ops), key=lambda pair: (self._as_int(pair[1].get("id"), pair[0]), pair[0]))
            indexed = [(pos, op) for pos, (_orig, op) in enumerate(ordered)]
        results = {}
        for idx, op in indexed:
            if idx is None or not 0 <= idx < count:
                continue
            status = self._as_int(op.get("status"), STATUS_OPEN)
            if status not in _FINAL_STATUSES:
                status = STATUS_OPEN
            message = op.get("result_message") or ""
            if op.get("error_code") and status != STATUS_COMPLETE:
                message = "%s (error code %s)" % (message or _("Magento operation failed"), op["error_code"])
            results[idx] = (status, message)
        return results

    def _apply_item_results(self, results):
        """Record ``{index: (status, message)}`` once per item into the error center."""
        self.ensure_one()
        items = self._get_items()
        issues = self.env["magento.sync.issue"]
        instance = self.instance_id
        for idx, item in enumerate(items):
            if idx not in results or item.get("status") in _FINAL_STATUSES:
                continue
            status, message = results[idx]
            item["status"] = status
            item["message"] = message or ""
            if status == STATUS_OPEN:
                continue
            try:
                with self.env.cr.savepoint():
                    if status == STATUS_COMPLETE:
                        issues._record_success(instance, item["entity"], item["ref"])
                    else:
                        issues._record_failure(
                            instance, item["entity"], item["ref"],
                            message or _("Magento bulk operation failed (status %s).", status),
                            payload=item.get("payload"),
                            record=self._item_record(item),
                            severity="transient" if status == STATUS_RETRIABLE else "permanent",
                        )
            except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the batch
                _logger.warning("Magento bulk operation %s: could not record result of %s %s: %s",
                                self.id, item.get("entity"), item.get("ref"), exc)
        self.items_json = _dumps(items)
        self._update_counts()

    def _update_counts(self):
        self.ensure_one()
        statuses = [item.get("status") for item in self._get_items()]
        counts = {
            "complete_count": statuses.count(STATUS_COMPLETE),
            "retriable_count": statuses.count(STATUS_RETRIABLE),
            "failed_count": statuses.count(STATUS_FAILED),
            "rejected_count": statuses.count(STATUS_REJECTED),
        }
        counts["open_count"] = len(statuses) - sum(counts.values())
        vals = dict(counts, item_count=len(statuses))
        if counts["open_count"]:
            vals["state"] = "accepted"
        else:
            if counts["complete_count"] == len(statuses):
                vals["state"] = "done"
            elif counts["rejected_count"] == len(statuses):
                vals["state"] = "rejected"
            elif not counts["complete_count"]:
                vals["state"] = "failed"
            else:
                vals["state"] = "partial"
            if not self.date_done:
                vals["date_done"] = fields.Datetime.now()
        self.write(vals)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _bulk_can_commit(self):
        in_test_mode = getattr(self.env.registry, "in_test_mode", None)
        return bool(
            self.env.context.get("ir_cron_progress_id")
            and self.env.context.get("cron_id")
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    @api.model
    def _cron_poll_bulk_operations(self, limit=_POLL_BATCH):
        """Poll accepted async operations (cron, every 2 minutes)."""
        ops = self.sudo().search(
            [("state", "=", "accepted"), ("transport", "=", "async"), ("bulk_uuid", "!=", False)],
            order="id", limit=limit,
        )
        for op in ops:
            try:
                with self.env.cr.savepoint():
                    op._poll()
            except Exception:  # noqa: BLE001 - one broken operation must not stop the others
                _logger.exception("Magento bulk operation %s: poll failed", op.id)
            if self._bulk_can_commit():
                cron_compat.commit_progress(self.env, 1)
        return len(ops)

    def _poll(self):
        """Fetch the detailed status once and record the items Magento finished."""
        self.ensure_one()
        if self.state != "accepted" or not self.bulk_uuid:
            return False
        now = fields.Datetime.now()
        vals = {"poll_attempts": self.poll_attempts + 1, "last_polled": now}
        try:
            data = MagentoAPI(self.instance_id.sudo()).get_bulk_detailed_status(self.bulk_uuid)
        except Exception as exc:  # noqa: BLE001 - network, HTTP or JSON errors: poll again later
            vals["message"] = _("Status check failed: %s", MagentoAPI.bulk_error_message(exc))
            self.write(vals)
            self._give_up_if_exhausted()
            return False
        data = data if isinstance(data, dict) else {}
        vals.update(result_json=_dumps(data), message=False)
        self.write(vals)
        self._apply_item_results(self._results_by_index(data.get("operations_list"), self.item_count))
        self._give_up_if_exhausted()
        return True

    def _give_up_if_exhausted(self):
        self.ensure_one()
        max_polls = self._max_poll_attempts()
        if self.state != "accepted" or self.poll_attempts < max_polls:
            return False
        message = _(
            "Magento did not finish bulk operation %(uuid)s after %(polls)s status checks "
            "(%(open)s item(s) still open). Check that the RabbitMQ consumers "
            "(async.operations.all) run on the Magento server, or turn off "
            "'Use Async Bulk API' on the instance.",
            uuid=self.bulk_uuid, polls=self.poll_attempts, open=self.open_count,
        )
        self.write({"state": "failed", "message": message, "date_done": fields.Datetime.now()})
        try:
            with self.env.cr.savepoint():
                self.env["magento.sync.issue"]._record_failure(
                    self.instance_id, "other", "bulk-%s" % self.bulk_uuid, message,
                    payload={"bulk_uuid": self.bulk_uuid, "route": self.route, "items": self.item_count},
                    severity="permanent",
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Magento bulk operation %s: could not record the give-up issue: %s", self.id, exc)
        return True

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_poll_now(self):
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can poll bulk operations."))
        for op in self.sudo().filtered(lambda o: o.state == "accepted" and o.bulk_uuid):
            op._poll()
        return True

    def action_view_issues(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_sync_issue"
        )
        refs = {item.get("ref") for item in self._get_items() if item.get("ref")}
        if self.bulk_uuid:
            refs.add("bulk-%s" % self.bulk_uuid)
        action["domain"] = [("instance_id", "=", self.instance_id.id), ("magento_ref", "in", sorted(refs))]
        action["context"] = {"search_default_open": 1}
        return action


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    use_async_bulk = fields.Boolean(
        string="Use Async Bulk API", default=False,
        help="Send mass writes (stock, mapped fields) through Magento's asynchronous bulk API and "
             "poll their status. Requires RabbitMQ and the async.operations.all consumer on the "
             "Magento server. When off, the same items are sent one by one.",
    )
    bulk_chunk_size = fields.Integer(
        string="Bulk Chunk Size", default=MagentoAPI.DEFAULT_BULK_CHUNK_SIZE,
        help="Items per bulk request. Magento rejects larger arrays when Web API input limits are "
             "enabled (webapi/validation/complex_array_limit, default 20).",
    )
    stock_push_mode = fields.Selection(
        [("msi", "MSI (source items)"), ("legacy", "Legacy (stock item)")],
        string="Stock Push Mode", default="msi", required=True,
        help="MSI: POST /V1/inventory/source-items for the source below (sources are never "
             "unassigned). Legacy: PUT /V1/products/<sku>/stockItems/<itemId>, for Magento "
             "without the Inventory (MSI) modules.",
    )
    stock_location_id = fields.Many2one(
        "stock.location", string="Stock Location",
        default=lambda self: self._default_magento_stock_location(),
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Odoo location whose quantity (children included) is pushed to Magento when no "
             "Stock Locations are set. Empty: the stock location of the company's first warehouse.",
    )
    msi_source_code = fields.Char(string="MSI Source Code", default="default")
    bulk_operation_count = fields.Integer(compute="_compute_bulk_operation_count")

    @api.model
    def _default_magento_stock_location(self, company=None):
        company = company or self.env.company
        warehouse = self.env["stock.warehouse"].sudo().search([("company_id", "=", company.id)], limit=1)
        return warehouse.lot_stock_id

    @api.constrains("bulk_chunk_size")
    def _check_bulk_chunk_size(self):
        for instance in self:
            if not 1 <= instance.bulk_chunk_size <= MagentoAPI.MAX_BULK_CHUNK_SIZE:
                raise ValidationError(_("The bulk chunk size must be between 1 and %s.",
                                        MagentoAPI.MAX_BULK_CHUNK_SIZE))

    @api.constrains("stock_location_id")
    def _check_stock_location_id(self):
        for instance in self:
            if instance.stock_location_id and instance.stock_location_id.usage != "internal":
                raise ValidationError(_("The Magento stock location must be an internal location."))

    def _compute_bulk_operation_count(self):
        rows = self.env["magento.bulk.operation"]._read_group(
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"]
        )
        counts = {inst.id: n for inst, n in rows}
        for instance in self:
            instance.bulk_operation_count = counts.get(instance.id, 0)

    def action_view_bulk_operations(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_bulk_operation"
        )
        action["domain"] = [("instance_id", "=", self.id)]
        return action

    # ------------------------------------------------------------------
    # Bulk stock push: see magento_stock.py (roadmap 2.6)
    # ------------------------------------------------------------------
    def _magento_stock_location(self):
        self.ensure_one()
        return self.stock_location_id or self._default_magento_stock_location(self.company_id)

    # ------------------------------------------------------------------
    # Retention: finished bulk operations follow the instance's log retention.
    # ------------------------------------------------------------------
    @api.model
    def _cron_cleanup_logs(self):
        totals = super()._cron_cleanup_logs()
        now = fields.Datetime.now()
        deleted = 0
        for instance in self.sudo().with_context(active_test=False).search([]):
            cutoff = now - timedelta(days=instance.log_retention_days or 30)
            deleted += self._ops_delete_batched("magento.bulk.operation", [
                ("instance_id", "=", instance.id),
                ("state", "!=", "accepted"),
                ("date_done", "<", cutoff),
            ])
        if isinstance(totals, dict):
            totals["bulk_operations"] = deleted
        return totals


class MagentoProductMap(models.Model):
    _inherit = "magento.product.map"

    magento_stock_item_id = fields.Char(
        string="Magento Stock Item ID", readonly=True, copy=False,
        help="extension_attributes.stock_item.item_id read at import; used by the legacy stock push.",
    )

    def _apply_magento_data(self, data):
        values = super()._apply_magento_data(data)
        stock_item = ((data or {}).get("extension_attributes") or {}).get("stock_item") or {}
        if stock_item.get("item_id"):
            values["magento_stock_item_id"] = str(stock_item["item_id"])
        return values

