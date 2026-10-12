"""Phase 2.3: Magento customers linked to res.partner.

The customer sync and the order import must converge on one contact per Magento
customer (instance + customer id, then normalized e-mail, never the name), with
every Magento address as a child contact, the customer group as a tag (and
optional pricelist), the VAT, and optionally the billing company.
"""
from unittest.mock import patch

import requests

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import MagentoTestCommon, build_item, build_order, once

GROUPS = [
    {"id": 0, "code": "NOT LOGGED IN", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
    {"id": 1, "code": "General", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
    {"id": 2, "code": "Wholesale", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
    {"id": 3, "code": "Retailer", "tax_class_id": 3, "tax_class_name": "Retail Customer"},
]


def magento_address(address_id, customer_id, street, city="Calder", postcode="49628-7978",
                    region=("MI", "Michigan", 33), country="US", firstname="Veronica",
                    lastname="Costello", billing=False, shipping=False, **extra):
    """One entry of ``addresses`` in GET /V1/customers/search (Magento 2.4)."""
    address = {
        "id": address_id,
        "customer_id": customer_id,
        "region": {"region_code": region[0], "region": region[1], "region_id": region[2]},
        "region_id": region[2],
        "country_id": country,
        "street": [street] if isinstance(street, str) else list(street),
        "telephone": "(555) 229-3326",
        "postcode": postcode,
        "city": city,
        "firstname": firstname,
        "lastname": lastname,
    }
    if billing:
        address["default_billing"] = True
    if shipping:
        address["default_shipping"] = True
    address.update(extra)
    return address


def magento_customer(customer_id, addresses=(), email="roni_cost@example.com", firstname="Veronica",
                     lastname="Costello", group_id=1, **extra):
    data = {
        "id": customer_id,
        "group_id": group_id,
        "created_at": "2026-09-01 08:00:00",
        "updated_at": "2026-09-02 08:00:00",
        "created_in": "Default Store View",
        "dob": "1973-12-15",
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "gender": 2,
        "store_id": 1,
        "website_id": 1,
        "addresses": list(addresses),
        "disable_auto_group_change": 0,
        "extension_attributes": {"is_subscribed": False},
    }
    for address in addresses:
        if address.get("default_billing"):
            data["default_billing"] = str(address["id"])
        if address.get("default_shipping"):
            data["default_shipping"] = str(address["id"])
    data.update(extra)
    return data


def three_address_customer(customer_id=101, **extra):
    return magento_customer(customer_id, [
        magento_address(11, customer_id, "6146 Honey Bluff Parkway", billing=True),
        magento_address(12, customer_id, "4 Main Street", city="Austin", postcode="78701",
                        region=("TX", "Texas", 57), shipping=True),
        magento_address(13, customer_id, "10 Lake Road", city="Denver", postcode="80202",
                        region=("CO", "Colorado", 13)),
    ], **extra)


@tagged("post_install", "-at_install", "magento")
class TestP2Customers(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env["product.product"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.env["magento.product.map"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True,
        ).create({
            "instance_id": cls.instance.id, "magento_id": "1", "sku": "24-MB01",
            "name": cls.bag.name, "odoo_product_id": cls.bag.id,
        })
        cls.Partner = cls.env["res.partner"].with_context(active_test=False)
        cls.Customer = cls.env["magento.customer"]

    def setUp(self):
        super().setUp()
        self.patch_order_side_calls()
        # Groups are fetched once per transaction: forget earlier tests' fetch.
        self.env.cr.cache.pop("magento_customer_groups_fetched", None)
        self.group_calls = []
        self.groups_payload = GROUPS

        def _search_list(api, endpoint, **kwargs):
            if "customerGroups" in endpoint:
                self.group_calls.append(endpoint)
                if isinstance(self.groups_payload, Exception):
                    raise self.groups_payload
                return list(self.groups_payload)
            raise AssertionError("unexpected listing %s" % endpoint)

        self.patch_api("_search_list", side_effect=_search_list)

    # ------------------------------------------------------------------ helpers
    def sync(self, *customers, instance=None):
        instance = instance or self.instance
        self.patch_api("get_customers", side_effect=once(list(customers)))
        counts = instance.sync_customers()
        self.assertFalse(counts.get("errors"), counts)
        return counts

    def contact(self, customer_id, instance=None):
        return self.Partner.search([
            ("magento_instance_id", "=", (instance or self.instance).id),
            ("magento_customer_id", "=", str(customer_id)),
        ])

    def binding(self, customer_id):
        return self.Customer.search([("instance_id", "=", self.instance.id), ("magento_id", "=", str(customer_id))])

    def push_jobs(self, record):
        return self.env["magento.job"].search([
            ("identity_key", "in", ("push-%s-%s" % (record._name, record.id),
                                    "push-%s-%s-next" % (record._name, record.id))),
        ])

    def by_address(self, partner):
        return {c.magento_address_id + ":" + c.type: c for c in partner.child_ids}

    # ------------------------------------------------------------------ addresses
    def test_three_addresses_make_one_contact_and_three_children(self):
        self.sync(three_address_customer())
        partner = self.contact(101)
        self.assertEqual(len(partner), 1)
        self.assertEqual(partner.name, "Veronica Costello")
        self.assertEqual(partner.email, "roni_cost@example.com")
        self.assertEqual(partner.street, "6146 Honey Bluff Parkway", "Own address = default billing")
        self.assertEqual(self.binding(101).partner_id, partner)

        children = partner.child_ids
        self.assertEqual(len(children), 3)
        self.assertEqual(sorted(self.by_address(partner)), ["11:invoice", "12:delivery", "13:other"])
        delivery = self.by_address(partner)["12:delivery"]
        self.assertEqual((delivery.street, delivery.city, delivery.zip), ("4 Main Street", "Austin", "78701"))
        self.assertEqual(delivery.country_id, self.country_us)
        self.assertEqual(delivery.state_id.code, "TX", "State from region_code")
        self.assertEqual(self.by_address(partner)["13:other"].state_id.code, "CO")

    def test_state_by_name_when_region_code_unknown(self):
        address = magento_address(21, 102, "1 Elm St", billing=True, shipping=True,
                                  region=("XX", "Michigan", 0))
        self.sync(magento_customer(102, [address], email="elm@example.com"))
        partner = self.contact(102)
        self.assertEqual(partner.state_id.code, "MI")

    def test_billing_and_shipping_on_one_address(self):
        address = magento_address(31, 103, "6146 Honey Bluff Parkway", billing=True, shipping=True)
        self.sync(magento_customer(103, [address], email="both@example.com"))
        partner = self.contact(103)
        self.assertEqual(sorted(self.by_address(partner)), ["31:delivery", "31:invoice"])

    def test_resync_is_idempotent(self):
        self.sync(three_address_customer())
        partner = self.contact(101)
        children = partner.child_ids
        partner_count = self.Partner.search_count([])

        self.sync(three_address_customer())
        self.assertEqual(self.contact(101), partner)
        self.assertEqual(partner.child_ids, children)
        self.assertEqual(self.Partner.search_count([]), partner_count, "Re-sync creates no contact")

        changed = three_address_customer()
        changed["addresses"][2]["city"] = "Boulder"
        self.sync(changed)
        self.assertEqual(partner.child_ids, children, "An edited address updates its contact")
        self.assertEqual(self.by_address(partner)["13:other"].city, "Boulder")

    def test_duplicate_addresses_are_not_duplicated(self):
        data = three_address_customer()
        data["addresses"].append(magento_address(14, 101, "10 Lake Road", city="Denver",
                                                 postcode="80202", region=("CO", "Colorado", 13)))
        self.sync(data)
        self.assertEqual(len(self.contact(101).child_ids), 3)

    def test_default_flag_moving_retypes_contacts(self):
        self.sync(three_address_customer())
        partner = self.contact(101)
        moved = three_address_customer()
        moved["addresses"][1].pop("default_shipping")
        moved["addresses"][2]["default_shipping"] = True
        moved["default_shipping"] = "13"
        self.sync(moved)
        self.assertEqual(sorted(self.by_address(partner)), ["11:invoice", "12:other", "13:delivery"])
        self.assertEqual(len(partner.child_ids), 3)

    # ------------------------------------------------------------------ order import convergence
    def test_contact_from_order_import_is_reused(self):
        order = build_order(5001, [build_item(50011, "24-MB01", price=34.0, product_id=1)],
                            customer_id=101)
        self.import_orders([order])
        so = self.magento_orders(5001)
        partner = so.partner_id
        self.assertEqual(partner.magento_customer_id, "101")
        order_children = partner.child_ids
        self.assertEqual(sorted(order_children.mapped("type")), ["delivery", "invoice"])

        address = magento_address(11, 101, "6146 Honey Bluff Parkway", billing=True, shipping=True)
        self.sync(magento_customer(101, [address]))
        self.assertEqual(self.contact(101), partner, "Customer sync reuses the order's contact")
        self.assertEqual(partner.child_ids, order_children, "Order address contacts are adopted")
        self.assertEqual(set(partner.child_ids.mapped("magento_address_id")), {"11"})
        self.assertEqual(self.binding(101).partner_id, partner)

    def test_order_import_after_sync_uses_synced_contact(self):
        address = magento_address(11, 101, "6146 Honey Bluff Parkway", billing=True, shipping=True)
        self.sync(magento_customer(101, [address]))
        partner = self.contact(101)
        children = partner.child_ids

        order = build_order(5002, [build_item(50021, "24-MB01", price=34.0, product_id=1)],
                            customer_id=101)
        self.import_orders([order])
        so = self.magento_orders(5002)
        self.assertEqual(so.partner_id, partner)
        self.assertEqual(so.partner_invoice_id, self.by_address(partner)["11:invoice"])
        self.assertEqual(so.partner_shipping_id, self.by_address(partner)["11:delivery"])
        self.assertEqual(partner.child_ids, children, "No duplicate address contacts")

    def test_existing_contact_matched_by_email(self):
        existing = self.env["res.partner"].create({"name": "Roni C.", "email": "Roni_Cost@Example.com"})
        self.sync(three_address_customer())
        self.assertEqual(self.contact(101), existing)
        self.assertEqual(existing.name, "Veronica Costello")

    # ------------------------------------------------------------------ never by name
    def test_name_only_match_is_never_merged(self):
        namesake = self.env["res.partner"].create({"name": "Veronica Costello", "email": "other@example.com"})
        no_email = self.env["res.partner"].create({"name": "Veronica Costello"})
        self.sync(three_address_customer())
        partner = self.contact(101)
        self.assertNotIn(partner, namesake | no_email)
        self.assertFalse(namesake.magento_customer_id or no_email.magento_customer_id)
        self.assertFalse(namesake.child_ids or no_email.child_ids)

        # Two Magento customers with the same name stay two contacts.
        self.sync(magento_customer(202, [], email="veronica2@example.com"))
        self.assertNotEqual(self.contact(202), partner)

    def test_guest_contact_stays_separate(self):
        guest = build_order(5003, [build_item(50031, "24-MB01", price=34.0, product_id=1)],
                            guest=True, email="")
        self.import_orders([guest])
        guest_partner = self.magento_orders(5003).partner_id
        self.sync(three_address_customer())
        self.assertNotEqual(self.contact(101), guest_partner)
        self.assertFalse(guest_partner.magento_customer_id)

    def test_contact_bound_to_another_customer_is_not_taken(self):
        self.sync(magento_customer(301, [], email="shared@example.com"))
        first = self.contact(301)
        self.sync(magento_customer(302, [], email="shared@example.com", firstname="Other"))
        second = self.contact(302)
        self.assertTrue(second)
        self.assertNotEqual(first, second)

    # ------------------------------------------------------------------ groups
    def test_group_tag_and_pricelist(self):
        self.sync(three_address_customer(group_id=2))
        partner = self.contact(101)
        group = self.env["magento.customer.group"].search([
            ("instance_id", "=", self.instance.id), ("magento_group_id", "=", 2)])
        self.assertEqual(group.name, "Wholesale")
        self.assertTrue(group.from_magento)
        self.assertEqual(len(self.instance.customer_group_ids), 4, "All groups are read once")
        self.assertEqual(len(self.group_calls), 1)
        self.assertIn("Magento: Wholesale", partner.category_id.mapped("name"))
        self.assertEqual(partner.magento_customer_group_id, group)
        self.assertEqual(self.binding(101).customer_group_id, group)

        pricelist = self.env["product.pricelist"].create({"name": "Wholesale prices"})
        group.pricelist_id = pricelist
        self.sync(three_address_customer(group_id=2))
        self.assertEqual(partner.with_company(self.instance.company_id).property_product_pricelist, pricelist)

        self.sync(three_address_customer(group_id=1))
        names = partner.category_id.mapped("name")
        self.assertIn("Magento: General", names)
        self.assertNotIn("Magento: Wholesale", names, "Old group tag removed")

    def test_unreachable_group_listing_never_fails_the_customer(self):
        self.groups_payload = requests.exceptions.ConnectionError("down")
        self.sync(three_address_customer(group_id=1))
        group = self.binding(101).customer_group_id
        self.assertEqual((group.name, group.from_magento), ("General", False))
        self.assertIn("Magento: General", self.contact(101).category_id.mapped("name"))

        # A later transaction reads the real names.
        self.env.cr.cache.pop("magento_customer_groups_fetched", None)
        self.groups_payload = [dict(GROUPS[1], code="General Public")]
        self.sync(three_address_customer(group_id=1))
        self.assertEqual((group.name, group.from_magento), ("General Public", True))
        self.assertEqual(group.category_id.name, "Magento: General Public")

    def test_sync_customer_groups_button(self):
        self.instance.action_sync_customer_groups()
        self.assertEqual(sorted(self.instance.customer_group_ids.mapped("name")),
                         ["General", "NOT LOGGED IN", "Retailer", "Wholesale"])
        self.assertTrue(all(self.instance.customer_group_ids.mapped("category_id")))

    # ------------------------------------------------------------------ VAT / company
    def test_vat_from_taxvat_or_billing_vat_id(self):
        self.sync(three_address_customer(taxvat="US-TAX-1"))
        self.assertEqual(self.contact(101).vat, "US-TAX-1")
        data = three_address_customer(customer_id=111, email="vatid@example.com")
        data["addresses"][0]["vat_id"] = "US-VAT-2"
        self.sync(data)
        self.assertEqual(self.contact(111).vat, "US-VAT-2")

    def test_invalid_vat_is_flagged_not_failed(self):
        model = type(self.env["magento.customer"])
        with patch.object(model, "_magento_vat_is_valid", return_value=False):
            counts = self.sync(three_address_customer(taxvat="BE0000000000"))
        self.assertEqual(counts["created"], 1, counts)
        partner = self.contact(101)
        self.assertTrue(partner.magento_vat_invalid)
        self.assertEqual(partner.magento_vat_raw, "BE0000000000")
        self.assertFalse(partner.vat)

        self.sync(three_address_customer(taxvat="US-OK-1"))
        self.assertFalse(partner.magento_vat_invalid)
        self.assertEqual(partner.vat, "US-OK-1")

    def test_vat_rejected_on_write_is_flagged(self):
        partner_model = type(self.env["res.partner"])
        original = partner_model.write

        def _write(records, vals):
            if vals.get("vat") == "BAD":
                raise ValidationError("bad vat")
            return original(records, vals)

        with patch.object(partner_model, "write", _write):
            self.sync(three_address_customer(taxvat="BAD"))
        partner = self.contact(101)
        self.assertTrue(partner.magento_vat_invalid)
        self.assertFalse(partner.vat)

    def test_company_created_from_billing_company(self):
        self.instance.create_company_partners = True
        data = three_address_customer()
        data["addresses"][0].update(company="Acme Outdoor Inc", vat_id="US-ACME-9")
        self.sync(data)
        person = self.contact(101)
        company = person.parent_id
        self.assertTrue(company.is_company)
        self.assertEqual(company.name, "Acme Outdoor Inc")
        self.assertEqual(company.vat, "US-ACME-9", "The VAT belongs to the company")
        self.assertEqual(company.street, "6146 Honey Bluff Parkway")
        self.assertEqual(len(person.child_ids), 3, "Addresses stay under the person")

        # A colleague at the same company joins the same company contact.
        colleague = three_address_customer(customer_id=102, email="colleague@example.com", firstname="Ann")
        colleague["addresses"][0].update(company="ACME Outdoor Inc", vat_id="US-ACME-9")
        self.sync(colleague)
        self.assertEqual(self.contact(102).parent_id, company)
        self.assertEqual(self.Partner.search_count([("is_company", "=", True), ("name", "=ilike", "acme outdoor inc")]), 1)

        # Re-sync: idempotent.
        self.sync(data)
        self.assertEqual(person.parent_id, company)

    def test_no_company_when_option_off(self):
        data = three_address_customer()
        data["addresses"][0]["company"] = "Acme Outdoor Inc"
        self.sync(data)
        self.assertFalse(self.contact(101).parent_id)
        self.assertFalse(self.Partner.search([("name", "=", "Acme Outdoor Inc")]))

    # ------------------------------------------------------------------ Odoo -> Magento
    def test_sync_itself_queues_no_push(self):
        self.instance.push_partner_changes = True
        self.sync(three_address_customer())
        self.assertFalse(self.push_jobs(self.binding(101)))
        self.assertFalse(self.http_attempts)

    def test_partner_edit_pushes_only_with_option(self):
        self.sync(three_address_customer())
        partner = self.contact(101)
        customer = self.binding(101)

        partner.write({"name": "Veronica Smith", "phone": "555-0100"})
        self.assertFalse(self.push_jobs(customer), "Option off: no write to Magento")
        self.assertEqual(customer.lastname, "Costello")

        self.instance.push_partner_changes = True
        partner.write({"name": "Veronica Smith-Jones", "city": "Lansing"})
        self.assertEqual((customer.firstname, customer.lastname), ("Veronica", "Smith-Jones"))
        self.assertEqual(customer.city, "Lansing")
        job = self.push_jobs(customer)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.args, [["address"]])
        self.assertFalse(self.http_attempts, "The push is queued, never sent inside write")

        # Unrelated fields never queue a push.
        job.unlink()
        partner.write({"comment": "VIP"})
        self.assertFalse(self.push_jobs(customer))

    def test_push_payload_keeps_other_addresses(self):
        self.sync(three_address_customer())
        customer = self.binding(101)
        payload = customer._build_magento_payload(include_address=True)
        addresses = payload["customer"]["addresses"]
        self.assertEqual(sorted(a.get("id") for a in addresses), [11, 12, 13])
        by_id = {a["id"]: a for a in addresses}
        self.assertTrue(by_id[11]["default_billing"])
        self.assertTrue(by_id[12]["default_shipping"])
        self.assertFalse(by_id[13]["default_shipping"] or by_id[13]["default_billing"])

    def test_pull_links_the_contact(self):
        customer = self.Customer.with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "magento_id": "101", "email": "roni_cost@example.com",
        })
        self.patch_api("get_customer", return_value=three_address_customer())
        customer.action_pull_magento_customer()
        self.assertEqual(customer.partner_id, self.contact(101))
        self.assertEqual(len(customer.partner_id.child_ids), 3)

    def test_customer_in_two_instances_shares_contact_by_email(self):
        other = self.create_instance("Second Store")
        self.sync(three_address_customer())
        self.sync(three_address_customer(customer_id=900), instance=other)
        first = self.contact(101)
        binding = self.Customer.search([("instance_id", "=", other.id), ("magento_id", "=", "900")])
        self.assertEqual(binding.partner_id, first, "Same e-mail: one contact")
        self.assertEqual(first.magento_instance_id, self.instance, "First binding is kept")
        self.assertEqual(len(first.child_ids), 3, "The owning instance manages the addresses")
