"""Phase 3.8: Data Ownership, dry-run preview and mapping transforms (all mocked)."""
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.models.magento_field_mapping import (
    TransformError, evaluate_transform, transform_expression_error,
)
from odoo.addons.sdlc_magento_connector.services.magento_api import (
    DRY_RUN, MagentoAPI, MagentoDryRunWriteBlocked,
)

from .common import MagentoTestCommon, RealHttpBlocked, build_item, build_order, build_product, once

ENTITY_LIST_GETTERS = {
    "product": ("sync_products", "get_products"),
    "category": ("sync_categories", "get_categories"),
    "customer": ("sync_customers", "get_customers"),
    "order": ("sync_orders", "get_orders"),
}


def build_customer(magento_id, email, firstname="Roni", lastname="Cost"):
    return {
        "id": magento_id, "email": email, "firstname": firstname, "lastname": lastname,
        "group_id": 1, "store_id": 1, "website_id": 1, "addresses": [],
        "created_at": "2026-09-01 08:00:00", "updated_at": "2026-09-02 08:00:00",
    }


def category_tree(*children):
    return {
        "id": 1, "parent_id": 0, "name": "Root Catalog", "is_active": True,
        "children_data": [{"id": cid, "parent_id": 1, "name": name, "is_active": True,
                           "children_data": []} for cid, name in children],
    }


class _P3Common(MagentoTestCommon):

    def _own(self, entity, direction="two_way", update="create_update"):
        self.instance.write({
            "ownership_%s_direction" % entity: direction,
            "ownership_%s_update" % entity: update,
        })

    def _patch_products(self, *products):
        by_sku = {p["sku"]: p for p in products}
        self.patch_api("get_products", side_effect=once(list(products)))
        self.patch_api("get_product", side_effect=lambda api, sku, *a, **k: dict(by_sku[sku]))

    def _push_jobs(self, record):
        return self.env["magento.job"].search([
            ("identity_key", "in", ("push-%s-%s" % (record._name, record.id),
                                    "push-%s-%s-next" % (record._name, record.id))),
        ])

    def _map(self, sku):
        return self.env["magento.product.map"].with_context(active_test=False).search([
            ("instance_id", "=", self.instance.id), ("sku", "=", sku)])

    def _exportable_map(self, sku, magento_id, **vals):
        attribute_set = self.env["magento.attribute.set"].with_context(**self.no_push_ctx()).create({
            "name": "Default %s" % sku, "magento_id": "4%s" % magento_id, "instance_id": self.instance.id})
        return self.create_map(self.create_product(sku), magento_id,
                               attribute_set_id=attribute_set.id, **vals)

    def _import_order(self, magento_id, sku, **order_vals):
        self.patch_order_side_calls()
        if not self._map(sku):
            self.create_map(self.create_product(sku), magento_id + 50000)
        self.import_orders([build_order(magento_id, [build_item(1, sku, qty=1, price=5.0)], **order_vals)])
        return self.magento_orders(magento_id)


