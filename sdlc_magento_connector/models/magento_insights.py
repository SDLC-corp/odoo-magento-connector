"""Phase 3.10: operations insights.

* **Daily digest** (``magento.digest``): one e-mail per instance with yesterday's and the
  last 7 days' figures. Every number links back into Odoo through a
  ``magento.digest.link`` row, opened by the server action
  ``action_magento_digest_link_open`` (``/web#action=...&active_id=<link id>``), so the list opens
  on exactly the records counted, with the reader's own access rights.
* **Net profit** (``magento.net.profit``): read-only SQL view, one row per product line
  of a Magento order (order-level amounts allocated by line value), so it can be grouped
  by order, month, instance, website, payment method and product category.
* **Settings export / import** (``magento.settings.io``, ``magento.settings.transfer``):
  an instance's configuration as a versioned JSON file, never including secrets, and a
  previewed, idempotent import into another instance.
"""
import base64
import json
import logging
import re
from datetime import datetime, time, timedelta

import pytz

from odoo import Command, _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL, format_amount
from odoo.tools.sql import column_exists, table_exists

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "sdlc_magento_connector.group_magento_manager"
DIGEST_TEMPLATE = "sdlc_magento_connector.mail_template_magento_digest"
DIGEST_LINK_ACTION = "sdlc_magento_connector.action_magento_digest_link_open"
ERROR_CENTER_ACTION = "sdlc_magento_connector.action_magento_sync_issue"
JOB_QUEUE_ACTION = "sdlc_magento_connector.action_magento_job"
DIGEST_RETENTION_DAYS = 90
TOP_PRODUCTS = 5
_LINK_MARK = "__magento_digest_link_%d__"
_LINK_RE = re.compile(r"__magento_digest_link_(\d+)__")

SETTINGS_FORMAT = "sdlc_magento_settings/1"
# Never exported, whatever the field: credentials and keys.
SECRET_RE = re.compile(r"(token|secret|passw|consumer_key|api_key|private_key|credential)", re.I)
# Fields that identify the connection itself, not its configuration. The authentication
# method goes with the credentials, which are never exported: switching the target to OAuth
# without its own keys would break it (and is refused by the instance constraint).
INSTANCE_IDENTITY_FIELDS = {
    "name", "base_url", "active", "company_id", "website_id", "store_id", "store_code",
    "root_category_id", "auth_method", "oauth_signature_method",
}
ALWAYS_SKIPPED = {
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date",
    "instance_id", "company_id", "sequence_rank",
}


def _utc(value):
    return fields.Datetime.to_string(value)


# ======================================================================
# Payment fees and instance settings
# ======================================================================
class MagentoPaymentMap(models.Model):
    _inherit = "magento.payment.map"

    fee_percent = fields.Float(
        string="Fee (%)", digits=(16, 4),
        help="Payment provider fee as a percentage of the Magento grand total. Used by the "
             "Net Profit report.",
    )
    fee_fixed = fields.Float(
        string="Fixed Fee", digits=(16, 2),
        help="Payment provider fee per order, in company currency. Used by the Net Profit "
             "report.",
    )


