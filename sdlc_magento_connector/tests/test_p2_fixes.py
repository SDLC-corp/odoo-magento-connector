from odoo.tests import tagged

from .common import MagentoTestCommon


@tagged("post_install", "-at_install", "magento")
class TestP2Fixes(MagentoTestCommon):
    """Fixes found in the live run against Magento 2.4.9."""

    def test_magento_tax_gets_group_named_after_its_rate(self):
        company = self.env.company
        country = company.account_fiscal_country_id or company.country_id
        group = self.instance._magento_tax_group(company, country, 8.25)
        self.assertEqual(group.name, "Tax 8.25%")
        self.assertEqual(self.instance._magento_tax_group(company, country, 8.25), group,
                         "the group is reused, not recreated")
        base = company.account_sale_tax_id.tax_group_id
        if base:
            self.assertEqual(group.property_tax_payable_account_id, base.property_tax_payable_account_id,
                             "the group keeps the default sale group's accounts")

    def test_translated_name_does_not_leak_into_magento_default(self):
        self.env["res.lang"]._activate_lang("fr_FR")
        product = self.create_product("TR-1", "Joust Duffle Bag")
        product_map = self.create_map(product, "901", sku="TR-1")
        product_map.with_context(magento_skip_push=True).name = "Joust Duffle Bag"

        product.product_tmpl_id.with_context(lang="fr_FR", magento_skip_push=True).name = "Sac de sport"
        self.assertEqual(product_map.name, "Joust Duffle Bag",
                         "a French edit must not become the Magento default name")

        product.product_tmpl_id.with_context(lang="en_US", magento_skip_push=True).name = "Joust Bag"
        self.assertEqual(product_map.name, "Joust Bag", "a default-language edit is still mirrored")
