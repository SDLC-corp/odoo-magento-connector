from odoo.tests import tagged

from .common import MagentoTestCommon


@tagged("post_install", "-at_install", "magento")
class TestBindingConstraints(MagentoTestCommon):
    """C4: (instance, magento id) is unique on every binding model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_instance = cls.create_instance("Second Store")
        cls.product = cls.env["product.product"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True
        ).create({"name": "Push It Messenger Bag", "default_code": "24-WB04", "type": "consu"})

    def _model(self, name):
        return self.env[name].with_context(**self.no_push_ctx(), skip_odoo_sync=True)

    def _map_vals(self, instance, sku="24-WB04"):
        return {"instance_id": instance.id, "magento_id": "14", "sku": sku,
                "name": "Push It Messenger Bag", "odoo_product_id": self.product.id}

    def _category_vals(self, instance):
        return {"instance_id": instance.id, "magento_id": "20", "name": "Women"}

    def _customer_vals(self, instance, email="roni_cost@example.com"):
        return {"instance_id": instance.id, "magento_id": "1", "email": email,
                "firstname": "Veronica", "lastname": "Costello"}

    def test_c4_product_map_duplicate_rejected(self):
        Map = self._model("magento.product.map")
        Map.create(self._map_vals(self.instance))
        self.assertDuplicateRejected(lambda: Map.create(self._map_vals(self.instance, sku="24-WB04-B")))

    def test_c4_category_duplicate_rejected(self):
        Category = self._model("magento.category")
        Category.create(self._category_vals(self.instance))
        self.assertDuplicateRejected(lambda: Category.create(self._category_vals(self.instance)))

    def test_c4_customer_duplicate_rejected(self):
        Customer = self._model("magento.customer")
        Customer.create([self._customer_vals(self.instance)])
        self.assertDuplicateRejected(
            lambda: Customer.create([self._customer_vals(self.instance, email="other@example.com")]))

    def test_c4_product_map_duplicate_sku_rejected(self):
        Map = self._model("magento.product.map")
        Map.create(dict(self._map_vals(self.instance), magento_id="15"))
        self.assertDuplicateRejected(
            lambda: Map.create(dict(self._map_vals(self.instance), magento_id="16")),
            "magento.product.map must be unique per (instance, sku)")

    def test_c4_attribute_set_duplicate_rejected(self):
        AttributeSet = self._model("magento.attribute.set")
        vals = {"instance_id": self.instance.id, "magento_id": "9", "name": "Bag"}
        AttributeSet.create(dict(vals))
        self.assertDuplicateRejected(lambda: AttributeSet.create(dict(vals, name="Bag 2")))

    def test_c4_same_magento_id_on_two_instances_is_allowed(self):
        self._model("magento.product.map").create([
            self._map_vals(self.instance), self._map_vals(self.other_instance)])
        self._model("magento.category").create([
            self._category_vals(self.instance), self._category_vals(self.other_instance)])
        self._model("magento.customer").create([
            self._customer_vals(self.instance), self._customer_vals(self.other_instance)])
        self.env.flush_all()

    def test_c4_bindings_without_magento_id_do_not_collide(self):
        Map = self._model("magento.product.map")
        Map.create([
            dict(self._map_vals(self.instance), magento_id=False, sku="LOCAL-1"),
            dict(self._map_vals(self.instance), magento_id=False, sku="LOCAL-2"),
            dict(self._map_vals(self.instance), magento_id="", sku="LOCAL-3"),
            dict(self._map_vals(self.instance), magento_id="", sku="LOCAL-4"),
        ])
        Category = self._model("magento.category")
        Category.create([
            {"instance_id": self.instance.id, "magento_id": False, "name": "Draft A"},
            {"instance_id": self.instance.id, "magento_id": False, "name": "Draft B"},
        ])
        self.env.flush_all()
