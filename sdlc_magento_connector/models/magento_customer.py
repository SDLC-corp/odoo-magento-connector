import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.magento_api import MagentoAPI
from .magento_product import magento_push_skipped, schedule_magento_push
from .sql_compat import Constraint, UniqueIndex
from . import orm_compat

_logger = logging.getLogger(__name__)

# Fields whose change triggers the automatic (queued) push to Magento.
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
    _magento_push_entity = "customer"
    _description = "Magento Customer"

    _uniq_instance_magento_id = UniqueIndex(
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
    magento_address_id = fields.Char(
        string="Magento Address ID", copy=False,
        help="Magento id of the address shown here (the default billing address).",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Contact", index=True, copy=False, ondelete="set null",
        help="Odoo contact this Magento customer is linked to. Customer syncs and order "
             "imports converge on this one contact.",
    )
    customer_group_id = fields.Many2one(
        "magento.customer.group", string="Customer Group", copy=False, ondelete="set null",
    )

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
                # Magento replaces the whole address list on a customer PUT: send every
                # address it knows (with ids) so the other ones are kept, not deleted.
                payload["customer"]["addresses"] = [address] + self._magento_linked_address_payloads()

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
        if self.magento_address_id and self.partner_id:
            try:
                payload["id"] = int(self.magento_address_id)
            except (TypeError, ValueError):
                pass
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
            "magento_address_id": str(address["id"]) if address and address.get("id") else False,
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
            record._magento_sync_partner(data)
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

    def _magento_push_ref(self):
        """Magento id, else the e-mail of a customer not created in Magento yet."""
        self.ensure_one()
        return self.magento_id or self.email or super()._magento_push_ref()

    def _magento_push_now(self, flags):
        """Automatic (queued) push to Magento. Raises on failure."""
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
        # Pushed by a queued job: Magento being slow or down never blocks the save.
        self._schedule_magento_push(set(vals))
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create([_normalize_yes_no_vals(vals) for vals in vals_list])
        # New customers without a Magento id are created in Magento by a queued job.
        if not magento_push_skipped(self.env):
            schedule_magento_push(
                records.filtered(lambda r: r.instance_id and not r.magento_id),
                flags={"address"},
            )
        return records

    # ------------------------------------------------------------------
    # Link to res.partner (Phase 2.3)
    #
    # The customer sync and the order import (sale.order._magento_resolve_customer)
    # converge on ONE contact per Magento customer: both match on
    # (magento_instance_id, magento_customer_id), then on the normalized e-mail, never
    # on the name; both build address contacts with the same helpers
    # (sale.order._magento_address_vals / _magento_address_key), so an invoice or
    # delivery contact created by one is reused by the other.
    # ------------------------------------------------------------------
    @api.model
    def _magento_partner_env(self):
        return self.env["res.partner"].sudo().with_context(
            magento_skip_push=True, skip_magento_sync=True, active_test=True,
        )

    @staticmethod
    def _magento_changed_vals(record, vals):
        """The subset of ``vals`` that differs from ``record`` (Many2one compared by id)."""
        changed = {}
        for fname, value in vals.items():
            current = record[fname]
            if isinstance(current, models.BaseModel):
                current = current.id
            if (current or False) != (value or False):
                changed[fname] = value
        return changed

    @staticmethod
    def _magento_default_address(data, addresses, kind):
        """The default ``kind`` (billing / shipping) address of a Magento customer."""
        key = "default_" + kind
        default_id = str(data.get(key) or "")
        for address in addresses:
            if address.get(key) is True or (default_id and str(address.get("id") or "") == default_id):
                return address
        return {}

    def _magento_match_partner(self, customer_id, email):
        """Linked contact, else (instance, Magento customer id), else normalized e-mail.

        Same rules as the order import; a name alone never matches.
        """
        self.ensure_one()
        instance = self.instance_id
        partners = self._magento_partner_env()
        linked = self.partner_id.sudo().exists()
        if linked and not (
            customer_id and linked.magento_customer_id and linked.magento_instance_id == instance
            and linked.magento_customer_id != customer_id
        ):
            return linked.with_env(partners.env)
        if customer_id:
            partner = partners.with_context(active_test=False).search([
                ("magento_instance_id", "=", instance.id),
                ("magento_customer_id", "=", customer_id),
            ], limit=1)
            if partner:
                return partner.with_env(partners.env)
        if email:
            domain = [("email_normalized", "=", email)]
            if customer_id:
                # Never take a contact already bound to another customer of this instance.
                domain += ["|", ("magento_customer_id", "=", False), ("magento_instance_id", "!=", instance.id)]
            partner = partners.search(domain + [("parent_id", "=", False)], limit=1)
            if not partner:
                partner = partners.search(domain + [("type", "=", "contact")], limit=1)
            return partner
        return partners.browse()

    def _magento_sync_partner(self, data):
        """Create or update the contact of this Magento customer from its REST JSON:
        the person, all its addresses as child contacts, its group tag / pricelist, its
        VAT and (instance option) its company. Returns the person contact."""
        self.ensure_one()
        data = data or {}
        instance = self.instance_id
        helper = self.env["sale.order"]
        partners = self._magento_partner_env()
        addresses = [a for a in (data.get("addresses") or []) if isinstance(a, dict)]
        billing = self._magento_default_address(data, addresses, "billing") or (addresses[0] if addresses else {})

        customer_id = str(data.get("id") or self.magento_id or "")
        email = helper._magento_normalize_email(data.get("email") or self.email)
        firstname = (data.get("firstname") or self.firstname or "").strip()
        lastname = (data.get("lastname") or self.lastname or "").strip()
        name = " ".join(p for p in (firstname, lastname) if p) or email or self.name or "Magento Customer"

        partner = self._magento_match_partner(customer_id, email)
        address_vals = helper._magento_address_vals(billing) if billing else {}
        address_vals.pop("name", None)
        vals = {"name": name}
        if email:
            vals["email"] = email
        group = self.env["magento.customer.group"]
        if not partner:
            vals.update(address_vals)
            vals.update({
                "magento_instance_id": instance.id,
                "magento_customer_id": customer_id or False,
            })
            partner = partners.create(vals)
            owner = True
        else:
            # The contact belongs to the instance it is bound to. A contact shared through
            # its e-mail with a customer of another instance is linked, never rewritten.
            owner = (
                not partner.magento_customer_id
                or partner.magento_instance_id == instance
                or not partner.magento_instance_id
            )
            if owner:
                if partner.is_company:
                    vals.pop("name")  # never rename a company matched by e-mail
                if not (partner.parent_id and partner.type == "contact"):
                    # A company's employee shares the company address: leave it alone.
                    vals.update(address_vals)
                if customer_id and not partner.magento_customer_id:
                    vals.update({"magento_instance_id": instance.id, "magento_customer_id": customer_id})
                changed = self._magento_changed_vals(partner, vals)
                if changed:
                    partner.write(changed)

        if owner:
            if instance.create_company_partners and (billing.get("company") or "").strip():
                self._magento_sync_company(partner, billing)
            self._magento_sync_address_contacts(partner, data, addresses)
            self._magento_sync_vat(partner, (data.get("taxvat") or billing.get("vat_id") or "").strip())
            group = self._magento_sync_group(partner, data.get("group_id"))
        elif data.get("group_id") not in (None, False, ""):
            group = group.sudo()._magento_get_group(instance, data["group_id"])

        link = {}
        if self.partner_id != partner:
            link["partner_id"] = partner.id
        if group and self.customer_group_id != group:
            link["customer_group_id"] = group.id
        if link:
            self.with_context(magento_skip_push=True, skip_magento_sync=True).write(link)
        return partner

    def _magento_sync_company(self, partner, billing):
        """Company contact from the billing company name (instance option): reuse the
        current parent company, else a company with the same VAT, else one with the same
        name; otherwise create it with the billing address. The person becomes its child."""
        helper = self.env["sale.order"]
        partners = self._magento_partner_env()
        company_name = " ".join(billing["company"].split())
        vat = (billing.get("vat_id") or "").strip()
        parent = partner.parent_id
        if parent.is_company and (parent.name or "").strip().lower() == company_name.lower():
            company = parent
        else:
            company = partners.browse()
            if vat:
                company = partners.search([("is_company", "=", True), ("vat", "=ilike", vat)], limit=1)
            if not company:
                company = partners.search([
                    ("is_company", "=", True), ("parent_id", "=", False), ("name", "=ilike", company_name),
                ], limit=1)
            if not company:
                vals = helper._magento_address_vals(billing)
                vals.pop("phone", None)
                vals.update({"name": company_name, "is_company": True})
                company = partners.create(vals)
        if partner.parent_id != company and partner != company:
            partner.write({"parent_id": company.id})
        return company

    def _magento_sync_address_contacts(self, partner, data, addresses):
        """One child contact per Magento address: ``invoice`` for the default billing,
        ``delivery`` for the default shipping (both when one address is both), ``other``
        for the rest. A child is found again by its Magento address id, else adopted when
        an unlinked child of that type has the same address (e.g. made by the order
        import); identical addresses are not duplicated."""
        helper = self.env["sale.order"]
        partners = self._magento_partner_env()
        children = partners.with_context(active_test=False).search([("parent_id", "=", partner.id)])
        children = children.with_env(partners.env)
        billing_id = str((self._magento_default_address(data, addresses, "billing") or {}).get("id") or "")
        shipping_id = str((self._magento_default_address(data, addresses, "shipping") or {}).get("id") or "")
        used = partners.browse()
        seen_keys = set()
        for address in addresses:
            address_id = str(address.get("id") or "")
            vals = helper._magento_address_vals(address)
            if not any(vals.get(f) for f in ("street", "city", "zip")):
                continue
            vals["name"] = vals.get("name") or partner.name
            key = helper._magento_address_key(vals)
            roles = []
            if address_id and address_id == billing_id:
                roles.append("invoice")
            if address_id and address_id == shipping_id:
                roles.append("delivery")
            roles = roles or ["other"]
            linked = children.filtered(lambda c: address_id and c.magento_address_id == address_id) - used
            for role in roles:
                child = linked.filtered(lambda c: c.type == role)[:1] or linked[:1]
                if not child:
                    child = (children - used).filtered(
                        lambda c: c.active and not c.magento_address_id and c.type == role
                        and helper._magento_partner_address_key(c) == key
                    )[:1]
                if not child and role == "other" and key in seen_keys:
                    continue  # the same address twice in Magento: one contact is enough
                child_vals = dict(vals, type=role, magento_address_id=address_id or False)
                if child:
                    if not child.active:
                        child_vals["active"] = True
                    changed = self._magento_changed_vals(child, child_vals)
                    if changed:
                        child.write(changed)
                else:
                    child_vals["parent_id"] = partner.id
                    child = partners.create(child_vals)
                used |= child
                linked -= child
                seen_keys.add(key)
            # Contacts of this address no longer needed (it lost a default flag).
            linked.filtered("active").write({"active": False})

    @api.model
    def _magento_vat_is_valid(self, vat, country, partner_name=""):
        """Syntactic VAT check through ``base_vat`` when installed; True otherwise."""
        partners = self.env["res.partner"]
        if not vat or "vies_valid" not in partners._fields:
            return True
        try:
            partners._run_vat_checks(country, vat, partner_name=partner_name, validation="error")
        except ValidationError:
            return False
        return True

    def _magento_sync_vat(self, partner, vat):
        """Copy Magento's taxvat / billing vat_id to the commercial contact. An invalid VAT
        never fails the import: the contact is flagged and the raw value kept."""
        if not vat:
            if partner.magento_vat_invalid:
                partner.write({"magento_vat_invalid": False, "magento_vat_raw": False})
            return
        commercial = partner.commercial_partner_id
        country = commercial.country_id or partner.country_id
        valid = self._magento_vat_is_valid(vat, country, commercial.name)
        if valid:
            try:
                with self.env.cr.savepoint():
                    if commercial.vat != vat:
                        commercial.write({"vat": vat})
            except ValidationError:
                valid = False
        flag = {"magento_vat_invalid": not valid, "magento_vat_raw": False if valid else vat}
        changed = self._magento_changed_vals(partner, flag)
        if changed:
            partner.write(changed)
        if not valid:
            _logger.info("Magento customer %s: VAT %r is not valid, contact %s flagged",
                         self.magento_id, vat, partner.id)

    def _magento_sync_group(self, partner, group_id):
        """Tag the contact "Magento: <group>" (other group tags of this instance removed)
        and apply the group's pricelist to the commercial contact when mapped."""
        group = self.env["magento.customer.group"]
        if group_id in (None, False, ""):
            return group
        group = group.sudo()._magento_get_group(self.instance_id, group_id)
        if not group:
            return group
        other_tags = self.instance_id.sudo().customer_group_ids.category_id - group.category_id
        commands = [(3, tag.id) for tag in (partner.category_id & other_tags)]
        if group.category_id and group.category_id not in partner.category_id:
            commands.append((4, group.category_id.id))
        vals = {}
        if commands:
            vals["category_id"] = commands
        if partner.magento_customer_group_id != group:
            vals["magento_customer_group_id"] = group.id
        if vals:
            partner.write(vals)
        if group.pricelist_id:
            commercial = partner.commercial_partner_id.with_company(self.instance_id.company_id)
            if commercial.property_product_pricelist != group.pricelist_id:
                commercial.write({"property_product_pricelist": group.pricelist_id.id})
        return group

    # ------------------------------------------------------------------
    # Odoo -> Magento: contact edits (instance option push_partner_changes)
    # ------------------------------------------------------------------
    def _magento_vals_from_partner(self, partner, changed_fields):
        """magento.customer values mirroring the edited ``changed_fields`` of ``partner``."""
        self.ensure_one()
        vals = {}
        if "name" in changed_fields and partner.name:
            parts = partner.name.strip().rsplit(" ", 1)
            vals["firstname"] = parts[0]
            vals["lastname"] = parts[1] if len(parts) > 1 else (self.lastname or parts[0])
        if "email" in changed_fields and partner.email:
            vals["email"] = partner.email
        simple = {"phone": "phone", "street": "street", "street2": "street2", "city": "city", "zip": "postcode"}
        for source, target in simple.items():
            if source in changed_fields:
                vals[target] = partner[source] or ""
        if "country_id" in changed_fields:
            vals["country_id"] = partner.country_id.id
        if "state_id" in changed_fields:
            vals["region"] = partner.state_id.name or ""
        return self._magento_changed_vals(self, vals)

    def _magento_linked_address_payloads(self):
        """Address payloads (with Magento ids) of the linked contact's other address
        contacts, so a customer PUT keeps them. Empty when nothing is linked."""
        self.ensure_one()
        if not self.partner_id:
            return []
        children = self.partner_id.sudo().child_ids.filtered(
            lambda c: c.magento_address_id and c.magento_address_id != (self.magento_address_id or "")
        )
        payloads = {}
        for child in children:
            try:
                address_id = int(child.magento_address_id)
            except (TypeError, ValueError):
                continue
            payload = payloads.get(address_id)
            if payload is None:
                parts = (child.name or self.name or "").strip().rsplit(" ", 1)
                street = [s for s in (child.street, child.street2) if s]
                payload = {
                    "id": address_id,
                    "firstname": parts[0] if parts and parts[0] else (self.firstname or ""),
                    "lastname": parts[1] if len(parts) > 1 else (self.lastname or ""),
                    "street": street or [""],
                    "city": child.city or "",
                    "postcode": child.zip or "",
                    "country_id": child.country_id.code or "",
                    "telephone": child.phone or self.phone or "",
                    "default_billing": False,
                    "default_shipping": False,
                }
                if child.state_id:
                    payload["region"] = {"region": child.state_id.name, "region_code": child.state_id.code}
                payloads[address_id] = payload
            if child.type == "delivery":
                payload["default_shipping"] = True
        return list(payloads.values())

    def action_open_partner(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "res.partner",
            "res_id": self.partner_id.id,
            "view_mode": "form",
            "target": "current",
        }


# Magento's built-in groups, used until the real names were fetched from Magento.
_DEFAULT_GROUP_NAMES = {0: "NOT LOGGED IN", 1: "General", 2: "Wholesale", 3: "Retailer"}
_GROUP_TAG_PREFIX = "Magento: "
_GROUPS_FETCHED_KEY = "magento_customer_groups_fetched"


class MagentoCustomerGroup(models.Model):
    _name = "magento.customer.group"
    _description = "Magento Customer Group"
    _order = "instance_id, magento_group_id"

    _uniq_instance_group = Constraint(
        "unique(instance_id, magento_group_id)",
        "This Magento customer group already exists for this instance.",
    )

    instance_id = fields.Many2one("magento.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="instance_id.company_id", string="Odoo Company", store=True, index=True, readonly=True,
    )
    magento_group_id = fields.Integer(string="Magento Group ID", required=True)
    name = fields.Char(required=True)
    tax_class_id = fields.Integer(string="Tax Class ID")
    tax_class_name = fields.Char(string="Tax Class")
    from_magento = fields.Boolean(
        string="Name from Magento", readonly=True,
        help="Unticked while the group only carries a default name: the next customer sync "
             "reads the real one from Magento.",
    )
    category_id = fields.Many2one(
        "res.partner.category", string="Contact Tag", ondelete="set null",
        help="Tag set on the contacts of this group's customers.",
    )
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist", ondelete="set null",
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Optional: pricelist set on the contacts of this group's customers.",
    )
    partner_count = fields.Integer(string="Contacts", compute="_compute_partner_count")

    def _compute_partner_count(self):
        counts = dict(orm_compat.read_group(self.env["res.partner"].sudo(), 
            [("magento_customer_group_id", "in", self.ids)], ["magento_customer_group_id"], ["__count"],
        ))
        for group in self:
            group.partner_count = counts.get(group, 0)

    def action_view_partners(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Contacts: %s" % self.name,
            "res_model": "res.partner",
            "view_mode": "tree,form",
            "domain": [("magento_customer_group_id", "=", self.id)],
        }

    @api.model
    def _magento_tag(self, group_name):
        name = _GROUP_TAG_PREFIX + group_name
        tags = self.env["res.partner.category"].sudo()
        return tags.search([("name", "=", name)], limit=1) or tags.create({"name": name})

    def _magento_ensure_tag(self, old_name=None):
        for group in self:
            if not group.category_id:
                group.category_id = self._magento_tag(group.name)
            elif old_name and old_name != group.name and group.category_id.name == _GROUP_TAG_PREFIX + old_name:
                renamed = _GROUP_TAG_PREFIX + group.name
                tags = self.env["res.partner.category"].sudo()
                if not tags.search_count([("name", "=", renamed)]):
                    group.category_id.sudo().name = renamed
                else:
                    group.category_id = self._magento_tag(group.name)

    @api.model
    def _magento_upsert_groups(self, instance, rows):
        groups = self.browse()
        for row in rows or []:
            if not isinstance(row, dict) or row.get("id") in (None, ""):
                continue
            group_id = int(row["id"])
            vals = {
                "name": (row.get("code") or "").strip()
                or _DEFAULT_GROUP_NAMES.get(group_id, "Group %s" % group_id),
                "tax_class_id": int(row.get("tax_class_id") or 0),
                "tax_class_name": row.get("tax_class_name") or False,
                "from_magento": True,
            }
            group = self.search([("instance_id", "=", instance.id), ("magento_group_id", "=", group_id)], limit=1)
            if group:
                old_name = group.name
                changed = self.env["magento.customer"]._magento_changed_vals(group, vals)
                if changed:
                    group.write(changed)
                group._magento_ensure_tag(old_name=old_name)
            else:
                group = self.create(dict(vals, instance_id=instance.id, magento_group_id=group_id))
                group._magento_ensure_tag()
            groups |= group
        return groups

    @api.model
    def _magento_fetch_groups(self, instance, retries=True):
        """GET /V1/customerGroups/search and upsert the groups of ``instance``."""
        api = MagentoAPI(instance.sudo())
        if not retries:
            api.MAX_RETRIES = 0  # best effort inside a customer sync: no backoff, no noise
        rows = api._search_list(
            "/rest/V1/customerGroups/search", sort=(("id", "ASC"),), id_key="id",
        )
        return self._magento_upsert_groups(instance, rows)

    @api.model
    def _magento_get_group(self, instance, group_id):
        """Group ``group_id`` of ``instance``. An unknown group (or one with a default
        name only) triggers one best-effort fetch of all groups per transaction; when
        Magento cannot tell, the group is kept under Magento's default name."""
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return self.browse()
        domain = [("instance_id", "=", instance.id), ("magento_group_id", "=", group_id)]
        group = self.search(domain, limit=1)
        fetched = self.env.cr.cache.setdefault(_GROUPS_FETCHED_KEY, set())
        if (not group or not group.from_magento) and instance.id not in fetched:
            fetched.add(instance.id)
            try:
                with self.env.cr.savepoint():
                    self._magento_fetch_groups(instance, retries=False)
            except Exception as exc:  # noqa: BLE001 - group names are cosmetic, never fail a customer
                _logger.info("Magento customer groups of instance %s not fetched: %s", instance.id, exc)
            group = self.search(domain, limit=1)
        if not group:
            group = self.create({
                "instance_id": instance.id,
                "magento_group_id": group_id,
                "name": _DEFAULT_GROUP_NAMES.get(group_id, "Group %s" % group_id),
                "from_magento": False,
            })
        if not group.category_id:
            group._magento_ensure_tag()
        return group


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    create_company_partners = fields.Boolean(
        string="Create companies from billing company name",
        default=False,
        help="When a customer's billing address has a company name, the customer sync creates "
             "(or reuses) a company contact and files the person under it. The VAT goes to the company.",
    )
    push_partner_changes = fields.Boolean(
        string="Push contact changes to Magento",
        default=False,
        help="Editing the name, e-mail, phone or address of a contact linked to a Magento customer "
             "queues an update of that customer in Magento. Off by default: nothing is written to "
             "Magento unless you turn it on.",
    )
    customer_group_ids = fields.One2many("magento.customer.group", "instance_id", string="Customer Groups")
    customer_group_count = fields.Integer(compute="_compute_customer_group_count")

    def _compute_customer_group_count(self):
        counts = dict(orm_compat.read_group(self.env["magento.customer.group"], 
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"],
        ))
        for instance in self:
            instance.customer_group_count = counts.get(instance, 0)

    def action_sync_customer_groups(self):
        self.ensure_one()
        try:
            groups = self.env["magento.customer.group"].sudo()._magento_fetch_groups(self)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            raise UserError("Customer groups could not be read from Magento: %s"
                            % self._format_magento_request_error(exc, self)) from exc
        if not groups:
            raise UserError("Magento returned no customer groups.")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Customer groups",
                "message": "%s customer group(s) read from Magento." % len(groups),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_view_customer_groups(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_customer_group")
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {"default_instance_id": self.id}
        return action
