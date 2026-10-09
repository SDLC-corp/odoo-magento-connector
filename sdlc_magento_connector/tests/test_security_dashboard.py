from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import MagentoTestCommon

USER_GROUP = "sdlc_magento_connector.group_magento_user"
MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
SALES_READ_GROUP = "sales_team.group_sale_salesman_all_leads"
ORDERS_ACTION = "sdlc_magento_connector.action_magento_order"


@tagged("post_install", "-at_install", "magento")
class TestSecurity(MagentoTestCommon):
    """D2: secrets and instance deletion are manager-only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.magento_user = cls.create_user("mg_user_d2", USER_GROUP)
        cls.magento_manager = cls.create_user("mg_manager_d2", MANAGER_GROUP)

    def _assert_field_hidden(self, record, field_name):
        if field_name not in record.fields_get():
            return
        with self.assertRaises(AccessError):
            record.read([field_name])

    def test_d2_user_cannot_read_secrets(self):
        instance = self.instance.with_user(self.magento_user)
        instance.read(["name", "base_url"])  # normal fields stay readable
        self._assert_field_hidden(instance, "access_token")
        self._assert_field_hidden(instance, "customer_default_password")

    def test_d2_user_cannot_unlink_instance(self):
        with self.assertRaises(AccessError):
            self.instance.with_user(self.magento_user).unlink()
        self.assertTrue(self.instance.exists())

    def test_d2_manager_can_read_token_and_unlink(self):
        instance = self.instance.with_user(self.magento_manager)
        self.assertEqual(instance.read(["access_token"])[0]["access_token"], "test-integration-token")
        instance.unlink()
        self.assertFalse(self.instance.exists())


@tagged("post_install", "-at_install", "magento")
class TestDashboard(MagentoTestCommon):
    """D1: the Magento Console payload (magento.dashboard) runs with the caller's
    rights, never leaks another company's data, and every figure drills down."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "Magento Company B"})
        cls.instance.write({"state": "connected"})
        cls.instance_b = cls.create_instance("Store B", company=cls.company_b, state="connected")
        Partner = cls.env["res.partner"]
        cls.country_fr = cls.env.ref("base.fr")
        cls.partner_us = Partner.create({"name": "Veronica Costello", "country_id": cls.country_us.id,
                                         "email": "roni_cost@example.com"})
        cls.partner_fr = Partner.create({"name": "Jean Dupont", "country_id": cls.country_fr.id,
                                         "email": "jean.dupont@example.fr"})
        SaleOrder = cls.env["sale.order"].with_context(skip_magento_sync=True, magento_skip_push=True)
        cls.order_a = SaleOrder.create({
            "partner_id": cls.partner_us.id,
            "partner_shipping_id": cls.partner_us.id,
            "company_id": cls.company_a.id,
            "magento_instance_id": cls.instance.id,
            "magento_order_id": "100",
            "magento_increment_id": "000000100",
            "magento_status": "processing",
        })
        cls.order_b = SaleOrder.with_company(cls.company_b).create({
            "partner_id": cls.partner_fr.id,
            "partner_shipping_id": cls.partner_fr.id,
            "company_id": cls.company_b.id,
            "magento_instance_id": cls.instance_b.id,
            "magento_order_id": "200",
            "magento_increment_id": "000000200",
            "magento_status": "processing",
        })
        # Only the Magento group: no right to read sale.order.
        cls.magento_only_user = cls.create_user("mg_user_d1", USER_GROUP, company=cls.company_a)
        # Magento group + sales read rights, but only company A.
        cls.sales_user_a = cls.create_user(
            "mg_sales_d1", "%s,%s" % (USER_GROUP, SALES_READ_GROUP), company=cls.company_a)

    def _console(self, user):
        self.assertIn("magento.dashboard", self.env, "magento.dashboard model is part of the contract")
        return self.env["magento.dashboard"].with_user(user)

    def test_d1_dashboard_model_exists(self):
        self.assertIn("magento.dashboard", self.env)

    def test_d1_runs_for_magento_only_user_with_orders_hidden(self):
        user = self.magento_only_user
        data = self._console(user).get_dashboard_data()
        self.assertIsInstance(data, dict)
        can_read_orders = self.env["sale.order"].with_user(user).has_access("read")
        self.assertEqual(data["orders_hidden"], not can_read_orders)
        if not can_read_orders:
            self.assertFalse(data["by_country"])
            self.assertFalse(data["trend"])
            self.assertFalse([k for k in data["kpis"] if k["drill"]["res_model"] == "sale.order"],
                             "Order figures must be hidden from a user who can't read orders")
            self.assertFalse([a for a in data["attention"] if a["drill"]["res_model"] == "sale.order"])

    def test_d1_no_cross_company_leak(self):
        console = self._console(self.sales_user_a)
        data = console.get_dashboard_data()
        self.assertFalse(data["orders_hidden"])
        instance_ids = {row["id"] for row in data["instances"]}
        self.assertIn(self.instance.id, instance_ids)
        self.assertNotIn(self.instance_b.id, instance_ids, "Company-B instance leaked")
        self.assertNotIn(self.instance_b.id, data["scope_ids"])

        orders_kpi = {k["key"]: k for k in data["kpis"]}.get("orders")
        self.assertTrue(orders_kpi, "The orders KPI must be present for a user who can read orders")
        orders = self.env["sale.order"].with_user(self.sales_user_a).search(orders_kpi["drill"]["domain"])
        self.assertIn(self.order_a, orders)
        self.assertNotIn(self.order_b, orders)
        self.assertEqual(orders_kpi["value"], len(orders))
        self.assertNotIn(self.country_fr.name, {row["name"] for row in data["by_country"]},
                         "Company-B order countries leaked")

        # Asking explicitly for the company-B instance must not widen the scope.
        data_b = console.get_dashboard_data(instance_ids=[self.instance_b.id])
        self.assertNotIn(self.instance_b.id, data_b["selected_ids"])
        self.assertNotIn(self.instance_b.id, data_b["scope_ids"])

    def test_d1_top_countries_from_shipping_country(self):
        data = self._console(self.sales_user_a).get_dashboard_data()
        countries = data["by_country"]
        self.assertTrue(countries, "Top countries must not be empty when orders have shipping countries")
        us_rows = [row for row in countries if row.get("code") == "US"]
        self.assertTrue(us_rows, "The shipping country of the order must be listed")
        self.assertGreaterEqual(us_rows[0]["orders"], 1)
        self.assertEqual(us_rows[0]["drill"]["res_model"], "sale.order")

    def test_d1_every_kpi_with_a_value_drills_down(self):
        for user in (self.sales_user_a, self.magento_only_user):
            data = self._console(user).get_dashboard_data()
            self.assertTrue(data["kpis"])
            for item in data["kpis"] + data["attention"]:
                drill = item.get("drill") or {}
                if item.get("value") or item.get("count") or "value" not in item:
                    self.assertTrue(drill.get("res_model"), "%s has no drill-down model" % item["key"])
                    self.assertIsInstance(drill.get("domain"), list, "%s has no domain" % item["key"])
                    self.assertTrue(self.env[drill["res_model"]].with_user(user).has_access("read"),
                                    "%s drills into a model the user cannot read" % item["key"])

    def test_d1_nav_links_omit_what_the_user_cannot_open(self):
        def xml_ids(user):
            groups = self._console(user).get_nav_links()
            return {link["xml_id"] for group in groups for link in group["links"]}

        def can_open(user, xml_id):
            action = self.env.ref(xml_id, raise_if_not_found=False)
            if not action:
                return False
            if action.group_ids and not (action.group_ids & user.all_group_ids):
                return False
            model = getattr(action, "res_model", False) or getattr(action, "model_name", False)
            return not model or self.env[model].with_user(user).has_access("read")

        for user in (self.magento_only_user, self.sales_user_a):
            expected = {xml_id for xml_id, *_rest in self.env["magento.dashboard"].NAV_LINKS
                        if can_open(user, xml_id)}
            self.assertEqual(xml_ids(user), expected)
        if self.env.ref(ORDERS_ACTION, raise_if_not_found=False):
            # Magento User implies Sales rights (its syncs create orders), so the
            # negative case is an internal user outside the Magento groups.
            plain_user = self.create_user("mg_plain_d1", "base.group_user", company=self.company_a)
            self.assertNotIn(ORDERS_ACTION, xml_ids(plain_user),
                             "A user who can't read orders must not get the Orders link")
            self.assertIn(ORDERS_ACTION, xml_ids(self.magento_only_user))
            self.assertIn(ORDERS_ACTION, xml_ids(self.sales_user_a))
