"""Queued syncs run one page per job, with a keyset cursor and a run-wide watermark."""
import copy
import re
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

import requests
from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.sdlc_magento_connector.models import magento_instance as instance_module
from odoo.addons.sdlc_magento_connector.models.magento_job import _STALE_AFTER

from .common import MagentoTestCommon, build_product, make_response

JOB_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_job"
SYNC_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_instance"
ISSUE_LOGGER = "odoo.addons.sdlc_magento_connector.models.magento_sync_issue"
QUEUE = {"magento_sync_queue": True}
BASE_TIME = datetime(2025, 3, 1, 10, 0, 0)

_FILTER_RE = re.compile(
    r"searchCriteria\[filter_groups\]\[(\d+)\]\[filters\]\[(\d+)\]\[(field|value|condition_type)\]"
)


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class FakeMagentoSearch:
    """A Magento search endpoint over ``rows``: applies the searchCriteria filter groups
    (AND between groups, OR inside a group), sorts by ``updated_at, entity_id`` and
    serves ``pageSize``/``currentPage`` slices of the data as it is *at request time*."""

    def __init__(self, rows, id_key="id"):
        self.id_key = id_key
        self.rows = {row[id_key]: row for row in rows}
        self.requests = []  # parsed filter groups + page of each search request
        self.fail_on_request = None  # request number (1-based) answered with HTTP 503

    @staticmethod
    def parse_filters(params):
        groups = {}
        for key, value in (params or {}).items():
            match = _FILTER_RE.fullmatch(key)
            if match:
                groups.setdefault(int(match[1]), {}).setdefault(int(match[2]), {})[match[3]] = value
        return [[groups[g][f] for f in sorted(groups[g])] for g in sorted(groups)]

    def _matches(self, row, flt):
        field, value, condition = flt["field"], flt["value"], flt.get("condition_type") or "eq"
        if field == "entity_id":
            left, right = int(row[self.id_key]), int(value)
        else:
            left, right = row.get(field), value
        return {
            "eq": left == right, "gt": left > right, "gteq": left >= right,
            "lt": left < right, "lteq": left <= right,
        }[condition]

    def search(self, params):
        groups = self.parse_filters(params)
        page_size = int(params.get("searchCriteria[pageSize]") or 20)
        current_page = int(params.get("searchCriteria[currentPage]") or 1)
        self.requests.append({"filters": groups, "page": current_page, "page_size": page_size})
        if self.fail_on_request == len(self.requests):
            raise requests.exceptions.HTTPError("HTTP 503", response=make_response(503))
        matched = [
            row for row in self.rows.values()
            if all(any(self._matches(row, flt) for flt in group) for group in groups)
        ]
        matched.sort(key=lambda row: (row["updated_at"], int(row[self.id_key])))
        start = (current_page - 1) * page_size
        return {
            "items": copy.deepcopy(matched[start:start + page_size]),
            "total_count": len(matched),
        }


