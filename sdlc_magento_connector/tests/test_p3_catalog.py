"""Roadmap 3.4 / 3.5: tier and customer-group price import into pricelists (idempotent,
removal, percentage tiers, no ping-pong with the price export) and bundle / grouped
product export (payloads, kit and link sources, missing components, unchanged -> no write).
"""
from unittest.mock import patch

import requests

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.models.magento_pricing import ALL_GROUPS

from .common import MagentoTestCommon, make_response


def _tier(sku, qty, price, group="all groups", price_type="fixed", website_id=0):
    """One row of POST /V1/products/tier-prices-information (Magento answers "all groups"
    in lower case)."""
    return {"sku": sku, "quantity": qty, "price": price, "price_type": price_type,
            "website_id": website_id, "customer_group": group}


def _not_found():
    return requests.exceptions.HTTPError(
        "404 Client Error", response=make_response(404, {"message": "The product that was requested doesn't exist."}))


@tagged("post_install", "-at_install", "magento")
class TestP3TierImport(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tracker = cls.env["magento.tier.price.import"]
        cls.Item = cls.env["product.pricelist.item"]
        cls.instance.website_id = 1
        cls.product_a = cls.create_product_cls("TP-A", 50.0)
        cls.product_b = cls.create_product_cls("TP-B", 20.0)
        cls.map_a = cls.create_map_cls(cls.product_a, 9101)
        cls.map_b = cls.create_map_cls(cls.product_b, 9102)
        cls.group_wholesale = cls.env["magento.customer.group"].create({
            "instance_id": cls.instance.id, "magento_group_id": 2, "name": "Wholesale",
        })
        cls.group_general = cls.env["magento.customer.group"].create({
            "instance_id": cls.instance.id, "magento_group_id": 1, "name": "General",
        })

    @classmethod
    def create_product_cls(cls, sku, price):
        return cls.env["product.product"].with_context(
            magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True,
        ).create({"name": sku, "default_code": sku, "type": "consu", "list_price": price}).with_env(cls.env)

    @classmethod
    def create_map_cls(cls, product, magento_id):
        return cls.env["magento.product.map"].with_context(
            magento_skip_push=True, skip_magento_sync=True, skip_odoo_sync=True,
        ).create({
            "instance_id": cls.instance.id, "magento_id": str(magento_id),
            "sku": product.default_code, "name": product.name, "odoo_product_id": product.id,
        }).with_env(cls.env)

    def setUp(self):
        super().setUp()
        self.tiers = self.patch_api("get_tier_prices", return_value=[])

    def _import(self, rows):
        self.patch_api("get_tier_prices", return_value=rows)
        return self.instance._magento_import_tier_prices()

    def _items(self, pricelist, product=None):
        domain = [("pricelist_id", "=", pricelist.id), ("applied_on", "=", "0_product_variant")]
        if product:
            domain.append(("product_id", "=", product.id))
        return self.Item.search(domain)

    # ------------------------------------------------------------------
    # ALL GROUPS / per group
    # ------------------------------------------------------------------
    def test_all_groups_tier_goes_to_instance_pricelist(self):
        summary = self._import([_tier("TP-A", 5, 40.0)])

        pricelist = self.instance.magento_tier_pricelist_id
        self.assertTrue(pricelist)
        self.assertEqual(pricelist.name, "Magento tier prices")
        item = self._items(pricelist, self.product_a)
        self.assertEqual(len(item), 1)
        self.assertEqual(item.applied_on, "0_product_variant")
        self.assertEqual(item.min_quantity, 5)
        self.assertEqual(item.compute_price, "fixed")
        self.assertEqual(item.fixed_price, 40.0)
        self.assertTrue(item.magento_tier_imported)
        self.assertEqual(pricelist._get_product_price(self.product_a, 5), 40.0)
        self.assertEqual(pricelist._get_product_price(self.product_a, 1), 50.0)
        tracker = self.Tracker.search([("instance_id", "=", self.instance.id)])
        self.assertEqual((tracker.sku, tracker.customer_group, tracker.item_id), ("TP-A", ALL_GROUPS, item))
        self.assertIn("1 created", summary)
        # one request for the page of mapped SKUs
        self.assertEqual(sorted(self.tiers.call_args.args[1]), ["TP-A", "TP-B"])
        self.assertEqual(self.instance.tier_import_count, 1)

    def test_group_tier_goes_to_group_pricelist(self):
        wholesale = self.env["product.pricelist"].create({"name": "Wholesale PL"})
        self.group_wholesale.pricelist_id = wholesale

        summary = self._import([
            _tier("TP-A", 10, 35.0, group="Wholesale"),
            _tier("TP-B", 3, 15.0, group="General"),  # no pricelist, option off: skipped
        ])

        item = self._items(wholesale, self.product_a)
        self.assertEqual((item.min_quantity, item.fixed_price), (10, 35.0))
        self.assertFalse(self.Tracker.search([("sku", "=", "TP-B")]))
        self.assertIn("1 skipped", summary)
        self.assertIn("customer group General has no pricelist", summary)

        # "Create Group Pricelists" on: a "Magento General" pricelist is created and set.
        self.instance.magento_tier_group_pricelists = True
        self._import([_tier("TP-A", 10, 35.0, group="Wholesale"), _tier("TP-B", 3, 15.0, group="General")])
        general = self.group_general.pricelist_id
        self.assertEqual(general.name, "Magento General")
        self.assertEqual(self._items(general, self.product_b).fixed_price, 15.0)
        # it falls back to the ALL GROUPS pricelist for everything else
        fallback = general.item_ids.filtered(lambda i: i.applied_on == "3_global")
        self.assertEqual(fallback.base_pricelist_id, self.instance.magento_tier_pricelist_id)
        self.assertEqual(general._get_product_price(self.product_b, 3), 15.0)
        self.assertEqual(general._get_product_price(self.product_b, 1), 20.0)

    def test_unknown_group_is_skipped(self):
        summary = self._import([_tier("TP-A", 2, 45.0, group="Retailer")])
        self.assertFalse(self.Tracker.search([]))
        self.assertIn("unknown customer group Retailer", summary)

    def test_website_tier_wins_and_other_websites_ignored(self):
        self._import([
            _tier("TP-A", 5, 42.0, website_id=0),
            _tier("TP-A", 5, 41.0, website_id=1),
            _tier("TP-A", 5, 30.0, website_id=7),
        ])
        item = self._items(self.instance.magento_tier_pricelist_id, self.product_a)
        self.assertEqual(item.fixed_price, 41.0)
        self.assertEqual(self.Tracker.search([]).website_id, 1)

    # ------------------------------------------------------------------
    # Idempotency / removal / percentage
    # ------------------------------------------------------------------
    def test_reimport_is_idempotent_and_removes_gone_tiers(self):
        rows = [_tier("TP-A", 5, 40.0), _tier("TP-A", 10, 38.0), _tier("TP-B", 4, 18.0)]
        self._import(rows)
        pricelist = self.instance.magento_tier_pricelist_id
        items = self._items(pricelist)
        self.assertEqual(len(items), 3)

        summary = self._import(rows)
        self.assertIn("0 created, 0 updated, 3 unchanged, 0 removed", summary)
        self.assertEqual(self._items(pricelist), items)

        # price changed in Magento: same item updated
        item_10 = items.filtered(lambda i: i.min_quantity == 10)
        summary = self._import([_tier("TP-A", 5, 40.0), _tier("TP-A", 10, 36.5), _tier("TP-B", 4, 18.0)])
        self.assertIn("1 updated", summary)
        self.assertEqual(item_10.fixed_price, 36.5)

        # the qty 10 tier is gone from Magento: its item and tracker go too
        summary = self._import([_tier("TP-A", 5, 40.0), _tier("TP-B", 4, 18.0)])
        self.assertIn("1 removed", summary)
        self.assertFalse(item_10.exists())
        self.assertEqual(len(self._items(pricelist)), 2)
        self.assertEqual(len(self.Tracker.search([])), 2)

        # a SKU no longer mapped: its tiers are removed on the next import
        self.map_b.with_context(**self.no_push_ctx()).unlink()
        self._import([_tier("TP-A", 5, 40.0)])
        self.assertFalse(self._items(pricelist, self.product_b))
        self.assertEqual(self.Tracker.search([]).sku, "TP-A")

    def test_percentage_tier(self):
        self._import([_tier("TP-A", 10, 10.0, price_type="discount")])
        pricelist = self.instance.magento_tier_pricelist_id
        item = self._items(pricelist, self.product_a)
        self.assertEqual((item.compute_price, item.percent_price, item.min_quantity), ("percentage", 10.0, 10))
        self.assertAlmostEqual(pricelist._get_product_price(self.product_a, 10), 45.0)
        # switching to a fixed tier rewrites the same item
        self._import([_tier("TP-A", 10, 44.0)])
        self.assertEqual((item.compute_price, item.fixed_price), ("fixed", 44.0))
        self.assertEqual(self.Tracker.search([]).price_type, "fixed")

    def test_existing_item_is_adopted_not_duplicated(self):
        pricelist = self.env["product.pricelist"].create({"name": "Mine", "item_ids": [Command.create({
            "applied_on": "0_product_variant", "product_id": self.product_a.id,
            "min_quantity": 5, "compute_price": "fixed", "fixed_price": 41.0,
        })]})
        self.instance.magento_tier_pricelist_id = pricelist
        own = pricelist.item_ids
        summary = self._import([_tier("TP-A", 5, 40.0)])
        self.assertEqual(self._items(pricelist, self.product_a), own)
        self.assertEqual(own.fixed_price, 40.0)
        self.assertIn("1 updated", summary)

    # ------------------------------------------------------------------
    # No ping-pong with the price export
    # ------------------------------------------------------------------
    def test_imported_tiers_are_not_pushed_back(self):
        sent = self.patch_api("update_tier_prices", return_value=[])
        deleted = self.patch_api("delete_tier_prices", return_value=[])
        self._import([_tier("TP-A", 5, 40.0), _tier("TP-A", 10, 10.0, price_type="discount")])
        pricelist = self.instance.magento_tier_pricelist_id
        rule = self.env["magento.price.rule"].create({
            "name": "Tiers", "instance_id": self.instance.id, "pricelist_id": pricelist.id, "kind": "tier",
        })
        # a tier the rule pushed earlier for the same key
        self.env["magento.price.push.state"].create({
            "rule_id": rule.id, "sku": "TP-A", "key": "tier|w0|ALL GROUPS|5", "kind": "tier",
            "price": 40.0, "customer_group": ALL_GROUPS, "quantity": 5,
        })

        summary = rule._magento_push_prices()[rule.id]

        self.assertFalse(sent.called)
        self.assertFalse(deleted.called)
        self.assertIn("2 imported from Magento, not pushed back", summary)

        # An Odoo-made tier in the same pricelist is still pushed.
        self.Item.create({
            "pricelist_id": pricelist.id, "applied_on": "0_product_variant",
            "product_id": self.product_b.id, "min_quantity": 3, "compute_price": "fixed", "fixed_price": 17.0,
        })
        rule._magento_push_prices()
        self.assertEqual([(p["sku"], p["quantity"]) for p in sent.call_args.args[1]], [("TP-B", 3.0)])

        # Removed in Magento: the item goes, the rule's state too, and nothing is deleted.
        sent.reset_mock()
        self._import([_tier("TP-A", 10, 10.0, price_type="discount")])
        self.assertFalse(rule.push_state_ids.filtered(lambda s: s.sku == "TP-A"))
        rule._magento_push_prices()
        self.assertFalse(deleted.called)
        self.assertFalse(sent.called)

    # ------------------------------------------------------------------
    # Product sync option / queue
    # ------------------------------------------------------------------
    def test_product_sync_imports_tiers_when_enabled(self):
        data = {"sku": "TP-A", "tier_prices": [
            {"customer_group_id": 32000, "qty": 5, "value": 40, "extension_attributes": {"website_id": 0}},
            {"customer_group_id": 2, "qty": 10, "value": 45, "extension_attributes": {
                "percentage_value": 10, "website_id": 0}},
        ]}
        self.instance._magento_after_product_import(self.map_a, data)
        self.assertFalse(self.Tracker.search([]), "import_tier_prices is off by default")

        wholesale = self.env["product.pricelist"].create({"name": "Wholesale PL"})
        self.group_wholesale.pricelist_id = wholesale
        self.instance.import_tier_prices = True
        self.instance._magento_after_product_import(self.map_a, data)
        self.assertEqual(self._items(self.instance.magento_tier_pricelist_id, self.product_a).fixed_price, 40.0)
        group_item = self._items(wholesale, self.product_a)
        self.assertEqual((group_item.compute_price, group_item.percent_price), ("percentage", 10.0))
        self.assertFalse(self.tiers.called, "the product payload carries the tiers")

    def test_action_queues_paged_jobs(self):
        self.instance.run_syncs_in_background = True
        Job = self.env["magento.job"]
        self.instance.with_context(magento_sync_queue=True).action_import_tier_prices()
        lister = Job.search([("identity_key", "=", "tier-import-%s" % self.instance.id)])
        self.assertEqual(lister.method_name, "_magento_enqueue_tier_import")

        with patch("odoo.addons.sdlc_magento_connector.models.magento_catalog_p3.TIER_IMPORT_PAGE", 1):
            message = self.instance._magento_enqueue_tier_import()
        pages = Job.search([("method_name", "=", "_magento_import_tier_prices_page"),
                            ("instance_id", "=", self.instance.id)])
        self.assertEqual(sorted(job.args[0][0] for job in pages), ["TP-A", "TP-B"])
        self.assertIn("2 job(s)", message)
        self.assertFalse(self.tiers.called)

        self.patch_api("get_tier_prices", return_value=[_tier("TP-A", 5, 40.0)])
        pages.filtered(lambda j: j.args[0] == ["TP-A"])._run_job()
        self.assertEqual(self.Tracker.search([]).sku, "TP-A")


@tagged("post_install", "-at_install", "magento")
class TestP3CompositeExport(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = {"magento_skip_push": True, "skip_magento_sync": True, "skip_odoo_sync": True}
        cls.attribute_set = cls.env["magento.attribute.set"].with_context(**ctx).create({
            "instance_id": cls.instance.id, "magento_id": "4", "name": "Default",
        })
        Product = cls.env["product.product"].with_context(**ctx)
        Map = cls.env["magento.product.map"].with_context(**ctx)
        cls.comp_1 = Product.create({"name": "Strap", "default_code": "CMP-1", "type": "consu"})
        cls.comp_2 = Product.create({"name": "Bottle", "default_code": "CMP-2", "type": "consu"})
        for index, product in enumerate((cls.comp_1, cls.comp_2), start=1):
            Map.create({"instance_id": cls.instance.id, "magento_id": str(800 + index),
                        "sku": product.default_code, "name": product.name, "odoo_product_id": product.id,
                        "attribute_set_id": cls.attribute_set.id})
        cls.parent = Product.create({"name": "Kit Parent", "default_code": "PACK-1", "type": "consu",
                                     "list_price": 99.0})
        cls.ctx = ctx

    def setUp(self):
        super().setUp()
        self.types = self.patch_api("get_product_types_by_sku",
                                    return_value={"CMP-1": "simple", "CMP-2": "virtual"})
        self.get = self.patch_api("get_product", side_effect=_not_found())
        self.create = self.patch_api("create_product", return_value={"id": 990, "sku": "PACK-1"})
        self.update = self.patch_api("update_product", return_value={"id": 990, "sku": "PACK-1"})

    def _map(self, type_id, links=()):
        product_map = self.env["magento.product.map"].with_context(**self.ctx).create({
            "instance_id": self.instance.id, "sku": "PACK-1", "name": "Kit Parent", "price": 99.0,
            "type_id": type_id, "odoo_product_id": self.parent.id, "attribute_set_id": self.attribute_set.id,
        })
        for vals in links:
            self.env["magento.product.link"].create(dict(vals, parent_map_id=product_map.id, link_type=type_id))
        return product_map.with_env(self.env)

    def _sent(self, mock):
        return mock.call_args.args[-1]["product"]

    def test_grouped_export_payload(self):
        product_map = self._map("grouped", [
            {"child_sku": "CMP-2", "qty": 2.0, "position": 2},
            {"child_sku": "CMP-1", "qty": 1.0, "position": 1},
        ])

        self.assertEqual(product_map._magento_export_composite(), "created")

        product = self._sent(self.create)
        self.assertEqual(product["type_id"], "grouped")
        self.assertEqual(product["sku"], "PACK-1")
        self.assertNotIn("weight", product)
        self.assertEqual(product["product_links"], [
            {"sku": "PACK-1", "link_type": "associated", "linked_product_sku": "CMP-1",
             "linked_product_type": "simple", "position": 1, "extension_attributes": {"qty": 1.0}},
            {"sku": "PACK-1", "link_type": "associated", "linked_product_sku": "CMP-2",
             "linked_product_type": "virtual", "position": 2, "extension_attributes": {"qty": 2.0}},
        ])
        self.assertEqual(self.types.call_args.args[1], ["CMP-1", "CMP-2"])
        self.assertEqual(product_map.magento_id, "990")
        self.assertTrue(product_map.magento_composite_hash)
        self.assertFalse(self.update.called)

    def test_bundle_export_from_links(self):
        self.parent.product_tmpl_id.magento_bundle_price_type = "fixed"
        product_map = self._map("bundle", [
            {"child_sku": "CMP-1", "qty": 1.0, "option_title": "Strap", "option_type": "radio",
             "option_required": True, "is_default": True, "price": 5.0, "price_type": "0", "position": 1},
            {"child_sku": "CMP-2", "qty": 2.0, "option_title": "Extras", "option_type": "checkbox",
             "option_position": 2, "can_change_quantity": True, "position": 1},
        ])

        product_map.action_create_magento_product()

        product = self._sent(self.create)
        self.assertEqual(product["type_id"], "bundle")
        self.assertEqual(product["price"], 99.0)
        attrs = {a["attribute_code"]: a["value"] for a in product["custom_attributes"]}
        self.assertEqual((attrs["price_type"], attrs["sku_type"], attrs["shipment_type"]), ("1", "0", "0"))
        options = product["extension_attributes"]["bundle_product_options"]
        self.assertEqual([(o["title"], o["type"], o["required"], o["sku"]) for o in options],
                         [("Strap", "radio", True, "PACK-1"), ("Extras", "checkbox", False, "PACK-1")])
        self.assertEqual(options[0]["product_links"], [{
            "sku": "CMP-1", "qty": 1.0, "position": 1, "is_default": True, "price": 5.0,
            "price_type": 0, "can_change_quantity": 0}])
        self.assertEqual(options[1]["product_links"][0]["can_change_quantity"], 1)
        self.assertNotIn("qty", product["extension_attributes"].get("stock_item", {}))

    def test_bundle_export_from_kit(self):
        template = self.parent.product_tmpl_id
        template.magento_composite_type = "bundle"
        kit = [(self.comp_1, 1.0), (self.comp_2, 3.0)]
        if "mrp.bom" in self.env:
            self.env["mrp.bom"].create({
                "product_tmpl_id": template.id, "type": "phantom", "product_qty": 1.0,
                "bom_line_ids": [Command.create({"product_id": p.id, "product_qty": q}) for p, q in kit],
            })
            patcher = None
        else:
            Map = type(self.env["magento.product.map"])
            patcher = patch.object(Map, "_magento_kit_lines", lambda self, product, sku=None: kit)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.assertIn("From the kit", template.magento_composite_preview)
        template.action_magento_export_composite()

        product_map = self.env["magento.product.map"].search([("sku", "=", "PACK-1")])
        self.assertEqual((product_map.type_id, product_map.instance_id), ("bundle", self.instance))
        product = self._sent(self.create)
        self.assertEqual(product["price"], 0.0)  # dynamic price
        options = product["extension_attributes"]["bundle_product_options"]
        self.assertEqual([(o["title"], o["type"], o["required"]) for o in options],
                         [("Strap", "select", True), ("Bottle", "select", True)])
        self.assertEqual([(l["sku"], l["qty"], l["is_default"]) for o in options for l in o["product_links"]],
                         [("CMP-1", 1.0, True), ("CMP-2", 3.0, True)])

    def test_missing_components_are_listed(self):
        self.patch_api("get_product_types_by_sku", return_value={"CMP-1": "simple"})
        product_map = self._map("grouped", [
            {"child_sku": "CMP-1", "position": 1}, {"child_sku": "CMP-404", "position": 2},
            {"child_sku": "CMP-405", "position": 3},
        ])
        with self.assertRaisesRegex(UserError, "do not exist in Magento: CMP-404, CMP-405"):
            product_map._magento_export_composite()
        self.assertFalse(self.create.called)
        self.assertFalse(self.update.called)

    def test_no_components_and_configurable_still_blocked(self):
        with self.assertRaisesRegex(UserError, "no component"):
            self._map("bundle")._magento_export_composite()
        configurable = self.env["magento.product.map"].with_context(**self.ctx).create({
            "instance_id": self.instance.id, "sku": "CONF-1", "name": "Conf", "type_id": "configurable",
        })
        with self.assertRaisesRegex(UserError, "not supported"):
            configurable._check_exportable()

    def test_unchanged_sends_nothing_and_changes_replace(self):
        product_map = self._map("bundle", [
            {"child_sku": "CMP-1", "qty": 1.0, "option_title": "Strap", "option_required": True,
             "is_default": True, "position": 1},
        ])
        product_map._magento_export_composite()
        sent = self._sent(self.create)
        live = {
            "id": 990, "sku": "PACK-1", "type_id": "bundle",
            "extension_attributes": {"bundle_product_options": [{
                "option_id": 7, "title": "Strap", "required": True, "type": "select", "position": 1,
                "sku": "PACK-1", "product_links": [{
                    "id": "70", "sku": "CMP-1", "option_id": 7, "qty": 1, "position": 1,
                    "is_default": True, "price": 0, "price_type": 0, "can_change_quantity": 0}],
            }]},
        }
        self.patch_api("get_product", return_value=live)
        self.create.reset_mock()

        self.assertEqual(product_map._magento_export_composite(), "unchanged")
        self.assertFalse(self.create.called)
        self.assertFalse(self.update.called)

        # Odoo-side change of the product data only: sent without touching the options.
        product_map.with_context(**self.ctx).write({"name": "Kit Parent v2"})
        self.assertEqual(product_map._magento_export_composite(), "updated")
        product = self._sent(self.update)
        self.assertEqual(product["name"], "Kit Parent v2")
        self.assertNotIn("bundle_product_options", product.get("extension_attributes") or {})

        # Component change: the options are sent (and replace Magento's).
        self.update.reset_mock()
        product_map.child_link_ids.qty = 2.0
        self.assertEqual(product_map._magento_export_composite(), "updated")
        options = self._sent(self.update)["extension_attributes"]["bundle_product_options"]
        self.assertEqual(options[0]["product_links"][0]["qty"], 2.0)
        self.assertNotIn("option_id", options[0])
        self.assertEqual(sent["type_id"], "bundle")

    def test_grouped_update_keeps_other_links_and_refuses_type_change(self):
        product_map = self._map("grouped", [{"child_sku": "CMP-1", "qty": 1.0, "position": 1}])
        self.patch_api("get_product", return_value={
            "id": 990, "sku": "PACK-1", "type_id": "grouped", "product_links": [
                {"sku": "PACK-1", "link_type": "related", "linked_product_sku": "CMP-2",
                 "linked_product_type": "virtual", "position": 1},
            ]})
        product_map._magento_export_composite()
        links = self._sent(self.update)["product_links"]
        self.assertEqual([(l["link_type"], l["linked_product_sku"]) for l in links],
                         [("related", "CMP-2"), ("associated", "CMP-1")])

        self.patch_api("get_product", return_value={"id": 990, "sku": "PACK-1", "type_id": "simple"})
        with self.assertRaisesRegex(UserError, "is a simple product"):
            product_map._magento_export_composite()

    def test_import_sets_template_flag(self):
        product_map = self._map("bundle")
        product_map._magento_sync_composite({
            "type_id": "bundle",
            "custom_attributes": [{"attribute_code": "price_type", "value": "1"}],
            "extension_attributes": {"bundle_product_options": []},
        })
        template = self.parent.product_tmpl_id
        self.assertEqual((template.magento_composite_type, template.magento_bundle_price_type), ("bundle", "fixed"))
