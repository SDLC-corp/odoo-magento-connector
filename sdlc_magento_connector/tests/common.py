"""Shared fixtures and Magento 2 REST payload builders for the connector tests.

No test in this package may reach the network:

* ``requests.sessions.Session.send`` is patched for every test and raises
  ``RealHttpBlocked`` (a ``requests.ConnectionError``). Each attempt is
  recorded in ``self.http_attempts``, so a test can assert that an operation
  made zero HTTP calls.
* ``MagentoAPI`` methods are mocked per test with
  ``unittest.mock.patch.object`` through ``self.patch_api``.
"""
import json
from contextlib import contextmanager
from unittest.mock import patch

import requests
from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.services.magento_api import MagentoAPI

MAGENTO_BASE_URL = "https://magento.example.test"


class RealHttpBlocked(requests.exceptions.ConnectionError):
    """Raised when code under test tries real HTTP that was not mocked."""


def once(value):
    """side_effect for a list-returning API method: ``value`` on the first call, [] afterwards.

    Protects the tests against infinite loops if a fetch becomes paginated
    ("call until a short page is returned").
    """
    state = {"called": False}

    def _side_effect(*args, **kwargs):
        if state["called"]:
            return []
        state["called"] = True
        return value

    return _side_effect


def make_response(status=200, payload=None, url="https://magento.example.test/rest/V1/x"):
    response = requests.models.Response()
    response.status_code = status
    response._content = json.dumps(payload if payload is not None else {}).encode()
    response.headers["Content-Type"] = "application/json"
    response.url = url
    response.encoding = "utf-8"
    return response


# ---------------------------------------------------------------------------
# Magento 2 REST JSON builders (shape of GET /V1/products and /V1/orders)
# ---------------------------------------------------------------------------

def build_product(magento_id, sku, name=None, type_id="simple", status=1, price=10.0,
                  qty=5.0, attribute_set_id=4, visibility=4):
    return {
        "id": magento_id,
        "sku": sku,
        "name": name or sku,
        "attribute_set_id": attribute_set_id,
        "price": price,
        "status": status,
        "visibility": visibility,
        "type_id": type_id,
        "created_at": "2026-09-01 08:00:00",
        "updated_at": "2026-09-02 08:00:00",
        "weight": 1.0,
        "extension_attributes": {
            "website_ids": [1],
            "stock_item": {
                "item_id": magento_id,
                "product_id": magento_id,
                "stock_id": 1,
                "qty": qty,
                "is_in_stock": qty > 0,
            },
        },
        "product_links": [],
        "options": [],
        "media_gallery_entries": [],
        "tier_prices": [],
        "custom_attributes": [
            {"attribute_code": "description", "value": "<p>%s</p>" % (name or sku)},
            {"attribute_code": "tax_class_id", "value": "2"},
        ],
    }


def build_address(firstname="Veronica", lastname="Costello", email="roni_cost@example.com",
                  street=("6146 Honey Bluff Parkway",), city="Calder", postcode="49628-7978",
                  country_id="US", region="Michigan", telephone="(555) 229-3326",
                  address_type="billing", parent_id=1):
    return {
        "address_type": address_type,
        "city": city,
        "country_id": country_id,
        "email": email,
        "entity_id": parent_id * 10 + (1 if address_type == "billing" else 2),
        "firstname": firstname,
        "lastname": lastname,
        "parent_id": parent_id,
        "postcode": postcode,
        "region": region,
        "region_code": "MI",
        "street": list(street),
        "telephone": telephone,
    }


def build_item(item_id, sku, qty=1.0, price=10.0, product_id=None, product_type="simple",
               parent_item_id=None, name=None):
    item = {
        "item_id": item_id,
        "order_id": 1,
        "product_id": product_id,
        "sku": sku,
        "name": name or sku,
        "product_type": product_type,
        "qty_ordered": qty,
        "qty_invoiced": 0,
        "qty_shipped": 0,
        "price": price,
        "base_price": price,
        "row_total": price * qty,
        "tax_amount": 0,
        "discount_amount": 0,
    }
    if parent_item_id:
        item["parent_item_id"] = parent_item_id
    return item