@tagged("post_install", "-at_install", "magento")
class TestPagedSync(MagentoTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["magento.job"]
        cls.Report = cls.env["magento.sync.report"]
        cls.instance.write({"product_sync_page_size": 100, "sync_page_size": 100})

    # ------------------------------------------------------------------ fixtures
    def make_catalog(self, count, seconds_per_group=1, group=3):
        """``count`` products; ``group`` products share each ``updated_at`` second so
        page boundaries fall inside equal timestamps."""
        rows = []
        for i in range(1, count + 1):
            product = build_product(i, "PG-%03d" % i, "Paged %s" % i)
            product["updated_at"] = _ts(BASE_TIME + timedelta(seconds=(i // group) * seconds_per_group))
            rows.append(product)
        fake = FakeMagentoSearch(rows)

        def _prefix_fallback(api, method, endpoint_builder, *, include_all_scope=False,
                             payload=None, params=None):
            endpoint = endpoint_builder("/rest/V1")
            assert method == "get" and endpoint.endswith("/products"), endpoint
            return fake.search(params)

        def _get_product(api, sku):
            row = next(r for r in fake.rows.values() if r["sku"] == sku)
            if sku in self.failing_skus:
                raise requests.exceptions.HTTPError("HTTP 400", response=make_response(400))
            return copy.deepcopy(row)

        self.failing_skus = set()
        self.patch_api("_request_with_prefix_fallback", side_effect=_prefix_fallback)
        self.detail = self.patch_api("get_product", side_effect=_get_product)
        self.patch_api("get_product_media", return_value={})
        return fake

    def sync_jobs(self, entity="products"):
        return self.Job.search([
            ("identity_key", "=like", "sync-%s-%s%%" % (self.instance.id, entity)),
        ], order="id")

    def run_queue(self, entity="products", max_calls=60):
        """Call the runner one job at a time (one cron run each) until no sync job waits."""
        calls = 0
        with mute_logger(JOB_LOGGER, SYNC_LOGGER, ISSUE_LOGGER):
            while calls < max_calls and self.sync_jobs(entity).filtered(lambda j: j.state == "pending"):
                self.Job._cron_run_jobs(limit=1)
                calls += 1
        self.assertLess(calls, max_calls, "The sync run must end")
        return calls

    def fetched(self):
        """SKU -> number of product detail fetches (= imports) so far."""
        return Counter(call.args[1] for call in self.detail.call_args_list)

    def maps(self):
        return self.env["magento.product.map"].with_context(active_test=False).search([
            ("instance_id", "=", self.instance.id), ("sku", "=like", "PG-%"),
        ])

    def reports(self):
        return self.Report.search([("instance_id", "=", self.instance.id), ("sync_type", "=", "product")])

    # ------------------------------------------------------------------ tests
    def test_250_records_three_page_jobs(self):
        fake = self.make_catalog(250)
        action = self.instance.with_context(**QUEUE).action_sync_products()
        self.assertIn("Queued: products sync", action["params"]["message"])
        first = self.sync_jobs()
        self.assertEqual(len(first), 1)

        with mute_logger(JOB_LOGGER, SYNC_LOGGER):
            self.Job._cron_run_jobs(limit=1)
        self.assertEqual(first.state, "done", first.last_error)
        self.assertEqual(len(self.maps()), 100, "The first job imports one page only")
        report = self.reports()
        self.assertEqual(len(report), 1)
        self.assertEqual(report.products_created, 100)
        self.assertIn("Running", report.notes)
        self.assertFalse(self.instance.products_sync_watermark, "The watermark waits for the end of the run")
        second = self.sync_jobs() - first
        self.assertEqual(len(second), 1)
        self.assertEqual(second.state, "pending")
        self.assertEqual(second.identity_key, "sync-%s-products-p2-r%s" % (self.instance.id, report.id))
        self.assertEqual((second.channel, second.priority), (first.channel, first.priority))
        self.assertIn("next page in job %s" % second.id, first.result)

        self.run_queue()
        jobs = self.sync_jobs()
        self.assertEqual(len(jobs), 3, "250 records / 100 per page = 3 page jobs")
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(len(self.maps()), 250)
        self.assertEqual(set(self.fetched().values()), {1}, "Every record is imported exactly once")
        self.assertEqual(len(self.fetched()), 250)

        # Keyset paging: page 2 asks for the rows after the last row of page 1.
        upper = fake.requests[0]["filters"][0][0]
        self.assertEqual(upper["condition_type"], "lteq")
        self.assertEqual(len(fake.requests[0]["filters"]), 1, "Full sync: no lower bound")
        last_of_page1 = sorted(fake.rows.values(), key=lambda r: (r["updated_at"], r["id"]))[99]
        cursor_groups = fake.requests[1]["filters"]
        self.assertEqual(cursor_groups[0], [upper])
        self.assertEqual(cursor_groups[1], [{"field": "updated_at", "value": last_of_page1["updated_at"],
                                             "condition_type": "gteq"}])
        self.assertEqual(cursor_groups[2], [
            {"field": "updated_at", "value": last_of_page1["updated_at"], "condition_type": "gt"},
            {"field": "entity_id", "value": str(last_of_page1["id"]), "condition_type": "gt"},
        ])
        self.assertEqual({r["page"] for r in fake.requests}, {1}, "No offset paging")

        report = self.reports()
        self.assertEqual(len(report), 1, "One sync report per run")
        self.assertEqual((report.products_created, report.total_records, report.errors), (250, 250, 0))
        self.assertNotIn("Running", report.notes)
        self.assertIn("pages fetched: 3; records seen: 250", report.notes)
        newest = max(r["updated_at"] for r in fake.rows.values())
        self.assertEqual(fields.Datetime.to_string(self.instance.products_sync_watermark), newest)
        self.assertIn("Product sync: 250 synced", jobs[-1].result)
        self.assertTrue(self.instance.last_sync_at)

    def test_record_updated_mid_run_is_neither_skipped_nor_duplicated(self):
        self.instance.write({"product_sync_page_size": 10, "incremental_overlap_minutes": 0})
        fake = self.make_catalog(25)
        self.instance.with_context(**QUEUE).action_sync_products()
        with mute_logger(JOB_LOGGER, SYNC_LOGGER):
            self.Job._cron_run_jobs(limit=1)
        self.assertEqual(len(self.maps()), 10)

        # Magento saves two products while the run is between jobs: one already
        # imported (page 1) and one not yet reached (page 3). Their updated_at moves
        # past the run's upper bound, which with offset paging would shift every
        # following row one place and skip the first row of page 2.
        upper = fields.Datetime.to_datetime(self.sync_jobs()[-1].kwargs["run"]["upper"])
        moved_at = _ts(upper + timedelta(minutes=1))
        fake.rows[2].update(updated_at=moved_at, name="Paged 2 v2")
        fake.rows[24].update(updated_at=moved_at, name="Paged 24 v2")
        self.run_queue()
        run1 = self.fetched()
        self.assertEqual(set(run1), {"PG-%03d" % i for i in range(1, 26)} - {"PG-024"},
                         "No row is skipped; the row saved past the window waits for the next run")
        self.assertEqual(set(run1.values()), {1}, "No row is imported twice in a run")
        self.assertEqual(len(self.reports()), 1)

        # The next run picks both changed products up, with their new data.
        self.detail.reset_mock()
        with freeze_time(upper + timedelta(minutes=5)):
            self.instance.with_context(**QUEUE).action_sync_products()
            self.run_queue()
        run2 = self.fetched()
        self.assertEqual(run2["PG-002"], 1)
        self.assertEqual(run2["PG-024"], 1)
        names = dict(self.maps().mapped(lambda m: (m.sku, m.odoo_product_id.name)))
        self.assertEqual(names["PG-002"], "Paged 2 v2")
        self.assertEqual(names["PG-024"], "Paged 24 v2")
        self.assertEqual(len(self.maps()), 25)
        self.assertEqual(len(self.reports()), 2, "One report per run")

    def test_failure_on_page_two_holds_the_watermark(self):
        self.instance.write({"product_sync_page_size": 10, "incremental_overlap_minutes": 0})
        fake = self.make_catalog(25, group=1)  # one second per product
        ordered = sorted(fake.rows.values(), key=lambda r: (r["updated_at"], r["id"]))
        failed = ordered[14]  # on page 2
        self.failing_skus.add(failed["sku"])
        self.instance.with_context(**QUEUE).action_sync_products()
        self.run_queue()
        self.assertEqual(len(self.sync_jobs()), 3)
        report = self.reports()
        self.assertEqual((report.errors, report.products_created), (1, 24))
        expected = datetime.strptime(failed["updated_at"], "%Y-%m-%d %H:%M:%S") - timedelta(seconds=1)
        self.assertEqual(self.instance.products_sync_watermark, expected,
                         "The watermark stops just before the failed record, not at the newest one")

        # The next run fetches from there again and imports the failed record.
        self.failing_skus.clear()
        self.detail.reset_mock()
        fake.requests.clear()
        self.instance.with_context(**QUEUE).action_sync_products()
        self.run_queue()
        self.assertEqual(fake.requests[0]["filters"][1][0],
                         {"field": "updated_at", "value": _ts(expected), "condition_type": "gteq"})
        self.assertEqual(self.fetched()[failed["sku"]], 1)
        self.assertEqual(len(self.maps()), 25)
        newest = max(r["updated_at"] for r in fake.rows.values())
        self.assertEqual(fields.Datetime.to_string(self.instance.products_sync_watermark), newest)

    def test_listing_error_on_page_two_ends_run_safely(self):
        self.instance.write({"product_sync_page_size": 10, "incremental_overlap_minutes": 0})
        fake = self.make_catalog(25, group=1)
        fake.fail_on_request = 2
        self.instance.with_context(**QUEUE).action_sync_products()
        self.run_queue()
        self.assertEqual(len(self.sync_jobs()), 2, "A listing error ends the run")
        self.assertEqual(set(self.sync_jobs().mapped("state")), {"done"})
        report = self.reports()
        self.assertEqual((report.errors, report.products_created), (1, 10))
        ordered = sorted(fake.rows.values(), key=lambda r: (r["updated_at"], r["id"]))
        self.assertEqual(fields.Datetime.to_string(self.instance.products_sync_watermark),
                         ordered[9]["updated_at"], "Watermark = newest record of page 1")

        fake.fail_on_request = None
        self.detail.reset_mock()
        self.instance.with_context(**QUEUE).action_sync_products()
        self.run_queue()
        self.assertEqual(len(self.maps()), 25, "The next run resumes after page 1")

    def test_double_click_dedup_and_running_run(self):
        self.instance.write({"product_sync_page_size": 10})
        self.make_catalog(25)
        instance = self.instance.with_context(**QUEUE)
        instance.action_sync_products()
        again = instance.action_sync_products()
        self.assertIn("Already queued", again["params"]["message"])
        self.assertEqual(len(self.sync_jobs()), 1)

        with mute_logger(JOB_LOGGER, SYNC_LOGGER):
            self.Job._cron_run_jobs(limit=1)
        self.assertEqual(len(self.sync_jobs()), 2, "Page 2 is queued")
        # While the run goes on, a click or a scheduled sync joins it.
        again = instance.action_sync_products()
        self.assertIn("Already queued", again["params"]["message"])
        (_entity, _job, created), = self.instance._magento_enqueue_sync(
            ["products"], source_action="cron_sync_all", mode="cron")
        self.assertFalse(created, "A scheduled sync joins the running run")
        self.assertEqual(len(self.sync_jobs()), 2)

        self.run_queue()
        self.assertEqual(len(self.sync_jobs()), 3)
        self.assertEqual(len(self.maps()), 25)
        # Once the run is over a click starts a new one.
        again = instance.action_sync_products()
        self.assertIn("Queued: products sync", again["params"]["message"])

    def test_page_time_budget_resumes_at_cursor(self):
        self.make_catalog(5)
        self.instance.with_context(**QUEUE).action_sync_products()
        with patch.object(instance_module, "_SYNC_PAGE_TIME_BUDGET", 0):
            self.run_queue()
        self.assertEqual(len(self.sync_jobs()), 6, "One record per job, then an empty last page")
        self.assertEqual(set(self.fetched().values()), {1})
        self.assertEqual(len(self.maps()), 5)
        self.assertEqual(len(self.reports()), 1)

    def test_page_jobs_commit_before_the_next_page(self):
        """In a real cron run, every page job is committed before the next one starts."""
        self.instance.write({"product_sync_page_size": 10})
        self.make_catalog(25)
        events = []
        test = self
        Job = type(self.Job)
        cron_cls = type(self.env["ir.cron"])
        real_page = type(self.instance)._magento_run_sync_page

        def _commit_progress(cron, processed=0, **kwargs):
            test.env.flush_all()
            events.append(("commit", len(test.maps()),
                           tuple(test.sync_jobs().mapped("state"))))
            return 1000.0

        def _page(instance, *args, **kwargs):
            events.append(("page", len(test.maps())))
            return real_page(instance, *args, **kwargs)

        with patch.object(Job, "_job_can_commit", lambda job: True), \
                patch.object(cron_cls, "_commit_progress", _commit_progress), \
                patch.object(type(self.instance), "_magento_run_sync_page", _page):
            self.instance.with_context(**QUEUE).action_sync_products()
            with mute_logger(JOB_LOGGER, SYNC_LOGGER):
                self.Job._cron_run_jobs()
        pages = [i for i, event in enumerate(events) if event[0] == "page"]
        self.assertEqual(len(pages), 3)
        for previous, following in zip(pages, pages[1:]):
            # Commits after a page job finished (no sync job "started" any more).
            commits = [e for e in events[previous + 1:following]
                       if e[0] == "commit" and "started" not in e[2]]
            self.assertTrue(commits, "A commit separates two page jobs")
            self.assertEqual(commits[-1][1], events[following][1],
                             "Everything imported so far is committed before the next page")
        self.assertEqual(events[-1][0], "commit")
        self.assertEqual(events[-1][1], 25)

    def test_customers_are_paged_too(self):
        self.instance.write({"sync_page_size": 5})
        rows = [{
            "id": i, "email": "pg%s@example.com" % i, "firstname": "Page", "lastname": "C%s" % i,
            "website_id": 1, "group_id": 1, "addresses": [],
            "updated_at": _ts(BASE_TIME + timedelta(seconds=i // 2)),
        } for i in range(1, 13)]
        fake = FakeMagentoSearch(rows)

        def _get(api, endpoint, params=None):
            assert endpoint == "/rest/V1/customers/search", endpoint
            return fake.search(params)

        self.patch_api("_get", side_effect=_get)
        self.instance.with_context(**QUEUE).action_sync_customers()
        self.run_queue("customers")
        jobs = self.sync_jobs("customers")
        self.assertEqual(len(jobs), 3)
        customers = self.env["magento.customer"].with_context(active_test=False).search([
            ("instance_id", "=", self.instance.id), ("email", "=like", "pg%@example.com")])
        self.assertEqual(len(customers), 12)
        report = self.Report.search([("instance_id", "=", self.instance.id), ("sync_type", "=", "customer")])
        self.assertEqual((len(report), report.customers_created), (1, 12))
        self.assertTrue(self.instance.customers_sync_watermark)

    def test_cursor_key_of_each_entity(self):
        key = self.env["magento.instance"]._magento_cursor_key
        when = datetime(2025, 3, 1, 10, 0, 5)
        self.assertEqual(key("order", {"entity_id": 7, "updated_at": "2025-03-01 10:00:05"}), (when, 7))
        self.assertEqual(key("product", {"id": "8", "updated_at": "2025-03-01T10:00:05"}), (when, 8))
        self.assertEqual(key("customer", {"id": 9, "updated_at": "2025-03-01 10:00:05"}), (when, 9))
        self.assertIsNone(key("customer", {"id": 9}))

    def test_direct_sync_products_runs_all_pages_at_once(self):
        fake = self.make_catalog(250)
        counts = self.instance.sync_products()
        self.assertEqual(counts["created"], 250)
        self.assertEqual(counts["pages_fetched"], 3)
        self.assertEqual([r["page"] for r in fake.requests], [1, 2, 3])
        self.assertFalse(self.sync_jobs(), "A direct call queues nothing")
        self.assertEqual(len(self.maps()), 250)
        newest = max(r["updated_at"] for r in fake.rows.values())
        self.assertEqual(fields.Datetime.to_string(self.instance.products_sync_watermark), newest)

    def test_list_issue_retry_in_a_job_starts_a_paged_run(self):
        """Retrying a failed listing from a queued job must not import the whole
        listing in that one job: it starts (or joins) the paged run."""
        self.make_catalog(5)
        result = self.instance.with_context(magento_job_id=-1)._magento_resync_one("product", "list")
        self.assertIn("queued", result)
        self.assertEqual(len(self.sync_jobs()), 1)
        self.assertFalse(self.detail.called, "Nothing is imported inside the retry job")
        result = self.instance.with_context(magento_job_id=-1)._magento_resync_one("product", "list")
        self.assertIn("already queued", result)
        self.run_queue()
        self.assertEqual(len(self.maps()), 5)

    # ------------------------------------------------------------------ dead jobs
    def test_dead_started_job_can_be_retried_when_unlocked(self):
        manager = self.create_user("mg_paged_mgr", "sdlc_magento_connector.group_magento_manager")
        user = self.create_user("mg_paged_user", "sdlc_magento_connector.group_magento_user")
        (_entity, job, _created), = self.instance._magento_enqueue_sync(["products"])
        job.sudo().write({"state": "started", "attempts": 1,
                          "date_started": fields.Datetime.now() - timedelta(minutes=2)})

        with self.assertRaises(AccessError):
            job.with_user(user).action_retry()
        # Its row is locked by a live worker: it is really running.
        with patch.object(type(self.Job), "_lock_unlocked_ids", lambda jobs: set()), \
                self.assertRaises(UserError):
            job.with_user(manager).action_retry()
        self.assertEqual(job.state, "started")

        # No worker holds the row: the worker died, the manager can requeue it.
        job.with_user(manager).action_retry()
        self.assertEqual((job.state, job.attempts), ("pending", 0))
        self.assertIn("requeued manually", job.last_error)

    def test_stale_started_job_is_requeued_after_ten_minutes(self):
        self.assertEqual(_STALE_AFTER, timedelta(minutes=10))
        (_entity, job, _created), = self.instance._magento_enqueue_sync(["products"])
        job.sudo().write({"state": "started", "attempts": 1,
                          "date_started": fields.Datetime.now() - timedelta(minutes=5)})
        self.assertFalse(self.Job.sudo()._requeue_stale_jobs())
        job.sudo().write({"date_started": fields.Datetime.now() - timedelta(minutes=11)})
        self.assertEqual(self.Job.sudo()._requeue_stale_jobs(), job.sudo())
        self.assertEqual(job.state, "pending")