class MagentoInstance(models.Model):
    _inherit = "magento.instance"

    digest_enabled = fields.Boolean(
        string="Daily Digest", default=True,
        help="Include this instance in the daily digest e-mail (scheduled action "
             "'Magento: Daily Digest', inactive by default).",
    )
    digest_partner_ids = fields.Many2many(
        "res.partner", "magento_instance_digest_partner_rel", "instance_id", "partner_id",
        string="Digest Recipients",
        help="Receive the daily digest, in addition to the Alert Users.",
    )
    digest_always_send = fields.Boolean(
        string="Send Even When Quiet",
        help="Off: the scheduled digest is skipped on days with no order, refund, failed job, "
             "new issue, new conflict or reconciliation finding.",
    )
    digest_last_sent_at = fields.Datetime(string="Last Digest Sent", readonly=True, copy=False)
    digest_count = fields.Integer(string="Digests", compute="_compute_digest_count")

    def _compute_digest_count(self):
        counts = dict(self.env["magento.digest"]._read_group(
            [("instance_id", "in", self.ids)], ["instance_id"], ["__count"]))
        for instance in self:
            instance.digest_count = counts.get(instance, 0)

    def _magento_insights_check_manager(self):
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can do this."))

    def _magento_digest_recipients(self):
        self.ensure_one()
        partners = self.alert_user_ids.filtered("active").partner_id | self.digest_partner_ids
        return partners.filtered("email")

    def action_send_digest_now(self):
        """Build and queue the digest now, even on a quiet day."""
        self._magento_insights_check_manager()
        sent = self.env["magento.digest"]
        for instance in self:
            if not instance.sudo()._magento_digest_recipients():
                raise UserError(_(
                    "%s has no digest recipient with an e-mail address. Add Alert Users or "
                    "Digest Recipients first.", instance.name))
            sent |= self.env["magento.digest"]._magento_build_and_send(instance, force=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Digest queued"),
                "message": _("%(count)s digest(s) placed in the outgoing mail queue.",
                             count=len(sent)),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_view_digests(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_digest")
        action["domain"] = [("instance_id", "=", self.id)]
        action["context"] = {}
        return action

    def action_export_settings(self):
        self.ensure_one()
        self._magento_insights_check_manager()
        return self.env["magento.settings.io"]._magento_export_download(self)

    def action_import_settings(self):
        self.ensure_one()
        self._magento_insights_check_manager()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_settings_transfer")
        action["context"] = {"default_instance_id": self.id}
        return action

    def action_open_net_profit(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "sdlc_magento_connector.action_magento_net_profit")
        action["domain"] = [("instance_id", "=", self.id)]
        return action

    @api.model
    def _cron_magento_send_digest(self):
        """Scheduled digest: one e-mail per enabled instance; quiet days are skipped."""
        Digest = self.env["magento.digest"]
        for instance in self.sudo().search([("digest_enabled", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    Digest._magento_build_and_send(instance)
            except Exception:  # noqa: BLE001 - one broken instance must not stop the others
                _logger.exception("Magento digest failed for instance %s", instance.id)
        Digest._magento_cleanup()
        return True


# ======================================================================
# Digest
# ======================================================================
class MagentoDigest(models.Model):
    _name = "magento.digest"
    _description = "Magento Daily Digest"
    _order = "sent_at desc, id desc"

    name = fields.Char(required=True, readonly=True)
    instance_id = fields.Many2one("magento.instance", required=True, index=True,
                                  ondelete="cascade", readonly=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    day = fields.Date(string="Day", readonly=True, help="The day the digest reports on.")
    sent_at = fields.Datetime(string="Queued At", readonly=True, index=True)
    state = fields.Selection([("sent", "Queued"), ("skipped", "Skipped")], readonly=True,
                             default="sent", required=True)
    recipient_ids = fields.Many2many("res.partner", "magento_digest_partner_rel", "digest_id",
                                     "partner_id", string="Recipients", readonly=True)
    payload = fields.Json(readonly=True)
    body_html = fields.Html(string="E-mail", readonly=True, sanitize=False)
    mail_id = fields.Many2one("mail.mail", string="Mail", readonly=True, ondelete="set null")
    link_ids = fields.One2many("magento.digest.link", "digest_id", string="Links", readonly=True)
    orders_yesterday = fields.Integer(readonly=True)
    revenue_yesterday = fields.Float(readonly=True, digits=(16, 2))

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------
    @api.model
    def _magento_windows(self, instance, now=None):
        """(yesterday start, today start, week start) as naive UTC, plus local dates."""
        tz_name = instance.auto_sync_tz or self.env.user.tz or "UTC"
        try:
            tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            tz = pytz.utc
        now = now or fields.Datetime.now()
        today = pytz.utc.localize(now).astimezone(tz).date()

        def start(day):
            return tz.localize(datetime.combine(day, time.min)).astimezone(pytz.utc).replace(tzinfo=None)

        yesterday = today - timedelta(days=1)
        week = today - timedelta(days=7)
        return {
            "today": today, "yesterday": yesterday, "week": week,
            "t0": start(today), "y0": start(yesterday), "w0": start(week),
        }

    # ------------------------------------------------------------------
    # Figures
    # ------------------------------------------------------------------
    @api.model
    def _magento_digest_payload(self, instance, now=None):
        """All the digest figures of ``instance``; links are placeholders (see _finalize)."""
        env = self.env
        win = self._magento_windows(instance, now=now)
        company = instance.company_id
        currency = company.currency_id
        rate = env["magento.dashboard"]._rate_getter(currency, company)
        links = []

        def link(name, model, domain, action=False):
            links.append({"name": name, "res_model": model, "domain": domain,
                          "action_xmlid": action or False})
            return _LINK_MARK % (len(links) - 1)

        def money(value):
            return format_amount(env, value or 0.0, currency)

        windows = {
            "y": (_utc(win["y0"]), _utc(win["t0"]), fields.Date.to_string(win["yesterday"])),
            "w": (_utc(win["w0"]), _utc(win["t0"]), fields.Date.to_string(win["week"])),
        }
        today_str = fields.Date.to_string(win["today"])
        raw = {}

        # -- orders and revenue --------------------------------------------------------
        SaleOrder = env["sale.order"]
        order_base = [("magento_instance_id", "=", instance.id), ("magento_order_id", "!=", False)]
        sales_rows = []
        orders, revenue, refunds = {}, {}, {}
        order_domains, live_domains, refund_domains = {}, {}, {}
        for key, (start, end, start_day) in windows.items():
            dom = order_base + [("date_order", ">=", start), ("date_order", "<", end)]
            order_domains[key] = dom
            live_domains[key] = dom + [("state", "!=", "cancel")]
            count, amount = 0, 0.0
            for cur, day, state, total, n in SaleOrder._read_group(
                    dom, ["currency_id", "date_order:day", "state"], ["amount_total:sum", "__count"]):
                count += n
                if state != "cancel":
                    amount += (total or 0.0) * rate(cur, env["magento.dashboard"]._as_date(day))
            orders[key], revenue[key] = count, currency.round(amount)
            rdom = [
                ("move_type", "=", "out_refund"), ("state", "=", "posted"),
                ("magento_instance_id", "=", instance.id),
                ("invoice_date", ">=", start_day), ("invoice_date", "<", today_str),
            ]
            refund_domains[key] = rdom
            rows = env["account.move"]._read_group(rdom, [], ["amount_total_signed:sum", "__count"])
            total_signed, refund_count = rows[0] if rows else (0.0, 0)
            refunds[key] = currency.round(-(total_signed or 0.0))
            raw["refund_count_" + key] = refund_count
        for key in windows:
            raw["orders_" + key] = orders[key]
            raw["revenue_" + key] = revenue[key]
            raw["refunds_" + key] = refunds[key]
            raw["net_" + key] = currency.round(revenue[key] - refunds[key])
        sales_rows.append({
            "label": _("Orders"),
            "y": str(orders["y"]), "y_url": link(_("Magento orders: yesterday"), "sale.order", order_domains["y"]),
            "w": str(orders["w"]), "w_url": link(_("Magento orders: last 7 days"), "sale.order", order_domains["w"]),
        })
        sales_rows.append({
            "label": _("Revenue"),
            "y": money(revenue["y"]), "y_url": link(_("Magento revenue: yesterday"), "sale.order", live_domains["y"]),
            "w": money(revenue["w"]), "w_url": link(_("Magento revenue: last 7 days"), "sale.order", live_domains["w"]),
        })
        sales_rows.append({
            "label": _("Refunds"),
            "y": money(refunds["y"]), "y_url": link(_("Magento credit notes: yesterday"), "account.move", refund_domains["y"]),
            "w": money(refunds["w"]), "w_url": link(_("Magento credit notes: last 7 days"), "account.move", refund_domains["w"]),
        })
        sales_rows.append({
            "label": _("Net (revenue - refunds)"), "strong": True,
            "y": money(raw["net_y"]),
            "y_url": link(_("Net profit: yesterday"), "magento.net.profit",
                          [("instance_id", "=", instance.id), ("date_order", ">=", windows["y"][0]),
                           ("date_order", "<", windows["y"][1])],
                          action="sdlc_magento_connector.action_magento_net_profit"),
            "w": money(raw["net_w"]),
            "w_url": link(_("Net profit: last 7 days"), "magento.net.profit",
                          [("instance_id", "=", instance.id), ("date_order", ">=", windows["w"][0]),
                           ("date_order", "<", windows["w"][1])],
                          action="sdlc_magento_connector.action_magento_net_profit"),
        })

        # -- top products (7 days) -----------------------------------------------------
        top_products = []
        line_rows = env["sale.order.line"]._read_group(
            [("order_id", "any", live_domains["w"]), ("display_type", "=", False),
             ("product_id", "!=", False),
             "|", ("magento_line_role", "=", False), ("magento_line_role", "=", "product")],
            ["product_id", "currency_id", "order_id"],  # Odoo 18: no related-path group-by
            ["price_subtotal:sum", "product_uom_qty:sum"],
        )
        per_product = {}
        for product, cur, order, amount, qty in line_rows:
            entry = per_product.setdefault(product, {"revenue": 0.0, "qty": 0.0})
            entry["revenue"] += (amount or 0.0) * rate(cur, env["magento.dashboard"]._as_date(order.date_order))
            entry["qty"] += qty or 0.0
        ranked = sorted(per_product.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:TOP_PRODUCTS]
        for product, entry in ranked:
            top_products.append({
                "name": product.display_name,
                "qty": "%g" % round(entry["qty"], 2),
                "revenue": money(entry["revenue"]),
                "url": link(_("Orders with %s", product.display_name), "sale.order",
                            live_domains["w"] + [("order_line.product_id", "=", product.id)]),
            })
        raw["top_products"] = [(p.id, currency.round(e["revenue"])) for p, e in ranked]

        # -- operations health ---------------------------------------------------------
        health_rows = []
        window_labels = {"y": _("yesterday"), "w": _("last 7 days")}

        def windowed(label, model, date_field, base, key_name, tone, action=False, title=None):
            if model not in env:
                return
            vals = {"label": label, "tone": ""}
            for key, (start, end, _day) in windows.items():
                dom = base + [(date_field, ">=", start), (date_field, "<", end)]
                count = env[model].search_count(dom)
                raw["%s_%s" % (key_name, key)] = count
                vals[key] = str(count)
                vals[key + "_url"] = link("%s: %s" % (title or label, window_labels[key]),
                                          model, dom, action=action)
                if key == "w" and count:
                    vals["tone"] = tone
            health_rows.append(vals)

        windowed(_("Failed jobs"), "magento.job", "date_done",
                 [("instance_id", "=", instance.id), ("state", "=", "failed")],
                 "failed_jobs", "crit", action=JOB_QUEUE_ACTION)
        windowed(_("New sync issues"), "magento.sync.issue", "first_seen",
                 [("instance_id", "=", instance.id)], "new_issues", "warn", action=ERROR_CENTER_ACTION)
        windowed(_("New order conflicts"), "magento.order.conflict", "detected_at",
                 [("instance_id", "=", instance.id)], "new_conflicts", "warn",
                 action="sdlc_magento_connector.action_magento_order_conflict")
        windowed(_("Reconciliation findings"), "magento.reconciliation.order", "create_date",
                 [("instance_id", "=", instance.id), ("kind", "in", ("missing", "mismatch"))],
                 "mismatches", "crit")
        if "magento.totals.explanation" in env:
            windowed(_("Unexplained totals"), "magento.totals.explanation", "date_explained",
                     [("instance_id", "=", instance.id), ("state", "!=", "explained")],
                     "unexplained_totals", "warn",
                     action="sdlc_magento_connector.action_magento_totals_explanation")

        # -- open now -----------------------------------------------------------------
        open_rows = []
        Issue = env["magento.sync.issue"]
        open_base = [("instance_id", "=", instance.id), ("state", "=", "open")]
        labels = dict(Issue._fields["entity"]._description_selection(env))
        open_total = 0
        for entity, count in Issue._read_group(open_base, ["entity"], ["__count"], order="__count desc"):
            open_total += count
            label = labels.get(entity, entity or _("Other"))
            open_rows.append({
                "label": _("Open issues: %s", label), "count": str(count), "tone": "warn",
                "url": link(_("Open issues: %s", label), "magento.sync.issue",
                            open_base + [("entity", "=", entity)], action=ERROR_CENTER_ACTION),
            })
        raw["open_issues"] = open_total
        if "magento.order.conflict" in env:
            cdom = [("instance_id", "=", instance.id), ("state", "=", "open")]
            count = env["magento.order.conflict"].search_count(cdom)
            raw["open_conflicts"] = count
            open_rows.append({
                "label": _("Unresolved conflicts"), "count": str(count), "tone": "crit" if count else "",
                "url": link(_("Unresolved order conflicts"), "magento.order.conflict", cdom,
                            action="sdlc_magento_connector.action_magento_order_conflict"),
            })

        # -- pushes and real-time ------------------------------------------------------
        push_rows = []
        if "stock_push_enabled" in instance._fields and "magento.stock.dirty" in env:
            waiting_dom = [("instance_id", "=", instance.id)]
            waiting = env["magento.stock.dirty"].search_count(waiting_dom)
            failed_dom = [("instance_id", "=", instance.id), ("state", "=", "failed"),
                          ("method_name", "ilike", "stock"),
                          ("date_done", ">=", windows["w"][0])]
            failed = env["magento.job"].search_count(failed_dom)
            raw["stock_waiting"], raw["stock_failed_w"] = waiting, failed
            status = _("Automatic") if instance.stock_push_enabled else _("Manual only")
            push_rows.append({
                "label": _("Stock push"),
                "status": _("%(mode)s · %(waiting)s change(s) waiting", mode=status, waiting=waiting),
                "url": link(_("Stock changes waiting"), "magento.stock.dirty", waiting_dom),
                "detail": _("%s failed push job(s) in 7 days", failed),
                "detail_url": link(_("Failed stock push jobs"), "magento.job", failed_dom, action=JOB_QUEUE_ACTION),
                "tone": "crit" if failed else ("ok" if instance.stock_push_enabled else ""),
            })
        if "magento.price.rule" in env:
            rule_dom = [("instance_id", "=", instance.id)]
            rules = env["magento.price.rule"].search(rule_dom)
            bad = rules.filtered(lambda r: r.last_push_state in ("failed", "partial"))
            last = max((r.last_push_at for r in rules if r.last_push_at), default=False)
            raw["price_rules"], raw["price_failed"] = len(rules), len(bad)
            push_rows.append({
                "label": _("Price push"),
                "status": (_("%(rules)s rule(s) · last push %(when)s", rules=len(rules),
                             when=fields.Datetime.to_string(last)[:16] if last else _("never"))),
                "url": link(_("Price rules"), "magento.price.rule", rule_dom,
                            action="sdlc_magento_connector.action_magento_price_rule"),
                "detail": _("%s rule(s) failed or partial", len(bad)),
                "detail_url": link(_("Price rules failing"), "magento.price.rule",
                                   rule_dom + [("last_push_state", "in", ("failed", "partial"))],
                                   action="sdlc_magento_connector.action_magento_price_rule"),
                "tone": "crit" if bad else ("ok" if rules else ""),
            })
        webhook = False
        if "webhook_enabled" in instance._fields:
            health_labels = dict(instance._fields["webhook_health"]._description_selection(env)) \
                if "webhook_health" in instance._fields else {}
            health = instance.webhook_health if "webhook_health" in instance._fields else False
            since = _utc(fields.Datetime.now() - timedelta(hours=24))
            ev_dom = [("instance_id", "=", instance.id), ("received_at", ">=", since)]
            events = instance.webhook_event_count if "webhook_event_count" in instance._fields else 0
            rejected = instance.webhook_rejected_count if "webhook_rejected_count" in instance._fields else 0
            last = instance.last_webhook_at if "last_webhook_at" in instance._fields else False
            raw["webhook_events"], raw["webhook_rejected"] = events, rejected
            webhook = {
                "label": _("Real-time push"),
                "status": health_labels.get(health, _("Off") if not instance.webhook_enabled else ""),
                "last": fields.Datetime.to_string(last)[:16] if last else _("never"),
                "events": str(events), "rejected": str(rejected),
                "url": link(_("Real-time events (24 h)"), "magento.webhook.event", ev_dom,
                            action="sdlc_magento_connector.action_magento_webhook_event")
                if "magento.webhook.event" in env else False,
                "tone": {"healthy": "ok", "rejecting": "crit", "stale": "warn"}.get(health, ""),
            }

        has_news = any(raw.get(k) for k in (
            "orders_y", "refund_count_y", "failed_jobs_y", "new_issues_y", "new_conflicts_y",
            "mismatches_y"))
        base_url = instance.get_base_url()
        payload = {
            "instance": instance.name,
            "company": company.name,
            "currency": currency.name,
            "day": fields.Date.to_string(win["yesterday"]),
            "week_from": fields.Date.to_string(win["week"]),
            "console_url": "%s/web#action=sdlc_magento_connector.action_magento_dashboard" % base_url,
            "instance_url": "%s/web#model=magento.instance&id=%s&view_type=form" % (base_url, instance.id),
            "sales": sales_rows,
            "top_products": top_products,
            "health": health_rows,
            "open": open_rows,
            "push": push_rows,
            "webhook": webhook,
            "has_news": bool(has_news),
            "raw": raw,
        }
        return payload, links

    # ------------------------------------------------------------------
    # Build, send, clean up
    # ------------------------------------------------------------------
    @api.model
    def _magento_build_and_send(self, instance, force=False, now=None):
        """Compute and queue ``instance``'s digest. Returns the digest, or an empty recordset
        when the day was quiet (and neither ``force`` nor "Send Even When Quiet")."""
        instance = instance.sudo()
        Digest = self.sudo().with_company(instance.company_id).with_context(
            allowed_company_ids=[instance.company_id.id])
        payload, links = Digest._magento_digest_payload(instance, now=now)
        recipients = instance._magento_digest_recipients()
        if not recipients:
            _logger.info("Magento digest of %s skipped: no recipient", instance.name)
            return self.browse()
        if not (force or instance.digest_always_send or payload["has_news"]):
            _logger.info("Magento digest of %s skipped: nothing new", instance.name)
            return self.browse()
        now = now or fields.Datetime.now()
        digest = Digest.create({
            "name": _("%(instance)s · %(day)s", instance=instance.name, day=payload["day"]),
            "instance_id": instance.id,
            "day": payload["day"],
            "sent_at": now,
            "recipient_ids": [Command.set(recipients.ids)],
            "orders_yesterday": payload["raw"].get("orders_y", 0),
            "revenue_yesterday": payload["raw"].get("revenue_y", 0.0),
        })
        link_records = Digest.env["magento.digest.link"].create([
            dict(spec, digest_id=digest.id, domain=json.dumps(spec["domain"])) for spec in links
        ])
        base_url = instance.get_base_url()
        urls = ["%s/web#action=%s&active_id=%s" % (base_url, DIGEST_LINK_ACTION, rec.id) for rec in link_records]
        text = _LINK_RE.sub(lambda m: urls[int(m.group(1))], json.dumps(payload))
        digest.payload = json.loads(text)

        template = Digest.env.ref(DIGEST_TEMPLATE)
        body = template._render_field("body_html", digest.ids)[digest.id]
        subject = template._render_field("subject", digest.ids)[digest.id]
        email_from = template._render_field("email_from", digest.ids)[digest.id] \
            or instance.company_id.email_formatted or Digest.env.user.email_formatted
        mail = Digest.env["mail.mail"].create({
            "subject": subject,
            "body_html": body,
            "email_from": email_from,
            "recipient_ids": [Command.set(recipients.ids)],
            "model": "magento.digest",
            "res_id": digest.id,
            "auto_delete": True,
        })
        digest.write({"body_html": body, "mail_id": mail.id})
        instance.digest_last_sent_at = now
        return digest.with_env(self.env)

    @api.model
    def _magento_cleanup(self):
        cutoff = fields.Datetime.now() - timedelta(days=DIGEST_RETENTION_DAYS)
        old = self.sudo().search([("sent_at", "<", cutoff)])
        old.unlink()
        return len(old)

    def action_open_mail(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "mail.mail",
            "res_id": self.mail_id.id,
            "views": [[False, "form"]],
            "target": "current",
        }


class MagentoDigestLink(models.Model):
    """The records behind one digest figure: opening the link lists exactly those."""
    _name = "magento.digest.link"
    _description = "Magento Digest Link"

    digest_id = fields.Many2one("magento.digest", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one(related="digest_id.company_id", store=True, index=True)
    name = fields.Char(required=True)
    res_model = fields.Char(required=True)
    domain = fields.Text(default="[]", help="JSON domain of the records counted.")
    action_xmlid = fields.Char(help="Action to open (search view, help) instead of a plain list.")

    def _magento_open(self):
        self.ensure_one()
        if self.res_model not in self.env:
            raise UserError(_("The records of this figure are no longer available."))
        domain = json.loads(self.domain or "[]")
        action = {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": self.res_model,
            "views": [[False, "tree"], [False, "form"]],
            "domain": domain,
            "target": "current",
            "context": {"create": False},
        }
        if self.action_xmlid and self.env.ref(self.action_xmlid, raise_if_not_found=False):
            try:
                base = self.env["ir.actions.act_window"]._for_xml_id(self.action_xmlid)
            except (AccessError, ValueError):
                base = {}
            if base.get("res_model") == self.res_model:
                base.update(name=self.name, domain=domain, context={"create": False}, target="current")
                return base
        return action


# ======================================================================
# Net profit
# ======================================================================
class MagentoNetProfit(models.Model):
    """Net profit of Magento orders, in company currency.

    One row per product line of a non-cancelled Magento order (one row per order when it
    has no product line). Order-level amounts (revenue, tax, refunds, shipping, payment
    fee) are allocated to the lines by their share of the order's product subtotal, so
    every grouping (order, month, product category...) adds up to the order totals. The
    ``Orders`` measure is the sum of those shares: the number of orders.

    * Revenue: the Magento grand total (Odoo total when Magento's is unknown), with and
      without tax.
    * Refunds: posted credit notes of the order, untaxed.
    * Cost of goods: stock valuation of the delivered (minus returned) quantities when
      ``stock_account`` valued the moves, else product cost x quantity delivered.
    * Shipping cost: carrier price of the done deliveries when the Delivery module knows
      it; otherwise the shipping charged to the customer, flagged as an estimate.
    * Payment fee: percentage and fixed fee of the order's payment method mapping.

    Website: orders do not carry their Magento store yet, so the instance's default
    website is used.
    """
    _name = "magento.net.profit"
    _description = "Magento Net Profit"
    _auto = False
    _order = "date_order desc, order_id desc, id"
    _rec_name = "order_id"

    order_id = fields.Many2one("sale.order", string="Order", readonly=True)
    line_id = fields.Many2one("sale.order.line", string="Order Line", readonly=True)
    instance_id = fields.Many2one("magento.instance", string="Instance", readonly=True)
    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    website_id = fields.Many2one("magento.website", string="Website", readonly=True)
    date_order = fields.Datetime(string="Order Date", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer", readonly=True)
    payment_method = fields.Char(string="Payment Method", readonly=True)
    product_id = fields.Many2one("product.product", string="Product", readonly=True)
    categ_id = fields.Many2one("product.category", string="Product Category", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)
    order_count = fields.Float(string="Orders", digits=(16, 2), readonly=True)
    revenue_total = fields.Monetary(string="Revenue (incl. tax)", readonly=True)
    tax_amount = fields.Monetary(string="Tax", readonly=True)
    revenue_untaxed = fields.Monetary(string="Revenue (excl. tax)", readonly=True)
    refund_amount = fields.Monetary(string="Refunds", readonly=True)
    cogs = fields.Monetary(string="Cost of Goods", readonly=True)
    shipping_revenue = fields.Monetary(string="Shipping Charged", readonly=True)
    shipping_cost = fields.Monetary(string="Shipping Cost", readonly=True)
    shipping_estimated = fields.Boolean(
        string="Shipping Cost Estimated", readonly=True,
        help="No carrier cost is known: the shipping charged to the customer is used.")
    payment_fee = fields.Monetary(string="Payment Fees", readonly=True)
    net_profit = fields.Monetary(string="Net Profit", readonly=True)
    cost_source = fields.Selection(
        [("valuation", "Stock valuation"), ("standard", "Product cost"), ("none", "No cost")],
        string="Cost Source", readonly=True)

    def _magento_query(self):
        cr = self.env.cr
        valued = column_exists(cr, "stock_move", "value") and column_exists(cr, "stock_move", "is_in")
        carrier = column_exists(cr, "stock_picking", "carrier_price") and column_exists(cr, "stock_picking", "sale_id")
        cr.execute("SELECT data_type FROM information_schema.columns "
                   "WHERE table_name = 'product_product' AND column_name = 'standard_price'")
        row = cr.fetchone()
        if row and row[0] == "jsonb":
            std = "COALESCE((pp.standard_price ->> o.company_id::text)::numeric, 0)"
        elif row:
            std = "COALESCE(pp.standard_price, 0)::numeric"
        else:
            # Odoo 17 stores company-dependent fields in ir_property.
            std = """COALESCE((SELECT ip.value_float FROM ir_property ip
                                WHERE ip.name = 'standard_price'
                                  AND ip.res_id = 'product.product,' || pp.id
                                  AND (ip.company_id = o.company_id OR ip.company_id IS NULL)
                                ORDER BY ip.company_id NULLS LAST LIMIT 1), 0)::numeric"""
        valued_cte = """
            SELECT sm.sale_line_id AS line_id,
                   SUM(CASE WHEN sm.is_in THEN -COALESCE(sm.value, 0) ELSE COALESCE(sm.value, 0) END) AS cost
              FROM stock_move sm
             WHERE sm.state = 'done' AND sm.sale_line_id IS NOT NULL AND (sm.is_out OR sm.is_in)
             GROUP BY sm.sale_line_id
        """ if valued else "SELECT NULL::integer AS line_id, NULL::numeric AS cost WHERE FALSE"
        carrier_cte = """
            SELECT sp.sale_id AS order_id, SUM(COALESCE(sp.carrier_price, 0)) AS cost
              FROM stock_picking sp
             WHERE sp.state = 'done' AND sp.sale_id IS NOT NULL
             GROUP BY sp.sale_id
        """ if carrier else "SELECT NULL::integer AS order_id, NULL::numeric AS cost WHERE FALSE"
        downpayment = "AND NOT COALESCE(sol.is_downpayment, FALSE)" \
            if column_exists(cr, "sale_order_line", "is_downpayment") else ""
        return """
            WITH o AS (
                SELECT so.id AS order_id, so.magento_instance_id AS instance_id, so.company_id,
                       so.date_order, so.partner_id, so.magento_payment_method AS payment_method,
                       c.currency_id,
                       CASE WHEN COALESCE(so.currency_rate, 0) = 0 THEN 1.0
                            ELSE so.currency_rate::numeric END AS rate,
                       COALESCE(NULLIF(so.magento_grand_total, 0), so.amount_total, 0)::numeric AS grand_total,
                       COALESCE(so.amount_tax, 0)::numeric AS tax
                  FROM sale_order so
                  JOIN res_company c ON c.id = so.company_id
                 WHERE so.magento_instance_id IS NOT NULL
                   AND so.magento_order_id IS NOT NULL
                   AND so.state != 'cancel'
            ),
            pl AS (
                SELECT sol.id AS line_id, sol.order_id, sol.product_id, pt.categ_id,
                       COALESCE(sol.price_subtotal, 0)::numeric AS subtotal,
                       COALESCE(sol.qty_delivered, 0)::numeric AS qty_delivered,
                       %(std)s AS std_cost
                  FROM sale_order_line sol
                  JOIN o ON o.order_id = sol.order_id
                  JOIN product_product pp ON pp.id = sol.product_id
                  JOIN product_template pt ON pt.id = pp.product_tmpl_id
                 WHERE sol.display_type IS NULL
                   AND COALESCE(sol.magento_line_role, 'product') = 'product'
                   %(downpayment)s
            ),
            plt AS (
                SELECT order_id, SUM(subtotal) AS total, COUNT(*) AS n FROM pl GROUP BY order_id
            ),
            ship AS (
                SELECT sol.order_id, SUM(COALESCE(sol.price_subtotal, 0))::numeric AS revenue
                  FROM sale_order_line sol
                  JOIN o ON o.order_id = sol.order_id
                 WHERE sol.magento_line_role = 'shipping'
                 GROUP BY sol.order_id
            ),
            refund AS (
                SELECT am.magento_sale_order_id AS order_id,
                       -SUM(COALESCE(am.amount_untaxed_signed, 0))::numeric AS untaxed
                  FROM account_move am
                 WHERE am.move_type = 'out_refund' AND am.state = 'posted'
                   AND am.magento_sale_order_id IS NOT NULL
                 GROUP BY am.magento_sale_order_id
            ),
            fee AS (
                SELECT pm.instance_id, pm.magento_method,
                       MAX(COALESCE(pm.fee_percent, 0))::numeric AS fee_percent,
                       MAX(COALESCE(pm.fee_fixed, 0))::numeric AS fee_fixed
                  FROM magento_payment_map pm
                 WHERE pm.active
                 GROUP BY pm.instance_id, pm.magento_method
            ),
            valued AS (%(valued)s),
            carrier AS (%(carrier)s),
            website AS (
                SELECT DISTINCT ON (w.instance_id) w.instance_id, w.id AS website_id
                  FROM magento_website w
                 WHERE w.active
                 ORDER BY w.instance_id, w.is_default DESC, w.sequence, w.magento_id
            ),
            ord AS (
                SELECT o.order_id, o.instance_id, o.company_id, o.date_order, o.partner_id,
                       o.payment_method, o.currency_id, ws.website_id,
                       o.grand_total / o.rate AS revenue_total,
                       o.tax / o.rate AS tax_amount,
                       (o.grand_total - o.tax) / o.rate AS revenue_untaxed,
                       COALESCE(r.untaxed, 0) AS refund_amount,
                       COALESCE(s.revenue, 0) / o.rate AS shipping_revenue,
                       CASE WHEN COALESCE(cr.cost, 0) > 0 THEN cr.cost
                            ELSE COALESCE(s.revenue, 0) / o.rate END AS shipping_cost,
                       (COALESCE(cr.cost, 0) <= 0 AND COALESCE(s.revenue, 0) <> 0) AS shipping_estimated,
                       COALESCE(f.fee_percent, 0) * o.grand_total / o.rate / 100.0
                           + COALESCE(f.fee_fixed, 0) AS payment_fee
                  FROM o
                  LEFT JOIN refund r ON r.order_id = o.order_id
                  LEFT JOIN ship s ON s.order_id = o.order_id
                  LEFT JOIN carrier cr ON cr.order_id = o.order_id
                  LEFT JOIN fee f ON f.instance_id = o.instance_id AND f.magento_method = o.payment_method
                  LEFT JOIN website ws ON ws.instance_id = o.instance_id
            ),
            lines AS (
                SELECT pl.line_id * 2 AS id, pl.line_id, pl.order_id, pl.product_id, pl.categ_id,
                       CASE WHEN plt.total <> 0 THEN pl.subtotal / plt.total
                            ELSE 1.0 / plt.n END AS share,
                       CASE WHEN COALESCE(v.cost, 0) <> 0 THEN v.cost
                            ELSE pl.qty_delivered * pl.std_cost END AS cogs,
                       CASE WHEN COALESCE(v.cost, 0) <> 0 THEN 'valuation'
                            WHEN pl.qty_delivered * pl.std_cost <> 0 THEN 'standard'
                            ELSE 'none' END AS cost_source
                  FROM pl
                  JOIN plt ON plt.order_id = pl.order_id
                  LEFT JOIN valued v ON v.line_id = pl.line_id
                UNION ALL
                SELECT ord.order_id * 2 + 1, NULL, ord.order_id, NULL, NULL, 1.0, 0, 'none'
                  FROM ord
                 WHERE NOT EXISTS (SELECT 1 FROM pl WHERE pl.order_id = ord.order_id)
            )
            SELECT l.id, l.order_id, l.line_id, ord.instance_id, ord.company_id, ord.website_id,
                   ord.date_order, ord.partner_id, ord.payment_method, l.product_id, l.categ_id,
                   ord.currency_id, l.cost_source,
                   l.share AS order_count,
                   ord.revenue_total * l.share AS revenue_total,
                   ord.tax_amount * l.share AS tax_amount,
                   ord.revenue_untaxed * l.share AS revenue_untaxed,
                   ord.refund_amount * l.share AS refund_amount,
                   l.cogs AS cogs,
                   ord.shipping_revenue * l.share AS shipping_revenue,
                   ord.shipping_cost * l.share AS shipping_cost,
                   ord.shipping_estimated,
                   ord.payment_fee * l.share AS payment_fee,
                   (ord.revenue_untaxed - ord.refund_amount - ord.shipping_cost - ord.payment_fee)
                       * l.share - l.cogs AS net_profit
              FROM lines l
              JOIN ord ON ord.order_id = l.order_id
        """ % {"std": std, "downpayment": downpayment, "valued": valued_cte, "carrier": carrier_cte}

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(SQL("CREATE OR REPLACE VIEW %s AS (%s)",
                                SQL.identifier(self._table), SQL(self._magento_query())))


# ======================================================================
# Settings export / import
# ======================================================================
class _Pending:
    """A reference to a record that an earlier section of the same import creates."""

    def __repr__(self):
        return "<pending>"


PENDING = _Pending()


class MagentoSettingsIO(models.AbstractModel):
    _name = "magento.settings.io"
    _description = "Magento Settings Export/Import"

    # (section key, label, model, natural key fields). Order matters: references to records
    # of an earlier section (customer groups, return reasons, Magento fields) resolve on import.
    SECTIONS = [
        ("customer_groups", "Customer groups", "magento.customer.group", ["magento_group_id"]),
        ("magento_fields", "Magento fields", "magento.available.field", ["mapping_type", "code"]),
        ("tax_map", "Tax mapping", "magento.tax.map", ["magento_code", "percent", "price_include"]),
        ("payment_map", "Payment methods", "magento.payment.map", ["magento_method"]),
        ("carrier_map", "Carriers", "magento.carrier.map", ["magento_code"]),
        ("order_workflows", "Order workflows", "magento.order.workflow", ["name"]),
        ("return_reasons", "Return reasons", "magento.return.reason", ["code"]),
        ("return_location_rules", "Return location rules", "magento.return.location.rule",
         ["warehouse_id", "categ_id", "reason_id", "location_id"]),
        ("field_mappings", "Field mappings", "magento.field.mapping",
         ["mapping_type", "odoo_field", "magento_field_id"]),
        ("price_rules", "Price rules", "magento.price.rule", ["name"]),
        ("stock_source_maps", "Stock sources", "magento.stock.source.map", ["source_code"]),
    ]
    # Records owned by an instance: referenced by their key inside the target instance.
    INSTANCE_SCOPED_REFS = {
        "magento.customer.group": ["magento_group_id"],
        "magento.return.reason": ["code"],
        "magento.available.field": ["mapping_type", "code"],
        "magento.order.workflow": ["name"],
    }
    # Other references: alternatives tried in order, the first unique match wins.
    REF_KEYS = {
        "account.journal": [["code"], ["name"]],
        "account.tax": [["name", "amount", "type_tax_use"], ["name", "type_tax_use"]],
        "product.product": [["default_code"], ["name"]],
        "product.category": [["complete_name"]],
        "stock.location": [["complete_name"]],
        "stock.warehouse": [["code"], ["name"]],
        "res.users": [["login"]],
        "res.partner": [["email"], ["name"]],
        "res.currency": [["name"]],
        "ir.model.fields": [["model", "name"]],
    }

    # ------------------------------------------------------------------
    # Field selection
    # ------------------------------------------------------------------
    @api.model
    def _magento_is_secret(self, name, field):
        if SECRET_RE.search(name):
            return True
        return bool(field.groups) and field.type in ("char", "text", "binary")

    @api.model
    def _magento_fields(self, model):
        """Fields of ``model`` that are configuration: stored, editable, copied, not secret."""
        Model = self.env[model]
        names = []
        for name, field in Model._fields.items():
            if name in ALWAYS_SKIPPED or name.startswith(("message_", "activity_", "website_message")):
                continue
            if model == "magento.instance" and name in INSTANCE_IDENTITY_FIELDS:
                continue
            if not field.store or field.compute or field.related or field.readonly or not field.copy:
                continue
            if field.type in ("one2many", "binary", "html", "json", "many2one_reference", "properties"):
                continue
            if self._magento_is_secret(name, field):
                continue
            if field.type in ("many2one", "many2many") and field.comodel_name in ("ir.cron", "mail.activity"):
                continue
            names.append(name)
        return names

    @api.model
    def _magento_sections(self):
        return [s for s in self.SECTIONS if s[2] in self.env]

    @api.model
    def _magento_section_domain(self, model, instance):
        return [("instance_id", "=", instance.id)]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @api.model
    def _magento_ref(self, record):
        model = record._name
        if model in self.INSTANCE_SCOPED_REFS:
            keys = {f: record[f] for f in self.INSTANCE_SCOPED_REFS[model]}
        elif model == "account.payment.method.line":
            keys = {"name": record.name, "payment_type": record.payment_type,
                    "journal": self._magento_ref(record.journal_id) if record.journal_id else False}
        else:
            alternatives = self.REF_KEYS.get(model) or [[record._rec_name or "name"]]
            keys = {}
            for alt in alternatives:
                for fname in alt:
                    if fname in record._fields:
                        value = record[fname]
                        keys[fname] = value.id if isinstance(value, models.BaseModel) else value
        return {"model": model, "keys": keys, "label": record.display_name}

    @api.model
    def _magento_value(self, record, name):
        field = record._fields[name]
        value = record[name]
        if field.type in ("many2one", "reference"):
            return self._magento_ref(value) if value else False
        if field.type == "many2many":
            return [self._magento_ref(rec) for rec in value]
        if field.type == "date":
            return fields.Date.to_string(value) if value else False
        if field.type == "datetime":
            return fields.Datetime.to_string(value) if value else False
        return value

    @api.model
    def _magento_export(self, instance):
        instance = instance.with_context(active_test=False)
        data = {
            "format": SETTINGS_FORMAT,
            "exported_at": fields.Datetime.to_string(fields.Datetime.now()),
            "source": {"instance": instance.name, "base_url": instance.base_url,
                       "company": instance.company_id.name},
            "instance": {name: self._magento_value(instance, name)
                         for name in self._magento_fields("magento.instance")},
            "sections": {},
        }
        for key, _label, model, _natural in self._magento_sections():
            Model = self.env[model].with_context(active_test=False)
            names = self._magento_fields(model)
            records = Model.search(self._magento_section_domain(model, instance), order="id")
            data["sections"][key] = [
                {name: self._magento_value(rec, name) for name in names} for rec in records
            ]
        return self._magento_strip_secrets(data)

    @api.model
    def _magento_strip_secrets(self, value):
        """Last line of defence: drop any key that looks like a credential."""
        if isinstance(value, dict):
            return {k: self._magento_strip_secrets(v) for k, v in value.items()
                    if not (isinstance(k, str) and SECRET_RE.search(k))}
        if isinstance(value, list):
            return [self._magento_strip_secrets(v) for v in value]
        return value

    @api.model
    def _magento_export_download(self, instance):
        data = self._magento_export(instance)
        content = json.dumps(data, indent=2, sort_keys=True, default=str).encode()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", instance.name or "instance").strip("-").lower()
        attachment = self.env["ir.attachment"].create({
            "name": "magento-settings-%s-%s.json" % (slug, fields.Date.context_today(self)),
            "raw": content,
            "mimetype": "application/json",
            "res_model": "magento.instance",
            "res_id": instance.id,
        })
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    @api.model
    def _magento_parse(self, raw):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise UserError(_("This file is not a Magento settings export (invalid JSON: %s).", exc)) from exc
        if not isinstance(data, dict) or data.get("format") != SETTINGS_FORMAT:
            raise UserError(_(
                "Unsupported settings file: expected format %(expected)s, got %(got)s.",
                expected=SETTINGS_FORMAT, got=(data or {}).get("format") if isinstance(data, dict) else "?"))
        return data

    def _magento_model_label(self, model):
        return self.env["ir.model"]._get(model).name or model

    @api.model
    def _magento_resolve(self, ref, target, pending):
        """Return (record | PENDING | None, error message)."""
        if not isinstance(ref, dict) or not ref.get("model"):
            return None, _("invalid reference")
        model, keys = ref["model"], ref.get("keys") or {}
        label = ref.get("label") or ", ".join("%s" % v for v in keys.values())
        if model not in self.env:
            return None, _("%(label)s: %(model)s is not installed", label=label, model=model)
        Model = self.env[model].with_context(active_test=False)
        not_found = _("%(what)s “%(label)s” not found", what=self._magento_model_label(model), label=label)
        if model in self.INSTANCE_SCOPED_REFS:
            domain = [(k, "=", v) for k, v in keys.items() if k in Model._fields]
            if model == "magento.return.reason":
                domain += ["|", ("instance_id", "=", target.id), ("instance_id", "=", False)]
            else:
                domain += [("instance_id", "=", target.id)]
            record = Model.search(domain, limit=1, order="instance_id")
            if record:
                return record, None
            if (model, json.dumps(keys, sort_keys=True, default=str)) in pending:
                return PENDING, None
            return None, not_found
        if model == "account.payment.method.line":
            journal, error = self._magento_resolve(keys.get("journal"), target, pending) \
                if keys.get("journal") else (None, _("no journal"))
            if not journal:
                return None, error
            record = Model.search([("journal_id", "=", journal.id), ("name", "=", keys.get("name")),
                                   ("payment_type", "=", keys.get("payment_type"))], limit=1)
            return (record, None) if record else (None, not_found)
        company_domain = []
        if "company_id" in Model._fields and Model._fields["company_id"].store:
            company_domain = [("company_id", "in", [target.company_id.id, False])]
        alternatives = self.REF_KEYS.get(model) or [list(keys)]
        for alt in alternatives:
            if not alt or not all(keys.get(k) not in (None, False, "") for k in alt):
                continue
            if not all(k in Model._fields for k in alt):
                continue
            records = Model.search([(k, "=", keys[k]) for k in alt] + company_domain, limit=5)
            if records:
                own = records.filtered(lambda r: "company_id" in r._fields
                                       and r.company_id == target.company_id)
                return (own or records)[:1], None
        return None, not_found

    @api.model
    def _magento_vals(self, model, row, target, pending, required_keys=()):
        """Turn an exported row into write values. Returns (vals, unresolved, blocking)."""
        Model = self.env[model]
        allowed = set(self._magento_fields(model))
        vals, unresolved, blocking = {}, [], False
        for name, value in row.items():
            if name not in allowed:
                continue
            field = Model._fields[name]
            if field.type in ("many2one", "reference"):
                if not value:
                    vals[name] = False
                    continue
                record, error = self._magento_resolve(value, target, pending)
                if record is None:
                    unresolved.append("%s: %s" % (field.string, error))
                    if field.required or name in required_keys:
                        blocking = True
                    continue
                if record is PENDING:
                    vals[name] = PENDING
                elif field.type == "reference":
                    vals[name] = "%s,%s" % (record._name, record.id)
                else:
                    vals[name] = record.id
            elif field.type == "many2many":
                ids, waiting = [], False
                for ref in value or []:
                    record, error = self._magento_resolve(ref, target, pending)
                    if record is None:
                        unresolved.append("%s: %s" % (field.string, error))
                    elif record is PENDING:
                        waiting = True
                    else:
                        ids.append(record.id)
                if field.required and not ids and not waiting:
                    blocking = True
                vals[name] = PENDING if waiting else ids
            else:
                vals[name] = value
        return vals, unresolved, blocking

    @api.model
    def _magento_diff(self, record, vals):
        """Field names whose value in ``vals`` differs from ``record``."""
        changed = []
        for name, value in vals.items():
            field = record._fields[name]
            current = record[name]
            if value is PENDING:
                changed.append(name)
            elif field.type == "many2one":
                if (current.id or False) != (value or False):
                    changed.append(name)
            elif field.type == "many2many":
                if set(current.ids) != set(value or []):
                    changed.append(name)
            elif field.type == "reference":
                cur = "%s,%s" % (current._name, current.id) if current else False
                if cur != (value or False):
                    changed.append(name)
            elif field.type == "float":
                if tools.float_compare(current or 0.0, value or 0.0, precision_digits=6):
                    changed.append(name)
            elif field.type in ("date", "datetime"):
                cur = field.to_string(current) if current else False
                if cur != (value or False):
                    changed.append(name)
            elif (current or False) != (value or False):
                changed.append(name)
        return changed

    @api.model
    def _magento_row_label(self, model, row):
        for name in ("name", "magento_method", "magento_code", "code", "source_code"):
            value = row.get(name)
            if value and not isinstance(value, dict):
                return str(value)
        parts = []
        for value in row.values():
            if isinstance(value, dict) and value.get("label"):
                parts.append(value["label"])
        return " / ".join(parts[:3]) or model

    @api.model
    def _magento_plan_section(self, section, rows, target, pending):
        key, label, model, natural = section
        Model = self.env[model].with_context(active_test=False)
        plans = []
        for row in rows or []:
            vals, unresolved, blocking = self._magento_vals(model, row, target, pending, natural)
            plan = {
                "section": key, "section_label": label, "model": model,
                "name": self._magento_row_label(model, row),
                "vals": vals, "unresolved": unresolved, "record": Model.browse(), "changes": [],
            }
            if blocking:
                plan["action"] = "error"
                plans.append(plan)
                continue
            key_values = {k: vals.get(k, False) for k in natural if k in Model._fields}
            if any(v is PENDING for v in key_values.values()):
                existing = Model.browse()
            else:
                domain = [("instance_id", "=", target.id)]
                for k, v in key_values.items():
                    field = Model._fields[k]
                    if field.type == "float":
                        domain += [(k, ">=", (v or 0.0) - 1e-6), (k, "<=", (v or 0.0) + 1e-6)]
                    elif field.type == "many2many":
                        domain += [(k, "in", v or [])]
                    else:
                        domain.append((k, "=", v if v not in (None, "") else False))
                existing = Model.search(domain, limit=1)
            if existing:
                plan["record"] = existing
                plan["changes"] = self._magento_diff(existing, vals)
                plan["action"] = "update" if plan["changes"] else "unchanged"
            else:
                plan["action"] = "create"
            plans.append(plan)
        return plans

    @api.model
    def _magento_plan_instance(self, data, target, pending):
        vals, unresolved, _blocking = self._magento_vals(
            "magento.instance", data.get("instance") or {}, target, pending)
        changes = self._magento_diff(target, vals)
        return {
            "section": "instance", "section_label": _("Instance settings"), "model": "magento.instance",
            "name": target.display_name, "vals": vals, "unresolved": unresolved, "record": target,
            "changes": changes, "action": "update" if changes else "unchanged",
        }

    @api.model
    def _magento_pending_keys(self, data, target):
        """Instance-scoped records the import will create: (model, key json) pairs."""
        pending = set()
        sections = data.get("sections") or {}
        for key, _label, model, _natural in self._magento_sections():
            scoped = self.INSTANCE_SCOPED_REFS.get(model)
            if not scoped:
                continue
            for row in sections.get(key) or []:
                keys = {f: row.get(f) for f in scoped}
                pending.add((model, json.dumps(keys, sort_keys=True, default=str)))
        return pending

    @api.model
    def _magento_preview(self, data, target):
        pending = self._magento_pending_keys(data, target)
        plans = [self._magento_plan_instance(data, target, pending)]
        sections = data.get("sections") or {}
        for section in self._magento_sections():
            plans += self._magento_plan_section(section, sections.get(section[0]), target, pending)
        missing = sorted(set(sections) - {s[0] for s in self._magento_sections()})
        return plans, missing

    @api.model
    def _magento_apply(self, data, target):
        """Apply the import section by section; re-planned per section so references to
        records created by an earlier section resolve. Idempotent: a second run is a no-op."""
        results = []
        plan = self._magento_plan_instance(data, target, set())
        vals = {k: v for k, v in plan["vals"].items() if k in plan["changes"] and v is not PENDING}
        if vals:
            try:
                with self.env.cr.savepoint():
                    self._magento_write(target, self._magento_commands(target, vals))
            except Exception as exc:  # noqa: BLE001 - reported on the line, sections still go on
                plan["action"] = "error"
                plan["unresolved"] = plan["unresolved"] + [str(exc)]
        results.append(plan)
        sections = data.get("sections") or {}
        for section in self._magento_sections():
            Model = self.env[section[2]].with_context(active_test=False)
            for plan in self._magento_plan_section(section, sections.get(section[0]), target, set()):
                vals = {k: v for k, v in plan["vals"].items() if v is not PENDING}
                try:
                    with self.env.cr.savepoint():
                        if plan["action"] == "create":
                            vals["instance_id"] = target.id
                            plan["record"] = Model.create(self._magento_commands(Model, vals))
                        elif plan["action"] == "update":
                            vals = {k: v for k, v in vals.items() if k in plan["changes"]}
                            self._magento_write(plan["record"], self._magento_commands(Model, vals))
                except Exception as exc:  # noqa: BLE001 - reported on the line, others go on
                    plan["action"] = "error"
                    plan["unresolved"] = plan["unresolved"] + [str(exc)]
                results.append(plan)
        return results

    @api.model
    def _magento_commands(self, Model, vals):
        out = {}
        for name, value in vals.items():
            if Model._fields[name].type == "many2many":
                out[name] = [Command.set(value or [])]
            else:
                out[name] = value
        return out

    @api.model
    def _magento_write(self, record, vals):
        record.with_context(skip_magento_sync=True, magento_skip_push=True).write(vals)


class MagentoSettingsTransfer(models.TransientModel):
    _name = "magento.settings.transfer"
    _description = "Magento Settings Export/Import"

    instance_id = fields.Many2one(
        "magento.instance", string="Instance", required=True,
        help="Export: the instance whose settings are exported. Import: the instance that "
             "receives the settings.")
    import_file = fields.Binary(string="Settings File")
    import_filename = fields.Char()
    state = fields.Selection([("start", "Start"), ("preview", "Preview"), ("done", "Done")],
                             default="start", required=True)
    source_label = fields.Char(string="Exported From", readonly=True)
    line_ids = fields.One2many("magento.settings.transfer.line", "wizard_id", string="Changes")
    create_count = fields.Integer(readonly=True)
    update_count = fields.Integer(readonly=True)
    unchanged_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    unresolved_count = fields.Integer(readonly=True)
    note = fields.Text(readonly=True)

    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_("Only Magento administrators can export or import settings."))

    def action_export(self):
        self.ensure_one()
        self._check_manager()
        return self.env["magento.settings.io"]._magento_export_download(self.instance_id)

    def _data(self):
        if not self.import_file:
            raise UserError(_("Choose a settings file exported from another instance."))
        return self.env["magento.settings.io"]._magento_parse(base64.b64decode(self.import_file))

    def _fill(self, plans, state, missing=()):
        Line = self.env["magento.settings.transfer.line"]
        self.line_ids.unlink()
        Line.create([{
            "wizard_id": self.id,
            "sequence": i,
            "section": plan["section_label"],
            "name": plan["name"],
            "action": plan["action"],
            "changes": ", ".join(
                self.env[plan["model"]]._fields[f].string for f in plan["changes"]
            ) if plan["changes"] else False,
            "unresolved": "\n".join(plan["unresolved"]) or False,
        } for i, plan in enumerate(plans)])
        actions = [p["action"] for p in plans]
        note = False
        if missing:
            note = _("Sections not installed here and ignored: %s", ", ".join(missing))
        self.write({
            "state": state,
            "create_count": actions.count("create"),
            "update_count": actions.count("update"),
            "unchanged_count": actions.count("unchanged"),
            "error_count": actions.count("error"),
            "unresolved_count": sum(1 for p in plans if p["unresolved"]),
            "note": note,
        })

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "views": [[False, "form"]],
            "target": "new",
            "name": _("Settings Export/Import"),
        }

    def action_preview(self):
        self.ensure_one()
        self._check_manager()
        data = self._data()
        source = data.get("source") or {}
        self.source_label = "%s (%s)" % (source.get("instance") or "?", source.get("base_url") or "")
        plans, missing = self.env["magento.settings.io"]._magento_preview(data, self.instance_id)
        self._fill(plans, "preview", missing)
        return self._reopen()

    def action_apply(self):
        self.ensure_one()
        self._check_manager()
        data = self._data()
        plans = self.env["magento.settings.io"]._magento_apply(data, self.instance_id)
        self._fill(plans, "done")
        return self._reopen()

    def action_back(self):
        self.ensure_one()
        self.line_ids.unlink()
        self.state = "start"
        return self._reopen()


class MagentoSettingsTransferLine(models.TransientModel):
    _name = "magento.settings.transfer.line"
    _description = "Magento Settings Import Line"
    _order = "sequence, id"

    wizard_id = fields.Many2one("magento.settings.transfer", required=True, ondelete="cascade")
    sequence = fields.Integer()
    section = fields.Char()
    name = fields.Char(string="Record")
    action = fields.Selection([
        ("create", "Create"), ("update", "Update"), ("unchanged", "Unchanged"), ("error", "Cannot import"),
    ])
    changes = fields.Char(string="Fields Changed")
    unresolved = fields.Text(string="Unresolved References")
