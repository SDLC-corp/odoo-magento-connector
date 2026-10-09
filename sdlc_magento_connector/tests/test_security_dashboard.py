from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import MagentoTestCommon

USER_GROUP = "sdlc_magento_connector.group_magento_user"
MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"


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
    """D1: dashboard data for Magento users, without sudo, with countries."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "Magento Company B"})
        cls.instance_b = cls.create_instance("Store B", company=cls.company_b)
        Partner = cls.env["res.partner"]
        cls.partner_us = Partner.create({"name": "Veronica Costello", "country_id": cls.env.ref("base.us").id,
                                         "email": "roni_cost@example.com"})
        cls.partner_fr = Partner.create({"name": "Jean Dupont", "country_id": cls.env.ref("base.fr").id,
                                         "email": "jean.dupont@example.fr"})
        SaleOrder = cls.env["sale.order"].with_context(skip_magento_sync=True, magento_skip_push=True)
        cls.order_a = SaleOrder.create({
            "partner_id": cls.partner_us.id,
            "company_id": cls.company_a.id,
            "magento_instance_id": cls.instance.id,
            "magento_order_id": "100",
            "magento_increment_id": "000000100",
            "magento_status": "processing",
        })
        cls.order_b = SaleOrder.with_company(cls.company_b).create({
            "partner_id": cls.partner_fr.id,
            "company_id": cls.company_b.id,
            "magento_instance_id": cls.instance_b.id,
            "magento_order_id": "200",
            "magento_increment_id": "000000200",
            "magento_status": "processing",
        })
        cls.user_a = cls.create_user("mg_user_d1", USER_GROUP, company=cls.company_a)

    def _dashboard_model(self, user):
        name = "magento.dashboard" if "magento.dashboard" in self.env else "magento.instance"
        return self.env[name].with_user(user)

    def test_d1_dashboard_model_exists(self):
        self.assertIn("magento.dashboard", self.env, "magento.dashboard model is part of the contract")

    def test_d1_dashboard_runs_for_magento_user(self):
        data = self._dashboard_model(self.user_a).get_dashboard_data()
        self.assertIsInstance(data, dict)

    def test_d1_dashboard_respects_company_isolation(self):
        data = self._dashboard_model(self.user_a).get_dashboard_data()
        instance_ids = {row.get("id") for row in data.get("instances", [])}
        self.assertNotIn(self.instance_b.id, instance_ids,
                         "A company-A user must not see company-B instances")
        if "kpis" in data and "total_orders" in data["kpis"]:
            expected = self.env["sale.order"].search_count([
                ("magento_order_id", "!=", False), ("company_id", "=", self.company_a.id)])
            self.assertEqual(data["kpis"]["total_orders"], expected,
                             "A company-A user must not count company-B orders (no sudo)")
            # Asking explicitly for the company-B instance must not reveal its orders either.
            data_b = self._dashboard_model(self.user_a).get_dashboard_data(instance_id=self.instance_b.id)
            self.assertNotEqual(data_b.get("selected_instance_id"), self.instance_b.id)
            self.assertLessEqual(data_b["kpis"]["total_orders"], expected)
        country_names = {row.get("name") for row in data.get("top_countries", [])}
        self.assertNotIn(self.env.ref("base.fr").name, country_names,
                         "Company-B order countries leaked into the dashboard")

    def test_d1_top_countries_from_orders(self):
        data = self._dashboard_model(self.user_a).get_dashboard_data()
        top = data.get("top_countries")
        self.assertTrue(top, "top_countries must not be empty when orders have countries")
        self.assertIn(self.env.ref("base.us").name, {row.get("name") for row in top})
