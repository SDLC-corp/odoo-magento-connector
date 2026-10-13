import ast
import copy
import datetime
import json
import logging
from pprint import pformat

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.models import BaseModel
from odoo.tools.safe_eval import safe_eval, test_python_expr

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transform expressions (roadmap 3.8)
#
# A mapping may carry a Python *expression* evaluated with Odoo's ``safe_eval`` on
# every value it maps. The namespace is deliberately tiny: ``value`` (the value being
# mapped), ``record`` (a plain dict: the Magento JSON inbound, the Odoo record's simple
# field values outbound; never a recordset, so no way to reach ``env``) and the helpers
# below. ``env``, ``self``, models and modules are not exposed.
# ---------------------------------------------------------------------------
def _tf_text(func):
    def helper(value, *args):
        if value is None or value is False:
            return value
        return func(str(value), *args)
    return helper


def _tf_default(value, fallback):
    """``value`` unless it is empty (None, False, "" or blank text), else ``fallback``."""
    if value is None or value is False or (isinstance(value, str) and not value.strip()):
        return fallback
    return value


def _tf_replace(value, old, new):
    if value is None or value is False:
        return value
    return str(value).replace(str(old), str(new))


TRANSFORM_HELPERS = {
    "upper": _tf_text(str.upper),
    "lower": _tf_text(str.lower),
    "strip": _tf_text(str.strip),
    "replace": _tf_replace,
    "default": _tf_default,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
}
TRANSFORM_NAMES = frozenset({"value", "record", *TRANSFORM_HELPERS})
# Builtins safe_eval provides that a transform may also use.
_TRANSFORM_BUILTINS = frozenset({
    "True", "False", "None", "bool", "len", "min", "max", "abs", "sum", "sorted", "list",
    "tuple", "dict", "set", "any", "all", "isinstance", "range", "zip", "enumerate",
})
_TRANSFORM_MAX_LENGTH = 2000


class TransformError(Exception):
    """A transform expression failed on a value (message is user-facing)."""


def transform_expression_error(expr):
    """User-facing reason why ``expr`` is not a valid transform, or None."""
    expr = (expr or "").strip()
    if not expr:
        return None
    if len(expr) > _TRANSFORM_MAX_LENGTH:
        return "A transform is limited to %s characters." % _TRANSFORM_MAX_LENGTH
    error = test_python_expr(expr, mode="eval")
    if error:
        return "Invalid transform expression: %s" % error
    tree = ast.parse(expr, mode="eval")
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.Lambda):
            bound.update(arg.arg for arg in node.args.args)
    allowed = TRANSFORM_NAMES | _TRANSFORM_BUILTINS | bound
    unknown = sorted({
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in allowed
    })
    if unknown:
        return "Unknown name(s) in transform: %s. Use value, record and the helpers %s." % (
            ", ".join(unknown), ", ".join(sorted(TRANSFORM_HELPERS)),
        )
    return None


def evaluate_transform(expr, value, record=None):
    """Evaluate a (valid) transform; raises ``TransformError`` with a clear message."""
    namespace = dict(TRANSFORM_HELPERS)
    namespace["value"] = copy.deepcopy(value)
    namespace["record"] = copy.deepcopy(record) if isinstance(record, dict) else {}
    try:
        return safe_eval(expr.strip(), namespace, mode="eval")
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never raised as is
        # safe_eval wraps most errors in "ValueError: <original> while evaluating ...":
        # report the original one.
        original = exc.__context__ if type(exc) is ValueError and exc.__context__ else exc
        text = " ".join(("%s: %s" % (type(original).__name__, original)).split())
        raise TransformError(text[:300]) from exc


class MagentoAvailableField(models.Model):
    _name = "magento.available.field"
    _description = "Available Magento Field"

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    instance_id = fields.Many2one(
        "magento.instance",
        required=True,
        ondelete="cascade",
    )
    mapping_type = fields.Selection(
        [
            ("product", "Product"),
            ("customer", "Customer"),
            ("category", "Category"),
        ],
        required=True,
        default=lambda self: self.env.context.get("default_mapping_type", "product"),
    )

    _uniq_magento_field = models.Constraint(
        "unique(code, instance_id, mapping_type)",
        "This Magento field already exists!",
    )


