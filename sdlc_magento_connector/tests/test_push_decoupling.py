import requests

from odoo.tests import tagged

from .common import MagentoTestCommon


@tagged("post_install", "-at_install", "magento")
class TestPushDecoupling(MagentoTestCommon):
    """C1-C3: Odoo saves never depend on Magento being reachable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = dict(skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.attribute_set = cls.env["magento.attribute.set"].with_context(**ctx).create({
            "name": "Default", "magento_id": "4", "instance_id": cls.instance.id,
        })
        cls.product = cls.env["product.product"].with_context(**ctx).create({
            "name": "Fusion Backpack", "default_code": "24-MB02", "type": "consu",
            "list_price": 59.0,
        })
        cls.product_map = cls.env["magento.product.map"].with_context(**ctx).create({
            "instance_id": cls.instance.id,
            "magento_id": "2",
            "sku": "24-MB02",
            "name": "Fusion Backpack",
            "price": 59.0,
            "attribute_set_id": cls.attribute_set.id,
            "odoo_product_id": cls.product.id,
        })
        # Records keep the env they were created with: drop the skip flags so
        # the tests exercise the real push path.
        cls.product = cls.product.with_env(cls.env)
        cls.product_map = cls.product_map.with_env(cls.env)

    def _magento_down(self):
        refused = requests.exceptions.ConnectionError("Magento is down")
        mocks = {}
        for name in ("update_product", "get_product", "create_product", "add_product_media",
                     "update_product_media", "update_customer", "create_customer",
                     "update_category", "create_category"):
            mocks[name] = self.patch_api(name, side_effect=refused)
        return mocks

    # ---------------------------------------------------------------- C1
    def test_c1_product_edit_saves_while_magento_is_down(self):
        self._magento_down()
        # Must not raise even though every Magento call fails.
        self.product.write({"name": "Fusion Backpack v2", "list_price": 61.0})
        self.env.flush_all()
        self.product.invalidate_recordset()
        self.assertEqual(self.product.name, "Fusion Backpack v2")
        self.assertEqual(self.product.list_price, 61.0)

    def test_c1_template_edit_saves_while_magento_is_down(self):
        self._magento_down()
        self.product.product_tmpl_id.write({"name": "Fusion Backpack v3"})
        self.env.flush_all()
        self.assertEqual(self.product.product_tmpl_id.name, "Fusion Backpack v3")

    def test_c1_push_is_not_done_inside_write(self):
        mocks = self._magento_down()
        self.product.write({"name": "Fusion Backpack v4"})
        self.assertFalse(mocks["update_product"].called,
                         "The push must run post-commit, not inside write()")
        self.assertFalse(self.http_attempts)

    def test_c1_postcommit_push_failure_is_recorded_not_raised(self):
        mocks = self._magento_down()
        self.product.write({"name": "Fusion Backpack v6"})
        self.assertFalse(any(m.called for m in mocks.values()), "No push inside write()")
        self.env.flush_all()
        # The push opens its own cursor after commit; in registry test mode that
        # cursor shares the test transaction, so the queued push can run here.
        with self.enter_registry_test_mode():
            self.env.cr.postcommit.run()
        self.assertTrue(any(m.called for m in mocks.values()), "The queued push must call Magento")
        self.product_map.invalidate_recordset()
        if "magento_last_error" in self.product_map._fields:
            self.assertTrue(self.product_map.magento_last_error,
                            "A failed post-commit push must be recorded on the map")
        self.assertEqual(self.product.name, "Fusion Backpack v6")

    def test_c1_skip_push_context_makes_no_api_call(self):
        mocks = self._magento_down()
        self.product.with_context(magento_skip_push=True).write({"name": "Fusion Backpack v5"})
        self.product_map.with_context(magento_skip_push=True).write({"price": 65.0})
        self.env.flush_all()
        # Run whatever was queued for after the commit: nothing may reach Magento.
        with self.enter_registry_test_mode():
            self.env.cr.postcommit.run()
        for name, mock in mocks.items():
            self.assertFalse(mock.called, "%s called despite magento_skip_push" % name)
        self.assertFalse(self.http_attempts, "HTTP attempted despite magento_skip_push")

    # ---------------------------------------------------------------- C2
    def test_c2_field_mapping_create_and_write_make_no_api_call(self):
        Field = self.env["ir.model.fields"]
        name_field = Field._get("magento.product.map", "name")
        desc_field = Field._get("magento.product.map", "description")
        available = self.env["magento.available.field"].create({
            "name": "Meta Title", "code": "meta_title",
            "instance_id": self.instance.id, "mapping_type": "product",
        })
        mapping = self.env["magento.field.mapping"].create({
            "mapping_type": "product",
            "instance_id": self.instance.id,
            "odoo_field": name_field.id,
            "magento_field_id": available.id,
        })
        mapping.write({"odoo_field": desc_field.id})
        mapping.write({"active": False})
        self.env.flush_all()
        self.assertFalse(self.http_attempts,
                         "Saving a field mapping must not call Magento: %s" % self.http_attempts)

    # ---------------------------------------------------------------- C3
    def test_c3_customer_create_accepts_single_dict(self):
        Customer = self.env["magento.customer"].with_context(**self.no_push_ctx())
        customer = Customer.create({
            "instance_id": self.instance.id,
            "magento_id": "501",
            "email": "roni_cost@example.com",
            "firstname": "Veronica",
            "lastname": "Costello",
        })
        self.assertEqual(len(customer), 1)
        self.assertEqual(customer.email, "roni_cost@example.com")

    def test_c3_customer_create_accepts_list(self):
        Customer = self.env["magento.customer"].with_context(**self.no_push_ctx())
        customers = Customer.create([
            {"instance_id": self.instance.id, "magento_id": "502", "email": "a@example.com",
             "firstname": "A", "lastname": "One"},
            {"instance_id": self.instance.id, "magento_id": "503", "email": "b@example.com",
             "firstname": "B", "lastname": "Two"},
        ])
        self.assertEqual(len(customers), 2)
