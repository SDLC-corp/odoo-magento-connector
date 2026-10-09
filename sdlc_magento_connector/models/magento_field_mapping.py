import ast
import logging
from pprint import pformat

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.models import BaseModel

from ..services.magento_api import MagentoAPI


_logger = logging.getLogger(__name__)


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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.instance_id and rec.mapping_type:
                rec._sync_magento_fields_for(rec.instance_id, rec.mapping_type)
        # Apply freshly saved mappings on existing records so values are persisted immediately.
        records._apply_mapping_to_existing_records()
        return records

    def write(self, vals):
        res = super().write(vals)
        watched = {"mapping_type", "instance_id", "odoo_field", "magento_field_id", "active"}
        if watched.intersection(set(vals.keys())) and not self.env.context.get("skip_apply_mapping"):
            self._apply_mapping_to_existing_records()
        return res

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
        api = MagentoAPI(instance)
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

    def _apply_mapping_to_existing_records(self):
        """Push mapped values to Magento immediately for existing mapped records."""
        for rec in self.filtered(
            lambda r: r.active and r.instance_id and r.mapping_type and r.odoo_field and r.magento_field_id
        ):
            model_name = rec._mapping_model_name(rec.mapping_type)
            if not model_name:
                continue
            Model = self.env[model_name].sudo()
            domain = [("instance_id", "=", rec.instance_id.id)] if "instance_id" in Model._fields else []
            records = Model.search(domain)
            for row in records:
                try:
                    self._apply_local_mapped_value(rec, row)
                    if rec.mapping_type == "product":
                        if getattr(row, "sku", False):
                            row.with_context(skip_odoo_sync=True).action_update_magento_mapped_fields()
                    elif rec.mapping_type == "customer":
                        if getattr(row, "magento_id", False):
                            row.action_update_magento_customer()
                    elif rec.mapping_type == "category":
                        if getattr(row, "magento_id", False):
                            row.action_update_magento_category()
                except Exception:
                    _logger.exception(
                        "Failed applying mapping id=%s on %s record id=%s",
                        rec.id,
                        rec.mapping_type,
                        row.id,
                    )

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

        ctx = {"skip_magento_sync": True, "skip_apply_mapping": True}
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

        for mapping in mappings:
            field_name = mapping.odoo_field.name
            if field_name not in source_record._fields:
                continue
            value = self._normalize_field_value(source_record[field_name])
            # Do not skip empty values: mapped value must overwrite existing payload value.
            if value is None:
                value = False
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


class MagentoMappingTestWizard(models.TransientModel):
    _name = "magento.mapping.test.wizard"
    _description = "Magento Mapping Test Result"

    message = fields.Text(readonly=True)


class MagentoMappingReport(models.Model):
    _name = "magento.mapping.report"
    _description = "Magento Mapping Report"
    _order = "create_date desc, id desc"

    @api.model
    def _replace_shopify_text(self, value):
        if not isinstance(value, str):
            return value
        replaced = value.replace("Shopify", "Magento")
        replaced = replaced.replace("shopify", "magento")
        replaced = replaced.replace("SHOPIFY", "MAGENTO")
        return replaced

    @api.model_create_multi
    def create(self, vals_list):
        text_keys = {
            "name",
            "source_action",
            "record_name",
            "payload_text",
            "notes",
        }
        for vals in vals_list:
            for key in text_keys:
                if key in vals:
                    vals[key] = self._replace_shopify_text(vals.get(key))
        return super().create(vals_list)

    def write(self, vals):
        text_keys = {
            "name",
            "source_action",
            "record_name",
            "payload_text",
            "notes",
        }
        for key in text_keys:
            if key in vals:
                vals[key] = self._replace_shopify_text(vals.get(key))
        return super().write(vals)

    def init(self):
        # Keep upgrade work bounded: cleanup only recent rows and only
        # short label fields (skip large text payloads).
        self.env.cr.execute(
            """
            WITH target AS (
                SELECT id
                  FROM magento_mapping_report
              ORDER BY id DESC
                 LIMIT 5000
            )
            UPDATE magento_mapping_report AS report
               SET name = REPLACE(REPLACE(REPLACE(report.name, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO'),
                   source_action = REPLACE(REPLACE(REPLACE(report.source_action, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO'),
                   record_name = REPLACE(REPLACE(REPLACE(report.record_name, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO')
              FROM target
             WHERE report.id = target.id;
            """
        )

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

    @api.model
    def _replace_shopify_text(self, value):
        if not isinstance(value, str):
            return value
        replaced = value.replace("Shopify", "Magento")
        replaced = replaced.replace("shopify", "magento")
        replaced = replaced.replace("SHOPIFY", "MAGENTO")
        return replaced

    @api.model_create_multi
    def create(self, vals_list):
        text_keys = {"scope", "odoo_field", "magento_field", "old_value", "new_value"}
        for vals in vals_list:
            for key in text_keys:
                if key in vals:
                    vals[key] = self._replace_shopify_text(vals.get(key))
        return super().create(vals_list)

    def write(self, vals):
        text_keys = {"scope", "odoo_field", "magento_field", "old_value", "new_value"}
        for key in text_keys:
            if key in vals:
                vals[key] = self._replace_shopify_text(vals.get(key))
        return super().write(vals)

    def init(self):
        # Keep upgrade work bounded: cleanup only recent rows and only
        # short label fields (skip large text values).
        self.env.cr.execute(
            """
            WITH target AS (
                SELECT id
                  FROM magento_mapping_report_line
              ORDER BY id DESC
                 LIMIT 5000
            )
            UPDATE magento_mapping_report_line AS line
               SET scope = REPLACE(REPLACE(REPLACE(line.scope, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO'),
                   odoo_field = REPLACE(REPLACE(REPLACE(line.odoo_field, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO'),
                   magento_field = REPLACE(REPLACE(REPLACE(line.magento_field, 'Shopify', 'Magento'), 'shopify', 'magento'), 'SHOPIFY', 'MAGENTO')
              FROM target
             WHERE line.id = target.id;
            """
        )

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