@tagged("post_install", "-at_install", "magento")
class TestDataOwnership(_P3Common):

    def test_defaults_keep_two_way_create_and_update(self):
        for entity in ("product", "category", "customer", "order", "stock", "price"):
            self.assertEqual(self.instance["ownership_%s_direction" % entity], "two_way")
            self.assertEqual(self.instance["ownership_%s_update" % entity], "create_update")
            self.assertIsNone(self.instance._magento_import_block_reason(entity, existing=True))
            self.assertIsNone(self.instance._magento_export_block_reason(entity, existing=True))

    def test_direction_matrix(self):
        expected = {
            "two_way": (True, True),
            "magento_to_odoo": (True, False),
            "odoo_to_magento": (False, True),
            "off": (False, False),
        }
        for direction, (inbound, outbound) in expected.items():
            with self.subTest(direction=direction):
                self._own("product", direction)
                self.assertEqual(not self.instance._magento_import_block_reason("product"), inbound)
                self.assertEqual(not self.instance._magento_export_block_reason("product"), outbound)

    def test_blocked_import_skips_without_fetching(self):
        for entity, (method, getter) in ENTITY_LIST_GETTERS.items():
            for direction in ("odoo_to_magento", "off"):
                with self.subTest(entity=entity, direction=direction):
                    self._own(entity, direction)
                    fetch = self.patch_api(getter, return_value=[])
                    counts = getattr(self.instance, method)()
                    fetch.assert_not_called()
                    self.assertEqual((counts["skipped"], counts["errors"]), (1, 0))
                    self.assertIn("Data Ownership", counts["skip_messages"][0])
                    self._own(entity)

    def test_blocked_import_report_and_watermark(self):
        self._own("order", "odoo_to_magento")
        self.patch_api("get_orders", return_value=[])
        result = self.instance._magento_run_sync_job("orders")
        self.assertIn("skipped", result)
        self.assertFalse(self.instance.orders_sync_watermark)
        report = self.env["magento.sync.report"].search(
            [("instance_id", "=", self.instance.id)], order="id desc", limit=1)
        self.assertEqual(report.sync_type, "order")
        self.assertIn("Data Ownership", report.notes)

    def test_magento_to_odoo_still_imports(self):
        self._own("product", "magento_to_odoo")
        self._patch_products(build_product(501, "OWN-1", name="Owned by Magento"))
        counts = self.instance.sync_products()
        self.assertEqual(counts["created"], 1)
        self.assertEqual(self._map("OWN-1").name, "Owned by Magento")

    def test_create_only_import_skips_existing_creates_new(self):
        product = self.create_product("CO-OLD", name="Odoo name")
        self.create_map(product, 601)
        self._own("product", "two_way", "create_only")
        self._patch_products(build_product(601, "CO-OLD", name="Magento name"),
                             build_product(602, "CO-NEW", name="Brand new"))
        counts = self.instance.sync_products()
        self.assertEqual((counts["created"], counts["updated"], counts["skipped"]), (1, 0, 1))
        self.assertIn("create-only", counts["skip_messages"][0])
        self.assertEqual(self._map("CO-OLD").name, "Odoo name")
        self.assertEqual(product.name, "Odoo name")
        self.assertTrue(self._map("CO-NEW"))

    def test_create_only_resync_one_is_skipped(self):
        product = self.create_product("CO-RS", name="Keep me")
        self.create_map(product, 611)
        self._own("product", "magento_to_odoo", "create_only")
        self.patch_api("get_product", return_value=build_product(611, "CO-RS", name="Changed"))
        counts = self.instance._magento_resync_one("product", "CO-RS")
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(self._map("CO-RS").name, "Keep me")

    def test_create_only_categories_and_customers(self):
        cat = self.env["magento.category"].with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "magento_id": "2", "name": "Odoo Gear"})
        self._own("category", update="create_only")
        self.patch_api("get_categories", return_value=category_tree((2, "Gear"), (3, "Bags")))
        counts = self.instance.sync_categories()
        self.assertEqual(cat.name, "Odoo Gear")
        self.assertTrue(self.env["magento.category"].search([
            ("instance_id", "=", self.instance.id), ("magento_id", "=", "3")]))
        self.assertGreaterEqual(counts["skipped"], 1)

        customer = self.env["magento.customer"].with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "magento_id": "77", "email": "keep@example.com",
            "firstname": "Keep", "lastname": "Name"})
        self._own("customer", update="create_only")
        self.patch_api("get_customers", side_effect=once([build_customer(77, "keep@example.com", "Changed")]))
        counts = self.instance.sync_customers()
        self.assertEqual((counts["skipped"], counts["updated"]), (1, 0))
        self.assertEqual(customer.firstname, "Keep")

    def test_create_only_orders(self):
        order = self._import_order(9101, "ORD-SKU")
        self.assertEqual(len(order), 1)
        before = order.magento_status
        self._own("order", update="create_only")
        changed = build_order(9101, [build_item(1, "ORD-SKU", qty=1, price=5.0)], status="processing")
        counts = self.import_orders([changed])
        self.assertEqual((counts["skipped"], counts["updated"]), (1, 0))
        self.assertEqual(order.magento_status, before)

    def test_price_and_stock_ownership_on_import(self):
        product = self.create_product("PS-1")
        mapping = self.create_map(product, 701, price=10.0, quantity=3.0)
        self._own("price", "odoo_to_magento")
        self._own("stock", "off")
        self._patch_products(build_product(701, "PS-1", price=99.0, qty=50.0))
        self.instance.sync_products()
        self.assertEqual(mapping.price, 10.0, "Odoo owns prices: the import keeps them")
        self.assertEqual(mapping.quantity, 3.0, "Stock is off: the import keeps it")
        # A product created by the import still takes Magento's first values under create-only.
        self._own("price", "two_way", "create_only")
        self._patch_products(build_product(702, "PS-NEW", price=42.0))
        self.instance.sync_products()
        self.assertEqual(self._map("PS-NEW").price, 42.0)

    # ------------------------------------------------------------------
    # Push side
    # ------------------------------------------------------------------
    def test_product_edit_queues_push_only_when_odoo_may_write(self):
        product = self.create_product("PUSH-1", name="Pushable")
        mapping = self.create_map(product, 801)
        mapping.write({"name": "Edited once"})
        self.assertTrue(self._push_jobs(mapping), "Default two-way: the edit is queued")
        self._push_jobs(mapping).write({"state": "done"})

        self._own("product", "magento_to_odoo")
        mapping.write({"name": "Edited twice"})
        product.write({"name": "Edited in Odoo"})
        self.assertFalse(self._push_jobs(mapping).filtered(lambda j: j.state == "pending"))

    def test_auto_push_job_skips_blocked_records(self):
        mapping = self._exportable_map("PUSH-2", 802)
        update = self.patch_api("update_product", return_value={})
        self._own("product", "off")
        result = mapping._magento_auto_push([])
        update.assert_not_called()
        self.assertIn("Skipped 1", result)
        self._own("product")
        self.patch_api("get_product", return_value={})
        with patch.object(type(mapping), "_magento_check_url_key", lambda *a, **k: None):
            mapping._magento_auto_push([])
        self.assertTrue(update.called)

    def test_create_only_push_allows_new_blocks_existing(self):
        self._own("customer", "two_way", "create_only")
        Customer = self.env["magento.customer"]
        new = Customer.create({"instance_id": self.instance.id, "email": "new@example.com",
                               "firstname": "New"})
        self.assertTrue(self._push_jobs(new), "A customer not yet in Magento is created there")
        existing = Customer.with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "email": "old@example.com", "firstname": "Old",
            "magento_id": "55"}).with_env(self.env)
        existing.write({"firstname": "Renamed"})
        self.assertFalse(self._push_jobs(existing))

    def test_category_push_blocked(self):
        self._own("category", "magento_to_odoo")
        category = self.env["magento.category"].with_context(**self.no_push_ctx()).create({
            "instance_id": self.instance.id, "magento_id": "9", "name": "Bags"}).with_env(self.env)
        category.write({"name": "Bags & Totes"})
        self.assertFalse(self._push_jobs(category))

    def test_stock_push_guard(self):
        maps = self.create_map(self.create_product("ST-1", is_storable=True), 901) | \
            self.create_map(self.create_product("ST-2", is_storable=True), 902)
        bulk = self.patch_api("async_bulk", return_value=[])
        self._own("stock", "magento_to_odoo")
        result = self.instance._magento_push_stock_maps(maps)
        bulk.assert_not_called()
        self.assertEqual(result["skipped"], 2)
        self.assertIn("Data Ownership", result["skip_reason"])
        with self.assertRaises(UserError):
            self.instance.action_push_stock_bulk()

        self._own("stock", "two_way", "create_only")
        maps[0].with_context(**self.no_push_ctx()).write({"last_stock_push_at": "2026-10-01 00:00:00"})
        captured = []
        with patch.object(type(self.env["magento.bulk.operation"]), "_submit",
                          lambda op, inst, operation, method, route, entries, **kw:
                          captured.extend(entries) or op.browse()):
            result = self.instance._magento_push_stock_maps(maps)
        self.assertEqual([entry["ref"] for entry in captured], ["ST-2"])
        self.assertEqual(result["skipped"], 1)

    def test_price_push_guard(self):
        pricelist = self.env["product.pricelist"].create({
            "name": "P3", "currency_id": self.env.company.currency_id.id})
        rule = self.env["magento.price.rule"].create({
            "name": "base rule", "instance_id": self.instance.id, "pricelist_id": pricelist.id,
            "kind": "base"})
        Rule = type(rule)
        with patch.object(Rule, "_magento_push_one", autospec=True, return_value="pushed") as push_one:
            self.assertEqual(rule._magento_push_prices()[rule.id], "pushed")
            self.assertEqual(push_one.call_count, 1)
            self._own("price", "magento_to_odoo")
            summary = rule._magento_push_prices()[rule.id]
            self.assertEqual(push_one.call_count, 1, "Magento owns prices: nothing pushed")
            self.assertIn("Data Ownership", summary)
            self.assertIn("Skipped", rule.last_push_summary)
            self._own("price", "two_way", "create_only")
            rule.last_push_at = "2026-10-01 00:00:00"
            rule._magento_push_prices()
            self.assertEqual(push_one.call_count, 1, "Create only: a rule pushed once is not pushed again")

    def test_product_payload_drops_price_and_stock_magento_owns(self):
        mapping = self._exportable_map("PL-1", 951, price=12.0)
        payload = mapping._build_magento_payload()
        self.assertIn("price", payload["product"])
        self._own("price", "magento_to_odoo")
        self._own("stock", "magento_to_odoo")
        payload = mapping._build_magento_payload()
        self.assertNotIn("price", payload["product"])
        self.assertNotIn("stock_item", (payload["product"].get("extension_attributes") or {}))

    def test_order_pushes_blocked(self):
        order = self._import_order(9201, "ORD-2")
        self.assertEqual(len(order), 1)
        comment = self.patch_api("add_order_comment", return_value={})
        self._own("order", "magento_to_odoo")
        with self.assertRaisesRegex(UserError, "Data Ownership"):
            order._magento_push_status("complete", "paid")
        comment.assert_not_called()
        self.assertIsNone(order._magento_push_shipments())
        self._own("order", "two_way", "create_only")
        with self.assertRaisesRegex(UserError, "create-only"):
            order._magento_push_status("complete", "paid")

    def test_apply_mapping_to_existing_respects_ownership(self):
        field = self.env["magento.available.field"].create({
            "name": "Name", "code": "name", "instance_id": self.instance.id, "mapping_type": "product"})
        mapping = self.env["magento.field.mapping"].create({
            "mapping_type": "product", "instance_id": self.instance.id,
            "odoo_field": self.env["ir.model.fields"]._get("magento.product.map", "name").id,
            "magento_field_id": field.id})
        self.create_map(self.create_product("AM-1"), 1201)
        bulk = self.patch_api("async_bulk", return_value=[])
        update = self.patch_api("update_product", return_value={})
        self._own("product", "magento_to_odoo")
        result = mapping.action_apply_to_existing()
        bulk.assert_not_called()
        update.assert_not_called()
        self.assertIn("Data Ownership", result["params"]["message"])


