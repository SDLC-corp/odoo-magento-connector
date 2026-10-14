"""Phase 3.10: daily digest, net profit report, settings export/import.

All figures are built from records created here; no HTTP is made (common.py blocks it).
The digest runs at a fixed ``NOW`` in UTC, so "yesterday" is 2026-10-12 and the 7-day
window starts 2026-10-06.
"""
import base64
import json
import re
from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import MagentoTestCommon

NOW = datetime(2026, 10, 13, 9, 0, 0)
LINK_URL = re.compile(r"/odoo/(\d+)/action-sdlc_magento_connector\.action_magento_digest_link_open$")
NO_PUSH = {"skip_magento_sync": True, "magento_skip_push": True, "skip_odoo_sync": True}


class InsightsCase(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance.write({"auto_sync_tz": "UTC"})
        cls.reader = cls.env["res.partner"].create({"name": "Digest Reader", "email": "reader@example.com"})
        cls.customer = cls.env["res.partner"].create({"name": "Veronica Costello",
                                                      "email": "roni@example.com"})
        Product = cls.env["product.product"].with_context(**NO_PUSH)
        cls.category = cls.env["product.category"].create({"name": "Bags P310"})
        cls.bag = Product.create({
            "name": "Joust Duffle Bag", "default_code": "24-MB01", "type": "consu",
            "list_price": 10.0, "standard_price": 4.0, "taxes_id": [Command.clear()],
            "categ_id": cls.category.id,
        })
        cls.watch = Product.create({
            "name": "Aim Analog Watch", "default_code": "24-MG04", "type": "consu",
            "list_price": 30.0, "standard_price": 12.0, "taxes_id": [Command.clear()],
        })
        cls.ship_product = Product.create({
            "name": "Magento shipping P310", "type": "service", "taxes_id": [Command.clear()],
        })

    def setUp(self):
        super().setUp()
        # Credit notes of Magento orders may be pushed: never reach Magento.
        self.patch_api("create_credit_memo", return_value=901)
        self.patch_api("create_invoice_refund", return_value=902)

    # ------------------------------------------------------------------ helpers
    def make_order(self, ref, lines, date="2026-10-12 10:00:00", instance=None, shipping=0.0,
                   grand_total=None, payment="checkmo", state=None):
        commands = [
            Command.create({"product_id": product.id, "product_uom_qty": qty, "price_unit": price,
                            "tax_ids": [Command.clear()], "magento_line_role": "product"})
            for product, qty, price in lines
        ]
        if shipping:
            commands.append(Command.create({
                "product_id": self.ship_product.id, "product_uom_qty": 1, "price_unit": shipping,
                "tax_ids": [Command.clear()], "magento_line_role": "shipping"}))
        order = self.env["sale.order"].with_context(**NO_PUSH).create({
            "partner_id": self.customer.id,
            "magento_instance_id": (instance or self.instance).id,
            "magento_order_id": ref,
            "magento_increment_id": ref,
            "magento_payment_method": payment,
            "date_order": date,
            "order_line": commands,
        })
        values = {"magento_grand_total": grand_total if grand_total is not None else order.amount_total}
        if state:
            values["state"] = state
        order.with_context(**NO_PUSH).write(values)
        return order.with_env(self.env)

    def refund(self, order, amount, date="2026-10-12"):
        move = self.env["account.move"].with_context(
            skip_magento_sync=True, default_move_type="out_refund").create({
                "move_type": "out_refund",
                "partner_id": order.partner_id.id,
                "invoice_origin": order.name,
                "invoice_date": date,
                "invoice_line_ids": [Command.create({
                    "name": "Refund", "quantity": 1, "price_unit": amount, "tax_ids": [Command.clear()],
                })],
            })
        self.assertEqual(move.magento_sale_order_id, order)
        move.with_context(skip_magento_sync=True, magento_skip_push=True).action_post()
        return move

    def link_of(self, url):
        match = LINK_URL.search(url or "")
        self.assertTrue(match, "Not a digest link: %r" % url)
        return self.env["magento.digest.link"].browse(int(match.group(1)))

    def open_link(self, url):
        action = self.link_of(url)._magento_open()
        self.assertEqual(action["type"], "ir.actions.act_window")
        return self.env[action["res_model"]].search(action["domain"])


@tagged("post_install", "-at_install", "magento")
class TestDigest(InsightsCase):

    def test_cron_inactive_by_default(self):
        cron = self.env.ref("sdlc_magento_connector.ir_cron_magento_send_digest")
        self.assertFalse(cron.active)
        self.assertEqual(cron.model_id.model, "magento.instance")
        self.assertIn("_cron_magento_send_digest", cron.code)
        template = self.env.ref("sdlc_magento_connector.mail_template_magento_digest")
        self.assertEqual(template.model, "magento.digest")

    def test_digest_numbers_and_links(self):
        self.instance.digest_partner_ids = self.reader
        yesterday_a = self.make_order("D-1", [(self.bag, 2, 10.0)])                      # 20
        yesterday_b = self.make_order("D-2", [(self.watch, 1, 30.0)], shipping=5.0)       # 35
        earlier = self.make_order("D-3", [(self.bag, 1, 10.0)], date="2026-10-09 15:00:00")  # 10
        self.make_order("D-OLD", [(self.bag, 9, 10.0)], date="2026-10-01 10:00:00")       # outside
        self.make_order("D-NEXT", [(self.bag, 9, 10.0)], date="2026-10-13 01:00:00")      # today
        self.refund(yesterday_a, 8.0)
        self.env["magento.job"].create({
            "name": "Sync orders", "instance_id": self.instance.id, "model_name": "magento.instance",
            "method_name": "action_test_connection", "state": "failed",
            "date_done": "2026-10-12 08:00:00",
        })
        self.env["magento.sync.issue"].create({
            "instance_id": self.instance.id, "entity": "order", "magento_ref": "000000777",
            "message": "boom", "first_seen": "2026-10-12 07:00:00",
        })

        digest = self.env["magento.digest"]._magento_build_and_send(self.instance, now=NOW)

        self.assertTrue(digest)
        raw = digest.payload["raw"]
        self.assertEqual((raw["orders_y"], raw["orders_w"]), (2, 3))
        self.assertAlmostEqual(raw["revenue_y"], 55.0)
        self.assertAlmostEqual(raw["revenue_w"], 65.0)
        self.assertAlmostEqual(raw["refunds_y"], 8.0)
        self.assertAlmostEqual(raw["net_y"], 47.0)
        self.assertAlmostEqual(raw["net_w"], 57.0)
        self.assertEqual(raw["failed_jobs_y"], 1)
        self.assertEqual(raw["new_issues_y"], 1)
        self.assertEqual(raw["open_issues"], 1)
        self.assertTrue(digest.payload["has_news"])
        # Top products of the week: the watch (30) before the bag (30 = 20 + 10, ties keep
        # both); amounts in company currency.
        self.assertEqual({pid for pid, _amount in raw["top_products"]}, {self.bag.id, self.watch.id})

        sales = {row["label"]: row for row in digest.payload["sales"]}
        self.assertEqual(self.open_link(sales["Orders"]["y_url"]), yesterday_a | yesterday_b)
        self.assertEqual(self.open_link(sales["Orders"]["w_url"]), yesterday_a | yesterday_b | earlier)
        refunds = self.open_link(sales["Refunds"]["y_url"])
        self.assertEqual(refunds.magento_sale_order_id, yesterday_a)
        health = {row["label"]: row for row in digest.payload["health"]}
        self.assertEqual(len(self.open_link(health["Failed jobs"]["y_url"])), 1)
        # Every figure carries a link, and every link resolves.
        urls = re.findall(r'"(https?://[^"]+)"', json.dumps(digest.payload))
        link_urls = [u for u in urls if "action_magento_digest_link_open" in u]
        self.assertEqual(len(link_urls), len(digest.link_ids))
        for url in link_urls:
            self.link_of(url)._magento_open()

        # The e-mail: queued, rendered, addressed to the recipients, with the links.
        mail = digest.mail_id
        self.assertTrue(mail)
        self.assertEqual(mail.state, "outgoing")
        self.assertIn(self.reader, mail.recipient_ids)
        self.assertIn("Main Magento", mail.body_html)
        self.assertIn(sales["Orders"]["y_url"].replace("&", "&amp;"), mail.body_html)
        self.assertIn("#eb5202", mail.body_html)
        self.assertNotIn("<script", mail.body_html)
        self.assertEqual(self.instance.digest_last_sent_at, NOW)

    def test_skip_when_nothing_new(self):
        quiet = self.create_instance("Quiet store", auto_sync_tz="UTC")
        quiet.digest_partner_ids = self.reader
        # Activity before yesterday only: nothing new.
        self.make_order("Q-1", [(self.bag, 1, 10.0)], date="2026-10-08 10:00:00", instance=quiet)
        Digest = self.env["magento.digest"]
        self.assertFalse(Digest._magento_build_and_send(quiet, now=NOW))
        self.assertFalse(Digest.search([("instance_id", "=", quiet.id)]))
        # "Send Even When Quiet", or a manual send, still sends.
        quiet.digest_always_send = True
        self.assertTrue(Digest._magento_build_and_send(quiet, now=NOW))
        quiet.digest_always_send = False
        self.assertTrue(Digest._magento_build_and_send(quiet, now=NOW, force=True))
        # No recipient: never sent.
        quiet.digest_partner_ids = False
        self.assertFalse(Digest._magento_build_and_send(quiet, now=NOW, force=True))
        with self.assertRaises(UserError):
            quiet.action_send_digest_now()

    def test_cron_sends_per_instance(self):
        self.instance.digest_partner_ids = self.reader
        now = fields.Datetime.now()
        self.make_order("C-1", [(self.bag, 1, 10.0)],
                        date=fields.Datetime.to_string(now.replace(hour=0, minute=0) - timedelta(hours=12)))
        other = self.create_instance("Disabled digest", auto_sync_tz="UTC", digest_enabled=False)
        other.digest_partner_ids = self.reader
        self.env["magento.instance"]._cron_magento_send_digest()
        digests = self.env["magento.digest"].search([])
        self.assertIn(self.instance, digests.instance_id)
        self.assertNotIn(other, digests.instance_id)


@tagged("post_install", "-at_install", "magento")
class TestNetProfit(InsightsCase):

    def rows(self, order):
        self.env.flush_all()
        return self.env["magento.net.profit"].search([("order_id", "=", order.id)])

    def test_net_profit_math(self):
        self.env["magento.payment.map"].create({
            "instance_id": self.instance.id, "magento_method": "checkmo",
            "fee_percent": 2.0, "fee_fixed": 0.30,
        })
        order = self.make_order("NP-1", [(self.bag, 2, 10.0)], shipping=5.0, date="2026-09-15 10:00:00")
        self.assertEqual(order.magento_grand_total, 25.0)
        order.order_line.filtered(lambda l: l.product_id == self.bag).qty_delivered = 2.0
        self.refund(order, 10.0, date="2026-09-20")

        rows = self.rows(order)
        self.assertEqual(len(rows), 1, "One row per product line; shipping is order-level")
        row = rows
        self.assertAlmostEqual(row.order_count, 1.0)
        self.assertAlmostEqual(row.revenue_total, 25.0)
        self.assertAlmostEqual(row.revenue_untaxed, 25.0)
        self.assertAlmostEqual(row.refund_amount, 10.0)
        self.assertAlmostEqual(row.cogs, 8.0)                 # 2 x 4.00 product cost
        self.assertEqual(row.cost_source, "standard")
        self.assertAlmostEqual(row.shipping_revenue, 5.0)
        self.assertAlmostEqual(row.shipping_cost, 5.0)        # estimate: shipping charged
        self.assertTrue(row.shipping_estimated)
        self.assertAlmostEqual(row.payment_fee, 0.80)         # 2% of 25 + 0.30
        self.assertAlmostEqual(row.net_profit, 25.0 - 10.0 - 8.0 - 5.0 - 0.80)
        self.assertEqual(row.categ_id, self.category)
        self.assertEqual(row.payment_method, "checkmo")

    def test_no_shipping_is_not_estimated(self):
        order = self.make_order("NP-2", [(self.watch, 1, 30.0)], date="2026-09-15 10:00:00")
        row = self.rows(order)
        self.assertFalse(row.shipping_estimated)
        self.assertAlmostEqual(row.shipping_cost, 0.0)
        self.assertAlmostEqual(row.payment_fee, 0.0, msg="No fee configured")
        self.assertAlmostEqual(row.cogs, 0.0, msg="Nothing delivered yet")
        self.assertEqual(row.cost_source, "none")
        self.assertAlmostEqual(row.net_profit, 30.0)

    def test_tax_and_cancelled(self):
        order = self.make_order("NP-3", [(self.watch, 1, 30.0)], grand_total=33.0)
        order.with_context(**NO_PUSH).write({"magento_grand_total": 33.0})
        # Odoo's tax is 0 here; Magento's grand total wins for revenue incl. tax.
        row = self.rows(order)
        self.assertAlmostEqual(row.revenue_total, 33.0)
        cancelled = self.make_order("NP-4", [(self.bag, 1, 10.0)], state="cancel")
        self.assertFalse(self.rows(cancelled))

    def test_view_groups(self):
        second = self.create_instance("Second store", auto_sync_tz="UTC")
        a = self.make_order("G-1", [(self.bag, 1, 10.0), (self.watch, 1, 30.0)], shipping=4.0,
                            date="2026-08-10 10:00:00")
        b = self.make_order("G-2", [(self.bag, 2, 10.0)], date="2026-09-10 10:00:00",
                            payment="paypal_express")
        c = self.make_order("G-3", [(self.watch, 1, 30.0)], date="2026-09-11 10:00:00", instance=second)
        self.env.flush_all()
        NetProfit = self.env["magento.net.profit"]
        domain = [("order_id", "in", (a | b | c).ids)]
        # Order-level amounts are allocated to lines: per order, the lines add up.
        per_order = dict(NetProfit._read_group(domain, ["order_id"], ["revenue_untaxed:sum"]))
        self.assertAlmostEqual(per_order[a], 44.0)
        self.assertAlmostEqual(per_order[b], 20.0)
        self.assertAlmostEqual(per_order[c], 30.0)
        # The "Orders" measure counts orders, whatever the number of lines.
        [(orders,)] = NetProfit._read_group(domain, [], ["order_count:sum"])
        self.assertAlmostEqual(orders, 3.0)
        by_month = {m.strftime("%Y-%m"): v for m, v in NetProfit._read_group(
            domain, ["date_order:month"], ["order_count:sum"])}
        self.assertEqual(by_month, {"2026-08": 1.0, "2026-09": 2.0})
        by_instance = dict(NetProfit._read_group(domain, ["instance_id"], ["revenue_untaxed:sum"]))
        self.assertAlmostEqual(by_instance[self.instance], 64.0)
        self.assertAlmostEqual(by_instance[second], 30.0)
        by_payment = dict(NetProfit._read_group(domain, ["payment_method"], ["order_count:sum"]))
        self.assertEqual(by_payment, {"checkmo": 2.0, "paypal_express": 1.0})
        by_categ = dict(NetProfit._read_group(domain, ["categ_id"], ["revenue_untaxed:sum"]))
        # Order A: bag line is 10/40 of the products, so 1/4 of the 44 order revenue.
        self.assertAlmostEqual(by_categ[self.category], 11.0 + 20.0)
        # Website: the instance's default website.
        website = self.env["magento.website"].create({
            "instance_id": self.instance.id, "magento_id": 1, "code": "base", "name": "Main Website",
            "is_default": True})
        self.env.flush_all()
        by_website = dict(NetProfit._read_group(domain, ["website_id"], ["order_count:sum"]))
        self.assertAlmostEqual(by_website[website], 2.0)

    def test_dashboard_net_profit_panel_and_digest(self):
        self.instance.digest_partner_ids = self.reader
        today = fields.Datetime.now()
        self.make_order("DB-1", [(self.bag, 1, 10.0)], shipping=5.0, date=fields.Datetime.to_string(today))
        Console = self.env["magento.dashboard"]
        data = Console.get_dashboard_data(instance_ids=self.instance.ids, days=7)
        panel = data["net_profit"]
        self.assertTrue(panel)
        self.assertEqual(panel["orders"], 1)
        self.assertAlmostEqual(panel["net"], 15.0 - 5.0)
        self.assertEqual([r["key"] for r in panel["rows"]],
                         ["revenue_untaxed", "refund_amount", "cogs", "shipping_cost", "payment_fee"])
        self.assertEqual(panel["estimated_orders"], 1)
        for row in panel["rows"]:
            self.assertEqual(row["drill"]["res_model"], "magento.net.profit")
            self.assertEqual(row["drill"]["context"], {"group_by": ["order_id"]})
        self.assertEqual(panel["drill"]["xml_id"], "sdlc_magento_connector.action_magento_net_profit")
        # The rail is full: net profit is a panel, not a tile.
        self.assertNotIn("net_profit", [k["key"] for k in data["kpis"]])
        self.assertFalse(data["last_digest"])
        digest = self.env["magento.digest"]._magento_build_and_send(self.instance, force=True)
        data = Console.get_dashboard_data(instance_ids=self.instance.ids, days=7)
        self.assertEqual(data["last_digest"]["drill"]["res_id"], digest.id)
        links = [link["xml_id"] for grp in Console.with_user(self.env.ref("base.user_admin")).get_nav_links()
                 for link in grp["links"]]
        self.assertIn("sdlc_magento_connector.action_magento_net_profit", links)
        self.assertIn("sdlc_magento_connector.action_magento_settings_transfer", links)


@tagged("post_install", "-at_install", "magento")
class TestSettingsTransfer(InsightsCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        inst = cls.instance
        cls.journal = env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.pricelist = env["product.pricelist"].create({"name": "Magento Wholesale P310"})
        cls.alert_user = env.ref("base.user_admin")
        inst.with_context(**NO_PUSH).write({
            "log_retention_days": 12,
            "auto_create_taxes": False,
            "alert_user_ids": [Command.set(cls.alert_user.ids)],
            "digest_partner_ids": [Command.set(cls.reader.ids)],
            "digest_always_send": True,
            "payment_journal_id": cls.journal.id,
        })
        if "webhook_enabled" in inst._fields:
            inst.sudo().write({"webhook_enabled": True})
        cls.tax = env["account.tax"].create({
            "name": "MG 8.25% P310", "amount": 8.25, "type_tax_use": "sale", "company_id": cls.company.id})
        env["magento.tax.map"].create({"instance_id": inst.id, "magento_code": "US-MI-*-Rate 1",
                                       "percent": 8.25, "tax_id": cls.tax.id})
        env["magento.payment.map"].create({
            "instance_id": inst.id, "magento_method": "checkmo", "name": "Check / Money order",
            "journal_id": cls.journal.id, "fee_percent": 1.5, "fee_fixed": 0.25})
        env["magento.carrier.map"].create({"instance_id": inst.id, "magento_code": "flatrate_flatrate",
                                           "name": "Flat Rate"})
        cls.group = env["magento.customer.group"].create({
            "instance_id": inst.id, "magento_group_id": 2, "name": "Wholesale",
            "pricelist_id": cls.pricelist.id})
        env["magento.price.rule"].create({
            "instance_id": inst.id, "name": "Wholesale tiers", "pricelist_id": cls.pricelist.id,
            "kind": "tier", "customer_group_mode": "group", "customer_group_id": cls.group.id})
        cls.reason = env["magento.return.reason"].create({
            "instance_id": inst.id, "code": "DMG", "name": "Damaged", "restock": False})
        stock = env.ref("stock.stock_location_stock")
        env["magento.return.location.rule"].create({
            "instance_id": inst.id, "reason_id": cls.reason.id, "location_id": stock.id})
        env["magento.stock.source.map"].create({
            "instance_id": inst.id, "source_code": "default", "location_ids": [Command.set(stock.ids)]})
        field = env["magento.available.field"].create({
            "name": "Meta title", "code": "custom_attributes.meta_title",
            "instance_id": inst.id, "mapping_type": "product"})
        env["magento.field.mapping"].create({
            "mapping_type": "product", "instance_id": inst.id,
            "odoo_field": env["ir.model.fields"]._get("magento.product.map", "name").id,
            "magento_field_id": field.id})
        cls.target = cls.create_instance("Staging store", base_url="https://staging.example.test")
        cls.IO = env["magento.settings.io"]

    def export(self, instance=None):
        return self.IO._magento_export(instance or self.instance)

    def wizard(self, data, target=None):
        return self.env["magento.settings.transfer"].create({
            "instance_id": (target or self.target).id,
            "import_file": base64.b64encode(json.dumps(data).encode()).decode(),
            "import_filename": "settings.json",
        })

    def test_export_contains_no_secrets(self):
        inst = self.instance.sudo()
        if "consumer_secret" in inst._fields:
            inst.write({"consumer_key": "ck-SECRET-1", "consumer_secret": "cs-SECRET-2",
                        "access_token_secret": "ats-SECRET-3"})
        inst.customer_default_password = "pw-SECRET-4"
        data = self.export()
        self.assertEqual(data["format"], "sdlc_magento_settings/1")
        text = json.dumps(data)
        for secret in ("test-integration-token", "ck-SECRET-1", "cs-SECRET-2", "ats-SECRET-3",
                       "pw-SECRET-4"):
            self.assertNotIn(secret, text)
        if "webhook_secret" in inst._fields:
            self.assertNotIn(inst.webhook_secret, text)
            self.assertNotIn(inst.webhook_uuid, text)
        for key in data["instance"]:
            self.assertNotRegex(key, r"token|secret|passw|consumer_key")
        # Identity and runtime state stay with the instance.
        for key in ("name", "base_url", "orders_sync_watermark", "state", "auto_sync_cron_id",
                    "digest_last_sent_at", "auth_method", "oauth_signature_method"):
            self.assertNotIn(key, data["instance"])
        self.assertEqual(data["instance"]["log_retention_days"], 12)
        self.assertEqual(data["sections"]["payment_map"][0]["fee_percent"], 1.5)
        self.assertEqual(data["sections"]["payment_map"][0]["journal_id"]["keys"]["code"], self.journal.code)
        self.assertEqual(data["sections"]["price_rules"][0]["pricelist_id"]["keys"],
                         {"name": "Magento Wholesale P310"})
        # The download action.
        action = self.instance.action_export_settings()
        self.assertEqual(action["type"], "ir.actions.act_url")
        attachment = self.env["ir.attachment"].browse(int(re.search(r"/web/content/(\d+)", action["url"]).group(1)))
        self.assertEqual(json.loads(attachment.raw.content)["format"], "sdlc_magento_settings/1")

    def test_preview_reports_unresolved_references(self):
        data = self.export()
        data["sections"]["price_rules"][0]["pricelist_id"] = {
            "model": "product.pricelist", "keys": {"name": "No such pricelist"}, "label": "No such pricelist"}
        data["sections"]["payment_map"][0]["journal_id"] = {
            "model": "account.journal", "keys": {"code": "NOPE", "name": "Nope"}, "label": "Nope"}
        wizard = self.wizard(data)
        wizard.action_preview()
        self.assertEqual(wizard.state, "preview")
        lines = {(l.section, l.name): l for l in wizard.line_ids}
        rule = lines[("Price rules", "Wholesale tiers")]
        self.assertEqual(rule.action, "error", "Pricelist is required: the rule cannot be imported")
        self.assertIn("No such pricelist", rule.unresolved)
        payment = lines[("Payment methods", "Check / Money order")]
        self.assertEqual(payment.action, "create", "An optional journal is left empty")
        self.assertIn("Nope", payment.unresolved)
        # References to records the same import creates (the return reason) are not unresolved.
        loc_rule = wizard.line_ids.filtered(lambda l: l.section == "Return location rules")
        self.assertEqual(loc_rule.action, "create")
        self.assertFalse(loc_rule.unresolved)
        self.assertGreaterEqual(wizard.unresolved_count, 2)
        self.assertEqual(wizard.error_count, 1)
        # Preview writes nothing.
        self.assertFalse(self.env["magento.payment.map"].search([("instance_id", "=", self.target.id)]))

    def test_import_is_idempotent(self):
        data = self.export()
        wizard = self.wizard(data)
        wizard.action_preview()
        self.assertGreater(wizard.create_count, 0)
        wizard.action_apply()
        self.assertEqual(wizard.state, "done")
        self.assertEqual(wizard.error_count, 0, wizard.line_ids.filtered(lambda l: l.action == "error").mapped("unresolved"))
        counts = {key: self.env[model].with_context(active_test=False).search_count(
                  [("instance_id", "=", self.target.id)]) for key, _l, model, _k in self.IO._magento_sections()}
        again = self.wizard(data)
        again.action_preview()
        self.assertEqual((again.create_count, again.update_count, again.error_count), (0, 0, 0))
        again.action_apply()
        counts_after = {key: self.env[model].with_context(active_test=False).search_count(
                        [("instance_id", "=", self.target.id)]) for key, _l, model, _k in self.IO._magento_sections()}
        self.assertEqual(counts, counts_after)

    def test_round_trip_between_instances(self):
        data = self.export()
        self.wizard(data).action_apply()
        target = self.target
        self.assertEqual(target.log_retention_days, 12)
        self.assertFalse(target.auto_create_taxes)
        self.assertEqual(target.alert_user_ids, self.alert_user)
        self.assertEqual(target.payment_journal_id, self.journal)
        self.assertEqual(target.name, "Staging store", "Identity is never imported")
        self.assertEqual(target.base_url, "https://staging.example.test")
        group = self.env["magento.customer.group"].search([("instance_id", "=", target.id)])
        self.assertEqual((group.magento_group_id, group.pricelist_id), (2, self.pricelist))
        rule = self.env["magento.price.rule"].search([("instance_id", "=", target.id)])
        self.assertEqual(rule.customer_group_id, group, "Re-linked to the target's own group")
        loc_rule = self.env["magento.return.location.rule"].search([("instance_id", "=", target.id)])
        self.assertEqual(loc_rule.reason_id.instance_id, target)
        mapping = self.env["magento.field.mapping"].search([("instance_id", "=", target.id)])
        self.assertEqual(mapping.magento_field_id.instance_id, target)
        pay = self.env["magento.payment.map"].search([("instance_id", "=", target.id)])
        self.assertEqual((pay.fee_percent, pay.fee_fixed, pay.journal_id), (1.5, 0.25, self.journal))
        # Exporting the target gives back the same configuration.
        again = self.export(target)
        self.assertEqual(again["sections"], data["sections"])
        self.assertEqual(again["instance"], data["instance"])

    def test_invalid_instance_settings_do_not_block_sections(self):
        data = self.export()
        data["instance"]["log_retention_days"] = 0       # refused by the instance constraint
        wizard = self.wizard(data)
        wizard.action_apply()
        line = wizard.line_ids.filtered(lambda l: l.section == "Instance settings")
        self.assertEqual(line.action, "error")
        self.assertIn("at least one day", line.unresolved)
        self.assertNotEqual(self.target.log_retention_days, 0)
        self.assertTrue(self.env["magento.payment.map"].search([("instance_id", "=", self.target.id)]))

    def test_rejects_unknown_format(self):
        wizard = self.wizard({"format": "something/2"})
        with self.assertRaises(UserError):
            wizard.action_preview()