def build_order(entity_id, items, increment_id=None, email="roni_cost@example.com",
                firstname="Veronica", lastname="Costello", guest=False, customer_id=None,
                billing=None, shipping=None, status="pending", state="new",
                currency="USD", created_at="2026-10-01 10:00:00"):
    increment_id = increment_id or "%09d" % (100000000 + entity_id)
    if billing is None:
        billing = build_address(firstname=firstname, lastname=lastname, email=email,
                                parent_id=entity_id)
    if shipping is None:
        shipping = dict(billing, address_type="shipping")
    grand_total = sum(float(i.get("price") or 0) * float(i.get("qty_ordered") or 0) for i in items)
    for item in items:
        item["order_id"] = entity_id
    order = {
        "entity_id": entity_id,
        "increment_id": increment_id,
        "state": state,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "customer_email": email,
        "customer_firstname": None if guest else firstname,
        "customer_lastname": None if guest else lastname,
        "customer_is_guest": 1 if guest else 0,
        "customer_group_id": 0 if guest else 1,
        "order_currency_code": currency,
        "base_currency_code": currency,
        "store_id": 1,
        "grand_total": grand_total,
        "base_grand_total": grand_total,
        "subtotal": grand_total,
        "total_invoiced": 0,
        "total_paid": 0,
        "total_qty_ordered": sum(float(i.get("qty_ordered") or 0) for i in items),
        "items": items,
        "billing_address": billing,
        "payment": {"method": "checkmo", "amount_ordered": grand_total},
        "status_histories": [],
        "extension_attributes": {
            "shipping_assignments": [{
                "shipping": {
                    "address": shipping,
                    "method": "flatrate_flatrate",
                    "total": {"shipping_amount": 0},
                },
                "items": items,
            }],
        },
    }
    if not guest:
        order["customer_id"] = customer_id or entity_id + 1000
    return order


