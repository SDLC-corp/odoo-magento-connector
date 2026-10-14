"""Roadmap 3.6: Adobe Commerce B2B (companies, shared catalogs, negotiable quotes).

Simulated HTTP only (the local store is Magento Open Source, which has no B2B). Every
payload below follows the examples of Adobe's B2B REST documentation:
https://developer.adobe.com/commerce/webapi/rest/b2b/integrations and the company,
company-structures, credit-manage, shared-cat-*, negotiable-* pages.
"""
import json
from unittest.mock import patch

import requests

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

from .common import MAGENTO_BASE_URL, MagentoTestCommon, build_item, build_order
from .test_p3_accs import FakeTransport

REST = MAGENTO_BASE_URL + "/rest/V1"


def company_json(company_id=2, name="Test company", super_user_id=5, status=1, group=4, vat="BE0477472701"):
    """Shape of GET /V1/company/2 (Adobe company-object page)."""
    return {
        "id": company_id,
        "status": status,
        "company_name": name,
        "legal_name": name + " LLC",
        "company_email": "newemail@example.com",
        "vat_tax_id": vat,
        "reseller_id": "RS-%s" % company_id,
        "street": ["100 Big Tree Avenue"],
        "city": "San Francisco",
        "country_id": "US",
        "region": "California",
        "region_id": "12",
        "postcode": "99999",
        "telephone": "4155551212",
        "customer_group_id": group,
        "sales_representative_id": 1,
        "reject_reason": None,
        "rejected_at": None,
        "super_user_id": super_user_id,
        "extension_attributes": {
            "quote_config": {"company_id": str(company_id), "is_quote_enabled": True},
            "is_company_address_book_enabled": False,
            "is_custom_shipping_address_allowed": False,
        },
    }


CREDIT = {"id": 2, "company_id": 2, "credit_limit": 500, "balance": 0, "currency_code": "USD",
          "exceed_limit": False, "available_limit": 500}
HIERARCHY = [
    {"structure_id": 2, "entity_id": 5, "entity_type": "customer", "structure_parent_id": 0},
    {"structure_id": 7, "entity_id": 1, "entity_type": "team", "structure_parent_id": 2},
    {"structure_id": 6, "entity_id": 7, "entity_type": "customer", "structure_parent_id": 7},
]


def customer_json(customer_id, email, firstname, lastname, company_id=2):
    return {
        "id": customer_id, "email": email, "firstname": firstname, "lastname": lastname,
        "group_id": 4, "website_id": 1, "store_id": 1, "addresses": [],
        "extension_attributes": {"company_attributes": {"customer_id": customer_id, "company_id": company_id,
                                                        "status": 1}},
    }


def quote_json(quote_id=5, status="submitted_by_customer", price_type=None, price_value=None,
               items=None, customer_id=5):
    """Shape of GET /V1/carts/5 for a negotiable quote (Adobe negotiable-order-workflow page)."""
    items = items if items is not None else [
        {"item_id": 10, "sku": "24-UG02", "qty": 15, "price": 12.48, "name": "Pursuit Lumaflex",
         "product_type": "simple", "quote_id": str(quote_id)},
        {"item_id": 11, "sku": "24-UG03", "qty": 10, "price": 17.16, "name": "Harmony Lumaflex",
         "product_type": "simple", "quote_id": str(quote_id)},
    ]
    original = sum(i["price"] * i["qty"] for i in items)
    negotiable = {
        "quote_id": quote_id, "is_regular_quote": True, "status": status, "quote_name": "Q3 restock",
        "expiration_period": "2026-12-31", "original_total_price": original,
        "negotiated_total_price": original, "creator_id": customer_id, "creator_type": 3,
    }
    if price_type:
        negotiable.update(negotiated_price_type=price_type, negotiated_price_value=price_value)
    return {
        "id": quote_id, "is_active": True, "items": items, "items_count": len(items),
        "customer": {"id": customer_id, "email": "roni_cost@example.com", "firstname": "Veronica",
                     "lastname": "Costello", "extension_attributes": {"company_attributes": {"company_id": 2}}},
        "currency": {"quote_currency_code": "USD"},
        "extension_attributes": {"negotiable_quote": negotiable},
    }


