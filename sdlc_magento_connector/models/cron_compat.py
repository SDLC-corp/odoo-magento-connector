"""Odoo 17 stand-in for Odoo 19's ``ir.cron._commit_progress``.

Odoo 19 commits a cron batch, records progress and returns the seconds left
before the run should yield (``MIN_TIME_PER_JOB``, 10 s). Odoo 17 has no
progress record and gives a cron job only ``lastcall`` in its context, so a
real cron run is recognised by that key and the time budget is measured from
the run's first commit on its cursor. Callers use ``cron_compat.<function>``
(module attribute) so tests can patch it.
"""

import time
import weakref

# Odoo 19 ir_cron.MIN_TIME_PER_JOB: how long one cron run works before yielding.
RUN_BUDGET = 10.0

_run_started = weakref.WeakKeyDictionary()


def in_cron_run(env):
    """Whether ``env`` belongs to a job started by the cron runner (Odoo 17 puts
    ``lastcall`` in the context of every cron job)."""
    return "lastcall" in env.context


def commit_progress(env, processed=0):
    """Commit a batch of the running cron job and return its time left.

    Outside a cron run it only commits and returns ``inf``, like Odoo 19.
    """
    env.cr.commit()
    if not in_cron_run(env):
        return float("inf")
    now = time.monotonic()
    started = _run_started.setdefault(env.cr, now)
    return max(started + RUN_BUDGET - now, 0.0)
