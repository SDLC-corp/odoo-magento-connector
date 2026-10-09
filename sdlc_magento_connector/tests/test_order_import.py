from odoo.tests import tagged

from .common import MagentoTestCommon, build_address, build_item, build_order


@tagged("post_install", "-at_install", "magento")
class TestOrderImport(MagentoTestCommon):
    """B1-B5: idempotent order import, confirmed-order conflicts, configurable
    items, guest partner matching, and the order unique key."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Product = cls.env["product.product"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        Map = cls.env["magento.product.map"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True)
        cls.bag = Product.create({"name": "Joust Duffle Bag", "default_code": "24-MB01",
                                  "type": "consu", "list_price": 34.0})
        cls.pack = Product.create({"name": "Strive Shoulder Pack", "default_code": "24-MB04",
                                   "type": "consu", "list_price": 32.0})
        cls.hoodie_xs = Product.create({"name": "Chaz Kangeroo Hoodie-XS-Black",
                                        "default_code": "MH01-XS-Black", "type": "consu",
                                        "list_price": 52.0})
        for product, magento_id in ((cls.bag, 1), (cls.pack, 4), (cls.hoodie_xs, 68)):
            Map.create({
                "instance_id": cls.instance.id,
                "magento_id": str(magento_id),
                "sku": product.default_code,
                "name": product.name,
                "odoo_product_id": product.id,
            })

    def setUp(self):
        super().setUp()
        self.patch_order_side_calls()

    def _two_line_order(self, entity_id=1001, bag_qty=1.0):
        return build_order(entity_id, [
            build_item(entity_id * 10 + 1, "24-MB01", qty=bag_qty, price=34.0, product_id=1),
            build_item(entity_id * 10 + 2, "24-MB04", qty=2.0, price=32.0, product_id=4),
        ])

    # ---------------------------------------------------------------- B1
    def test_b1_import_twice_is_idempotent(self):
        data = self._two_line_order()
        self.import_orders([data])
        order = self.magento_orders(1001)
        self.assertEqual(len(order), 1)
        line_count = len(order.order_line)
        self.assertEqual(line_count, 2)

        self.import_orders([self._two_line_order()])
        orders = self.magento_orders(1001)
        self.assertEqual(len(orders), 1, "Re-import must not create a second sale.order")
        self.assertEqual(len(orders.order_line), line_count, "Re-import must not duplicate lines")

    # ---------------------------------------------------------------- B2
    def test_b2_confirmed_order_change_flags_conflict(self):
        self.import_orders([self._two_line_order(bag_qty=1.0)])
        order = self.magento_orders(1001)
        order.action_confirm()
        self.assertEqual(order.state, "sale")
        before = sorted((l.product_id.id, l.product_uom_qty) for l in order.order_line)

        # Magento now says 3 bags. Must not raise on the confirmed order.
        self.import_orders([self._two_line_order(bag_qty=3.0)])

        order.invalidate_recordset()
        self.assertTrue(order.magento_sync_conflict,
                        "A Magento change on a confirmed order must set magento_sync_conflict")
        after = sorted((l.product_id.id, l.product_uom_qty) for l in order.order_line)
        self.assertEqual(after, before, "Lines of a confirmed order must not be rewritten")
        self.assertEqual(order.state, "sale")

    # ---------------------------------------------------------------- B3
    def test_b3_configurable_parent_and_child_give_one_line(self):
        parent = build_item(2001, "MH01-XS-Black", qty=2.0, price=52.0, product_id=67,
                            product_type="configurable", name="Chaz Kangeroo Hoodie")
        child = build_item(2002, "MH01-XS-Black", qty=2.0, price=0.0, product_id=68,
                           product_type="simple", parent_item_id=2001,
                           name="Chaz Kangeroo Hoodie-XS-Black")
        self.import_orders([build_order(1002, [parent, child])])

        order = self.magento_orders(1002)
        self.assertEqual(len(order), 1)
        lines = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(len(lines), 1, "Configurable parent + child must give one order line")
        self.assertEqual(lines.product_id, self.hoodie_xs, "The line must use the child (simple) product")
        self.assertEqual(lines.product_uom_qty, 2.0)

    # ---------------------------------------------------------------- B4
    def test_b4_guests_same_name_different_email_are_different_partners(self):
        first = build_order(1003, [build_item(30031, "24-MB01", product_id=1, price=34.0)],
                            guest=True, firstname="John", lastname="Smith",
                            email="john.smith@example.com")
        second = build_order(1004, [build_item(30041, "24-MB01", product_id=1, price=34.0)],
                             guest=True, firstname="John", lastname="Smith",
                             email="j.smith@another.example")
        self.import_orders([first, second])
        order_1 = self.magento_orders(1003)
        order_2 = self.magento_orders(1004)
        self.assertTrue(order_1 and order_2)
        self.assertNotEqual(
            order_1.partner_id.commercial_partner_id, order_2.partner_id.commercial_partner_id,
            "Two guests with the same name but different emails must not be merged",
        )

    def test_b4_guest_without_email_never_attaches_to_existing_partner(self):
        existing = self.env["res.partner"].create({
            "name": "John Smith", "email": "john.smith@example.com",
        })
        billing = build_address(firstname="John", lastname="Smith", email=None, parent_id=1005)
        data = build_order(1005, [build_item(30051, "24-MB01", product_id=1, price=34.0)],
                           guest=True, firstname="John", lastname="Smith", email=None,
                           billing=billing)
        self.import_orders([data])
        order = self.magento_orders(1005)
        self.assertEqual(len(order), 1)
        self.assertNotEqual(order.partner_id.commercial_partner_id, existing,
                            "A guest without email must never be attached to an existing partner")

    def test_b4_shipping_address_is_delivery_child(self):
        billing = build_address(firstname="Veronica", lastname="Costello",
                                email="roni_cost@example.com", parent_id=1006)
        shipping = build_address(firstname="Veronica", lastname="Costello",
                                 email="roni_cost@example.com", street=("12 Warehouse Road",),
                                 city="Detroit", postcode="48201", address_type="shipping",
                                 parent_id=1006)
        data = build_order(1006, [build_item(30061, "24-MB01", product_id=1, price=34.0)],
                           billing=billing, shipping=shipping)
        self.import_orders([data])
        order = self.magento_orders(1006)
        self.assertEqual(len(order), 1)
        delivery = order.partner_shipping_id
        self.assertEqual(delivery.type, "delivery", "Shipping address must be a delivery contact")
        self.assertEqual(delivery.commercial_partner_id, order.partner_id.commercial_partner_id)
        self.assertNotEqual(delivery, order.partner_id.commercial_partner_id,
                            "Shipping address must be a child contact, not the customer itself")
        self.assertEqual(delivery.street, "12 Warehouse Road")
        self.assertEqual(delivery.city, "Detroit")

    # ---------------------------------------------------------------- B5
    def test_b5_unique_instance_magento_order_id(self):
        partner = self.env["res.partner"].create({"name": "Constraint Customer"})
        SaleOrder = self.env["sale.order"].with_context(skip_magento_sync=True, magento_skip_push=True)
        vals = {
            "partner_id": partner.id,
            "magento_instance_id": self.instance.id,
            "magento_order_id": "9001",
        }
        SaleOrder.create(dict(vals))
        self.assertDuplicateRejected(lambda: SaleOrder.create(dict(vals)),
                                     "sale.order must be unique per (instance, magento_order_id)")

    def test_b5_same_magento_order_id_on_other_instance_is_allowed(self):
        other = self.create_instance("Second Store")
        partner = self.env["res.partner"].create({"name": "Constraint Customer"})
        SaleOrder = self.env["sale.order"].with_context(skip_magento_sync=True, magento_skip_push=True)
        SaleOrder.create({"partner_id": partner.id, "magento_instance_id": self.instance.id,
                          "magento_order_id": "9002"})
        SaleOrder.create({"partner_id": partner.id, "magento_instance_id": other.id,
                          "magento_order_id": "9002"})
        self.env.flush_all()
