"""Roadmap 2.9: price export (base, special with dates, tier per customer group), only
changed prices sent, ownership-safe deletion, failed-item parsing and the queued job."""
from datetime import datetime, timedelta

import requests

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.sdlc_magento_connector.models.magento_pricing import ALL_GROUPS, price_failure_message

from .common import MagentoTestCommon, make_response


def _dt(days=0):
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0)


@tagged("post_install", "-at_install", "magento")
class TestPricingP2(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Rule = cls.env["magento.price.rule"]
        cls.State = cls.env["magento.price.push.state"]
        cls.Issue = cls.env["magento.sync.issue"]
        cls.Job = cls.env["magento.job"]
        cls.currency = cls.env.company.currency_id

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def setUp(self):
        super().setUp()
        self.calls = {}
        for name in ("update_base_prices", "update_special_prices", "delete_special_prices",
                     "update_tier_prices", "delete_tier_prices"):
            self.calls[name] = self.patch_api(name, return_value=[])
        self.calls["get_special_prices"] = self.patch_api("get_special_prices", return_value=[])

    def sent(self, method):
        """Every price sent through ``method``, all calls flattened."""
        mock = self.calls[method]
        return [item for call in mock.call_args_list for item in call.args[1]]

    def reset_calls(self):
        for mock in self.calls.values():
            mock.reset_mock()

    def _mapped(self, sku, price=10.0, **map_vals):
        product = self.create_product(sku, list_price=price)
        self.create_map(product, abs(hash(sku)) % 10 ** 7, **map_vals)
        return product

    def _pricelist(self, items, name="Retail"):
        return self.env["product.pricelist"].create({
            "name": name,
            "currency_id": self.currency.id,
            "item_ids": [Command.create(vals) for vals in items],
        })

    def _rule(self, pricelist, kind="base", **vals):
        return self.Rule.create(dict({
            "name": "%s rule" % kind,
            "instance_id": self.instance.id,
            "pricelist_id": pricelist.id,
            "kind": kind,
        }, **vals))

    def _states(self, rule):
        return {(s.sku, s.key): s for s in rule.push_state_ids}

    @staticmethod
    def _global_discount(percent):
        return {"applied_on": "3_global", "compute_price": "discount", "price_discount": percent}

    @staticmethod
    def _fixed(product, price, **extra):
        return dict({"applied_on": "1_product", "product_tmpl_id": product.product_tmpl_id.id,
                     "compute_price": "fixed", "fixed_price": price}, **extra)

    # ------------------------------------------------------------------
    # base price
    # ------------------------------------------------------------------
    def test_base_price_from_pricelist_payload(self):
        self._mapped("PR-A", 10.0)
        self._mapped("PR-B", 25.0)
        pricelist = self._pricelist([self._global_discount(10)])
        rule = self._rule(pricelist)

        summary = rule._magento_push_prices()[rule.id]

        self.assertEqual(
            sorted(self.sent("update_base_prices"), key=lambda p: p["sku"]),
            [{"sku": "PR-A", "price": 9.0, "store_id": 0}, {"sku": "PR-B", "price": 22.5, "store_id": 0}],
        )
        states = self._states(rule)
        self.assertEqual(states[("PR-A", "base|s0")].price, 9.0)
        self.assertEqual(states[("PR-B", "base|s0")].price, 22.5)
        self.assertEqual(rule.last_push_state, "done")
        self.assertIn("2 sent", summary)
        self.assertFalse(self.calls["update_special_prices"].called)
        self.assertFalse(self.calls["update_tier_prices"].called)

    def test_only_mapped_priced_products_are_sent(self):
        self._mapped("PR-SIMPLE")
        self._mapped("PR-GROUPED", type_id="grouped")
        self.create_product("PR-UNMAPPED")
        archived = self._mapped("PR-ARCHIVED")
        archived.with_context(**self.no_push_ctx()).active = False
        rule = self._rule(self._pricelist([]))

        rule._magento_push_prices()

        self.assertEqual([p["sku"] for p in self.sent("update_base_prices")], ["PR-SIMPLE"])

    def test_store_scope_sets_store_and_website_ids(self):
        product = self._mapped("PR-SCOPE", 10.0)
        pricelist = self._pricelist([
            self._global_discount(10),
            self._fixed(product, 6.0, min_quantity=5),
        ])
        base_rule = self._rule(pricelist, scope="store", magento_store_id=2, magento_website_id=3)
        tier_rule = self._rule(pricelist, kind="tier", scope="store", magento_store_id=2, magento_website_id=3)
        global_rule = self._rule(pricelist, magento_store_id=2, magento_website_id=3)

        (base_rule | tier_rule)._magento_push_prices()

        self.assertEqual(self.sent("update_base_prices"), [{"sku": "PR-SCOPE", "price": 9.0, "store_id": 2}])
        self.assertEqual(self.sent("update_tier_prices")[0]["website_id"], 3)
        self.assertIn(("PR-SCOPE", "base|s2"), self._states(base_rule))
        # Global scope ignores the ids.
        self.assertEqual((global_rule.effective_store_id, global_rule.effective_website_id), (0, 0))
        self.reset_calls()
        global_rule._magento_push_prices()
        self.assertEqual(self.sent("update_base_prices")[0]["store_id"], 0)

    def test_scope_change_moves_the_price(self):
        product = self._mapped("PR-MOVE", 10.0)
        pricelist = self._pricelist([self._fixed(product, 8.0)])
        rule = self._rule(pricelist, kind="special")
        rule._magento_push_prices()
        self.reset_calls()
        self.calls["get_special_prices"].return_value = [{"sku": "PR-MOVE", "price": 8, "store_id": 0}]

        rule.write({"scope": "store", "magento_store_id": 1})
        rule._magento_push_prices()

        self.assertEqual([(p["sku"], p["store_id"]) for p in self.sent("delete_special_prices")], [("PR-MOVE", 0)])
        self.assertEqual([(p["sku"], p["store_id"]) for p in self.sent("update_special_prices")], [("PR-MOVE", 1)])
        self.assertEqual(list(self._states(rule)), [("PR-MOVE", "special|s1")])

    # ------------------------------------------------------------------
    # only changes
    # ------------------------------------------------------------------
    def test_only_changed_prices_are_sent(self):
        self._mapped("PR-C1", 10.0)
        changed = self._mapped("PR-C2", 20.0)
        rule = self._rule(self._pricelist([self._global_discount(50)]))
        rule._magento_push_prices()
        self.assertEqual(len(self.sent("update_base_prices")), 2)

        self.reset_calls()
        summary = rule._magento_push_prices()[rule.id]
        self.assertFalse(self.calls["update_base_prices"].called, "Unchanged prices were sent again")
        self.assertIn("2 unchanged", summary)

        changed.with_context(**self.no_push_ctx()).list_price = 30.0
        self.reset_calls()
        rule._magento_push_prices()
        self.assertEqual(self.sent("update_base_prices"), [{"sku": "PR-C2", "price": 15.0, "store_id": 0}])
        self.assertEqual(self._states(rule)[("PR-C2", "base|s0")].price, 15.0)

        # "Resend All" sends everything again.
        self.reset_calls()
        rule._magento_push_prices(force=True)
        self.assertEqual(len(self.sent("update_base_prices")), 2)

    def test_chunks_follow_the_instance_chunk_size(self):
        self.instance.bulk_chunk_size = 2
        for idx in range(5):
            self._mapped("PR-CH%s" % idx)
        self._rule(self._pricelist([]))._magento_push_prices()
        self.assertEqual([len(c.args[1]) for c in self.calls["update_base_prices"].call_args_list], [2, 2, 1])

    # ------------------------------------------------------------------
    # special price
    # ------------------------------------------------------------------
    def test_special_price_with_dates(self):
        product = self._mapped("PR-SP", 10.0)
        self._mapped("PR-NOITEM", 10.0)
        start, end = _dt(-1), _dt(10)
        pricelist = self._pricelist([self._fixed(product, 7.5, date_start=start, date_end=end)])
        rule = self._rule(pricelist, kind="special")

        rule._magento_push_prices()

        self.assertEqual(self.sent("update_special_prices"), [{
            "sku": "PR-SP", "price": 7.5, "store_id": 0,
            "price_from": fields.Datetime.to_string(start), "price_to": fields.Datetime.to_string(end),
        }])
        state = self._states(rule)[("PR-SP", "special|s0")]
        self.assertEqual((state.price_from, state.price_to), (fields.Datetime.to_string(start),
                                                              fields.Datetime.to_string(end)))

    def test_special_price_deleted_when_item_removed(self):
        product = self._mapped("PR-DEL", 10.0)
        pricelist = self._pricelist([self._fixed(product, 6.0)])
        rule = self._rule(pricelist, kind="special")
        rule._magento_push_prices()
        self.assertEqual(len(self.sent("update_special_prices")), 1)
        self.assertNotIn("price_from", self.sent("update_special_prices")[0])

        pricelist.item_ids.unlink()
        self.reset_calls()
        self.calls["get_special_prices"].return_value = [{"sku": "PR-DEL", "price": 6, "store_id": 0}]
        summary = rule._magento_push_prices()[rule.id]

        self.assertEqual(self.sent("delete_special_prices"), [{"sku": "PR-DEL", "price": 6.0, "store_id": 0}])
        self.assertFalse(self.calls["update_special_prices"].called)
        self.assertFalse(rule.push_state_ids)
        self.assertIn("1 removed", summary)

    def test_special_price_deleted_when_item_expired(self):
        product = self._mapped("PR-EXP", 10.0)
        pricelist = self._pricelist([self._fixed(product, 6.0, date_start=_dt(-5), date_end=_dt(5))])
        rule = self._rule(pricelist, kind="special")
        rule._magento_push_prices()
        pricelist.item_ids.write({"date_start": _dt(-10), "date_end": _dt(-1)})
        self.reset_calls()
        self.calls["get_special_prices"].return_value = [{"sku": "PR-EXP", "price": 6, "store_id": 0}]

        rule._magento_push_prices()

        self.assertEqual([p["sku"] for p in self.sent("delete_special_prices")], ["PR-EXP"])
        self.assertFalse(rule.push_state_ids)

    def test_special_price_changed_in_magento_is_left_alone(self):
        product = self._mapped("PR-FOREIGN", 10.0)
        pricelist = self._pricelist([self._fixed(product, 6.0)])
        rule = self._rule(pricelist, kind="special")
        rule._magento_push_prices()
        pricelist.item_ids.unlink()
        self.reset_calls()
        # A merchant set another special price in Magento since our push.
        self.calls["get_special_prices"].return_value = [{"sku": "PR-FOREIGN", "price": 5.5, "store_id": 0}]

        summary = rule._magento_push_prices()[rule.id]

        self.assertFalse(self.calls["delete_special_prices"].called)
        self.assertFalse(rule.push_state_ids, "The connector no longer owns that special price")
        self.assertIn("left alone", summary)

    def test_special_price_dates_removed_are_cleared(self):
        product = self._mapped("PR-UNDATE", 10.0)
        pricelist = self._pricelist([self._fixed(product, 6.0, date_start=_dt(-1), date_end=_dt(5))])
        rule = self._rule(pricelist, kind="special")
        rule._magento_push_prices()
        pricelist.item_ids.write({"date_start": False, "date_end": False})
        self.reset_calls()
        self.calls["get_special_prices"].return_value = [{"sku": "PR-UNDATE", "price": 6, "store_id": 0}]

        rule._magento_push_prices()

        # Magento keeps a date the request leaves out: delete, then set without dates.
        self.assertEqual([p["sku"] for p in self.sent("delete_special_prices")], ["PR-UNDATE"])
        self.assertEqual(self.sent("update_special_prices"), [{"sku": "PR-UNDATE", "price": 6.0, "store_id": 0}])
        state = self._states(rule)[("PR-UNDATE", "special|s0")]
        self.assertFalse(state.price_from)

    def test_unlink_rule_owning_prices_is_blocked(self):
        product = self._mapped("PR-OWN", 10.0)
        rule = self._rule(self._pricelist([self._fixed(product, 6.0)]), kind="special")
        rule._magento_push_prices()
        with self.assertRaises(UserError):
            rule.unlink()
        self.calls["get_special_prices"].return_value = [{"sku": "PR-OWN", "price": 6, "store_id": 0}]
        rule._magento_push_prices(remove_all=True)
        self.assertEqual([p["sku"] for p in self.sent("delete_special_prices")], ["PR-OWN"])
        rule.unlink()

    # ------------------------------------------------------------------
    # tier prices / customer groups
    # ------------------------------------------------------------------
    def test_tier_prices_all_groups(self):
        product = self._mapped("PR-TIER", 10.0)
        self._mapped("PR-NOTIER", 10.0)
        pricelist = self._pricelist([
            self._fixed(product, 8.0, min_quantity=5),
            self._fixed(product, 7.0, min_quantity=10),
            self._fixed(product, 12.0, min_quantity=20),  # dearer than one unit: no tier
        ])
        rule = self._rule(pricelist, kind="tier")

        rule._magento_push_prices()

        sent = sorted(self.sent("update_tier_prices"), key=lambda p: p["quantity"])
        self.assertEqual(sent, [
            {"sku": "PR-TIER", "price": 8.0, "price_type": "fixed", "website_id": 0,
             "customer_group": ALL_GROUPS, "quantity": 5.0},
            {"sku": "PR-TIER", "price": 7.0, "price_type": "fixed", "website_id": 0,
             "customer_group": ALL_GROUPS, "quantity": 10.0},
        ])
        self.assertEqual(set(self._states(rule)), {("PR-TIER", "tier|w0|ALL GROUPS|5"),
                                                   ("PR-TIER", "tier|w0|ALL GROUPS|10")})

        # Removing a quantity break deletes that tier in Magento, with the pushed price.
        pricelist.item_ids.filtered(lambda i: i.min_quantity == 10).unlink()
        self.reset_calls()
        rule._magento_push_prices()
        self.assertEqual(self.sent("delete_tier_prices"), [
            {"sku": "PR-TIER", "price": 7.0, "price_type": "fixed", "website_id": 0,
             "customer_group": ALL_GROUPS, "quantity": 10.0},
        ])
        self.assertFalse(self.calls["update_tier_prices"].called)
        self.assertEqual(set(self._states(rule)), {("PR-TIER", "tier|w0|ALL GROUPS|5")})

    def test_tier_prices_for_customer_groups(self):
        product = self._mapped("PR-GRP", 10.0)
        pricelist = self._pricelist([self._fixed(product, 8.0, min_quantity=5)], name="Wholesale")
        Group = self.env["magento.customer.group"]
        wholesale = Group.create({"instance_id": self.instance.id, "magento_group_id": 2,
                                  "name": "Wholesale", "pricelist_id": pricelist.id})
        Group.create({"instance_id": self.instance.id, "magento_group_id": 3,
                      "name": "Retailer", "pricelist_id": pricelist.id})
        Group.create({"instance_id": self.instance.id, "magento_group_id": 1, "name": "General"})

        mapped = self._rule(pricelist, kind="tier", customer_group_mode="mapped")
        self.assertEqual(mapped.customer_group_codes, "Wholesale, Retailer")
        mapped._magento_push_prices()
        self.assertEqual(sorted(p["customer_group"] for p in self.sent("update_tier_prices")),
                         ["Retailer", "Wholesale"])

        self.reset_calls()
        one = self._rule(pricelist, kind="tier", customer_group_mode="group", customer_group_id=wholesale.id)
        one._magento_push_prices()
        self.assertEqual([p["customer_group"] for p in self.sent("update_tier_prices")], ["Wholesale"])

        with self.assertRaises(ValidationError):
            self._rule(pricelist, kind="tier", customer_group_mode="group")
        other = self.create_instance("Other Magento")
        foreign = Group.create({"instance_id": other.id, "magento_group_id": 2, "name": "Wholesale"})
        with self.assertRaises(ValidationError):
            self._rule(pricelist, kind="tier", customer_group_mode="group", customer_group_id=foreign.id)

    def test_variants_included(self):
        color = self.env["product.attribute"].create({
            "name": "Colour", "value_ids": [Command.create({"name": "Red"}), Command.create({"name": "Blue"})],
        })
        template = self.env["product.template"].with_context(**self.no_push_ctx(), skip_odoo_sync=True).create({
            "name": "Shirt", "list_price": 20.0,
            "attribute_line_ids": [Command.create({"attribute_id": color.id, "value_ids": [Command.set(color.value_ids.ids)]})],
        })
        red, blue = template.product_variant_ids.sorted(lambda p: p.product_template_attribute_value_ids.name)[::-1]
        self.create_map(red, 9001, sku="SHIRT-RED")
        self.create_map(blue, 9002, sku="SHIRT-BLUE")
        # The configurable parent is bound to the template, not to a product: never priced.
        self.env["magento.product.map"].with_context(**self.no_push_ctx(), skip_odoo_sync=True).create({
            "instance_id": self.instance.id, "magento_id": "9000", "sku": "SHIRT", "type_id": "configurable",
            "odoo_template_id": template.id,
        })
        pricelist = self._pricelist([{
            "applied_on": "0_product_variant", "product_id": red.id,
            "compute_price": "fixed", "fixed_price": 15.0,
        }])

        self._rule(pricelist)._magento_push_prices()

        self.assertEqual(sorted((p["sku"], p["price"]) for p in self.sent("update_base_prices")),
                         [("SHIRT-BLUE", 20.0), ("SHIRT-RED", 15.0)])

    # ------------------------------------------------------------------
    # failures -> Error Center
    # ------------------------------------------------------------------
    def test_failed_items_become_error_center_issues(self):
        self._mapped("PR-OK", 10.0)
        self._mapped("PR-BAD", 10.0)
        rule = self._rule(self._pricelist([]))
        # Shape returned by Magento 2.4.9 (parameters are a plain list).
        self.calls["update_base_prices"].return_value = [
            {"message": "Invalid attribute %fieldName = %fieldValue.", "parameters": ["SKU", "PR-BAD"]},
        ]

        summary = rule._magento_push_prices()[rule.id]

        issue = self.Issue.search([("instance_id", "=", self.instance.id), ("magento_ref", "=", "PR-BAD")])
        self.assertEqual(issue.entity, "product")
        self.assertEqual(issue.message, "price: Invalid attribute SKU = PR-BAD.")
        self.assertEqual(issue.state, "open")
        self.assertEqual(set(self._states(rule)), {("PR-OK", "base|s0")}, "A failed price must not be recorded")
        self.assertEqual(rule.last_push_state, "partial")
        self.assertIn("1 failed", summary)

        # Next push retries only the failed SKU; its success resolves the issue.
        self.calls["update_base_prices"].return_value = []
        self.reset_calls()
        rule._magento_push_prices()
        self.assertEqual([p["sku"] for p in self.sent("update_base_prices")], ["PR-BAD"])
        self.assertEqual(issue.state, "resolved")
        self.assertEqual(rule.last_push_state, "done")

    def test_failure_message_rendering(self):
        tier = {"message": "No such entity with Customer Group = %customerGroup. Row ID: SKU = %SKU, "
                           "Website ID: %websiteId, Customer Group: %customerGroup, Quantity: %qty.",
                "parameters": ["PR-T", "0", "NoSuchGroup", "5"]}
        self.assertEqual(price_failure_message(tier),
                         "No such entity with Customer Group = NoSuchGroup. Row ID: SKU = PR-T, "
                         "Website ID: 0, Customer Group: NoSuchGroup, Quantity: 5.")
        rendered = {"message": "Could not change non global Price when price scope is global. Row ID: "
                               "SKU = PR-S, Store ID: 1, Price From: , Price To: .",
                    "parameters": ["PR-S", "1", None, None]}
        self.assertEqual(price_failure_message(rendered), rendered["message"])

    def test_tier_failure_attributed_to_its_group_and_quantity(self):
        product = self._mapped("PR-TF", 10.0)
        pricelist = self._pricelist([self._fixed(product, 8.0, min_quantity=5), self._fixed(product, 7.0, min_quantity=10)])
        rule = self._rule(pricelist, kind="tier")
        self.calls["update_tier_prices"].return_value = [{
            "message": "Invalid attribute Price Type = %priceType. Row ID: SKU = %SKU, Website ID: %websiteId, "
                       "Customer Group: %customerGroup, Quantity: %qty.",
            "parameters": ["fixed", "PR-TF", "0", "ALL GROUPS", "10"],
        }]

        rule._magento_push_prices()

        self.assertEqual(set(self._states(rule)), {("PR-TF", "tier|w0|ALL GROUPS|5")})
        issue = self.Issue.search([("magento_ref", "=", "PR-TF")])
        self.assertIn("Quantity: 10", issue.message)

    def test_unattributed_failure_is_recorded_on_the_rule(self):
        self._mapped("PR-U1")
        rule = self._rule(self._pricelist([]))
        self.calls["update_base_prices"].return_value = [
            {"message": "Invalid attribute %fieldName = %fieldValue.", "parameters": ["Price", "-5"]},
        ]
        rule._magento_push_prices()
        self.assertFalse(rule.push_state_ids, "The request is resent when a failure cannot be attributed")
        issue = self.Issue.search([("magento_ref", "=", "price-rule-%s" % rule.id)])
        self.assertEqual(issue.entity, "other")
        self.assertEqual(rule.last_push_state, "failed")

    def test_http_error_of_a_chunk_is_recorded_per_sku(self):
        self._mapped("PR-H1")
        rule = self._rule(self._pricelist([]))
        error = requests.exceptions.HTTPError(response=make_response(400, {"message": "Bad request"}))
        self.calls["update_base_prices"].side_effect = error

        rule._magento_push_prices()

        issue = self.Issue.search([("magento_ref", "=", "PR-H1")])
        self.assertEqual(issue.message, "price: HTTP 400: Bad request")
        self.assertEqual(issue.severity, "permanent")
        self.assertFalse(rule.push_state_ids)

    def test_connection_error_raises_for_job_retry(self):
        self._mapped("PR-NET")
        rule = self._rule(self._pricelist([]))
        self.calls["update_base_prices"].side_effect = requests.exceptions.ConnectionError("down")
        with self.assertRaises(requests.exceptions.ConnectionError):
            rule._magento_push_prices()

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def test_push_is_a_deduplicated_job(self):
        self._mapped("PR-JOB", 10.0)
        manager = self.create_user(
            "price_manager", "base.group_user,sdlc_magento_connector.group_magento_manager")
        rule = self._rule(self._pricelist([self._global_discount(10)]))

        rule.with_user(manager).action_push_prices()
        rule.with_user(manager).action_push_prices()

        jobs = self.Job.search([("identity_key", "=", "prices-%s" % rule.id)])
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs.method_name, jobs.res_ids, jobs.channel),
                         ("_magento_push_prices", [rule.id], "prices-%s" % self.instance.id))
        self.assertEqual(jobs.state, "pending")

        jobs._run_job()
        self.assertEqual(jobs.state, "done")
        self.assertEqual(self.sent("update_base_prices"), [{"sku": "PR-JOB", "price": 9.0, "store_id": 0}])

        user = self.create_user("price_user", "base.group_user,sdlc_magento_connector.group_magento_user")
        with self.assertRaises(Exception):
            rule.with_user(user).action_push_prices()

    def test_price_issue_retry_replays_the_rule_job(self):
        self._mapped("PR-RETRY")
        rule = self._rule(self._pricelist([]))
        self.calls["update_base_prices"].return_value = [
            {"message": "Invalid attribute %fieldName = %fieldValue.", "parameters": ["SKU", "PR-RETRY"]},
        ]
        job = rule._magento_enqueue_push()
        job._run_job()
        issue = self.Issue.search([("magento_ref", "=", "PR-RETRY")])
        self.assertEqual(issue.job_id, job)

        issue.action_retry()

        retry = self.Job.search([("identity_key", "=", "prices-%s" % rule.id), ("state", "=", "pending")])
        self.assertEqual(retry.method_name, "_magento_push_prices")

    def test_cron_queues_active_scheduled_rules(self):
        pricelist = self._pricelist([])
        scheduled = self._rule(pricelist)
        self._rule(pricelist, auto_push=False)
        self._rule(pricelist, active=False)
        jobs = self.Rule._cron_magento_push_prices()
        self.assertEqual(jobs.mapped("identity_key"), ["prices-%s" % scheduled.id])
        cron = self.env.ref("sdlc_magento_connector.ir_cron_magento_push_prices")
        self.assertFalse(cron.active)

    def test_instance_smart_button_and_soft_store_view(self):
        rule = self._rule(self._pricelist([]), scope="store", magento_store_id=1)
        self.assertEqual(self.instance.price_rule_count, 1)
        action = self.instance.action_view_price_rules()
        self.assertEqual(action["domain"], [("instance_id", "=", self.instance.id)])
        if "magento.store.view" not in self.env:
            self.assertFalse(rule.store_view_name)
            self.assertEqual(rule._magento_soft_store_website(), (None, None))
