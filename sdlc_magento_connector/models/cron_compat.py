"""Odoo 18 stand-in for Odoo 19's ``ir.cron._commit_progress``.

Odoo 19 commits a cron batch, adds it to the run's progress record and returns
the seconds left before the run should yield (runs get ``MIN_TIME_PER_JOB``,
10 s, before the next cron gets a turn). Odoo 18 has the progress record but no
such call, so the connector keeps the same contract here. Callers use
``cron_compat.commit_progress(...)`` (module attribute) so tests can patch it.
"""

import time

# Odoo 19 ir_cron.MIN_TIME_PER_JOB: how long one cron run works before yielding.
RUN_BUDGET = 10.0
_MAX_TRACKED_RUNS = 500

_run_started = {}


def commit_progress(env, processed=0):
    """Commit, add ``processed`` to the running cron's progress, return its time left.

    Outside a cron run it only commits and returns ``inf``, like Odoo 19.
    """
    progress_id = env.context.get("ir_cron_progress_id")
    progress = env["ir.cron.progress"].sudo().browse(progress_id).exists() if progress_id else None
    if not progress:
        env.cr.commit()
        return float("inf")
    now = time.monotonic()
    if len(_run_started) > _MAX_TRACKED_RUNS:
        _run_started.clear()
    started = _run_started.setdefault(progress.id, now)
    progress.write({
        "done": progress.done + processed,
        "remaining": max(progress.remaining - processed, 0),
    })
    env.cr.commit()
    return max(started + RUN_BUDGET - now, 0.0)
