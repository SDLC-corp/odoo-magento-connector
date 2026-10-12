"""Lightweight job queue for the Magento connector (roadmap 1.1).

A ``magento.job`` stores a deferred method call ``env[model].browse(ids).method(*args, **kwargs)``.
The runner cron (every minute) picks ready jobs with ``FOR UPDATE SKIP LOCKED``, runs each one
in a savepoint as the user who enqueued it, retries transient failures with exponential backoff
and fails permanent ones immediately. No OCA ``queue_job`` dependency.
"""
import json
import logging
import re
import traceback
from datetime import timedelta

import psycopg2
import psycopg2.errors
import requests

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.modules import module as odoo_module
from odoo.tools import SQL, mute_logger

_logger = logging.getLogger(__name__)

# Backoff: eta = now + min(2**attempts * BASE, MAX)
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 3600
# A job "started" for longer than this is considered interrupted and requeued.
_STALE_AFTER = timedelta(hours=1)
# Jobs considered per lock query; only one of them is run per loop.
_LOCK_WINDOW = 20
_JOB_RUNNER_CRON = "sdlc_magento_connector.ir_cron_magento_job_runner"
# Context keys of the runner cron that must not leak into the job: the sync engine
# would otherwise commit inside the job's savepoint.
_CRON_CONTEXT_KEYS = ("ir_cron_progress_id", "cron_id", "cron_end_time", "lastcall", "magento_cron")
_AUTH_TEXT_RE = re.compile(r"\bHTTP (401|403)\b")

JOB_STATES = [
    ("pending", "Pending"),
    ("started", "Started"),
    ("done", "Done"),
    ("failed", "Failed"),
    ("cancelled", "Cancelled"),
]
ERROR_CLASSES = [("transient", "Transient"), ("permanent", "Permanent")]


def _exception_chain(exc, limit=10):
    """``exc`` and every exception linked to it through ``__cause__`` (``raise ... from``)
    and ``__context__`` (raised while handling another), breadth first, cycle safe."""
    seen = set()
    queue = [exc]
    while queue and len(seen) < limit:
        err = queue.pop(0)
        if err is None or id(err) in seen:
            continue
        seen.add(id(err))
        yield err
        queue.extend((err.__cause__, err.__context__))


def _classify_one(err):
    """``(severity, is_auth, status)`` of one exception, or None when it says nothing."""
    if isinstance(err, requests.exceptions.HTTPError):
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status is None:
            return None
        if status in (401, 403):
            return "permanent", True, status
        if status == 429 or status >= 500:
            return "transient", False, status
        return "permanent", False, status
    if isinstance(err, requests.exceptions.SSLError):
        return "permanent", False, None
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "transient", False, None
    if isinstance(err, (psycopg2.extensions.TransactionRollbackError, psycopg2.errors.LockNotAvailable)):
        return "transient", False, None
    return None


def classify_exception(exc):
    """Classify a failure as ``(severity, is_auth, http_status)``.

    * transient: connection errors, timeouts, HTTP 429/5xx, PostgreSQL serialization,
      deadlock and lock-not-available errors;
    * permanent: everything else (UserError, ValidationError, HTTP 4xx, SSL errors...).

    The whole chain (``__cause__`` and ``__context__``) is inspected: if ANY link is
    transient the failure is transient, so e.g. a ``UserError`` raised from a timeout
    of an ambiguous document push is retried. A 401/403 anywhere flags an auth error.
    A plain string is permanent; "HTTP 401/403" in it flags an auth error.
    """
    if exc is None:
        return "permanent", False, None
    if isinstance(exc, str):
        match = _AUTH_TEXT_RE.search(exc)
        return "permanent", bool(match), int(match.group(1)) if match else None
    severity, is_auth, status = "permanent", False, None
    for err in _exception_chain(exc):
        found = _classify_one(err)
        if not found:
            continue
        link_severity, link_auth, link_status = found
        is_auth = is_auth or link_auth
        if status is None:
            status = link_status
        if link_severity == "transient" and severity != "transient":
            severity, status = "transient", link_status if link_status is not None else status
    if is_auth:
        severity = "permanent"
    return severity, is_auth, status


def exception_message(exc, limit=1000):
    """One-line, bounded message for an exception or a plain string."""
    if exc is None:
        return ""
    if isinstance(exc, str):
        text = exc
    elif isinstance(exc, UserError):
        text = str(exc.args[0] if exc.args else exc)
    else:
        text = "%s: %s" % (type(exc).__name__, exc)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def backoff_delay(attempts):
    """Seconds before the next try after ``attempts`` failed attempts."""
    return min(2 ** max(int(attempts), 0) * _BACKOFF_BASE_SECONDS, _BACKOFF_MAX_SECONDS)


