import requests

from odoo.tests import tagged

from .common import MagentoTestCommon, build_product, make_response, once


@tagged("post_install", "-at_install", "magento")
class TestErrorCenterWiring(MagentoTestCommon):
    """The sync engine feeds magento.sync.issue: failures open issues, successes resolve them."""

    def _patch_catalog(self, products, broken_skus=()):
        by_sku = {p["sku"]: p for p in products}

        def _get_product(api, sku, *args, **kwargs):
            if sku in broken_skus:
                raise RuntimeError("Simulated processing failure for %s" % sku)
            return by_sku[sku]

        self.patch_api("get_products", side_effect=once(list(products)))
        self.patch_api("get_product", side_effect=_get_product)
        self.patch_api("get_product_media", return_value={})

    def _issues(self, entity, ref, state="open"):
        return self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id),
            ("entity", "=", entity),
            ("magento_ref", "=", ref),
            ("state", "=", state),
        ])

    def test_failed_product_opens_issue_and_success_resolves_it(self):
        catalog = [
            build_product(1, "24-MB01", "Joust Duffle Bag"),
            build_product(2, "24-MB-BROKEN", "Broken Bag"),
        ]
        self._patch_catalog(catalog, broken_skus={"24-MB-BROKEN"})
        self.instance.sync_products()

        issue = self._issues("product", "24-MB-BROKEN")
        self.assertEqual(len(issue), 1, "the failing product must open one issue keyed by SKU")
        self.assertIn("Simulated processing failure", issue.message)
        self.assertFalse(self._issues("product", "24-MB01"), "a successful product opens no issue")

        # Second failing run: same issue, more occurrences.
        self._patch_catalog(catalog, broken_skus={"24-MB-BROKEN"})
        self.instance.with_context(magento_full_sync=True).sync_products()
        self.assertEqual(self._issues("product", "24-MB-BROKEN"), issue)
        self.assertGreaterEqual(issue.occurrences, 2)

        # The product syncs fine now: the issue resolves itself.
        self._patch_catalog(catalog)
        self.instance.with_context(magento_full_sync=True).sync_products()
        self.assertEqual(issue.state, "resolved")

    def test_listing_auth_error_opens_list_issue_and_flags_instance(self):
        response = make_response(status=401, payload={"message": "The consumer isn't authorized"})
        self.patch_api("get_products", side_effect=requests.HTTPError(response=response))
        self.instance.sync_products()

        issue = self._issues("product", "list")
        self.assertEqual(len(issue), 1, "a failed listing opens a 'list' issue")
        self.assertTrue(issue.is_auth_error)
        self.assertEqual(self.instance.state, "error")

    def test_retrying_list_issue_reruns_the_entity_sync(self):
        self._patch_catalog([build_product(1, "24-MB01", "Joust Duffle Bag")])
        result = self.instance._magento_resync_one("product", "list")
        self.assertIsInstance(result, dict)
        maps = self.env["magento.product.map"].search(
            [("instance_id", "=", self.instance.id), ("sku", "=", "24-MB01")]
        )
        self.assertEqual(len(maps), 1)
