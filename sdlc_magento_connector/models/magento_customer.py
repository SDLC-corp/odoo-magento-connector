import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from .magento_product import magento_push_skipped, schedule_magento_push

_logger = logging.getLogger(__name__)

# Fields whose change triggers the automatic (post-commit) push to Magento.
_CUSTOMER_SYNC_FIELDS = {
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
}
_CUSTOMER_ADDRESS_FIELDS = {
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


def _normalize_yes_no_vals(vals):
    vals = dict(vals or {})
    for key in ("send_welcome_email_from", "default_billing"):
        if key not in vals:
            continue
        val = vals[key]
        if isinstance(val, bool):
            vals[key] = "yes" if val else "no"
        elif val in ("True", "False"):
            vals[key] = "yes" if val == "True" else "no"
    if vals.get("magento_id") == "":
        vals["magento_id"] = False
    return vals


class MagentoCustomer(models.Model):
    _name = "magento.customer"
    _inherit = ["magento.push.error.mixin"]
    _description = "Magento Customer"

    _uniq_instance_magento_id = models.UniqueIndex(
        "(instance_id, magento_id) WHERE magento_id IS NOT NULL AND magento_id != ''",
        "This Magento customer already exists for this instance.",
    )

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
            "magento_id": str(data["id"]) if data.get("id") else False,
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

    def _update_magento_customer(self, api, include_address=True):
        """PUT the customer to Magento, retrying with a minimal payload when
        Magento rejects an attribute as unsupported. Returns the response."""
        self.ensure_one()
        payload = self._build_magento_payload(include_address=include_address)
        try:
            return api.update_customer(self.magento_id, payload)
        except requests.exceptions.HTTPError as exc:
            response = exc.response
            if response is None or response.status_code != 400:
                raise
            try:
                message = response.json().get("message") or response.text
            except Exception:
                message = response.text
            _logger.warning(
                "Magento rejected customer update (magento id=%s): %s", self.magento_id, message
            )
            if "is not supported" not in (message or ""):
                raise
            _logger.debug("Retrying customer %s update with minimal payload", self.magento_id)
            return api.update_customer(self.magento_id, self._build_magento_payload_minimal())

    def _create_magento_customer(self, api):
        """POST the customer to Magento and store the returned id.

        No password is sent: Magento creates the account without one and its
        own welcome / account-confirmation email lets the customer set it.
        """
        self.ensure_one()
        payload = self._build_magento_payload(include_address=True)
        response = api.create_customer(payload)
        if isinstance(response, dict) and response.get("id") is not None:
            self.with_context(magento_skip_push=True, skip_magento_sync=True).write({
                "magento_id": str(response["id"]),
            })
        return response

    def action_create_magento_customer(self):
        for record in self:
            api = record._magento_api()
            try:
                record._create_magento_customer(api)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
        return self._rainbow_man_action(f"Created {len(self)} customer(s) in Magento.")

    def action_update_magento_customer(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to update a customer in Magento.")
            api = record._magento_api()
            try:
                response = record._update_magento_customer(api, include_address=True)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            if isinstance(response, dict):
                values = record._extract_magento_values(response, record.instance_id.id)
                record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
        return self._rainbow_man_action(f"Updated {len(self)} customer(s) in Magento.")

    def action_pull_magento_customer(self):
        for record in self:
            if not record.magento_id:
                raise UserError("Magento ID is required to pull customer from Magento.")
            api = record._magento_api()
            try:
                data = api.get_customer(record.magento_id)
            except requests.exceptions.RequestException as exc:
                record._raise_magento_http_error(exc)
            values = record._extract_magento_values(data, record.instance_id.id)
            record.with_context(magento_skip_push=True, skip_magento_sync=True).write(values)
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

    def _magento_auto_push(self, flags):
        """Automatic (post-commit) push to Magento. Raises on failure."""
        self.ensure_one()
        if not self.instance_id:
            return
        api = self._magento_api()
        try:
            if self.magento_id:
                self._update_magento_customer(api, include_address="address" in flags)
            else:
                self._create_magento_customer(api)
        except requests.exceptions.RequestException as exc:
            self._raise_magento_http_error(exc)

    def _schedule_magento_push(self, changed_fields):
        if magento_push_skipped(self.env):
            return
        sync_fields = set(_CUSTOMER_SYNC_FIELDS)
        for inst in self.mapped("instance_id"):
            sync_fields |= self.env["magento.field.mapping"].sudo().mapped_source_fields("customer", inst)
        if not sync_fields.intersection(changed_fields):
            return
        flags = {"address"} if _CUSTOMER_ADDRESS_FIELDS.intersection(changed_fields) else set()
        schedule_magento_push(self.filtered("instance_id"), flags=flags)

    def write(self, vals):
        vals = _normalize_yes_no_vals(vals)
        res = super().write(vals)
        # Pushed after commit: Magento being slow or down never blocks the save.
        self._schedule_magento_push(set(vals))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create([_normalize_yes_no_vals(vals) for vals in vals_list])
        # New customers without a Magento id are created in Magento after commit.
        if not magento_push_skipped(self.env):
            schedule_magento_push(
                records.filtered(lambda r: r.instance_id and not r.magento_id),
                flags={"address"},
            )
        return records