class MagentoJob(models.Model):
    _name = "magento.job"
    _description = "Magento Job"
    _order = "id desc"

    name = fields.Char(string="Description", required=True)
    instance_id = fields.Many2one("magento.instance", index=True, ondelete="cascade")
    company_id = fields.Many2one(
        related="instance_id.company_id", store=True, index=True, readonly=True
    )
    channel = fields.Char(default="root", required=True, index=True,
                          help="Jobs of one channel run one at a time.")
    model_name = fields.Char(string="Model", required=True, readonly=True)
    method_name = fields.Char(string="Method", required=True, readonly=True)
    res_ids = fields.Json(string="Record IDs", readonly=True)
    args = fields.Json(readonly=True)
    kwargs = fields.Json(readonly=True)
    identity_key = fields.Char(index=True, readonly=True,
                               help="At most one pending or started job per key.")
    priority = fields.Integer(default=10, help="Lower runs first.")
    eta = fields.Datetime(string="Run After", index=True)
    state = fields.Selection(JOB_STATES, default="pending", required=True, index=True, readonly=True)
    attempts = fields.Integer(readonly=True)
    max_attempts = fields.Integer(default=5)
    last_error = fields.Text(readonly=True)
    error_class = fields.Selection(ERROR_CLASSES, readonly=True)
    result = fields.Text(readonly=True)
    date_enqueued = fields.Datetime(default=fields.Datetime.now, readonly=True)
    date_started = fields.Datetime(readonly=True)
    date_done = fields.Datetime(readonly=True)
    exc_info = fields.Text(string="Traceback", readonly=True,
                           groups="sdlc_magento_connector.group_magento_manager")
    user_id = fields.Many2one("res.users", string="Run As", readonly=True,
                              default=lambda self: self.env.user, ondelete="set null")
    record_count = fields.Integer(compute="_compute_call_display")
    args_display = fields.Text(string="Arguments", compute="_compute_call_display")
    res_ids_display = fields.Char(string="Records", compute="_compute_call_display")

    _identity_key_active_uniq = models.UniqueIndex(
        "(identity_key) WHERE identity_key IS NOT NULL AND state IN ('pending', 'started')",
        "A pending or started job with this identity key already exists.",
    )
    _runner_idx = models.Index("(priority, eta, id) WHERE state = 'pending'")

    _PROTECTED_FIELDS = frozenset({"model_name", "method_name", "res_ids", "args", "kwargs", "user_id"})

    @api.depends("res_ids", "args", "kwargs")
    def _compute_call_display(self):
        for job in self:
            ids = job.res_ids or []
            job.record_count = len(ids)
            job.res_ids_display = ", ".join(str(i) for i in ids[:20]) + (" ..." if len(ids) > 20 else "")
            job.args_display = json.dumps(
                {"args": job.args or [], "kwargs": job.kwargs or {}}, indent=2, default=str
            )

    # ------------------------------------------------------------------
    # CRUD guards: a job runs code as its user, so its call is immutable.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                vals["user_id"] = self.env.uid
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and self._PROTECTED_FIELDS.intersection(vals):
            raise AccessError(_("The call of a Magento job cannot be changed."))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Enqueue API
    # ------------------------------------------------------------------
    @api.model
    def enqueue(self, records, method_name, args=(), kwargs=None, *, instance=None, channel="root",
                priority=10, eta=None, identity_key=None, description=None, max_attempts=5):
        """Queue ``records.method_name(*args, **kwargs)``; return the job.

        If a pending or started job with the same ``identity_key`` exists it is
        returned instead of creating a duplicate. ``args``/``kwargs`` must be
        JSON-serialisable (no recordsets: pass ids). The job runs as the current user.
        """
        if not method_name or method_name.startswith("__") or not hasattr(type(records), method_name):
            raise ValueError("Cannot queue unknown method %r on %s" % (method_name, records._name))
        args = list(args or ())
        kwargs = dict(kwargs or {})
        try:
            json.dumps([args, kwargs])
        except (TypeError, ValueError) as exc:
            raise ValueError("Magento job arguments must be JSON-serialisable: %s" % exc) from exc
        if instance is None:
            if records._name == "magento.instance" and len(records) == 1:
                instance = records
            elif "instance_id" in records._fields and records:
                instance = records[:1].instance_id
        jobs = self.sudo()
        if identity_key:
            existing = jobs._find_active(identity_key)
            if existing:
                return existing.with_env(self.env)
        vals = {
            "name": description or "%s.%s" % (records._name, method_name),
            "instance_id": instance.id if instance else False,
            "channel": channel or "root",
            "model_name": records._name,
            "method_name": method_name,
            "res_ids": list(records.ids),
            "args": args,
            "kwargs": kwargs,
            "identity_key": identity_key or False,
            "priority": priority,
            "eta": eta or fields.Datetime.now(),
            "max_attempts": max(int(max_attempts or 1), 1),
            "user_id": self.env.uid,
        }
        if identity_key:
            try:
                with mute_logger("odoo.sql_db"), self.env.cr.savepoint():
                    job = jobs.create(vals)
                    job.flush_recordset()
            except psycopg2.IntegrityError:
                # Lost a race against a concurrent enqueue of the same key.
                job = jobs._find_active(identity_key)
                if not job:
                    raise
        else:
            job = jobs.create(vals)
        self._trigger_runner(vals["eta"])
        return job.with_env(self.env)

    def _find_active(self, identity_key):
        return self.search(
            [("identity_key", "=", identity_key), ("state", "in", ("pending", "started"))], limit=1
        )

    @api.model
    def _trigger_runner(self, eta=None):
        """Wake the runner cron (once per transaction) for jobs due now."""
        if eta and eta > fields.Datetime.now() + timedelta(minutes=1):
            return
        data = self.env.cr.precommit.data
        if data.get("magento_job_runner_triggered"):
            return
        cron = self.sudo().env.ref(_JOB_RUNNER_CRON, raise_if_not_found=False)
        if cron and cron.active:
            cron._trigger()
            data["magento_job_runner_triggered"] = True

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------
    def _job_can_commit(self):
        """Same guard as ``magento.instance._magento_can_commit``: commit only inside a
        real ``ir.cron`` run, never interactively and never under tests."""
        in_test_mode = getattr(self.env.registry, "in_test_mode", None)
        return bool(
            self.env.context.get("ir_cron_progress_id")
            and self.env.context.get("cron_id")
            and not odoo_module.current_test
            and not (in_test_mode() if callable(in_test_mode) else False)
        )

    def _job_commit(self, processed=0):
        """Commit (with cron progress) when allowed; return the cron's remaining time."""
        if self._job_can_commit():
            return self.env["ir.cron"]._commit_progress(processed)
        return float("inf")

    @api.model
    def _cron_run_jobs(self, limit=50):
        """Run up to ``limit`` ready jobs; one started job per channel at a time."""
        jobs = self.sudo()
        jobs._requeue_stale_jobs()
        jobs._job_commit(0)
        done = 0
        while done < limit:
            job = jobs._acquire_next_job()
            if not job:
                break
            job._run_job()
            done += 1
            if jobs._job_commit(1) <= 0:
                break
        return done

    def _acquire_next_job(self):
        """Lock the next ready job (priority, eta, id) of a channel with no started job."""
        self.flush_model()
        now = fields.Datetime.now()
        self.env.cr.execute(SQL(
            """
            SELECT j.id, j.channel
              FROM magento_job j
             WHERE j.state = 'pending'
               AND (j.eta IS NULL OR j.eta <= %s)
               AND NOT EXISTS (
                   SELECT 1 FROM magento_job s
                    WHERE s.state = 'started' AND s.channel = j.channel
               )
             ORDER BY j.priority, j.eta NULLS FIRST, j.id
             LIMIT %s
               FOR UPDATE OF j SKIP LOCKED
            """,
            now, _LOCK_WINDOW,
        ))
        tried = set()
        for job_id, channel in self.env.cr.fetchall():
            if channel in tried:
                continue
            tried.add(channel)
            # Serialises two workers that both saw the channel idle.
            self.env.cr.execute(SQL(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))", "magento.job.channel:%s" % channel
            ))
            if self.env.cr.fetchone()[0]:
                return self.browse(job_id)
        return self.browse()

    @api.model
    def _requeue_stale_jobs(self):
        """Started jobs older than one hour were interrupted (worker killed): requeue them."""
        now = fields.Datetime.now()
        stale = self.search([("state", "=", "started"), ("date_started", "<", now - _STALE_AFTER)])
        for job in stale:
            if job.attempts >= job.max_attempts:
                job.write({"state": "failed", "date_done": now, "error_class": "transient",
                           "last_error": "Job was interrupted and has no attempts left."})
            else:
                job.write({"state": "pending", "eta": now,
                           "last_error": "Job was interrupted; requeued."})
        return stale

    def _run_job(self):
        """Mark started, run in a savepoint, record the outcome. Commits only in real cron."""
        self.ensure_one()
        job = self.sudo()
        job.write({
            "state": "started",
            "date_started": fields.Datetime.now(),
            "attempts": job.attempts + 1,
        })
        job._job_commit(0)
        try:
            with self.env.cr.savepoint():
                result = job._perform()
        except Exception as exc:  # noqa: BLE001
            job._handle_failure(exc, traceback.format_exc())
            return False
        job.write({
            "state": "done",
            "date_done": fields.Datetime.now(),
            "result": job._format_result(result),
            "last_error": False,
            "error_class": False,
        })
        return True

    def _execution_env(self):
        ctx = {k: v for k, v in self.env.context.items() if k not in _CRON_CONTEXT_KEYS}
        ctx["magento_job_id"] = self.id
        return self.env(user=self.user_id.id or SUPERUSER_ID, context=ctx, su=False)

    def _perform(self):
        self.ensure_one()
        env = self._execution_env()
        if self.model_name not in env:
            raise UserError(_("Model %s does not exist.", self.model_name))
        records = env[self.model_name].browse(self.res_ids or [])
        if self.company_id:
            records = records.with_company(self.company_id)
        if self.res_ids:
            records = records.exists()
            if not records:
                raise UserError(_("The records of this job no longer exist."))
        if self.method_name.startswith("__") or not hasattr(type(records), self.method_name):
            raise UserError(_("Method %(method)s does not exist on %(model)s.",
                              method=self.method_name, model=self.model_name))
        return getattr(records, self.method_name)(*(self.args or []), **(self.kwargs or {}))

    @staticmethod
    def _format_result(result):
        if result is None or result is True:
            return False
        if isinstance(result, models.BaseModel):
            return "%s%s" % (result._name, result.ids)
        text = repr(result)
        return text if len(text) <= 2000 else text[:2000] + "..."

    def _handle_failure(self, exc, exc_info=None):
        self.ensure_one()
        severity, is_auth, _status = classify_exception(exc)
        message = exception_message(exc)
        now = fields.Datetime.now()
        vals = {"last_error": message, "error_class": severity, "exc_info": exc_info or False}
        if severity == "transient" and self.attempts < self.max_attempts:
            delay = backoff_delay(self.attempts)
            vals.update(state="pending", eta=now + timedelta(seconds=delay))
            _logger.info("Magento job %s (%s) failed transiently, retry %s/%s in %ss: %s",
                         self.id, self.name, self.attempts, self.max_attempts, delay, message)
        else:
            vals.update(state="failed", date_done=now)
            _logger.warning("Magento job %s (%s) failed (%s, attempt %s): %s",
                            self.id, self.name, severity, self.attempts, message)
        self.write(vals)
        if is_auth and self.instance_id:
            self.instance_id._magento_mark_auth_error(message)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_retry(self):
        now = fields.Datetime.now()
        for job in self.filtered(lambda j: j.state in ("failed", "cancelled", "pending")):
            if job.identity_key and job.state != "pending":
                other = job.sudo()._find_active(job.identity_key)
                if other and other != job:
                    continue  # an equivalent job is already queued
            job.write({"state": "pending", "eta": now, "attempts": 0, "date_done": False,
                       "date_started": False})
        self._trigger_runner()
        return True

    def action_cancel(self):
        todo = self.filtered(lambda j: j.state in ("pending", "failed"))
        todo.write({"state": "cancelled", "date_done": fields.Datetime.now()})
        return True

    def action_run_now(self):
        if not self.env.user.has_group("sdlc_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento administrators can run jobs immediately."))
        for job in self.filtered(lambda j: j.state in ("pending", "failed")):
            if job.state == "failed":
                job.sudo().attempts = 0
            job._run_job()
        return True

    def action_open_record(self):
        self.ensure_one()
        ids = self.res_ids or []
        if not ids or self.model_name not in self.env:
            return False
        action = {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": self.model_name,
        }
        if len(ids) == 1:
            action.update(view_mode="form", res_id=ids[0])
        else:
            action.update(view_mode="list,form", domain=[("id", "in", ids)])
        return action