class MagentoFieldMapping(models.Model):
    _name = "magento.field.mapping"
    _description = "Magento Field Mapping"
    _order = "mapping_type, id"

    _MAPPING_MODEL_MAP = {
        "product": "magento.product.map",
        "customer": "magento.customer",
        "category": "magento.category",
    }

    mapping_type = fields.Selection(
        [
            ("product", "Product"),
            ("customer", "Customer"),
            ("category", "Category"),
        ],
        required=True,
        default=lambda self: self.env.context.get("default_mapping_type", "product"),
    )
    instance_id = fields.Many2one(
        "magento.instance",
        required=True,
        ondelete="cascade",
    )

    odoo_model = fields.Char(
        string="Odoo Model Name",
        compute="_compute_odoo_model",
    )
    odoo_model_id = fields.Many2one(
        "ir.model",
        compute="_compute_odoo_model",
        readonly=True,
    )
    odoo_field = fields.Many2one(
        "ir.model.fields",
        string="Odoo Field",
        ondelete="set null",
        domain="[('model_id', '=', odoo_model_id)]",
    )
    magento_field_id = fields.Many2one(
        "magento.available.field",
        string="Magento Field",
        required=True,
        domain="[('instance_id', '=', instance_id), ('mapping_type', '=', mapping_type)]",
    )
    active = fields.Boolean(default=True)

    # ---- Transform (roadmap 3.8) ----
    transform_expr = fields.Text(
        string="Transform",
        help="Optional Python expression applied to the mapped value, e.g. "
             "upper(strip(value)), round(float(value) * 1.2, 2) or default(value, 'n/a').\n"
             "Available: value, record (Magento JSON on import, the Odoo record's values on "
             "export) and the helpers upper, lower, strip, replace(value, old, new), "
             "default(value, fallback), round, int, float, str. A failing transform leaves "
             "the field unchanged and opens an Error Center issue.",
    )
    transform_direction = fields.Selection(
        [
            ("both", "Both directions"),
            ("inbound", "Magento → Odoo"),
            ("outbound", "Odoo → Magento"),
        ],
        string="Transform Applies To",
        default="both",
        required=True,
    )
    transform_test_input = fields.Text(string="Sample Input", readonly=True, copy=False)
    transform_test_output = fields.Text(string="Sample Output", readonly=True, copy=False)
    transform_test_status = fields.Selection(
        [("ok", "Passed"), ("error", "Failed")], string="Live Test", readonly=True, copy=False,
    )
    transform_test_source = fields.Char(string="Sample From", readonly=True, copy=False)

    _uniq_magento_mapping = models.Constraint(
        "unique(mapping_type, instance_id, odoo_field, magento_field_id)",
        "Duplicate mapping is not allowed.",
    )

    @api.depends("mapping_type")
    def _compute_odoo_model(self):
        model_obj = self.env["ir.model"].sudo()
        for rec in self:
            model_name = self._mapping_model_name(rec.mapping_type)
            rec.odoo_model = model_name
            rec.odoo_model_id = (
                model_obj.search([("model", "=", model_name)], limit=1).id
                if model_name
                else False
            )

    @api.onchange("mapping_type", "instance_id")
    def _onchange_sync_magento_fields(self):
        for rec in self:
            expected_model = rec._mapping_model_name(rec.mapping_type)
            if rec.odoo_field and rec.odoo_field.model != expected_model:
                rec.odoo_field = False
            if rec.instance_id and rec.mapping_type:
                rec._sync_magento_fields_for(rec.instance_id, rec.mapping_type)

    @api.constrains("mapping_type", "odoo_field")
    def _check_odoo_field_belongs_to_mapping_model(self):
        for rec in self:
            if not rec.odoo_field:
                continue
            expected_model = rec._mapping_model_name(rec.mapping_type)
            if expected_model and rec.odoo_field.model != expected_model:
                raise ValidationError(
                    _(
                        "Invalid Odoo field '%(field)s' for mapping type '%(mapping)s'. "
                        "Please select a field from model '%(model)s'."
                    )
                    % {
                        "field": rec.odoo_field.name,
                        "mapping": rec.mapping_type,
                        "model": expected_model,
                    }
                )

    @api.constrains("transform_expr")
    def _check_transform_expr(self):
        for rec in self:
            error = transform_expression_error(rec.transform_expr)
            if error:
                raise ValidationError(error)

    # ------------------------------------------------------------------
    # Transforms (roadmap 3.8)
    # ------------------------------------------------------------------
    def _transform_applies(self, inbound):
        self.ensure_one()
        if not (self.transform_expr or "").strip():
            return False
        return self.transform_direction in ("both", "inbound" if inbound else "outbound")

    @api.model
    def _transform_record_dict(self, record):
        """Plain-dict view of an Odoo record for outbound transforms (no recordset leaks)."""
        values = {}
        if not record:
            return values
        record = record[:1]
        simple = {"char", "text", "html", "integer", "float", "monetary", "boolean",
                  "selection", "date", "datetime", "many2one"}
        for name, field in record._fields.items():
            if field.type not in simple or not (field.store or field.inherited):
                continue
            if name.startswith(("message_", "activity_")) or field.groups:
                continue
            try:
                value = record[name]
            except Exception:  # noqa: BLE001 - access rights, etc.: just leave it out
                continue
            if isinstance(value, BaseModel):
                value = value.display_name if value else False
            elif isinstance(value, datetime.date):
                value = str(value)
            values[name] = value
        values["id"] = record.id
        return values

    def _apply_transform(self, value, record_dict, *, instance, ref, inbound, odoo_record=None,
                         report=True):
        """``(ok, value)``: the transformed value, or ``(False, None)`` when the transform
        fails. A failure never raises: it opens (or updates) an Error Center issue for this
        record and mapping (unless ``report`` is False), and the caller leaves the field
        unchanged. A success resolves that issue."""
        self.ensure_one()
        issues = self.env["magento.sync.issue"].sudo()
        issue_ref = "%s %s - transform %s" % (
            self.mapping_type, ref or "?", self.odoo_field.name or self.magento_field_id.code or self.id,
        )
        try:
            result = evaluate_transform(self.transform_expr, value, record_dict)
        except TransformError as exc:
            if not report:
                return False, None
            message = _(
                "Transform of mapping %(mapping)s (%(field)s) failed for %(type)s %(ref)s: "
                "%(error)s. The field was left unchanged.",
                mapping=self.id, field=self.magento_field_id.code or self.odoo_field.name,
                type=self.mapping_type, ref=ref or "?", error=exc,
            )
            _logger.warning("Magento %s", message)
            collected = self.env.context.get("magento_transform_errors")
            if isinstance(collected, list):
                collected.append(message)
            if instance:
                try:
                    with self.env.cr.savepoint():
                        issues._record_failure(
                            instance, "other", issue_ref, message, severity="permanent",
                            payload={"mapping_id": self.id, "expression": self.transform_expr,
                                     "direction": "inbound" if inbound else "outbound",
                                     "input": value},
                            record=odoo_record or None,
                        )
                except Exception:  # noqa: BLE001 - the Error Center must never break a sync
                    _logger.warning("Could not record transform failure %s", issue_ref, exc_info=True)
            return False, None
        if instance:
            try:
                with self.env.cr.savepoint():
                    issues._record_success(instance, "other", issue_ref)
            except Exception:  # noqa: BLE001
                _logger.debug("Could not resolve transform issue %s", issue_ref, exc_info=True)
        return True, result

    @staticmethod
    def _transform_ref_from_payload(mapping_type, payload):
        payload = payload or {}
        if mapping_type == "product":
            return payload.get("sku") or payload.get("id")
        if mapping_type == "customer":
            return payload.get("id") or payload.get("email")
        return payload.get("id")

    @staticmethod
    def _transform_ref_from_record(record):
        for name in ("sku", "magento_id"):
            if name in record._fields and record[name]:
                return record[name]
        return "odoo-%s" % record.id

    def _transform_sample_from_magento(self):
        """``(source label, payload)`` of one live Magento record of this mapping type."""
        self.ensure_one()
        api = MagentoAPI(self.instance_id.sudo())
        if self.mapping_type == "product":
            rows = api.get_products(page_size=1, limit=1)
            return _("Magento product"), (rows or [{}])[0]
        if self.mapping_type == "customer":
            rows = api.get_customers(page_size=1, limit=1)
            return _("Magento customer"), (rows or [{}])[0]
        tree = api.get_categories() or {}
        children = tree.get("children_data") or []
        node = children[0] if children and isinstance(children[0], dict) else tree
        return _("Magento category"), {k: v for k, v in node.items() if k != "children_data"}

    def action_live_test_transform(self):
        """Run the transform on a live sample (Magento for import, the latest Odoo record
        for an export-only transform) and show input -> output on the form."""
        self.ensure_one()
        if not self.magento_field_id or not self.instance_id:
            raise UserError(_("Choose an instance and a Magento field first."))
        if not (self.transform_expr or "").strip():
            raise UserError(_("Write a transform expression to test."))
        error = transform_expression_error(self.transform_expr)
        if error:
            raise UserError(error)
        code = self.magento_field_id.code
        if self.transform_direction == "outbound":
            Model = self.env[self._mapping_model_name(self.mapping_type)]
            domain = [("instance_id", "=", self.instance_id.id)] if "instance_id" in Model._fields else []
            sample = Model.search(domain, order="id desc", limit=1)
            if not sample:
                raise UserError(_("No %s record in Odoo to test with.", self.mapping_type))
            field_name = self.odoo_field.name
            value = self._normalize_field_value(sample[field_name]) if field_name in sample._fields else None
            record_dict = self._transform_record_dict(sample)
            source = _("Odoo %(model)s %(name)s", model=sample._description, name=sample.display_name)
        else:
            try:
                source, payload = self._transform_sample_from_magento()
            except Exception as exc:  # noqa: BLE001
                raise UserError(_("Could not read a sample from Magento: %s",
                                  self.instance_id._format_magento_request_error(exc, self.instance_id))) from exc
            if not payload:
                raise UserError(_("Magento returned no %s to test with.", self.mapping_type))
            value = self._extract_nested_value(payload, code)
            record_dict = payload
            ref = self._transform_ref_from_payload(self.mapping_type, payload)
            source = "%s %s" % (source, ref or "")
        vals = {
            "transform_test_input": self._transform_display(value),
            "transform_test_source": source.strip(),
        }
        try:
            output = evaluate_transform(self.transform_expr, value, record_dict)
        except TransformError as exc:
            vals.update(transform_test_output=str(exc), transform_test_status="error")
        else:
            vals.update(transform_test_output=self._transform_display(output), transform_test_status="ok")
        self.write(vals)
        return True

    @staticmethod
    def _transform_display(value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str, ensure_ascii=False, indent=1)[:2000]
        return repr(value)[:2000]

    def _pick_best_sample(self, records):
        if not records:
            return {}

        def score(item):
            total = len(item.keys())
            ext = item.get("extension_attributes")
            if isinstance(ext, dict):
                total += len(ext.keys()) * 5
            attrs = item.get("custom_attributes")
            if isinstance(attrs, list):
                total += len(attrs) * 3
            return total

        return max(records, key=score)

    def _flatten_dict_fields(self, data, code_prefix="", label_prefix=""):
        fields_list = []
        if not isinstance(data, dict):
            return fields_list
        for key, value in data.items():
            code = f"{code_prefix}.{key}" if code_prefix else key
            label = key.replace("_", " ").title()
            if label_prefix:
                label = f"{label_prefix} -> {label}"
            fields_list.append((code, label))
            if isinstance(value, dict):
                for k2 in value.keys():
                    code2 = f"{code}.{k2}"
                    label2 = f"{label} -> {k2.replace('_', ' ').title()}"
                    fields_list.append((code2, label2))
        return fields_list

    def _fetch_magento_fields_for(self, instance, mapping_type):
        api = MagentoAPI(instance.sudo())
        fields_list = []

        try:
            if mapping_type == "product":
                products = api.get_products()
                sample = self._pick_best_sample(products)
                fields_list.extend(self._flatten_dict_fields(sample))

                ext = sample.get("extension_attributes")
                if isinstance(ext, dict):
                    fields_list.extend(
                        self._flatten_dict_fields(ext, "extension_attributes", "Extension")
                    )

                custom_attrs = sample.get("custom_attributes") or []
                if isinstance(custom_attrs, list):
                    for item in custom_attrs:
                        code = item.get("attribute_code")
                        if code:
                            fields_list.append(
                                (f"custom_attributes.{code}", f"Custom -> {code}")
                            )

            elif mapping_type == "customer":
                customers = api.get_customers()
                sample = self._pick_best_sample(customers)
                fields_list.extend(self._flatten_dict_fields(sample))

                addresses = sample.get("addresses") or []
                if addresses and isinstance(addresses[0], dict):
                    fields_list.extend(
                        self._flatten_dict_fields(addresses[0], "addresses", "Address")
                    )

            elif mapping_type == "category":
                tree = api.get_categories() or {}
                fields_list.extend(self._flatten_dict_fields(tree))
                children = tree.get("children_data") or []
                if children and isinstance(children[0], dict):
                    fields_list.extend(
                        self._flatten_dict_fields(children[0], "child", "Child")
                    )
        except Exception:
            _logger.exception(
                "Failed to fetch Magento fields for instance=%s type=%s",
                instance.display_name,
                mapping_type,
            )
            return []

        dedup = {}
        for code, label in fields_list:
            if code and code not in dedup:
                dedup[code] = label
        return [(code, dedup[code]) for code in sorted(dedup.keys())]

    def action_apply_to_existing(self):
        """Explicit button: apply these mappings to every existing record of the
        instance and push the mapped values to Magento.

        Saving a mapping never does this on its own. The payloads of all records
        of one instance and mapping type are built first, then sent together through
        the bulk API (``magento.bulk.operation._submit``): one async bulk request
        per chunk when the instance uses the async bulk API, otherwise one
        synchronous request per record. A record whose payload cannot be built, or
        that Magento rejects, is counted and recorded in the error center; it never
        stops the batch.
        """
        # mapping type -> (HTTP method, bulk route, bulk operation, error-center entity)
        bulk_routes = {
            "product": ("put", "products/bySku", "product_update", "product"),
            "customer": ("put", "customers/byCustomerId", "other", "customer"),
            "category": ("put", "categories/byId", "other", "category"),
        }

        def _bulk_payload_for(mapping_type, row):
            """Request body of one record for the bulk update, or None to skip it.

            Products send only the mapped fields plus the SKU (Magento reads it from the
            body); customers and categories send their full update payload with their id.
            """
            if mapping_type == "product":
                if not row.sku or not row.instance_id or row.type_id in ("configurable", "bundle", "grouped"):
                    return None
                payload = row._build_mapped_payload_only()
                if not payload.get("product"):
                    return None
                payload["product"].setdefault("sku", row.sku)
                return payload
            if not row.magento_id:
                return None
            try:
                magento_id = int(row.magento_id)
            except (TypeError, ValueError):
                raise UserError(_("Invalid Magento ID %s.", row.magento_id)) from None
            if mapping_type == "customer":
                payload = row._build_magento_payload(include_address=True)
                payload["customer"]["id"] = magento_id
            else:
                payload = row._build_magento_payload(for_update=True)
                payload["category"]["id"] = magento_id
            return payload

        issues = self.env["magento.sync.issue"]
        counts = {"pushed": 0, "queued": 0, "failed": 0, "skipped": 0, "operations": 0}

        groups = {}
        blocked = {}
        for rec in self.filtered(
            lambda r: r.active and r.instance_id and r.mapping_type and r.odoo_field and r.magento_field_id
        ):
            # Data Ownership (3.8): no bulk push of an entity Odoo may not write to Magento.
            reason = rec.instance_id._magento_export_block_reason(rec.mapping_type)
            if reason:
                blocked[(rec.instance_id.id, rec.mapping_type)] = reason
                continue
            if rec.mapping_type in bulk_routes and rec._mapping_model_name(rec.mapping_type):
                key = (rec.instance_id.id, rec.mapping_type)
                groups[key] = groups.get(key, self.browse()) | rec

        for (instance_id, mapping_type), mappings in groups.items():
            instance = self.env["magento.instance"].browse(instance_id)
            method, route, operation, entity = bulk_routes[mapping_type]
            Model = self.env[self._mapping_model_name(mapping_type)]
            domain = [("instance_id", "=", instance_id)] if "instance_id" in Model._fields else []
            entries = []
            for row in Model.search(domain):
                ref = row.sku if mapping_type == "product" else row.magento_id
                if row.magento_id and instance._magento_export_block_reason(mapping_type, existing=True):
                    counts["skipped"] += 1  # create-only: existing Magento records stay as they are
                    continue
                try:
                    with self.env.cr.savepoint():
                        for mapping in mappings:
                            self._apply_local_mapped_value(mapping, row)
                        payload = _bulk_payload_for(mapping_type, row)
                except Exception as exc:  # noqa: BLE001 - one bad record must not stop the batch
                    counts["failed"] += 1
                    _logger.warning(
                        "Failed applying %s mappings on record id=%s: %s", mapping_type, row.id, exc,
                    )
                    if ref:
                        issues._record_failure(instance, entity, ref, exc, record=row)
                    continue
                if not payload:
                    counts["skipped"] += 1
                    continue
                entries.append({
                    "payload": payload, "entity": entity, "ref": ref,
                    "res_model": row._name, "res_id": row.id,
                })
            if not entries:
                continue
            ops = self.env["magento.bulk.operation"]._submit(
                instance, operation, method, route, entries,
                description=_("Apply %s mapping", mapping_type),
            )
            counts["operations"] += len(ops)
            for op in ops:
                counts["failed"] += op.failed_count + op.retriable_count + op.rejected_count
                if op.transport == "async":
                    counts["queued"] += op.open_count + op.complete_count
                else:
                    counts["pushed"] += op.complete_count

        if counts["queued"]:
            message = _(
                "Queued: %(queued)s in %(operations)s bulk operation(s), Pushed: %(pushed)s, "
                "Failed: %(failed)s, Skipped: %(skipped)s. Magento processes queued items in the "
                "background; results appear in Logs > Bulk Operations and the Error Center.",
                **counts,
            )
        else:
            message = _(
                "Pushed: %(pushed)s, Failed: %(failed)s, Skipped: %(skipped)s "
                "(failures are listed in the Error Center)",
                **counts,
            )
        if blocked:
            message += " " + " ".join(sorted(set(blocked.values()))) + "."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Mapping applied"),
                "message": message,
                "type": "warning" if counts["failed"] else "success",
                "sticky": bool(counts["failed"]),
            },
        }

    def _apply_local_mapped_value(self, mapping, record):
        """Save mapped source value into local target field when target field exists in Odoo model."""
        if not mapping or not record:
            return
        source_name = mapping.odoo_field.name if mapping.odoo_field else False
        target_name = mapping.magento_field_id.code if mapping.magento_field_id else False
        if not source_name or not target_name:
            return
        if "." in target_name:
            return
        if source_name not in record._fields:
            return
        target_field = record._fields.get(target_name)
        if not target_field or target_field.readonly:
            return

        source_value = self._normalize_field_value(record[source_name])
        if source_value is None:
            source_value = False
        coerced = self._coerce_to_odoo_field(target_field, source_value)
        if coerced is None:
            return

        ctx = {"magento_skip_push": True, "skip_magento_sync": True}
        if record._name == "magento.product.map":
            ctx["skip_odoo_sync"] = True
        try:
            if record[target_name] != coerced:
                record.with_context(**ctx).write({target_name: coerced})
        except Exception:
            _logger.debug(
                "Unable to save local mapped value %r into %s.%s",
                coerced,
                record._name,
                target_name,
                exc_info=True,
            )

    def _sync_magento_fields_for(self, instance, mapping_type):
        available_obj = self.env["magento.available.field"].sudo()
        fetched = self._fetch_magento_fields_for(instance, mapping_type)
        if not fetched:
            return 0, 0

        existing = available_obj.search(
            [
                ("instance_id", "=", instance.id),
                ("mapping_type", "=", mapping_type),
            ]
        )
        existing_map = {rec.code: rec for rec in existing}
        created = 0
        updated = 0

        for code, label in fetched:
            rec = existing_map.get(code)
            if rec:
                if rec.name != label:
                    rec.name = label
                    updated += 1
                continue
            available_obj.create(
                {
                    "name": label,
                    "code": code,
                    "instance_id": instance.id,
                    "mapping_type": mapping_type,
                }
            )
            created += 1
        return created, updated

    def action_sync_magento_fields(self):
        pairs = set()
        if self:
            for rec in self.filtered(lambda r: r.instance_id and r.mapping_type):
                pairs.add((rec.instance_id.id, rec.mapping_type))
        else:
            domain = self.env.context.get("active_domain")
            if domain:
                try:
                    if isinstance(domain, str):
                        domain = ast.literal_eval(domain)
                except Exception:
                    domain = []
                recs = self.search(domain or [])
                for rec in recs.filtered(lambda r: r.instance_id and r.mapping_type):
                    pairs.add((rec.instance_id.id, rec.mapping_type))

            if not pairs:
                default_type = self.env.context.get("default_mapping_type")
                instances = self.env["magento.instance"].search([("active", "=", True)])
                if not instances:
                    raise UserError(_("Please configure at least one active Magento instance."))
                mapping_types = [default_type] if default_type else ["product", "customer", "category"]
                for instance in instances:
                    for mapping_type in mapping_types:
                        pairs.add((instance.id, mapping_type))

        created_total = 0
        updated_total = 0
        for instance_id, mapping_type in pairs:
            instance = self.env["magento.instance"].browse(instance_id)
            created, updated = self._sync_magento_fields_for(instance, mapping_type)
            created_total += created
            updated_total += updated

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Magento fields refreshed"),
                "message": _(
                    "Available fields updated. Created: %(created)s, Updated: %(updated)s"
                )
                % {"created": created_total, "updated": updated_total},
                "type": "success",
            },
        }

    def _set_nested_value(self, payload, dotted_key, value):
        if not isinstance(payload, dict):
            return

        # Magento custom attributes are an array of {attribute_code, value}
        # instead of nested dictionaries.
        if dotted_key.startswith("custom_attributes."):
            code = dotted_key.split(".", 1)[1]
            attrs = payload.setdefault("custom_attributes", [])
            if not isinstance(attrs, list):
                attrs = []
                payload["custom_attributes"] = attrs
            for item in attrs:
                if isinstance(item, dict) and item.get("attribute_code") == code:
                    item["value"] = value
                    return
            attrs.append({"attribute_code": code, "value": value})
            return

        # Use first address slot for address.* mappings.
        if dotted_key.startswith("addresses."):
            sub_key = dotted_key.split(".", 1)[1]
            addresses = payload.setdefault("addresses", [{}])
            if not isinstance(addresses, list) or not addresses:
                addresses = [{}]
                payload["addresses"] = addresses
            if not isinstance(addresses[0], dict):
                addresses[0] = {}
            self._set_nested_value(addresses[0], sub_key, value)
            return

        keys = [k for k in (dotted_key or "").split(".") if k]
        if not keys:
            return
        target = payload
        for key in keys[:-1]:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
            target = target[key]
        target[keys[-1]] = value

    def _mapping_model_name(self, mapping_type):
        return self._MAPPING_MODEL_MAP.get(mapping_type)

    def _normalize_field_value(self, value):
        if isinstance(value, BaseModel):
            if not value:
                return False
            if len(value) == 1:
                return value.display_name
            return ", ".join(value.mapped("display_name"))
        return value

    def _extract_nested_value(self, source, dotted_key):
        if not dotted_key or not isinstance(source, dict):
            return None

        if dotted_key.startswith("custom_attributes."):
            code = dotted_key.split(".", 1)[1]
            attrs = source.get("custom_attributes")
            if not isinstance(attrs, list):
                return None
            for item in attrs:
                if isinstance(item, dict) and item.get("attribute_code") == code:
                    return item.get("value")
            return None

        if dotted_key.startswith("addresses."):
            sub_key = dotted_key.split(".", 1)[1]
            addresses = source.get("addresses")
            if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
                return self._extract_nested_value(addresses[0], sub_key)
            return None

        current = source
        for key in [k for k in dotted_key.split(".") if k]:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _coerce_boolean(self, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            val = value.strip().lower()
            if val in {"1", "true", "yes", "y", "on"}:
                return True
            if val in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    def _coerce_to_odoo_field(self, field_def, value):
        ttype = field_def.ttype

        try:
            if ttype in {"char", "text", "html"}:
                return "" if value is False else str(value)
            if ttype in {"float", "monetary"}:
                return float(value)
            if ttype == "integer":
                return int(float(value))
            if ttype == "boolean":
                return self._coerce_boolean(value)
            if ttype == "date":
                return fields.Date.to_date(value) if value else False
            if ttype == "datetime":
                return fields.Datetime.to_datetime(value) if value else False
            if ttype == "selection":
                return str(value) if value is not None else False
            if ttype == "many2one":
                relation = field_def.relation
                if not relation:
                    return False
                model = self.env[relation]
                if isinstance(value, BaseModel):
                    return value.id if len(value) == 1 else False
                if isinstance(value, int):
                    rec = model.browse(value)
                    return rec.id if rec.exists() else False
                if isinstance(value, str) and value.isdigit():
                    rec = model.browse(int(value))
                    if rec.exists():
                        return rec.id
                if isinstance(value, str) and "name" in model._fields:
                    rec = model.search([("name", "=", value)], limit=1)
                    return rec.id if rec else False
                return False
        except Exception:
            _logger.debug(
                "Unable to coerce value %r to field %s (%s)",
                value,
                field_def.name,
                ttype,
                exc_info=True,
            )
            return None

        # x2many/binary/reference and other complex types are intentionally not auto-mapped.
        return None

    def _mapping_domain(self, mapping_type, instance):
        model_name = self._mapping_model_name(mapping_type)
        instance_id = instance.id if hasattr(instance, "id") else int(instance or 0)
        return [
            ("mapping_type", "=", mapping_type),
            ("instance_id", "=", instance_id),
            ("active", "=", True),
            ("odoo_field", "!=", False),
            ("magento_field_id", "!=", False),
            ("odoo_field.model", "=", model_name),
        ]

    @api.model
    def mapped_source_fields(self, mapping_type, instance):
        fields_set = set()
        if not instance:
            return fields_set
        mappings = self.search(self._mapping_domain(mapping_type, instance))
        for name in mappings.mapped("odoo_field.name"):
            if name:
                fields_set.add(name)
        return fields_set

    @api.model
    def apply_outbound_mapping_to_payload(
        self, mapping_type, instance, source_record, payload, wrapper_key
    ):
        if not instance or not source_record or not isinstance(payload, dict):
            return payload

        mappings = self.search(self._mapping_domain(mapping_type, instance))
        target = payload.setdefault(wrapper_key, {})
        if not isinstance(target, dict):
            return payload

        record_dict = None
        for mapping in mappings:
            field_name = mapping.odoo_field.name
            if field_name not in source_record._fields:
                continue
            value = self._normalize_field_value(source_record[field_name])
            # Do not skip empty values: mapped value must overwrite existing payload value.
            if value is None:
                value = False
            if mapping._transform_applies(inbound=False):
                if record_dict is None:
                    record_dict = self._transform_record_dict(source_record)
                ok, value = mapping._apply_transform(
                    value, record_dict, instance=instance, inbound=False,
                    ref=self._transform_ref_from_record(source_record[:1]), odoo_record=source_record[:1],
                )
                if not ok:
                    continue  # leave the payload field as built without this mapping
            self._set_nested_value(target, mapping.magento_field_id.code, value)
        return payload

    @api.model
    def apply_inbound_mapping_to_vals(self, mapping_type, instance, source_payload):
        vals = {}
        if not instance or not isinstance(source_payload, dict):
            return vals

        mappings = self.search(self._mapping_domain(mapping_type, instance))
        for mapping in mappings:
            field_def = mapping.odoo_field
            if not field_def or field_def.readonly:
                continue
            raw_value = self._extract_nested_value(source_payload, mapping.magento_field_id.code)
            if mapping._transform_applies(inbound=True):
                # A field Magento did not send still goes through the transform (so
                # default(value, ...) works), but a failure on it is not an issue.
                ok, raw_value = mapping._apply_transform(
                    raw_value, source_payload, instance=instance, inbound=True,
                    ref=self._transform_ref_from_payload(mapping_type, source_payload),
                    report=raw_value is not None,
                )
                if not ok:
                    continue  # the Odoo field is left unchanged
            if raw_value is None:
                continue
            coerced = self._coerce_to_odoo_field(field_def, raw_value)
            if coerced is None:
                continue
            vals[field_def.name] = coerced
        return vals

    def _build_payload(self, mapping_type, record, instance):
        mappings = self.search(self._mapping_domain(mapping_type, instance))
        payload = {}
        before_values = {}
        change_lines = []

        for mapping in mappings:
            field_name = mapping.odoo_field.name
            if field_name not in record._fields:
                continue
            value = self._normalize_field_value(record[field_name])
            before_values[field_name] = value
            new_value = False if value is None else value
            if mapping._transform_applies(inbound=False):
                try:
                    new_value = evaluate_transform(
                        mapping.transform_expr, new_value, self._transform_record_dict(record))
                except TransformError as exc:
                    new_value = _("transform failed, field left unchanged: %s", exc)
            code = mapping.magento_field_id.code
            old_value = self._extract_nested_value(payload, code)
            self._set_nested_value(payload, code, new_value)
            change_lines.append(
                {
                    "scope": mapping.mapping_type.title(),
                    "odoo_field": mapping.odoo_field.field_description or field_name,
                    "magento_field": mapping.magento_field_id.name or code,
                    "old_value": old_value,
                    "new_value": new_value,
                    "applied": True,
                }
            )

        # Use fallback defaults only when no mapping rows exist.
        # If mappings exist but values are empty, keep payload empty and show before/after state.
        if not mappings:
            if mapping_type == "product":
                payload["name"] = record.name or _("Unnamed Product")
                before_values["name"] = record.name or ""
            elif mapping_type == "customer":
                payload["email"] = record.email or ""
                payload["firstname"] = (record.name or "").split(" ", 1)[0] if record.name else ""
                before_values["email"] = record.email or ""
                before_values["name"] = record.name or ""
            elif mapping_type == "category":
                payload["name"] = record.display_name or record.name or _("Unnamed Category")
                before_values["name"] = record.display_name or record.name or ""

        wrapper_key = {
            "product": "product",
            "customer": "customer",
            "category": "category",
        }[mapping_type]
        return {wrapper_key: payload}, before_values, change_lines

    def _create_mapping_report(
        self,
        sample_record,
        payload,
        change_lines,
        status="success",
        source_action="test_mapping",
    ):
        self.ensure_one()
        mapping_label = dict(self._fields["mapping_type"].selection).get(self.mapping_type, self.mapping_type)
        line_vals = []
        for line in change_lines:
            line_vals.append(
                (
                    0,
                    0,
                    {
                        "scope": line.get("scope") or mapping_label,
                        "odoo_field": line.get("odoo_field") or "",
                        "magento_field": line.get("magento_field") or "",
                        "old_value": pformat(line.get("old_value")),
                        "new_value": pformat(line.get("new_value")),
                        "applied": bool(line.get("applied")),
                    },
                )
            )

        return self.env["magento.mapping.report"].create(
            {
                "name": "%s - %s" % (mapping_label, sample_record.display_name),
                "mapping_id": self.id,
                "instance_id": self.instance_id.id,
                "mapping_type": self.mapping_type,
                "direction": "odoo_to_magento",
                "source_action": source_action,
                "status": status,
                "record_name": sample_record.display_name,
                "record_model": sample_record._name,
                "record_ref_id": sample_record.id,
                "payload_text": pformat(payload),
                "line_ids": line_vals,
            }
        )

    def action_test_mapping(self):
        mappings = self
        if not mappings:
            active_ids = self.env.context.get("active_ids") or []
            active_model = self.env.context.get("active_model")
            if active_model == "magento.field.mapping" and active_ids:
                mappings = self.browse(active_ids).exists()

        if not mappings:
            domain = self.env.context.get("active_domain")
            if isinstance(domain, str):
                try:
                    domain = ast.literal_eval(domain)
                except Exception:
                    domain = []
            mappings = self.search(domain or [])

        if not mappings:
            fallback_domain = []
            default_mapping_type = self.env.context.get("default_mapping_type")
            if default_mapping_type:
                fallback_domain.append(("mapping_type", "=", default_mapping_type))
            mappings = self.search(fallback_domain, limit=1)

        if not mappings:
            raise UserError(_("No mapping records found to test."))
        # Prefer a complete active row for testing.
        candidate = mappings.filtered(
            lambda m: m.active and m.instance_id and m.odoo_field and m.magento_field_id
        )[:1]
        return (candidate or mappings[:1])._action_test_mapping_single()

    def _action_test_mapping_single(self):
        self.ensure_one()
        model_name = self._mapping_model_name(self.mapping_type)
        if not model_name:
            raise UserError(_("Unsupported mapping type."))

        sample_domain = []
        Model = self.env[model_name]
        if "instance_id" in Model._fields and self.instance_id:
            sample_domain.append(("instance_id", "=", self.instance_id.id))
        sample = Model.search(sample_domain, order="id desc", limit=1)
        if not sample and sample_domain:
            sample = Model.search([], order="id desc", limit=1)
        if not sample:
            raise UserError(_("No record found for model %s.") % model_name)

        payload, before_values, change_lines = self._build_payload(
            self.mapping_type, sample, self.instance_id
        )
        report = self._create_mapping_report(
            sample_record=sample,
            payload=payload,
            change_lines=change_lines,
            status="success",
            source_action="test_mapping",
        )
        message = (
            f"Mapping Type: {self.mapping_type}\n"
            f"Model: {model_name}\n"
            f"Record ID: {sample.id}\n\n"
            f"Before (Odoo Source Fields):\n{pformat(before_values)}\n\n"
            f"After (Magento Payload):\n{pformat(payload)}"
        )
        report.notes = message
        return {
            "type": "ir.actions.act_window",
            "name": "Mapping Report",
            "res_model": "magento.mapping.report",
            "view_mode": "form",
            "res_id": report.id,
            "target": "current",
        }


class MagentoMappingReport(models.Model):
    _name = "magento.mapping.report"
    _description = "Magento Mapping Report"
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, copy=False)
    mapping_id = fields.Many2one(
        "magento.field.mapping",
        string="Mapping",
        ondelete="set null",
        copy=False,
    )
    instance_id = fields.Many2one(
        "magento.instance",
        string="Instance",
        required=True,
        ondelete="cascade",
    )
    mapping_type = fields.Selection(
        [
            ("product", "Product"),
            ("customer", "Customer"),
            ("category", "Category"),
        ],
        string="Mapping Type",
        required=True,
    )
    direction = fields.Selection(
        [
            ("odoo_to_magento", "Odoo to Magento"),
            ("magento_to_odoo", "Magento to Odoo"),
        ],
        string="Direction",
        required=True,
        default="odoo_to_magento",
    )
    source_action = fields.Char(string="Source Action")
    status = fields.Selection(
        [
            ("success", "Success"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="success",
        required=True,
    )
    record_name = fields.Char(string="Record Name")
    record_model = fields.Char(string="Model")
    record_ref_id = fields.Integer(string="Record ID")
    payload_text = fields.Text(string="Payload")
    notes = fields.Text(string="Notes")
    line_ids = fields.One2many(
        "magento.mapping.report.line",
        "report_id",
        string="Changes",
        copy=False,
    )


class MagentoMappingReportLine(models.Model):
    _name = "magento.mapping.report.line"
    _description = "Magento Mapping Report Line"
    _order = "id"

    report_id = fields.Many2one(
        "magento.mapping.report",
        required=True,
        ondelete="cascade",
    )
    scope = fields.Char(string="Scope")
    odoo_field = fields.Char(string="Odoo Field")
    magento_field = fields.Char(string="Magento Field")
    old_value = fields.Text(string="Old Value")
    new_value = fields.Text(string="New Value")
    applied = fields.Boolean(string="Applied", default=True)
