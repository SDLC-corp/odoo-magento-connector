from datetime import date, timedelta
from unittest.mock import patch

import requests

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import MagentoTestCommon


def mg_order(entity_id, total, created="2026-10-01 10:00:00", currency="USD", store=1,
             status="pending", increment_id=None):
    """One item as GET /V1/orders returns it with the reconciliation ``fields`` projection."""
    return {
        "entity_id": entity_id,
        "increment_id": increment_id or "%09d" % (100000000 + entity_id),
        "created_at": created,
        "grand_total": total,
        "order_currency_code": currency,
        "store_id": store,
        "status": status,
    }


@tagged("post_install", "-at_install", "magento")
class TestMagentoReconciliation(MagentoTestCommon):
    """1.6 reconciliation: Magento vs Odoo order counts and totals per day."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Recon = cls.env["magento.reconciliation"]
        cls.usd = cls.env.ref("base.USD")
        cls.eur = cls.env.ref("base.EUR")
        (cls.usd | cls.eur).write({"active": True})
        cls.partner = cls.env["res.partner"].create({"name": "Reconciliation Customer"})
        cls.product = cls.env["product.product"].with_context(
            skip_magento_sync=True, magento_skip_push=True, skip_odoo_sync=True,
        ).create({"name": "Recon Item", "default_code": "RECON-1", "type": "consu", "list_price": 10.0})

    def setUp(self):
        super().setUp()
        self.search_calls = []
        self.magento_items = []
        test = self

        def _iter_search(api, endpoint, **kwargs):
            test.search_calls.append(dict(kwargs, endpoint=endpoint))
            return iter(list(test.magento_items))

        self.patch_api("iter_search", side_effect=_iter_search)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def odoo_order(self, entity_id, total, created="2026-10-01 10:00:00", currency=None,
                   instance=None, **extra):
        currency = currency or self.usd
        # sale.order recomputes its currency from the pricelist: give it one in that currency.
        pricelist = self.env["product.pricelist"].search([("currency_id", "=", currency.id)], limit=1)             or self.env["product.pricelist"].create({"name": "Recon %s" % currency.name,
                                                     "currency_id": currency.id})
        vals = {
            "partner_id": self.partner.id,
            "magento_instance_id": (instance or self.instance).id,
            "magento_order_id": str(entity_id),
            "magento_increment_id": "%09d" % (100000000 + entity_id),
            "magento_currency": currency.name,
            "magento_created_at": created,
            "date_order": created,
            "pricelist_id": pricelist.id,
            "currency_id": currency.id,
            "order_line": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": total,
                "tax_ids": [Command.clear()],
            })],
        }
        vals.update(extra)
        order = self.env["sale.order"].with_context(**self.no_push_ctx()).create(vals)
        self.assertAlmostEqual(order.amount_total, total, places=2)
        if "currency_id" not in extra:
            self.assertEqual(order.currency_id, currency)
        return order

    def make_run(self, date_from="2026-10-01", date_to="2026-10-02", instance=None, **extra):
        vals = {"instance_id": (instance or self.instance).id, "date_from": date_from, "date_to": date_to}
        vals.update(extra)
        return self.Recon.create(vals)

    def run_recon(self, **kwargs):
        run = self.make_run(**kwargs)
        run.action_run()
        self.assertEqual(run.state, "done", run.note)
        return run

    # ------------------------------------------------------------------
    def test_all_matching_days(self):
        self.magento_items = [
            mg_order(101, 50.0, "2026-10-01 08:00:00"),
            mg_order(102, 25.5, "2026-10-01 23:59:59"),
            mg_order(103, 10.0, "2026-10-02 00:00:00"),
        ]
        self.odoo_order(101, 50.0, "2026-10-01 08:00:00")
        self.odoo_order(102, 25.5, "2026-10-01 23:59:59")
        self.odoo_order(103, 10.0, "2026-10-02 00:00:00")
        # Same instance, outside the range: ignored.
        self.odoo_order(99, 12.0, "2026-09-30 23:00:00")

        run = self.run_recon()
        self.assertEqual(len(run.line_ids), 2, "one line per UTC day")
        self.assertEqual(set(run.line_ids.mapped("status")), {"match"})
        day1 = run.line_ids.filtered(lambda l: l.day == date(2026, 10, 1))
        self.assertEqual((day1.magento_count, day1.odoo_count), (2, 2))
        self.assertAlmostEqual(day1.magento_total, 75.5)
        self.assertAlmostEqual(day1.odoo_total, 75.5)
        self.assertAlmostEqual(day1.difference, 0.0)
        self.assertEqual(day1.store_id, 1)
        self.assertEqual(day1.currency_id, self.usd)
        self.assertEqual((run.magento_count, run.odoo_count, run.matched_count), (3, 3, 3))
        self.assertEqual((run.missing_count, run.mismatch_count, run.extra_count), (0, 0, 0))
        self.assertFalse(run.finding_ids)

        call = self.search_calls[0]
        self.assertEqual(call["endpoint"], "/rest/V1/orders")
        self.assertEqual(call["filters"], [
            ("created_at", "2026-10-01 00:00:00", "gteq"),
            ("created_at", "2026-10-02 23:59:59", "lteq"),
        ])
        self.assertEqual(call["sort"], (("entity_id", "ASC"),), "stable paging while orders change")
        self.assertEqual(call["id_key"], "entity_id")
        self.assertIn("items[entity_id,increment_id,created_at,grand_total,order_currency_code,"
                      "store_id,status]", call["fields"])

    def test_missing_order_detected_and_listed(self):
        self.magento_items = [mg_order(201, 20.0), mg_order(202, 30.0), mg_order(203, 40.0)]
        self.odoo_order(201, 20.0)
        self.odoo_order(203, 40.0)

        run = self.run_recon()
        self.assertEqual(run.missing_count, 1)
        line = run.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.status, "missing")
        self.assertEqual((line.magento_count, line.odoo_count), (3, 2))
        self.assertEqual(line.missing_magento_ids, "202")
        self.assertAlmostEqual(line.difference, 30.0)
        finding = run.finding_ids
        self.assertEqual(finding.kind, "missing")
        self.assertEqual(finding.magento_order_id, "202")
        self.assertEqual(finding.increment_id, "100000202")
        self.assertEqual(finding.line_id, line)
        action = run.action_view_missing()
        self.assertEqual(self.env[action["res_model"]].search(action["domain"]), finding)

    def test_total_mismatch_detected(self):
        self.magento_items = [mg_order(301, 100.0), mg_order(302, 50.004)]
        mismatched = self.odoo_order(301, 99.0)
        self.odoo_order(302, 50.0)  # within currency rounding: not a mismatch

        run = self.run_recon()
        self.assertEqual(run.mismatch_count, 1)
        self.assertEqual(run.missing_count, 0)
        line = run.line_ids
        self.assertEqual(line.status, "mismatch")
        self.assertEqual(line.mismatch_magento_ids, "301")
        finding = run.finding_ids
        self.assertEqual(finding.kind, "mismatch")
        self.assertEqual(finding.sale_order_id, mismatched)
        self.assertAlmostEqual(finding.difference, 1.0)
        action = run.action_view_mismatch_orders()
        self.assertEqual(self.env["sale.order"].search(action["domain"]), mismatched)

    def test_extra_odoo_order_detected(self):
        self.magento_items = [mg_order(401, 20.0)]
        self.odoo_order(401, 20.0)
        extra = self.odoo_order(402, 15.0, "2026-10-01 12:00:00")

        run = self.run_recon()
        self.assertEqual(run.extra_count, 1)
        self.assertEqual(run.missing_count, 0)
        self.assertEqual(len(run.line_ids), 1, "the extra order joins the only store line of its day")
        line = run.line_ids
        self.assertEqual(line.status, "mismatch")
        self.assertEqual((line.magento_count, line.odoo_count), (1, 2))
        self.assertIn(extra.name, line.extra_odoo_ids)
        self.assertAlmostEqual(line.difference, -15.0)
        self.assertEqual(run.finding_ids.kind, "extra")
        self.assertEqual(run.finding_ids.sale_order_id, extra)
        action = run.action_view_extra_orders()
        self.assertEqual(self.env["sale.order"].search(action["domain"]), extra)

    def test_multi_currency_grouping(self):
        self.magento_items = [
            mg_order(501, 100.0, currency="USD"),
            mg_order(502, 80.0, currency="EUR"),
            mg_order(503, 20.0, currency="EUR"),
            mg_order(504, 5.0, currency="EUR", store=2),
        ]
        self.odoo_order(501, 100.0, currency=self.usd)
        self.odoo_order(502, 80.0, currency=self.eur)
        self.odoo_order(503, 20.0, currency=self.eur)
        self.odoo_order(504, 5.0, currency=self.eur)

        run = self.run_recon()
        self.assertEqual(len(run.line_ids), 3, "one line per day, store and currency")
        usd = run.line_ids.filtered(lambda l: l.currency_id == self.usd)
        eur = run.line_ids.filtered(lambda l: l.currency_id == self.eur and l.store_id == 1)
        self.assertAlmostEqual(usd.magento_total, 100.0)
        self.assertAlmostEqual(eur.magento_total, 100.0)
        self.assertEqual(eur.magento_count, 2)
        self.assertEqual(eur.currency_code, "EUR")
        self.assertEqual(set(run.line_ids.mapped("status")), {"match"}, [
            (l.currency_code, l.store_id, l.magento_count, l.odoo_count, l.magento_total, l.odoo_total)
            for l in run.line_ids
        ] + [(f.kind, f.magento_order_id, f.sale_order_id.currency_id.name) for f in run.finding_ids])

        # An Odoo order in another currency than Magento's is a mismatch, even at equal amounts.
        later = "2026-10-05 10:00:00"
        self.magento_items = [mg_order(505, 10.0, later, currency="EUR")]
        self.odoo_order(505, 10.0, later, currency=self.usd, magento_currency="EUR")
        run = self.run_recon(date_from="2026-10-05", date_to="2026-10-05")
        self.assertEqual(run.mismatch_count, 1)
        self.assertEqual(run.finding_ids.magento_order_id, "505")

    def test_canceled_orders_follow_include_canceled(self):
        self.magento_items = [
            mg_order(601, 40.0),
            mg_order(602, 30.0, status="canceled"),
            mg_order(603, 12.0, status="canceled"),  # canceled and not in Odoo
        ]
        self.odoo_order(601, 40.0)
        self.odoo_order(602, 25.0)  # differs, but the order is canceled

        run = self.run_recon()
        line = run.line_ids
        self.assertEqual(run.canceled_count, 2)
        self.assertEqual(line.canceled_count, 2)
        self.assertEqual((line.magento_count, line.odoo_count), (3, 2), "canceled orders are counted")
        self.assertAlmostEqual(line.magento_total, 40.0, msg="canceled orders are not in the totals")
        self.assertAlmostEqual(line.odoo_total, 40.0)
        self.assertEqual(run.mismatch_count, 0)
        self.assertEqual(run.missing_count, 1, "a canceled order missing in Odoo is still missing")
        self.assertEqual(run.finding_ids.magento_order_id, "603")
        self.assertTrue(run.finding_ids.canceled)

        run_all = self.run_recon(include_canceled=True)
        line = run_all.line_ids
        self.assertAlmostEqual(line.magento_total, 82.0)
        self.assertAlmostEqual(line.odoo_total, 65.0)
        self.assertEqual(run_all.mismatch_count, 1)
        self.assertEqual(run_all.missing_count, 1)
        self.assertEqual(
            run_all.finding_ids.filtered(lambda f: f.kind == "mismatch").magento_order_id, "602")

        # Running again replaces the previous result.
        run_all.include_canceled = False
        run_all.action_run()
        self.assertEqual(run_all.mismatch_count, 0)
        self.assertEqual(len(run_all.line_ids), 1)
        self.assertEqual(len(run_all.finding_ids), 1)

    def test_reimport_missing_enqueues_deduplicated_jobs(self):
        calls = []

        def _resync(instance, entity, magento_ref):
            calls.append((instance.id, entity, magento_ref))
            return {"orders": 1}

        self.startPatcher(patch.object(
            type(self.env["magento.instance"]), "_magento_resync_one", _resync, create=True,
        ))
        self.magento_items = [mg_order(701, 10.0), mg_order(702, 20.0), mg_order(703, 30.0)]
        self.odoo_order(701, 10.0)
        run = self.run_recon()
        self.assertEqual(run.missing_count, 2)

        run.action_reimport_missing()
        Job = self.env["magento.job"]
        keys = ["retry-%s-order-%s" % (self.instance.id, ref) for ref in ("702", "703")]
        jobs = Job.search([("identity_key", "in", keys)])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs.mapped("channel")), {"orders"})
        self.assertEqual(set(jobs.mapped("method_name")), {"_magento_resync_one"})
        self.assertEqual(jobs.instance_id, self.instance)
        self.assertEqual(run.finding_ids.job_id, jobs)

        run.action_reimport_missing()
        self.assertEqual(Job.search_count([("identity_key", "in", keys)]), 2, "deduplicated")
        # Same key as an Error Center retry of that order: no second job either.
        issue = self.env["magento.sync.issue"]._record_failure(
            self.instance, "order", "702", "x", severity="transient")
        issue.action_retry()
        self.assertEqual(issue.job_id, jobs.filtered(lambda j: j.identity_key == keys[0]))
        self.assertEqual(Job.search_count([("identity_key", "in", keys)]), 2)

        for job in jobs:
            job._run_job()
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(sorted(calls), [
            (self.instance.id, "order", "702"), (self.instance.id, "order", "703"),
        ])

        self.magento_items = []
        fresh = self.run_recon(date_from="2026-10-03", date_to="2026-10-03")
        with self.assertRaises(UserError):
            fresh.action_reimport_missing()

    def test_background_run_and_failures(self):
        self.magento_items = [mg_order(801, 10.0)]
        self.odoo_order(801, 10.0)
        run = self.make_run()
        run.action_run_in_background()
        self.assertEqual(run.state, "running")
        key = "recon-%s-2026-10-01-2026-10-02" % self.instance.id
        job = self.env["magento.job"].search([("identity_key", "=", key)])
        self.assertEqual(len(job), 1)
        self.assertEqual(run.job_id, job)
        with self.assertRaises(UserError):
            run.action_run()
        job._run_job()
        self.assertEqual(job.state, "done")
        self.assertEqual(run.state, "done")
        self.assertEqual(run.matched_count, 1)

        # A long range always goes through the queue.
        long_run = self.make_run(date_from="2026-09-01", date_to="2026-09-30")
        long_run.action_run()
        self.assertEqual(long_run.state, "running")
        self.assertTrue(long_run.job_id)

        # A Magento failure marks the run failed, keeps the transaction usable.
        self.patch_api("iter_search", side_effect=requests.exceptions.ConnectionError("reset by peer"))
        failed = self.make_run()
        failed.action_run()
        self.assertEqual(failed.state, "failed")
        self.assertIn("reset by peer", failed.note)
        self.assertFalse(failed.line_ids)

        with self.assertRaises(ValidationError):
            self.make_run(date_from="2026-10-05", date_to="2026-10-01")

    def test_cron_records_issues_for_missing_orders(self):
        yesterday = fields.Datetime.now().date() - timedelta(days=1)
        created = "%s 09:30:00" % yesterday
        self.magento_items = [mg_order(901, 10.0, created), mg_order(902, 20.0, created)]
        self.odoo_order(901, 10.0, created)
        self.instance.write({"reconcile_daily": True, "reconcile_lookback_days": 2})
        other = self.create_instance("Not Reconciled")
        self.assertFalse(other.reconcile_daily)
        cron = self.env.ref("sdlc_magento_connector.ir_cron_magento_reconcile_daily")
        self.assertFalse(cron.active, "the nightly reconciliation ships inactive")

        runs = self.Recon._cron_reconcile_yesterday()
        self.assertEqual(runs.instance_id, self.instance)
        self.assertEqual(runs.trigger, "cron")
        self.assertEqual(runs.date_to, yesterday)
        self.assertEqual(runs.date_from, yesterday - timedelta(days=1))
        self.assertEqual(runs.missing_count, 1)
        issue = self.env["magento.sync.issue"].search([
            ("instance_id", "=", self.instance.id), ("entity", "=", "order"),
            ("magento_ref", "=", "902"), ("state", "=", "open"),
        ])
        self.assertEqual(len(issue), 1)
        self.assertEqual(issue.severity, "transient")
        self.assertEqual(issue.message, "Missing in Odoo (reconciliation)")
        self.assertFalse(self.env["magento.sync.issue"].search([("magento_ref", "=", "901")]))

        # The next night upserts the same open issue instead of duplicating it.
        self.Recon._cron_reconcile_yesterday()
        self.assertEqual(issue.occurrences, 2)
        self.assertEqual(self.env["magento.sync.issue"].search_count([("magento_ref", "=", "902")]), 1)

        with self.assertRaises(ValidationError):
            self.instance.reconcile_lookback_days = 0

    def test_user_can_run_but_not_unlink(self):
        user = self.create_user("mg_recon_user", "sdlc_magento_connector.group_magento_user")
        self.magento_items = [mg_order(1001, 10.0), mg_order(1002, 20.0)]
        self.odoo_order(1001, 10.0)
        run = self.Recon.with_user(user).create({
            "instance_id": self.instance.id, "date_from": "2026-10-01", "date_to": "2026-10-01",
        })
        run.action_run()
        self.assertEqual(run.state, "done", run.note)
        self.assertEqual(run.missing_count, 1)
        run.action_run()  # re-run replaces the lines without unlink rights
        self.assertEqual(len(run.line_ids), 1)
        run.write({"include_canceled": True})
        run.action_reimport_missing()
        with self.assertRaises(AccessError):
            run.unlink()
        with self.assertRaises(AccessError):
            run.line_ids.unlink()
        with self.assertRaises(AccessError):
            run.finding_ids.unlink()
        run.with_user(self.env.ref("base.user_admin")).unlink()
