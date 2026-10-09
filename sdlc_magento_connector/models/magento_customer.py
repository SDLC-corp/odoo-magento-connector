import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError
from ..services.magento_api import MagentoAPI


class MagentoCustomer(models.Model):
    _name = "magento.customer"
    _description = "Magento Customer"

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", string="Odoo Company", store=True, index=True, readonly=True
    )
    magento_id = fields.Char(string="Magento ID")
    email = fields.Char(required=True)
    prefix = fields.Char(string="Name Prefix")
    firstname = fields.Char()
    middlename = fields.Char(string="Middle Name/Initial")
    lastname = fields.Char()
    suffix = fields.Char(string="Name Suffix")
    name = fields.Char(compute="_compute_name", store=True)
    phone = fields.Char()
    group_id = fields.Integer(string="Group ID")
    website_id = fields.Integer(string="Website ID")
    store_id = fields.Integer(string="Store ID")
    disable_auto_group_change = fields.Boolean(
        string="Disable Automatic Group Change Based on VAT ID"
    )
    allow_remote_shopping_assistance = fields.Boolean(
        string="Allow Remote Shopping Assistance"
    )
    dob = fields.Date(string="Date of Birth")
    taxvat = fields.Char(string="Tax/VAT Number")
    gender = fields.Selection(
        [("1", "Male"), ("2", "Female"), ("3", "Not Specified")],
        string="Gender",
    )
    send_welcome_email_from = fields.Selection(
        [("yes", "Yes"), ("no", "No")],
        string="Send Welcome Email From",
    )
    street = fields.Char()
    street2 = fields.Char()
    city = fields.Char()
    region = fields.Char()
    postcode = fields.Char()
    country_id = fields.Many2one("res.country")
    company = fields.Char()
    vat_id = fields.Char(string="VAT Number")
    default_billing = fields.Selection(
        [("yes", "Yes"), ("no", "No")],
        string="Default Billing Address",
    )
    default_shipping = fields.Boolean()

    _logger = logging.getLogger(__name__)

    @api.depends("firstname", "lastname", "email")
    def _compute_name(self):
        for record in self:
            full = " ".join([p for p in [record.firstname, record.lastname] if p])
            record.name = full or record.email or ""

    def _build_magento_payload(self, include_address=True):
        self.ensure_one()
        if not self.email:
            raise UserError("Email is required for Magento customer.")
        website_id = self.website_id or self.instance_id.website_id
        if not website_id:
            raise UserError("Website ID is required for Magento customer.")
        payload = {
            "customer": {
                "email": self.email,
                "firstname": self.firstname or "",
                "lastname": self.lastname or "",
                "website_id": int(website_id),
            }
        }
        if self.magento_id:
            try:
                payload["customer"]["id"] = int(self.magento_id)
            except (TypeError, ValueError):
                pass
        if self.prefix:
            payload["customer"]["prefix"] = self.prefix
        if self.middlename:
            payload["customer"]["middlename"] = self.middlename
        if self.suffix:
            payload["customer"]["suffix"] = self.suffix
        if self.group_id:
            payload["customer"]["group_id"] = int(self.group_id)
        store_id = self.store_id or self.instance_id.store_id
        if store_id:
            payload["customer"]["store_id"] = int(store_id)
        if self.disable_auto_group_change:
            payload["customer"]["disable_auto_group_change"] = True
        if self.allow_remote_shopping_assistance:
            payload["customer"]["allow_remote_shopping_assistance"] = True
        if self.dob:
            payload["customer"]["dob"] = fields.Date.to_string(self.dob)
        if self.taxvat:
            payload["customer"]["taxvat"] = self.taxvat
        if self.gender:
            payload["customer"]["gender"] = int(self.gender)
        if self.send_welcome_email_from:
            payload["customer"]["send_welcome_email_from"] = (
                1 if self.send_welcome_email_from == "yes" else 0
            )
        if include_address:
            try:
                address = self._build_magento_address_payload()
            except UserError:
                address = None
            if address:
                payload["customer"]["addresses"] = [address]

        # Apply user-defined field mappings on top of default payload.
        self.env["magento.field.mapping"].sudo().apply_outbound_mapping_to_payload(
            mapping_type="customer",
            instance=self.instance_id,
            source_record=self,
            payload=payload,
            wrapper_key="customer",
        )
        return payload

    def _build_magento_payload_minimal(self):
        self.ensure_one()
        website_id = self.website_id or self.instance_id.website_id
        if not website_id:
            raise UserError("Website ID is required for Magento customer.")
        payload = {
            "customer": {
                "email": self.email,
                "firstname": self.firstname or "",
                "lastname": self.lastname or "",
                "website_id": int(website_id),
            }
        }
        if self.prefix:
            payload["customer"]["prefix"] = self.prefix
        if self.middlename:
            payload["customer"]["middlename"] = self.middlename
        if self.suffix:
            payload["customer"]["suffix"] = self.suffix
        if self.group_id:
            payload["customer"]["group_id"] = int(self.group_id)
        store_id = self.store_id or self.instance_id.store_id
        if store_id:
            payload["customer"]["store_id"] = int(store_id)
        # Keep mappings active even on minimal fallback payload.
        self.env["magento.field.mapping"].sudo().apply_outbound_mapping_to_payload(
            mapping_type="customer",
            instance=self.instance_id,
            source_record=self,
            payload=payload,
            wrapper_key="customer",
        )
        return payload

    def _validate_default_password(self):
        self.ensure_one()
        password = self.instance_id.customer_default_password or ""
        min_len = int(self.instance_id.customer_password_min_length or 0)
        if not password:
            raise UserError("Set a default customer password on the Magento Instance first.")
        if min_len and len(password) < min_len:
            raise UserError(
                f"Default customer password must be at least {min_len} characters."
            )
        return password

    def _build_magento_address_payload(self):
        self.ensure_one()
        if not any([self.street, self.city, self.postcode, self.country_id, self.phone]):
            return None
        missing = []
        if not self.street:
            missing.append("street")
        if not self.city:
            missing.append("city")
        if not self.postcode:
            missing.append("postcode")
        if not self.country_id:
            missing.append("country")
        if not self.phone:
            missing.append("phone")
        if missing:
            raise UserError(
                "Address incomplete. Required: " + ", ".join(missing)
            )
        street = [self.street or ""]
        if self.street2:
            street.append(self.street2)
        payload = {
            "firstname": self.firstname or "",
            "lastname": self.lastname or "",
            "street": street,
            "city": self.city or "",
            "postcode": self.postcode or "",
            "country_id": (self.country_id.code or "") if self.country_id else "",
            "telephone": self.phone or "",
            "default_billing": self.default_billing == "yes",
            "default_shipping": bool(self.default_shipping),
        }
        if self.prefix:
            payload["prefix"] = self.prefix
        if self.middlename:
            payload["middlename"] = self.middlename
        if self.suffix:
            payload["suffix"] = self.suffix
        if self.company:
            payload["company"] = self.company
        if self.vat_id:
            payload["vat_id"] = self.vat_id
        if self.region:
            payload["region"] = {"region": self.region}
        return payload

    @api.model
    def _extract_magento_values(self, data, instance_id):
        address = None
        for addr in data.get("addresses", []) or []:
            if addr.get("default_billing"):
                address = addr
                break
        if not address and data.get("addresses"):
            address = data["addresses"][0]
        street = address.get("street") if address else []
        street_line1 = street[0] if street else ""
        street_line2 = street[1] if street and len(street) > 1 else ""
        country_code = address.get("country_id") if address else ""
        country = False
        if country_code:
            country = self.env["res.country"].search([("code", "=", country_code)], limit=1)
        values = {
            "instance_id": instance_id,
            "magento_id": str(data.get("id") or ""),
            "email": data.get("email") or "",
            "prefix": data.get("prefix") or "",
            "firstname": data.get("firstname") or "",
            "middlename": data.get("middlename") or "",
            "lastname": data.get("lastname") or "",
            "suffix": data.get("suffix") or "",
            "group_id": data.get("group_id") or 0,
            "website_id": data.get("website_id") or 0,
            "store_id": data.get("store_id") or 0,
            "disable_auto_group_change": bool(data.get("disable_auto_group_change")),
            "allow_remote_shopping_assistance": bool(data.get("allow_remote_shopping_assistance")),
            "dob": data.get("dob") or False,
            "taxvat": data.get("taxvat") or "",
            "gender": (
                str(data.get("gender"))
                if str(data.get("gender")) in {"1", "2", "3"}
                else False
            ),
            "send_welcome_email_from": (
                "yes"
                if data.get("send_welcome_email_from")
                else "no"
                if data.get("send_welcome_email_from") is not None
                else False
            ),
            "phone": address.get("telephone") if address else "",
            "street": street_line1,
            "street2": street_line2,
            "city": address.get("city") if address else "",
            "region": (address.get("region") or {}).get("region") if address else "",
            "postcode": address.get("postcode") if address else "",
            "country_id": country.id if country else False,
            "company": address.get("company") if address else "",
            "vat_id": address.get("vat_id") if address else "",
            "default_billing": (
                "yes"
                if (address or {}).get("default_billing")
                else "no"
                if address is not None
                else False
            ),
            "default_shipping": bool(address.get("default_shipping")) if address else False,
        }
        instance = self.env["magento.instance"].browse(instance_id)
        mapped_vals = self.env["magento.field.mapping"].sudo().apply_inbound_mapping_to_vals(
            mapping_type="customer",
            instance=instance,
            source_payload=data,
        )
        if mapped_vals:
            values.update(mapped_vals)
        return values

    def _raise_magento_http_error(self, exc):
        response = exc.response
        if response is None:
            raise UserError(f"Magento API error: {exc}") from exc
        try:
            payload = response.json()
            message = payload.get("message") or response.text
        except Exception:
            message = response.text
        status = response.status_code
        raise UserError(f"Magento API error ({status}): {message}") from exc

    def action_create_magento_customer(self):
        for record in self:
            api = MagentoAPI(record.instance_id)
            payload = record._build_magento_payload(include_address=True)
            payload["password"] = record._validate_default_password()
            try:
                response = api.create_customer(payload)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                if not record.magento_id and response.get("id") is not None:
                    record.with_context(skip_magento_sync=True).write({
                        "magento_id": str(response.get("id")),
                    })
        return self._rainbow_man_action(f"Created {len(self)} customer(s) in Magento.")

    def action_update_magento_customer(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to update a customer in Magento.")
            api = MagentoAPI(record.instance_id)
            payload = record._build_magento_payload(include_address=True)
            self._logger.info(
                "Magento customer update payload (id=%s, email=%s): %s",
                record.magento_id,
                record.email,
                payload,
            )
            try:
                response = api.update_customer(record.magento_id, payload)
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code == 400:
                    message = ""
                    try:
                        message = response.json().get("message") or response.text
                    except Exception:
                        message = response.text
                    self._logger.warning(
                        "Magento customer update 400 (id=%s): %s",
                        record.magento_id,
                        message,
                    )
                    if "is not supported" in (message or ""):
                        payload = record._build_magento_payload_minimal()
                        self._logger.info(
                            "Magento customer update fallback payload (id=%s): %s",
                            record.magento_id,
                            payload,
                        )
                        try:
                            response = api.update_customer(record.magento_id, payload)
                        except requests.exceptions.HTTPError as exc2:
                            record._raise_magento_http_error(exc2)
                    else:
                        record._raise_magento_http_error(exc)
                else:
                    record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                self._logger.info(
                    "Magento customer update response (id=%s): %s",
                    record.magento_id,
                    response,
                )
                values = record._extract_magento_values(response, record.instance_id.id)
                record.with_context(skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Updated {len(self)} customer(s) in Magento.")

    def action_pull_magento_customer(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to pull customer from Magento.")
            api = MagentoAPI(record.instance_id)
            try:
                data = api.get_customer(record.magento_id)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
            values = record._extract_magento_values(data, record.instance_id.id)
            record.with_context(skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Pulled {len(self)} customer(s) from Magento.")

    def _rainbow_man_action(self, message):
        return {
            "type": "ir.actions.act_window_close",
            "effect": {
                "fadeout": "slow",
                "message": message,
                "type": "rainbow_man",
            }
        }

    def write(self, vals):
        vals = dict(vals or {})
        if "send_welcome_email_from" in vals:
            val = vals.get("send_welcome_email_from")
            if isinstance(val, bool):
                vals["send_welcome_email_from"] = "yes" if val else "no"
            elif val in ("True", "False"):
                vals["send_welcome_email_from"] = "yes" if val == "True" else "no"
        if "default_billing" in vals:
            val = vals.get("default_billing")
            if isinstance(val, bool):
                vals["default_billing"] = "yes" if val else "no"
            elif val in ("True", "False"):
                vals["default_billing"] = "yes" if val == "True" else "no"
        res = super().write(vals)

        if self.env.context.get("skip_magento_sync"):
            return res

        magento_sync_fields = {
            "email",
            "prefix",
            "firstname",
            "middlename",
            "lastname",
            "suffix",
            "phone",
            "group_id",
            "website_id",
            "store_id",
            "disable_auto_group_change",
            "allow_remote_shopping_assistance",
            "dob",
            "taxvat",
            "gender",
            "send_welcome_email_from",
            "street",
            "street2",
            "city",
            "region",
            "postcode",
            "country_id",
            "company",
            "vat_id",
            "default_billing",
            "default_shipping",
            "instance_id",
            "magento_id",
        }
        mapped_sync_fields = set()
        for inst in self.mapped("instance_id"):
            mapped_sync_fields.update(
                self.env["magento.field.mapping"].sudo().mapped_source_fields("customer", inst)
            )
        if not (magento_sync_fields | mapped_sync_fields).intersection(vals.keys()):
            return res

        for record in self:
            if not record.instance_id:
                continue
            api = MagentoAPI(record.instance_id)
            address_fields = {
                "street",
                "street2",
                "city",
                "postcode",
                "country_id",
                "phone",
                "company",
                "region",
                "default_billing",
                "default_shipping",
            }
            include_address = not bool(record.magento_id) or bool(address_fields.intersection(vals.keys()))
            payload = record._build_magento_payload(include_address=include_address)
            try:
                if record.magento_id:
                    try:
                        response = api.update_customer(record.magento_id, payload)
                    except requests.exceptions.HTTPError as exc:
                        response_obj = exc.response
                        if response_obj is not None and response_obj.status_code == 400:
                            message = ""
                            try:
                                message = response_obj.json().get("message") or response_obj.text
                            except Exception:
                                message = response_obj.text
                            self._logger.warning(
                                "Magento customer write update 400 (id=%s): %s",
                                record.magento_id,
                                message,
                            )
                            if "is not supported" in (message or ""):
                                payload = record._build_magento_payload_minimal()
                                self._logger.info(
                                    "Magento customer write fallback payload (id=%s): %s",
                                    record.magento_id,
                                    payload,
                                )
                                response = api.update_customer(record.magento_id, payload)
                            else:
                                raise
                        else:
                            raise
                else:
                    payload["password"] = record._validate_default_password()
                    response = api.create_customer(payload)
                if isinstance(response, dict):
                    self._logger.info(
                        "Magento customer write response (id=%s): %s",
                        record.magento_id,
                        response,
                    )
                    if not record.magento_id and response.get("id") is not None:
                        record.with_context(skip_magento_sync=True).write({
                            "magento_id": str(response.get("id")),
                        })
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
        return res

    def create(self, vals_list):
        clean_vals_list = []
        for vals in vals_list:
            clean = dict(vals or {})
            if "send_welcome_email_from" in clean:
                val = clean.get("send_welcome_email_from")
                if isinstance(val, bool):
                    clean["send_welcome_email_from"] = "yes" if val else "no"
                elif val in ("True", "False"):
                    clean["send_welcome_email_from"] = "yes" if val == "True" else "no"
            if "default_billing" in clean:
                val = clean.get("default_billing")
                if isinstance(val, bool):
                    clean["default_billing"] = "yes" if val else "no"
                elif val in ("True", "False"):
                    clean["default_billing"] = "yes" if val == "True" else "no"
            clean_vals_list.append(clean)
        records = super().create(clean_vals_list)

        if records.env.context.get("skip_magento_sync"):
            return records
        for record in records:
            if not record.instance_id:
                continue
            if record.magento_id:
                continue
            api = MagentoAPI(record.instance_id)
            payload = record._build_magento_payload(include_address=True)
            payload["password"] = record._validate_default_password()
            try:
                response = api.create_customer(payload)
                if isinstance(response, dict):
                    values = record._extract_magento_values(response, record.instance_id.id)
                    record.with_context(skip_magento_sync=True).write(values)
            except requests.exceptions.HTTPError as exc:
                record._raise_magento_http_error(exc)
        return records
