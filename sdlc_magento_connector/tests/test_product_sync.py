from odoo.tests import tagged

from .common import MagentoTestCommon, build_product, once


@tagged("post_install", "-at_install", "magento")
class TestProductSync(MagentoTestCommon):
    """A1 (per-record isolation, unsupported types) and A2 (archived product duplicates)."""

    def _patch_catalog(self, products, broken_skus=()):
        by_sku = {p["sku"]: p for p in products}

        def _get_product(api, sku, *args, **kwargs):
            if sku in broken_skus:
                raise RuntimeError("Simulated processing failure for %s" % sku)
            return by_sku[sku]

        self.patch_api("get_products", side_effect=once(list(products)))
        self.patch_api("get_product", side_effect=_get_product)
        self.patch_api("get_product_media", return_value={})

    def _products(self, sku):
        return self.env["product.product"].with_context(active_test=False).search(
            [("default_code", "=", sku)]
        )

    def _maps(self, sku):
        return self.env["magento.product.map"].search(
            [("instance_id", "=", self.instance.id), ("sku", "=", sku)]
        )

    def test_a1_one_failing_product_does_not_abort_the_run(self):
        catalog = [
            build_product(1, "24-MB01", "Joust Duffle Bag"),
            build_product(2, "24-MB-BROKEN", "Broken Bag"),
            build_product(3, "24-MB04", "Strive Shoulder Pack"),
        ]
        self._patch_catalog(catalog, broken_skus={"24-MB-BROKEN"})

        # Must not raise although one product's processing raises.
        self.instance.sync_products()

        for sku in ("24-MB01", "24-MB04"):
            self.assertEqual(len(self._products(sku)), 1, "%s should be imported" % sku)
            self.assertEqual(len(self._maps(sku)), 1, "%s should be mapped" % sku)

    def test_a1_failing_product_is_reported(self):
        catalog = [
            build_product(1, "24-MB01", "Joust Duffle Bag"),
            build_product(2, "24-MB-BROKEN", "Broken Bag"),
        ]
        Report = self.env["magento.sync.report"]
        before = Report.search([("instance_id", "=", self.instance.id)])

        self._patch_catalog(catalog, broken_skus={"24-MB-BROKEN"})
        self.instance.sync_products()
        # The UI entry point must report the run as well.
        self._patch_catalog(catalog, broken_skus={"24-MB-BROKEN"})
        self.instance.action_sync_products()

        reports = Report.search([("instance_id", "=", self.instance.id)]) - before
        self.assertTrue(reports, "A sync report must be created for the run")
        self.assertGreaterEqual(
            max(reports.mapped("errors")), 1,
            "The sync report must count the failed product (errors >= 1)",
        )

    def test_a1_unsupported_types_are_skipped(self):
        # Configurable, bundle and grouped are imported since Phase 2 (test_p2_catalog);
        # other types (gift cards...) and a configurable without options are skipped.
        catalog = [
            build_product(10, "MH01", "Chaz Kangeroo Hoodie", type_id="configurable"),
            build_product(11, "GIFT-25", "Gift Card", type_id="giftcard"),
            build_product(13, "MH01-XS-Black", "Chaz Kangeroo Hoodie-XS-Black"),
        ]
        self._patch_catalog(catalog)

        result = self.instance.sync_products()  # must not crash
        self.assertEqual(result["skipped"], 2, "Unsupported products are counted as skipped")

        for sku in ("MH01", "GIFT-25"):
            self.assertFalse(self._maps(sku), "%s (unsupported) must be skipped" % sku)
            self.assertFalse(self._products(sku), "%s must not create an Odoo product" % sku)
        self.assertEqual(len(self._maps("MH01-XS-Black")), 1, "simple child product is imported")

    def test_a2_disabled_product_linked_to_archived_product_not_duplicated(self):
        product = self.create_product("24-UG06", "Affirm Water Bottle")
        product_map = self.create_map(product, 50, status="2", enable_product=False)
        product.with_context(**self.no_push_ctx(), skip_odoo_sync=True).write({"active": False})
        self.assertFalse(product.active)

        Product = self.env["product.product"].with_context(active_test=False)
        count_before = Product.search_count([])

        self._patch_catalog([build_product(50, "24-UG06", "Affirm Water Bottle", status=2)])
        self.instance.sync_products()

        self.assertEqual(Product.search_count([]), count_before,
                         "Syncing a disabled product must not duplicate its archived Odoo product")
        self.assertEqual(len(self._products("24-UG06")), 1)
        self.assertEqual(product_map.odoo_product_id, product,
                         "The map must stay linked to the archived product")