COMMENTS = [
    {"entity_id": 6, "parent_id": 5, "creator_type": 3, "is_decline": 0, "is_draft": 0, "creator_id": 5,
     "comment": "Requesting a 5% discount", "created_at": "2026-10-01 21:14:51", "attachments": []},
    {"entity_id": 7, "parent_id": 5, "creator_type": 2, "is_decline": 0, "is_draft": 0, "creator_id": 1,
     "comment": "We&#039;re looking at it", "created_at": "2026-10-01 21:29:15", "attachments": []},
]


@tagged("post_install", "-at_install", "magento")
class TestB2B(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance.write({"b2b_enabled": True})
        cls.group = cls.env["magento.customer.group"].create({
            "instance_id": cls.instance.id, "magento_group_id": 4, "name": "Acme Shared", "from_magento": True,
        })
        cls.Job = cls.env["magento.job"]
        cls.Issue = cls.env["magento.sync.issue"]

    def setUp(self):
        super().setUp()
        self.http = FakeTransport()
        self.startPatcher(patch.object(requests.sessions.Session, "send", self.http))

    def run_jobs(self, method=None):
        jobs = self.Job.search([("state", "=", "pending")] + ([("method_name", "=", method)] if method else []))
        for job in jobs:
            job._run_job()
        return jobs

    def body(self, request):
        return json.loads(request.body)

    def company_routes(self, companies=None):
        companies = companies if companies is not None else [company_json()]
        self.http.add("GET", r"/rest/V1/company\?", (200, {"items": companies, "total_count": len(companies)}))
        self.http.add("GET", r"/rest/V1/companyCredits/company/2$", (200, CREDIT))
        self.http.add("GET", r"/rest/V1/hierarchy/2$", (200, HIERARCHY))
        self.http.add("GET", r"/rest/V1/customers/7$",
                      (200, customer_json(7, "bryce@example.com", "Bryce", "Martin")))
        self.http.add("GET", r"/rest/V1/customers/5$",
                      (200, customer_json(5, "roni_cost@example.com", "Veronica", "Costello")))

    def import_company(self, companies=None):
        self.company_routes(companies)
        return self.instance._magento_b2b_import_companies()

    def b2b_company(self, company_id="2"):
        return self.env["res.partner"].search([("magento_company_instance_id", "=", self.instance.id),
                                               ("magento_company_id", "=", company_id)])

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------
    def test_detect_b2b_present(self):
        self.http.add("GET", r"/rest/V1/company\?", (200, {"items": [company_json()], "total_count": 1}))
        self.instance.action_b2b_detect()
        self.assertEqual(self.instance.b2b_status, "present")
        self.assertIn("B2B detected", self.instance.b2b_status_message)
        self.assertIn("searchCriteria%5BpageSize%5D=1", self.http.requests[0].url)

    def test_detect_b2b_absent_on_open_source(self):
        self.http.add("GET", r"/rest/V1/company\?", (404, {"message": "Request does not match any route."}))
        self.instance.action_b2b_detect()
        self.assertEqual(self.instance.b2b_status, "absent")
        self.assertIn("B2B not available", self.instance.b2b_status_message)
        self.assertIn("Request does not match any route", self.instance.b2b_status_message)
        self.assertEqual(len(self.http.requests), 1, "detection is one probe, no retries")

    def test_detect_reports_missing_acl(self):
        self.http.add("GET", r"/rest/V1/company\?",
                      (403, {"message": "The consumer isn't authorized to access %resources.",
                             "parameters": {"resources": "Magento_Company::index"}}))
        self.instance.action_b2b_detect()
        self.assertEqual(self.instance.b2b_status, "error")
        self.assertIn("Magento_Company::index", self.instance.b2b_status_message)

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------
    def test_company_import_with_users_is_idempotent(self):
        existing = self.env["res.partner"].create({
            "name": "Veronica Costello", "email": "roni_cost@example.com",
            "magento_instance_id": self.instance.id, "magento_customer_id": "5",
        })
        term = self.env["account.payment.term"].create({"name": "Payment on Account 30"})
        self.instance.write({"b2b_credit_payment_term_id": term.id})
        result = self.import_company()
        self.assertIn("1 compan(ies) imported, 0 failed", result)
        company = self.b2b_company()
        self.assertEqual(len(company), 1)
        self.assertTrue(company.is_company)
        self.assertEqual(company.name, "Test company")
        self.assertEqual(company.magento_company_legal_name, "Test company LLC")
        self.assertEqual(company.magento_company_vat, "BE0477472701")
        self.assertEqual(company.magento_company_reseller_id, "RS-2")
        self.assertEqual(company.magento_company_status, "1")
        self.assertEqual(company.magento_company_group_id, 4)
        self.assertEqual(company.street, "100 Big Tree Avenue")
        self.assertEqual(company.city, "San Francisco")
        self.assertEqual(company.zip, "99999")
        self.assertEqual(company.country_id, self.country_us)
        self.assertEqual(company.state_id.code, "CA")
        self.assertEqual(company.magento_company_credit_limit, 500)
        self.assertEqual(company.magento_company_credit_available, 500)
        self.assertEqual(company.magento_company_credit_currency, "USD")
        self.assertEqual(company.with_company(self.instance.company_id).property_payment_term_id, term)
        # The admin (customer 5) is the existing contact, re-filed under the company.
        self.assertEqual(company.magento_company_admin_id, existing)
        self.assertEqual(existing.parent_id, company)
        bryce = self.env["res.partner"].search([("magento_instance_id", "=", self.instance.id),
                                                ("magento_customer_id", "=", "7")])
        self.assertEqual(bryce.parent_id, company)
        self.assertEqual(bryce.email, "bryce@example.com")
        self.assertFalse(self.http.sent("GET", r"/customers/5$"), "a bound contact is not fetched again")

        partner_count = self.env["res.partner"].search_count([])
        self.import_company()
        self.assertEqual(self.env["res.partner"].search_count([]), partner_count)
        self.assertEqual(len(self.b2b_company()), 1)

    def test_company_import_failure_goes_to_the_error_center(self):
        broken = company_json(company_id=3, name="Broken", super_user_id=99, vat="")
        self.http.add("GET", r"/rest/V1/companyCredits/company/3$", (200, {}))
        self.http.add("GET", r"/rest/V1/hierarchy/3$", (200, []))
        self.http.add("GET", r"/rest/V1/customers/99$", (500, {"message": "boom"}))
        self.import_company([company_json(), broken])
        self.assertTrue(self.b2b_company("2"))
        self.assertFalse(self.b2b_company("3"))
        issue = self.Issue.search([("instance_id", "=", self.instance.id), ("entity", "=", "b2b_company"),
                                   ("magento_ref", "=", "3"), ("state", "=", "open")])
        self.assertEqual(len(issue), 1)
        self.assertIn("500", issue.message)

    def test_company_export_requires_an_admin(self):
        partner = self.env["res.partner"].create({
            "name": "Odoo Wholesale", "is_company": True, "vat": "US123456789", "email": "buy@wholesale.example",
            "street": "1 Main St", "city": "Austin", "zip": "73301", "country_id": self.country_us.id,
            "phone": "5125550100", "magento_company_instance_id": self.instance.id, "magento_b2b_export": True,
        })
        with self.assertRaisesRegex(UserError, "Magento needs a company admin"):
            partner.action_magento_b2b_export()
        self.assertFalse(self.Job.search([("method_name", "=", "_magento_b2b_export_company")]))

        admin = self.env["res.partner"].create({
            "name": "Ada Admin", "parent_id": partner.id, "email": "ada@wholesale.example",
            "magento_instance_id": self.instance.id, "magento_customer_id": "42",
        })
        partner.write({"magento_company_admin_id": admin.id})
        partner.action_magento_b2b_export()
        self.http.add("POST", r"/rest/V1/company$", (200, dict(company_json(company_id=9), status=0)))
        self.run_jobs("_magento_b2b_export_company")
        post = self.http.sent("POST", r"/rest/V1/company$")[0]
        company = self.body(post)["company"]
        self.assertEqual(company["super_user_id"], 42)
        self.assertEqual(company["company_name"], "Odoo Wholesale")
        self.assertEqual(company["company_email"], "buy@wholesale.example")
        self.assertEqual(company["street"], ["1 Main St"])
        self.assertEqual(company["country_id"], "US")
        self.assertEqual(company["customer_group_id"], 1)
        self.assertEqual(partner.magento_company_id, "9")
        self.assertEqual(partner.magento_company_status, "0")

        # Second export updates it.
        self.http.add("PUT", r"/rest/V1/company/9$", (200, company_json(company_id=9)))
        partner.action_magento_b2b_export()
        self.run_jobs("_magento_b2b_export_company")
        put = self.body(self.http.sent("PUT")[0])["company"]
        self.assertEqual(put["id"], 9)

    # ------------------------------------------------------------------
    # Shared catalogs
    # ------------------------------------------------------------------
    def test_shared_catalog_to_and_from_a_pricelist(self):
        products = {}
        for sku, magento_id in (("24-MB01", 1), ("24-MB04", 2), ("24-UG06", 3)):
            products[sku] = self.create_product(sku, list_price=40.0)
            self.create_map(products[sku], magento_id)
        self.import_company()
        catalog_row = {"id": 2, "name": "Custom shared catalog", "description": "Just a sample custom shared catalog.",
                       "customer_group_id": 4, "type": 0, "created_at": "2017-07-21 15:39:40", "created_by": 1,
                       "store_id": 0, "tax_class_id": 3}
        self.http.add("GET", r"/rest/V1/sharedCatalog\?", (200, {"items": [catalog_row], "total_count": 1}))
        self.http.add("GET", r"/rest/V1/sharedCatalog/2/products$", (200, ["24-MB01", "24-MB04", "UNMAPPED"]))
        self.http.add("POST", r"/rest/V1/products/tier-prices-information$", (200, [
            {"sku": "24-MB01", "price": 30.0, "price_type": "fixed", "website_id": 0,
             "customer_group": "Acme Shared", "quantity": 1},
            {"sku": "24-MB01", "price": 27.0, "price_type": "fixed", "website_id": 0,
             "customer_group": "Acme Shared", "quantity": 10},
            {"sku": "24-MB04", "price": 15.0, "price_type": "discount", "website_id": 0,
             "customer_group": "Acme Shared", "quantity": 1},
            {"sku": "24-MB04", "price": 5.0, "price_type": "fixed", "website_id": 0,
             "customer_group": "Wholesale", "quantity": 1},
        ]))
        self.http.add("GET", r"/rest/V1/sharedCatalog/2/companies$", (200, '["2"]'))
        result = self.instance._magento_b2b_import_shared_catalogs()
        self.assertIn("1 shared catalog(s) imported, 0 failed", result)
        catalog = self.env["magento.shared.catalog"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(catalog.magento_catalog_id, 2)
        self.assertEqual(catalog.catalog_type, "0")
        self.assertEqual(catalog.customer_group_rec_id, self.group)
        self.assertEqual(catalog.product_skus, ["24-MB01", "24-MB04", "UNMAPPED"])
        pricelist = catalog.pricelist_id
        self.assertTrue(pricelist)
        items = {(i.product_id.default_code, i.min_quantity): i for i in pricelist.item_ids}
        self.assertEqual(set(items), {("24-MB01", 1.0), ("24-MB01", 10.0), ("24-MB04", 1.0)})
        self.assertEqual(items[("24-MB01", 1.0)].fixed_price, 30.0)
        self.assertEqual(items[("24-MB04", 1.0)].compute_price, "discount")
        self.assertEqual(items[("24-MB04", 1.0)].price_discount, 15.0)
        company = self.b2b_company()
        self.assertEqual(company.magento_shared_catalog_id, catalog)
        self.assertEqual(company.with_company(self.instance.company_id).property_product_pricelist, pricelist)

        # Re-import is idempotent.
        self.instance._magento_b2b_import_shared_catalogs()
        self.assertEqual(len(pricelist.item_ids), 3)

        # Export: pricelist -> assigned products + tier prices of the catalog's group + companies.
        pricelist.write({"item_ids": [Command.create({
            "applied_on": "0_product_variant", "product_id": products["24-UG06"].id,
            "compute_price": "fixed", "fixed_price": 33.0})]})
        self.http.add("POST", r"/rest/V1/sharedCatalog/2/assignProducts$", (200, True))
        self.http.add("POST", r"/rest/V1/products/tier-prices$", (200, []))
        self.http.add("POST", r"/rest/V1/sharedCatalog/2/assignCompanies$", (200, True))
        catalog.action_export()
        self.run_jobs("_magento_b2b_export_catalog")
        assigned = self.body(self.http.sent("POST", r"/assignProducts$")[0])
        self.assertEqual(assigned, {"products": [{"sku": "24-MB01"}, {"sku": "24-MB04"}, {"sku": "24-UG06"}]})
        tiers = self.body(self.http.sent("POST", r"/products/tier-prices$")[0])["prices"]
        by_key = {(t["sku"], t["quantity"]): t for t in tiers}
        self.assertEqual(by_key[("24-MB01", 1.0)]["price"], 30.0)
        self.assertEqual(by_key[("24-MB01", 10.0)]["price"], 27.0)
        self.assertEqual(by_key[("24-MB04", 1.0)]["price"], 34.0)  # 15 % off 40
        self.assertEqual(by_key[("24-UG06", 1.0)]["price"], 33.0)
        self.assertTrue(all(t["customer_group"] == "Acme Shared" and t["price_type"] == "fixed" for t in tiers))
        companies = self.body(self.http.sent("POST", r"/assignCompanies$")[0])
        self.assertEqual(companies, {"companies": [{"id": 2}]})
        self.assertIn("4 price(s) (0 rejected)", catalog.last_result)

    # ------------------------------------------------------------------
    # Negotiable quotes
    # ------------------------------------------------------------------
    def quote_setup(self):
        for sku, magento_id in (("24-UG02", 21), ("24-UG03", 22)):
            self.create_map(self.create_product(sku), magento_id)
        self.import_company()

    def import_quote(self, data, comments=COMMENTS):
        self.http.add("GET", r"/rest/V1/negotiableQuote/%s/comments$" % data["id"], (200, comments))
        return self.instance._magento_b2b_import_quote_data(MagentoAPI(self.instance), data)

    def test_negotiable_quote_import_proposal_and_decline(self):
        self.quote_setup()
        quotation = self.import_quote(quote_json(price_type=1, price_value=2.5))
        self.assertEqual(quotation.state, "draft")
        self.assertEqual(quotation.magento_quote_id, "5")
        self.assertEqual(quotation.magento_quote_status, "submitted_by_customer")
        self.assertEqual(str(quotation.validity_date), "2026-12-31")
        self.assertEqual(quotation.partner_id.magento_customer_id, "5")
        self.assertEqual(quotation.partner_id.parent_id, self.b2b_company())
        lines = {l.product_id.default_code: l for l in quotation.order_line}
        self.assertEqual(lines["24-UG02"].product_uom_qty, 15)
        self.assertEqual(lines["24-UG02"].price_unit, 12.48)
        self.assertEqual(lines["24-UG02"].discount, 2.5)
        bodies = [m.body for m in quotation.message_ids]
        self.assertTrue(any("Requesting a 5% discount" in b for b in bodies))
        self.assertTrue(any("looking at it" in b and "&amp;#039;" not in b for b in bodies))

        # Re-sync: same lines, comments not duplicated; the buyer changed a quantity.
        notes_before = len(quotation.message_ids)
        data = quote_json(price_type=1, price_value=2.5)
        data["items"][1]["qty"] = 12
        self.import_quote(data)
        self.assertEqual(len(quotation.order_line), 2)
        self.assertEqual(lines["24-UG03"].product_uom_qty, 12)
        self.assertEqual(len(quotation.message_ids), notes_before)

        # Seller proposes 10 % on every line -> type 1.
        quotation.order_line.write({"discount": 10.0})
        wizard = self.env["magento.b2b.quote.wizard"].create({
            "order_id": quotation.id, "mode": "proposal", "comment": "10% off for this volume"})
        self.assertIn("10.0% discount", wizard.summary)
        wizard.action_confirm()
        self.http.add("PUT", r"/rest/V1/negotiableQuote/5$", (200, True))
        self.http.add("POST", r"/rest/V1/negotiableQuote/submitToCustomer$", (200, True))
        self.run_jobs("_magento_b2b_send_proposal")
        put = self.body(self.http.sent("PUT", r"/negotiableQuote/5$")[0])
        self.assertEqual(put, {"quote": {"id": 5, "extension_attributes": {"negotiable_quote": {
            "negotiated_price_type": 1, "negotiated_price_value": 10.0, "expiration_period": "2026-12-31"}}}})
        submit = self.body(self.http.sent("POST", r"/submitToCustomer$")[0])
        self.assertEqual(submit, {"quoteId": 5, "comment": "10% off for this volume"})
        self.assertEqual(quotation.magento_quote_status, "submitted_by_admin")
        self.assertFalse(quotation.magento_quote_seller_turn)

        # Buyer answers; seller changes one price -> type 3 proposed total.
        self.import_quote(quote_json(status="submitted_by_customer", price_type=1, price_value=10.0))
        lines["24-UG02"].write({"price_unit": 11.0, "discount": 0.0})
        values = quotation._magento_b2b_proposal_values()
        self.assertEqual(values["negotiated_price_type"], 3)
        self.assertAlmostEqual(values["negotiated_price_value"], round(15 * 11.0 + 10 * 17.16 * 0.9, 2))

        # Quantities are the buyer's: a change in Odoo is refused.
        lines["24-UG02"].product_uom_qty = 20
        with self.assertRaisesRegex(UserError, "quantity"):
            quotation._magento_b2b_proposal_values()
        lines["24-UG02"].product_uom_qty = 15

        # Decline needs a reason, then posts it and closes the quotation.
        wizard = self.env["magento.b2b.quote.wizard"].create({"order_id": quotation.id, "mode": "decline"})
        with self.assertRaisesRegex(UserError, "reason"):
            wizard.action_confirm()
        wizard.comment = "Your order is too large."
        wizard.action_confirm()
        self.http.add("POST", r"/rest/V1/negotiableQuote/decline$", (200, True))
        self.run_jobs("_magento_b2b_decline")
        decline = self.body(self.http.sent("POST", r"/negotiableQuote/decline$")[0])
        self.assertEqual(decline, {"quoteId": 5, "reason": "Your order is too large."})
        self.assertEqual(quotation.magento_quote_status, "declined")
        self.assertEqual(quotation.state, "cancel")

    def test_proposal_failure_is_recorded(self):
        self.quote_setup()
        quotation = self.import_quote(quote_json())
        quotation.order_line.write({"discount": 5.0})
        self.http.add("PUT", r"/rest/V1/negotiableQuote/5$",
                      (400, {"message": "The quote %1 is currently locked.", "parameters": ["5"]}))
        quotation._magento_b2b_send_proposal("hi")
        issue = self.Issue.search([("entity", "=", "b2b_quote"), ("magento_ref", "=", "5"), ("state", "=", "open")])
        self.assertEqual(len(issue), 1)
        self.assertEqual(quotation.magento_quote_status, "submitted_by_customer")
        self.assertFalse(self.http.sent("POST", r"/submitToCustomer$"))

    def test_placed_order_links_back_to_the_quotation(self):
        self.quote_setup()
        quotation = self.import_quote(quote_json())
        self.patch_order_side_calls()
        order_data = build_order(400, [build_item(1, "24-UG02", qty=15, price=12.48)], customer_id=5)
        order_data["quote_id"] = 5
        order_data["payment"]["method"] = "companycredit"
        term = self.env["account.payment.term"].create({"name": "On account"})
        self.instance.write({"b2b_credit_payment_term_id": term.id})
        self.import_orders([order_data])
        order = self.magento_orders(400)
        self.assertEqual(order.magento_source_quotation_id, quotation)
        self.assertEqual(quotation.magento_placed_order_ids, order)
        self.assertEqual(quotation.magento_quote_status, "ordered")
        self.assertEqual(quotation.state, "cancel")
        self.assertEqual(order.payment_term_id, term)

    # ------------------------------------------------------------------
    # Inert when disabled
    # ------------------------------------------------------------------
    def test_everything_is_inert_when_disabled(self):
        self.instance.write({"b2b_enabled": False})
        self.assertFalse(self.env.ref("sdlc_magento_connector.menu_magento_b2b").active)
        self.assertEqual(self.instance._magento_b2b_import_companies(),
                         "B2B is disabled on this instance: nothing imported.")
        self.instance._magento_b2b_import_shared_catalogs()
        self.instance._magento_b2b_import_quotes()
        with self.assertRaisesRegex(UserError, "Turn on Adobe Commerce B2B"):
            self.instance.action_b2b_import_companies()
        partner = self.env["res.partner"].create({
            "name": "Disabled Co", "is_company": True, "magento_company_instance_id": self.instance.id})
        self.assertFalse(partner._magento_b2b_export_company())
        self.create_map(self.create_product("24-UG02"), 21)
        self.patch_order_side_calls()
        order_data = build_order(401, [build_item(1, "24-UG02")])
        order_data["quote_id"] = 5
        self.import_orders([order_data])
        self.assertFalse(self.magento_orders(401).magento_source_quotation_id)
        probes = [p["label"] for p in self.instance._magento_access_probes()]
        self.assertFalse([label for label in probes if label.startswith("B2B")])
        self.assertFalse(self.http.requests, "B2B off: no request at all")
        self.assertFalse(self.Job.search([("method_name", "like", "_magento_b2b%")]))

        self.instance.write({"b2b_enabled": True})
        self.assertTrue(self.env.ref("sdlc_magento_connector.menu_magento_b2b").active)