class MagentoTestCommon(TransactionCase):
    """Base class: one Magento instance, a product with a map, HTTP blocked."""

    @contextmanager
    def enter_registry_test_mode(self):
        """Odoo 19's ``TransactionCase.enter_registry_test_mode`` on Odoo 18: new
        cursors (e.g. a post-commit push) share the test transaction meanwhile."""
        registry = self.env.registry
        if registry.in_test_mode():
            yield
            return
        registry.enter_test_mode(self.env.cr)
        try:
            yield
        finally:
            registry.leave_test_mode()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(
            cls.env.context,
            tracking_disable=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            mail_notrack=True,
            no_reset_password=True,
        ))
        cls.company = cls.env.company
        cls.instance = cls.create_instance("Main Magento")
        cls.country_us = cls.env.ref("base.us")

    @classmethod
    def create_instance(cls, name, company=None, **extra):
        vals = {
            "name": name,
            "base_url": MAGENTO_BASE_URL,
            "access_token": "test-integration-token",
            "verify_ssl": True,
            "company_id": (company or cls.env.company).id,
        }
        vals.update(extra)
        return cls.env["magento.instance"].create(vals)

    def setUp(self):
        super().setUp()
        self.http_attempts = []
        self._api_mocks = {}
        test = self

        def _blocked_send(session, prepared_request, **kwargs):
            test.http_attempts.append((prepared_request.method, prepared_request.url))
            raise RealHttpBlocked(
                "Unmocked HTTP in Magento test: %s %s" % (prepared_request.method, prepared_request.url)
            )

        self.startPatcher(patch.object(requests.sessions.Session, "send", _blocked_send))
        # MagentoAPI retries transient errors with exponential backoff: never sleep in tests.
        if hasattr(MagentoAPI, "_sleep"):
            self.startPatcher(patch.object(MagentoAPI, "_sleep", lambda api, seconds: None))
        self.startPatcher(patch("time.sleep", lambda seconds: None))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def no_push_ctx():
        """Context keys that stop every outbound push (legacy and Phase 0 names)."""
        return {"skip_magento_sync": True, "magento_skip_push": True}

    def patch_api(self, method, **kwargs):
        """patch.object(MagentoAPI, method, autospec=True, **kwargs), stopped at test end.

        Returns the mock; ``mock.call_args.args[0]`` is the MagentoAPI instance.
        Patching a method twice in one test reconfigures the existing mock.
        """
        mock = self._api_mocks.get(method)
        if mock is not None:
            inner = getattr(mock, "mock", mock)  # autospec'd functions wrap a Mock
            inner.reset_mock(return_value=True, side_effect=True)
            # An autospec'd function keeps its own side_effect: clear it explicitly,
            # or switching from side_effect=... to return_value=... has no effect.
            kwargs.setdefault("side_effect", None)
            inner.configure_mock(**kwargs)
            return mock
        if not hasattr(MagentoAPI, method):
            # The method was renamed or removed: still install a mock so a call
            # is visible, but don't fail the fixture itself.
            mock = self.startPatcher(patch.object(MagentoAPI, method, create=True, **kwargs))
        else:
            mock = self.startPatcher(patch.object(MagentoAPI, method, autospec=True, **kwargs))
        self._api_mocks[method] = mock
        return mock

    def patch_order_side_calls(self):
        """Neutralise the per-order follow-up fetches done by sync_orders."""
        for name in ("get_invoices", "get_credit_memos", "get_shipments",
                     "get_order_transactions"):
            if hasattr(MagentoAPI, name):
                self.patch_api(name, return_value=[])

    def create_product(self, sku, name=None, **extra):
        if "type" in extra:  # Odoo 17: the product type is written through detailed_type
            extra["detailed_type"] = extra.pop("type")
        vals = {"name": name or sku, "default_code": sku, "detailed_type": "consu", "list_price": 10.0}
        vals.update(extra)
        product = self.env["product.product"].with_context(
            **self.no_push_ctx(), skip_odoo_sync=True).create(vals)
        return product.with_env(self.env)  # drop the skip flags for later writes

    def create_map(self, product, magento_id, sku=None, instance=None, **extra):
        vals = {
            "instance_id": (instance or self.instance).id,
            "magento_id": str(magento_id),
            "sku": sku or product.default_code,
            "name": product.name,
            "odoo_product_id": product.id,
        }
        vals.update(extra)
        product_map = self.env["magento.product.map"].with_context(
            **self.no_push_ctx(), skip_odoo_sync=True
        ).create(vals)
        return product_map.with_env(self.env)

    def import_orders(self, orders, instance=None):
        """Run the order import (instance.sync_orders) on the given Magento order JSON."""
        instance = instance or self.instance
        self.patch_api("get_orders", side_effect=once(list(orders)))
        return instance.sync_orders()

    def magento_orders(self, magento_order_id, instance=None):
        instance = instance or self.instance
        return self.env["sale.order"].search([
            ("magento_instance_id", "=", instance.id),
            ("magento_order_id", "=", str(magento_order_id)),
        ])

    def assertDuplicateRejected(self, create_fn, msg=None):
        """create_fn must be rejected by a DB unique constraint or a ValidationError."""
        rejected = False
        try:
            with mute_logger("odoo.sql_db", "odoo.models"), self.env.cr.savepoint():
                create_fn()
                self.env.flush_all()
        except (IntegrityError, ValidationError):
            rejected = True
        self.env.invalidate_all()
        self.assertTrue(rejected, msg or "Duplicate (instance, magento id) was accepted")

    @classmethod
    def create_user(cls, login, groups, company=None, companies=None):
        company = company or cls.env.company
        companies = companies or company
        return new_test_user(
            cls.env, login=login, groups=groups,
            company_id=company.id, company_ids=[(6, 0, companies.ids)],
        )