@tagged("post_install", "-at_install", "magento")
class TestDryRun(_P3Common):

    def _preview(self, entity="product", **vals):
        run = self.env["magento.dryrun"].create(dict(instance_id=self.instance.id, entity=entity, **vals))
        run.action_preview()
        return run

    def _lines(self, run):
        return {line.magento_ref: line for line in run.line_ids}

    def test_preview_captures_creates_and_updates_and_leaves_db_unchanged(self):
        product = self.create_product("DR-OLD", name="Old name")
        mapping = self.create_map(product, 1001)
        self._patch_products(build_product(1001, "DR-OLD", name="New name"),
                             build_product(1002, "DR-NEW", name="Brand new"))
        counts_before = {model: self.env[model].with_context(active_test=False).search_count([])
                         for model in ("product.product", "product.template", "magento.product.map",
                                       "magento.job", "magento.sync.issue", "magento.attribute.set")}

        run = self._preview()

        lines = self._lines(run)
        self.assertEqual((run.create_count, run.update_count, run.error_count), (1, 1, 0))
        self.assertEqual(lines["DR-NEW"].action, "create")
        self.assertEqual(lines["DR-OLD"].action, "update")
        diff = lines["DR-OLD"]._diff_rows()
        name_changes = [d for d in diff if d["field"] == "name"]
        self.assertIn(("Old name", "New name"), {(d["old"], d["new"]) for d in name_changes})
        self.assertIn("New name", lines["DR-OLD"].diff_html)
        self.assertEqual(lines["DR-OLD"].target_model, "magento.product.map")
        self.assertEqual(lines["DR-OLD"].target_id, mapping.id)
        self.assertFalse(lines["DR-NEW"].target_id, "The record it would create does not exist")
        # Nothing of the import survived the preview.
        counts_after = {model: self.env[model].with_context(active_test=False).search_count([])
                        for model in counts_before}
        self.assertEqual(counts_after, counts_before)
        self.assertEqual(mapping.name, product.name)
        self.assertEqual(product.name, "Old name")
        self.assertEqual(run.state, "previewed")

    def test_preview_of_synced_records_is_unchanged(self):
        data = build_product(1101, "DR-SAME", name="Same")
        self._patch_products(data)
        self.instance.sync_products()
        self._patch_products(data)
        run = self._preview()
        line = self._lines(run)["DR-SAME"]
        self.assertEqual(line.action, "unchanged", line.diff_json)

    def test_preview_respects_ownership(self):
        self.create_map(self.create_product("DR-OWN"), 1201)
        self._own("product", "two_way", "create_only")
        self._patch_products(build_product(1201, "DR-OWN", name="Changed"))
        line = self._preview().line_ids
        self.assertEqual(line.action, "skip")
        self.assertIn("create-only", line.message)

    def test_preview_never_writes_to_magento(self):
        Instance = type(self.instance)

        def writes_to_magento(instance, category):
            MagentoAPI(instance).update_category(category.magento_id, {"category": {"name": "x"}})

        self.patch_api("get_categories", return_value=category_tree((5, "Gear")))
        with patch.object(Instance, "_magento_after_category_import", writes_to_magento):
            run = self._preview("category")
        errors = run.line_ids.filtered(lambda l: l.action == "error")
        self.assertTrue(errors)
        self.assertIn("blocked", errors[0].message)
        self.assertEqual(self.http_attempts, [], "No request left Odoo")
        self.assertFalse(self.env["magento.category"].search([
            ("instance_id", "=", self.instance.id), ("magento_id", "=", "5")]))

    def test_api_write_guard(self):
        api = MagentoAPI(self.instance.with_context(magento_dry_run=True))
        with self.assertRaises(MagentoDryRunWriteBlocked):
            api.update_product("SKU", {"product": {}})
        with self.assertRaises(MagentoDryRunWriteBlocked):
            api.create_order({})
        token = DRY_RUN.set(True)
        try:
            with self.assertRaises(MagentoDryRunWriteBlocked):
                MagentoAPI(self.instance).update_category(1, {})
            # Reads still go out (here to the test's HTTP blocker).
            with self.assertRaises(RealHttpBlocked), mute_logger("odoo.addons.sdlc_magento_connector"):
                MagentoAPI(self.instance).get_order(1)
        finally:
            DRY_RUN.reset(token)
        self.assertTrue(self.http_attempts, "The read was attempted")
        self.assertEqual({method for method, _url in self.http_attempts}, {"GET"}, "Only reads went out")

    def test_preview_queues_no_push_and_no_postcommit(self):
        self.create_map(self.create_product("DR-PC"), 1301)
        self._patch_products(build_product(1301, "DR-PC", name="Renamed"))
        funcs_before = len(self.env.cr.postcommit)
        jobs_before = self.env["magento.job"].search_count([])
        self._preview()
        self.assertEqual(self.env["magento.job"].search_count([]), jobs_before)
        self.assertEqual(len(self.env.cr.postcommit), funcs_before)

    def test_order_preview_consumes_no_sequence(self):
        self.patch_order_side_calls()
        self.create_map(self.create_product("DR-ORD"), 9399)
        self.patch_api("get_orders", side_effect=once([build_order(9301, [build_item(1, "DR-ORD", qty=2, price=4.0)])]))
        sequence = self.env["ir.sequence"].search([("code", "=", "sale.order")], limit=1)
        next_before = sequence.number_next_actual
        orders_before = self.env["sale.order"].search_count([])
        run = self._preview("order")
        self.assertEqual(run.line_ids.action, "create", run.line_ids.message)
        self.assertEqual(self.env["sale.order"].search_count([]), orders_before)
        sequence.invalidate_recordset()
        self.assertEqual(sequence.number_next_actual, next_before)

    def test_apply_queues_jobs(self):
        self.create_map(self.create_product("AP-OLD", name="Old"), 1401)
        self._patch_products(build_product(1401, "AP-OLD", name="New"),
                             build_product(1402, "AP-NEW"))
        run = self._preview()
        run.action_apply()
        jobs = run.job_ids
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs.mapped("method_name")), {"_magento_resync_one"})
        self.assertEqual(sorted(job.args[1] for job in jobs), ["AP-NEW", "AP-OLD"])
        self.assertEqual(set(jobs.mapped("channel")), {"magento.%s.dryrun" % self.instance.id})
        self.assertEqual(run.state, "applied")
        self.assertTrue(all(run.line_ids.mapped("job_id")))
        # Running a job imports for real.
        self.patch_api("get_product", side_effect=lambda api, sku: build_product(
            1402 if sku == "AP-NEW" else 1401, sku, name="New"))
        for job in jobs:
            job._run_job()
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(self._map("AP-OLD").name, "New")
        self.assertTrue(self._map("AP-NEW"))

    def test_apply_full_sync_and_nothing_to_apply(self):
        run = self.env["magento.dryrun"].create({"instance_id": self.instance.id, "entity": "category"})
        self.patch_api("get_categories", return_value={})
        run.action_preview()
        with self.assertRaises(UserError):
            run.action_apply()
        run.with_context(magento_sync_queue=True).action_apply_full_sync()
        self.assertEqual(run.job_ids.mapped("method_name"), ["_magento_run_sync_job"])

    def test_view_lines_carries_action_filter(self):
        self._patch_products(build_product(1501, "VL-1"))
        run = self._preview()
        action = run.with_context(dryrun_action="create").action_view_lines()
        self.assertEqual(action["domain"], [("dryrun_id", "=", run.id)])
        self.assertTrue(action["context"].get("search_default_action_create"))


@tagged("post_install", "-at_install", "magento")
class TestTransforms(_P3Common):

    def _mapping(self, code="name", odoo_field="name", expr=False, mapping_type="product",
                 model="magento.product.map", direction="both"):
        field = self.env["magento.available.field"].search([
            ("instance_id", "=", self.instance.id), ("code", "=", code),
            ("mapping_type", "=", mapping_type)]) or self.env["magento.available.field"].create({
                "name": code, "code": code, "instance_id": self.instance.id, "mapping_type": mapping_type})
        return self.env["magento.field.mapping"].create({
            "mapping_type": mapping_type, "instance_id": self.instance.id,
            "odoo_field": self.env["ir.model.fields"]._get(model, odoo_field).id,
            "magento_field_id": field.id, "transform_expr": expr, "transform_direction": direction,
        })

    def test_valid_transform_inbound_and_outbound(self):
        self._mapping(expr="upper(strip(value))")
        vals = self.env["magento.field.mapping"].apply_inbound_mapping_to_vals(
            "product", self.instance, {"sku": "T-1", "name": "  backpack "})
        self.assertEqual(vals["name"], "BACKPACK")

        mapping = self.create_map(self.create_product("T-2", name="Mixed Case"), 2002)
        payload = self.env["magento.field.mapping"].apply_outbound_mapping_to_payload(
            "product", self.instance, mapping, {"product": {}}, "product")
        self.assertEqual(payload["product"]["name"], "MIXED CASE")

    def test_helpers_and_record(self):
        self.assertEqual(evaluate_transform("default(value, 'n/a')", ""), "n/a")
        self.assertEqual(evaluate_transform("replace(value, '-', ' ')", "a-b"), "a b")
        self.assertEqual(evaluate_transform("round(float(value) * 1.2, 2)", "10"), 12.0)
        self.assertEqual(evaluate_transform("record['sku'] + '-' + str(value)", 3, {"sku": "X"}), "X-3")
        self.assertEqual(evaluate_transform("lower(value)", None), None)

    def test_direction_of_transform(self):
        self._mapping(expr="upper(value)", direction="outbound")
        vals = self.env["magento.field.mapping"].apply_inbound_mapping_to_vals(
            "product", self.instance, {"sku": "T-3", "name": "plain"})
        self.assertEqual(vals["name"], "plain", "An export-only transform is not applied on import")

    def test_invalid_transform_is_a_clear_validation_error(self):
        for expr, error in (("value +", "Invalid transform expression"),
                            ("env['res.users'].search([])", "Unknown name.*env"),
                            ("open('/etc/passwd').read()", "Unknown name.*open")):
            with self.subTest(expr=expr), self.assertRaisesRegex(ValidationError, error), \
                    self.env.cr.savepoint():
                self._mapping(expr=expr)
        mapping = self._mapping(expr="upper(value)")
        with self.assertRaisesRegex(ValidationError, "Unknown name.*env"), self.env.cr.savepoint():
            mapping.transform_expr = "env.cr"
            mapping.flush_recordset()
        self.assertIsNone(transform_expression_error("[upper(v) for v in value]"))

    def test_safe_eval_does_not_expose_env(self):
        for expr, record in (("env", None), ("record.env", {}), ("value.env", None),
                             ("value.__class__", None), ("__import__('os')", None)):
            with self.subTest(expr=expr), self.assertRaises(TransformError):
                evaluate_transform(expr, self.instance.name, record)
        # An Odoo record never reaches the namespace: outbound ``record`` is a plain dict.
        mapping = self.create_map(self.create_product("T-ENV"), 2003)
        record_dict = self.env["magento.field.mapping"]._transform_record_dict(mapping)
        self.assertIsInstance(record_dict, dict)
        self.assertTrue(all(not hasattr(v, "env") for v in record_dict.values()))

    def test_runtime_error_records_issue_and_leaves_field(self):
        transform = self._mapping(code="name", odoo_field="short_description", expr="int(value)")
        product = self.create_product("T-RT", name="Runtime")
        mapping = self.create_map(product, 2004, short_description="Keep me")
        data = build_product(2004, "T-RT", name="Not a number")
        self._patch_products(data)
        with mute_logger("odoo.addons.sdlc_magento_connector.models.magento_field_mapping"):
            counts = self.instance.sync_products()
        self.assertEqual((counts["errors"], counts["updated"]), (0, 1),
                         "A transform error never fails the record")
        issue = self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("entity", "=", "other"),
            ("magento_ref", "like", "T-RT")])
        self.assertEqual(issue.state, "open")
        self.assertIn("Transform", issue.message)
        self.assertIn("left unchanged", issue.message)
        self.assertEqual(mapping.short_description, "Keep me", "The mapped field is untouched")
        self.assertEqual(mapping.name, "Not a number", "The rest of the record is imported")
        # Fixed transform: it applies and the issue resolves itself.
        transform.transform_expr = "upper(value)"
        self._patch_products(data)
        self.instance.sync_products()
        self.assertEqual(issue.state, "resolved")
        self.assertEqual(mapping.short_description, "NOT A NUMBER")

    def test_live_test_runs_on_magento_sample(self):
        mapping = self._mapping(expr="upper(value)")
        self.patch_api("get_products", return_value=[build_product(3001, "LT-1", name="Live bag")])
        mapping.action_live_test_transform()
        self.assertEqual(mapping.transform_test_status, "ok")
        self.assertEqual(mapping.transform_test_input, "'Live bag'")
        self.assertEqual(mapping.transform_test_output, "'LIVE BAG'")
        self.assertIn("LT-1", mapping.transform_test_source)

        mapping.transform_expr = "int(value)"
        mapping.action_live_test_transform()
        self.assertEqual(mapping.transform_test_status, "error")
        self.assertIn("ValueError", mapping.transform_test_output)

    def test_transform_error_shows_in_dry_run(self):
        self._mapping(expr="int(value)")
        self._patch_products(build_product(3101, "DR-TF", name="not a number"))
        run = self.env["magento.dryrun"].create({"instance_id": self.instance.id, "entity": "product"})
        with mute_logger("odoo.addons.sdlc_magento_connector.models.magento_field_mapping"):
            run.action_preview()
        self.assertEqual(run.line_ids.action, "create")
        self.assertIn("Transform", run.line_ids.message)
        self.assertFalse(self.env["magento.sync.issue"].search([("magento_ref", "like", "DR-TF")]),
                         "The preview's Error Center issue was rolled back too")
