"""Aggregation endpoint for the Magento Console (OWL client action).

Every figure is computed with the caller's own access rights (no sudo) and is
returned with a drill-down descriptor, so the console can open the records
behind it with the filters that are on screen.
"""
from datetime import date, datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

PERIODS = (7, 30, 90)
DEFAULT_PERIOD = 30
TOP_LIMIT = 6
ATTENTION_LIMIT = 10
LIST_FORM = [[False, "list"], [False, "form"]]
CLOSED_ORDER_STATES = ("cancel",)
DRAFT_ORDER_STATES = ("draft", "sent")

ISSUE_MODEL = "magento.sync.issue"
JOB_MODEL = "magento.job"
ERROR_CENTER_ACTION = "sdlc_magento_connector.action_magento_sync_issue"
JOB_QUEUE_ACTION = "sdlc_magento_connector.action_magento_job"
ACTIVE_JOB_STATES = ("pending", "started")
# An auto-synced instance whose orders marker has not moved for this long is flagged.
STALE_MARKER_HOURS = 24


class MagentoDashboard(models.AbstractModel):
    _name = "magento.dashboard"
    _description = "Magento Console"

    # (xml id, label, hint, icon, group). Missing or unreadable targets are
    # dropped by get_nav_links, so this list may name actions other modules
    # add or remove.
    NAV_LINKS = [
        ("sdlc_magento_connector.action_magento_order", "Orders",
         "Every imported Magento order", "fa-shopping-cart", "Operations"),
        ("sdlc_magento_connector.action_magento_invoices", "Invoices",
         "Invoices linked to Magento", "fa-file-text-o", "Operations"),
        ("sdlc_magento_connector.action_magento_shipments", "Shipments",
         "Deliveries pushed to Magento", "fa-truck", "Operations"),
        ("sdlc_magento_connector.action_magento_credit_memos", "Credit memos",
         "Refunds in both directions", "fa-undo", "Operations"),
        ("sdlc_magento_connector.action_magento_product_map", "Products",
         "Magento product bindings", "fa-cube", "Catalog"),
        ("sdlc_magento_connector.action_magento_category", "Categories",
         "Magento category tree", "fa-sitemap", "Catalog"),
        ("sdlc_magento_connector.action_magento_customer", "Customers",
         "Magento customer accounts", "fa-users", "Catalog"),
        ("sdlc_magento_connector.action_magento_inventory", "Inventory",
         "Stock per Magento product", "fa-cubes", "Catalog"),
        ("sdlc_magento_connector.action_magento_instance", "Instances",
         "Connections and sync settings", "fa-plug", "Configuration & logs"),
        (ERROR_CENTER_ACTION, "Error Center",
         "Records that failed to sync: retry, resolve or ignore", "fa-exclamation-triangle",
         "Configuration & logs"),
        (JOB_QUEUE_ACTION, "Job Queue",
         "Background syncs, pushes and retries", "fa-tasks", "Configuration & logs"),
        ("sdlc_magento_connector.action_magento_reconciliation", "Reconciliation",
         "Compare Odoo and Magento records", "fa-balance-scale", "Configuration & logs"),
        ("sdlc_magento_connector.action_magento_bulk_operation", "Bulk Operations",
         "Mass pushes and resyncs", "fa-bolt", "Configuration & logs"),
        ("sdlc_magento_connector.action_magento_field_mapping_product", "Field mapping",
         "Odoo to Magento field rules", "fa-random", "Configuration & logs"),
        ("sdlc_magento_connector.action_magento_sync_report", "Sync reports",
         "Every sync run and its errors", "fa-history", "Configuration & logs"),
        ("sdlc_magento_connector.action_magento_mapping_report", "Mapping report",
         "Mapping coverage check", "fa-check-square-o", "Configuration & logs"),
    ]

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    @api.model
    def get_nav_links(self):
        """Group the console's navigation targets, keeping only those the user can open."""
        groups, order = {}, []
        for xml_id, label, hint, icon, group in self.NAV_LINKS:
            if not self._user_can_open(xml_id):
                continue
            if group not in groups:
                groups[group] = []
                order.append(group)
            groups[group].append({
                "xml_id": xml_id,
                "label": label,
                "hint": hint,
                "icon": icon,
            })
        return [{"group": name, "links": groups[name]} for name in order]

    def _user_can_open(self, xml_id):
        """True when the action exists, its groups admit the user and its model is readable.

        Uses ``_for_xml_id``, the same safe-field read the web client's
        ``/web/action/load`` performs, because regular users cannot read
        ``ir.actions.*`` rows directly.
        """
        if not self.env.ref(xml_id, raise_if_not_found=False):
            return False
        try:
            action = self.env["ir.actions.actions"]._for_xml_id(xml_id)
        except (AccessError, ValueError):
            return False
        group_ids = set(action.get("group_ids") or [])
        if group_ids and not group_ids & set(self.env.user.all_group_ids.ids):
            return False
        model_name = action.get("res_model") or (
            action.get("model_name") if action.get("type") == "ir.actions.server" else None
        )
        if action.get("type") not in ("ir.actions.act_window", "ir.actions.server"):
            return True
        return bool(model_name) and self._readable(model_name)

    # ------------------------------------------------------------------
    # Dashboard payload
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self, instance_ids=None, days=DEFAULT_PERIOD):
        """Return the whole console payload as one JSON-serialisable dict.

        :param instance_ids: instances to scope to; empty means every
            instance of the companies currently open.
        :param days: period length; one of PERIODS.
        """
        try:
            days = int(days or DEFAULT_PERIOD)
        except (TypeError, ValueError):
            days = DEFAULT_PERIOD
        if days not in PERIODS:
            days = DEFAULT_PERIOD

        company = self.env.company
        currency = company.currency_id
        companies = self.env.companies
        Instance = self.env["magento.instance"]

        all_instances = Instance.search([("company_id", "in", companies.ids)], order="name, id")
        wanted = {int(i) for i in (instance_ids or []) if i}
        selected = all_instances.filtered(lambda inst: inst.id in wanted)
        scope = selected or all_instances

        date_from, date_from_local, today = self._period_bounds(days)
        payload = {
            "period_days": days,
            "periods": list(PERIODS),
            "date_from": fields.Datetime.to_string(date_from),
            "currency": {"id": currency.id, "name": currency.name},
            "company_count": len(companies),
            "instances": [self._instance_info(inst) for inst in all_instances],
            "selected_ids": selected.ids,
            "scope_ids": scope.ids,
            "instances_elsewhere": 0,
            "kpis": [],
            "attention": [],
            "trend": [],
            "by_status": [],
            "by_category": [],
            "by_country": [],
            "totals": {"orders": 0, "revenue": 0.0},
            "orders_hidden": False,
            "by_entity": [],
            "issues_hidden": not self._readable(ISSUE_MODEL),
        }
        if not all_instances:
            payload["instances_elsewhere"] = Instance.with_context(
                allowed_company_ids=self.env.user.company_ids.ids
            ).search_count([("company_id", "not in", companies.ids)])
            return payload

        # Drill-downs into the Error Center / Job Queue open those actions when
        # the user may; otherwise a plain list of the same records.
        actions = {
            xml_id: xml_id if self._user_can_open(xml_id) else False
            for xml_id in (ERROR_CENTER_ACTION, JOB_QUEUE_ACTION)
        }
        issue_scope = [
            ("instance_id", "in", scope.ids),
            ("company_id", "in", companies.ids),
            ("state", "=", "open"),
        ]
        issues = self._issue_stats(all_instances, scope, companies, payload["issues_hidden"])
        if not payload["issues_hidden"]:
            ec = actions[ERROR_CENTER_ACTION]
            for info in payload["instances"]:
                info["open_issues"] = issues["per_instance"].get(info["id"], 0)
                info["issues_drill"] = self._drill(
                    ISSUE_MODEL, _("Open issues: %s", info["name"]),
                    [("instance_id", "=", info["id"]), ("company_id", "in", companies.ids),
                     ("state", "=", "open")],
                    action=ec,
                )
            payload["by_entity"] = self._by_entity(issues, issue_scope, ec)
        if selected:
            job_scope = [("instance_id", "in", scope.ids)]
        else:
            # Unscoped: jobs not bound to an instance (company-wide work) count too.
            job_scope = ["|", ("instance_id", "in", scope.ids), ("instance_id", "=", False)]
        jobs = self._job_stats(job_scope, payload["date_from"])

        order_scope = [
            ("magento_instance_id", "in", scope.ids),
            ("magento_order_id", "!=", False),
            ("company_id", "in", companies.ids),
        ]
        period = [("date_order", ">=", payload["date_from"])]
        period_orders = order_scope + period
        live_period_orders = period_orders + [("state", "not in", CLOSED_ORDER_STATES)]

        totals = payload["totals"]
        payload["orders_hidden"] = not self._readable("sale.order")
        if not payload["orders_hidden"]:
            rate = self._rate_getter(currency, company)
            payload["trend"], totals = self._trend(period_orders, rate, date_from_local, today)
            payload["totals"] = totals
            payload["by_status"] = self._by_status(period_orders)
            payload["by_country"] = self._by_country(live_period_orders, rate)
            if self._readable("sale.order.line"):
                payload["by_category"] = self._by_category(live_period_orders, rate)

        counts = self._counts(order_scope, scope)
        ops = {
            "issues": issues, "issue_scope": issue_scope,
            "jobs": jobs, "job_scope": job_scope,
            "date_from": payload["date_from"], "days": days, "actions": actions,
        }
        kpis = self._kpis(totals, counts, order_scope, period_orders,
                          live_period_orders, scope, ops)
        attention = self._attention(counts, order_scope, scope, ops)
        # Drop every figure whose records the user could not open anyway.
        payload["kpis"] = [k for k in kpis if self._readable(k["drill"]["res_model"])]
        payload["attention"] = [a for a in attention if self._readable(a["drill"]["res_model"])]
        return payload

    def _readable(self, model_name):
        return model_name in self.env and self.env[model_name].has_access("read")

    # ------------------------------------------------------------------
    # Helpers: period, currency, instances
    # ------------------------------------------------------------------
    def _user_tz(self):
        tz_name = self.env.context.get("tz") or self.env.user.tz or "UTC"
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    def _period_bounds(self, days):
        """Return (UTC start datetime, local start date, local today) for the period."""
        tz = self._user_tz()
        today = fields.Date.context_today(self)
        start_local = today - timedelta(days=days - 1)
        start_utc = tz.localize(datetime.combine(start_local, time.min)).astimezone(pytz.utc)
        return start_utc.replace(tzinfo=None), start_local, today

    def _rate_getter(self, to_currency, company):
        """Return f(currency, day) -> factor converting one unit into `to_currency`."""
        cache = {}

        def factor(from_currency, day):
            if not from_currency or from_currency == to_currency:
                return 1.0
            day = day or fields.Date.context_today(self)
            key = (from_currency.id, day)
            if key not in cache:
                cache[key] = from_currency._convert(1.0, to_currency, company, day, round=False)
            return cache[key]

        return factor

    @staticmethod
    def _as_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return False

    def _instance_info(self, inst):
        state_labels = dict(inst._fields["state"]._description_selection(self.env))
        has_logo = "logo" in inst._fields and bool(inst.logo)
        return {
            "id": inst.id,
            "name": inst.name,
            "state": inst.state,
            "state_label": state_labels.get(inst.state, inst.state),
            "last_sync_at": fields.Datetime.to_string(inst.last_sync_at) if inst.last_sync_at else False,
            "message": inst.last_connection_message or "",
            "company": inst.company_id.name,
            "has_logo": has_logo,
            # Filled in when the user can read the Error Center.
            "open_issues": False,
            "issues_drill": False,
        }

    # ------------------------------------------------------------------
    # Helpers: aggregates
    # ------------------------------------------------------------------
    def _trend(self, period_orders, rate, date_from_local, today):
        SaleOrder = self.env["sale.order"]
        rows = SaleOrder._read_group(
            period_orders,
            ["date_order:day", "currency_id", "state"],
            ["amount_total:sum", "__count"],
        )
        by_day = {}
        orders = 0
        revenue = 0.0
        for day, cur, state, amount, count in rows:
            day = self._as_date(day)
            bucket = by_day.setdefault(day, {"orders": 0, "revenue": 0.0})
            bucket["orders"] += count
            orders += count
            if state not in CLOSED_ORDER_STATES:
                converted = (amount or 0.0) * rate(cur, day)
                bucket["revenue"] += converted
                revenue += converted
        trend = []
        day = date_from_local
        while day <= today:
            bucket = by_day.get(day, {"orders": 0, "revenue": 0.0})
            trend.append({
                "date": fields.Date.to_string(day),
                "orders": bucket["orders"],
                "revenue": round(bucket["revenue"], 2),
            })
            day += timedelta(days=1)
        return trend, {"orders": orders, "revenue": round(revenue, 2)}

    def _by_status(self, period_orders):
        rows = self.env["sale.order"]._read_group(
            period_orders, ["magento_status"], ["__count"], order="__count desc"
        )
        result = []
        for status, count in rows[:TOP_LIMIT + 2]:
            label = (status or _("Unknown")).replace("_", " ")
            result.append({
                "key": status or "",
                "label": label[:1].upper() + label[1:],
                "count": count,
                "drill": self._drill(
                    "sale.order", _("Magento orders: %s", label),
                    period_orders + [("magento_status", "=", status or False)],
                ),
            })
        return result

    def _by_category(self, live_period_orders, rate):
        rows = self.env["sale.order.line"]._read_group(
            [
                ("order_id", "any", live_period_orders),
                ("magento_product_id.magento_category_ids", "!=", False),
                ("display_type", "=", False),
            ],
            ["magento_product_id.magento_category_ids", "currency_id", "order_id.date_order:day"],
            ["price_subtotal:sum", "product_uom_qty:sum"],
        )
        totals = {}
        for category, cur, day, amount, qty in rows:
            if not category:
                continue
            entry = totals.setdefault(category, {"revenue": 0.0, "units": 0.0})
            entry["revenue"] += (amount or 0.0) * rate(cur, self._as_date(day))
            entry["units"] += qty or 0.0
        ranked = sorted(totals.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:TOP_LIMIT]
        return [
            {
                "id": category.id,
                "name": category.display_name,
                "revenue": round(entry["revenue"], 2),
                "units": entry["units"],
                "drill": self._drill(
                    "sale.order", _("Orders in %s", category.display_name),
                    live_period_orders + [
                        ("order_line.magento_product_id.magento_category_ids", "in", category.ids)
                    ],
                ),
            }
            for category, entry in ranked
        ]

    def _by_country(self, live_period_orders, rate):
        rows = self.env["sale.order"]._read_group(
            live_period_orders,
            ["partner_shipping_id.country_id", "currency_id", "date_order:day"],
            ["amount_total:sum", "__count"],
        )
        totals = {}
        for country, cur, day, amount, count in rows:
            entry = totals.setdefault(country, {"revenue": 0.0, "orders": 0})
            entry["revenue"] += (amount or 0.0) * rate(cur, self._as_date(day))
            entry["orders"] += count
        ranked = sorted(totals.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:TOP_LIMIT]
        result = []
        for country, entry in ranked:
            name = country.name if country else _("No country")
            result.append({
                "id": country.id or 0,
                "name": name,
                "code": (country.code or "") if country else "",
                "revenue": round(entry["revenue"], 2),
                "orders": entry["orders"],
                "drill": self._drill(
                    "sale.order", _("Orders shipped to %s", name),
                    live_period_orders + [
                        ("partner_shipping_id.country_id", "=", country.id or False)
                    ],
                ),
            })
        return result

    def _counts(self, order_scope, scope):
        """Counts behind the warn/crit figures; zero for models the user cannot read."""
        queries = {
            "drafts": ("sale.order", order_scope + [("state", "in", DRAFT_ORDER_STATES)]),
            "conflicts": ("sale.order", order_scope + [("magento_sync_conflict", "=", True)]),
            "products": ("magento.product.map", [("instance_id", "in", scope.ids)]),
        }
        return {
            key: self.env[model].search_count(domain) if self._readable(model) else 0
            for key, (model, domain) in queries.items()
        }

    # ------------------------------------------------------------------
    # Helpers: error center, job queue, sync markers
    # ------------------------------------------------------------------
    def _issue_stats(self, all_instances, scope, companies, hidden):
        """Open issues of the instances on screen, from one grouped query.

        ``per_instance`` covers every instance of the open companies (the
        masthead lists them); the other figures only the instances in scope.
        """
        stats = {
            "open": 0, "permanent": 0, "transient": 0,
            "by_entity": {}, "auth": {}, "per_instance": {},
        }
        if hidden:
            return stats
        rows = self.env[ISSUE_MODEL]._read_group(
            [
                ("instance_id", "in", all_instances.ids),
                ("company_id", "in", companies.ids),
                ("state", "=", "open"),
            ],
            ["instance_id", "entity", "severity", "is_auth_error"],
            ["__count"],
        )
        in_scope = set(scope.ids)
        for instance, entity, severity, is_auth, count in rows:
            stats["per_instance"][instance.id] = stats["per_instance"].get(instance.id, 0) + count
            if instance.id not in in_scope:
                continue
            severity = severity if severity in ("permanent", "transient") else "permanent"
            stats["open"] += count
            stats[severity] += count
            bucket = stats["by_entity"].setdefault(entity or "other", {"permanent": 0, "transient": 0})
            bucket[severity] += count
            if is_auth:
                stats["auth"][instance] = stats["auth"].get(instance, 0) + count
        return stats

    def _entity_labels(self):
        field = self.env[ISSUE_MODEL]._fields["entity"]
        return dict(field._description_selection(self.env))

    def _by_entity(self, issues, issue_scope, action):
        labels = self._entity_labels()
        ranked = sorted(
            issues["by_entity"].items(),
            key=lambda kv: (kv[1]["permanent"] + kv[1]["transient"], kv[1]["permanent"]),
            reverse=True,
        )
        result = []
        for entity, bucket in ranked:
            label = labels.get(entity, entity)
            result.append({
                "key": entity,
                "label": label,
                "count": bucket["permanent"] + bucket["transient"],
                "permanent": bucket["permanent"],
                "transient": bucket["transient"],
                "tone": "crit" if bucket["permanent"] else "warn",
                "drill": self._drill(
                    ISSUE_MODEL, _("Open issues: %s", label),
                    issue_scope + [("entity", "=", entity)], action=action,
                ),
            })
        return result

    def _job_stats(self, job_scope, date_from):
        """Queued or running jobs, and jobs that failed in the period."""
        if not self._readable(JOB_MODEL):
            return {"active": 0, "failed": 0}
        rows = self.env[JOB_MODEL]._read_group(
            job_scope + [
                "|", ("state", "in", ACTIVE_JOB_STATES),
                "&", ("state", "=", "failed"), ("date_done", ">=", date_from),
            ],
            ["state"],
            ["__count"],
        )
        counts = dict(rows)
        return {
            "active": sum(counts.get(state, 0) for state in ACTIVE_JOB_STATES),
            "failed": counts.get("failed", 0),
        }

    def _stale_markers(self, scope):
        """Auto-synced instances whose orders marker is older than STALE_MARKER_HOURS."""
        now = fields.Datetime.now()
        limit = now - timedelta(hours=STALE_MARKER_HOURS)
        stale = []
        for inst in scope:
            marker = inst.orders_sync_watermark
            if inst.auto_sync_enabled and inst.auto_sync_orders and marker and marker < limit:
                stale.append((inst, now - marker))
        return stale

    def _age_text(self, age):
        hours = int(age.total_seconds() // 3600)
        if hours >= 48:
            return _("%s days", hours // 24)
        return _("%s hours", hours)

    # ------------------------------------------------------------------
    # KPI rail and Needs action
    # ------------------------------------------------------------------
    def _kpis(self, totals, counts, order_scope, period_orders, live_period_orders, scope, ops):
        """The rail: seven tiles, kept to one row on desktop.

        Phase 1 replaced "Failed syncs" (sync reports) by "Open issues" (Error
        Center) and added "Queue". The "Customers" tile was dropped to keep the
        row: it is the lowest-value figure and the Customers nav link opens the
        same list.
        """
        days = ops["days"]
        issues, jobs = ops["issues"], ops["jobs"]
        if issues["permanent"]:
            issues_tone = "crit"
        elif issues["open"]:
            issues_tone = "warn"
        else:
            issues_tone = ""
        return [
            {
                "key": "orders", "label": _("Orders"), "note": "%sd" % days,
                "value": totals["orders"], "kind": "int", "tone": "",
                "hint": _("Magento orders dated in the period"),
                "drill": self._drill("sale.order", _("Magento orders"), period_orders),
            },
            {
                "key": "revenue", "label": _("Revenue"), "note": "%sd" % days,
                "value": totals["revenue"], "kind": "money", "tone": "",
                "hint": _("Order totals in the period, converted to company currency; cancelled orders excluded"),
                "drill": self._drill("sale.order", _("Magento revenue"), live_period_orders),
            },
            {
                "key": "drafts", "label": _("Unconfirmed"), "note": "",
                "value": counts["drafts"], "kind": "int",
                "tone": "warn" if counts["drafts"] else "",
                "hint": _("Magento orders still in quotation state"),
                "drill": self._drill(
                    "sale.order", _("Unconfirmed Magento orders"),
                    order_scope + [("state", "in", DRAFT_ORDER_STATES)],
                ),
            },
            {
                "key": "conflicts", "label": _("Conflicts"), "note": "",
                "value": counts["conflicts"], "kind": "int",
                "tone": "crit" if counts["conflicts"] else "",
                "hint": _("Orders Magento changed after they were confirmed in Odoo"),
                "drill": self._drill(
                    "sale.order", _("Magento order conflicts"),
                    order_scope + [("magento_sync_conflict", "=", True)],
                ),
            },
            {
                "key": "open_issues", "label": _("Open issues"), "note": "",
                "value": issues["open"], "kind": "int", "tone": issues_tone,
                "hint": _("Records that failed to sync and are still open in the Error Center; "
                          "red when any failure is permanent, amber when all are transient"),
                "drill": self._drill(
                    ISSUE_MODEL, _("Open sync issues"), ops["issue_scope"],
                    action=ops["actions"][ERROR_CENTER_ACTION],
                ),
            },
            {
                "key": "queue", "label": _("Queue"), "note": "",
                "value": jobs["active"], "kind": "int",
                "tone": "warn" if jobs["failed"] else "",
                "hint": _("Pending and running background jobs; amber when a job failed in the last %s days", days),
                "drill": self._drill(
                    JOB_MODEL, _("Queued and running jobs"),
                    ops["job_scope"] + [("state", "in", ACTIVE_JOB_STATES)],
                    action=ops["actions"][JOB_QUEUE_ACTION],
                ),
            },
            {
                "key": "products", "label": _("Products"), "note": "",
                "value": counts["products"], "kind": "int", "tone": "",
                "hint": _("Products bound to Magento"),
                "drill": self._drill(
                    "magento.product.map", _("Magento products"),
                    [("instance_id", "in", scope.ids)],
                ),
            },
        ]

    def _attention(self, counts, order_scope, scope, ops):
        """Needs-action items, critical first."""
        issues, jobs = ops["issues"], ops["jobs"]
        ec = ops["actions"][ERROR_CENTER_ACTION]
        items = []
        # Critical: authentication, connection, permanent failures, conflicts.
        for inst, count in sorted(issues["auth"].items(), key=lambda kv: kv[0].name or ""):
            items.append({
                "key": "auth_failing_%s" % inst.id,
                "tone": "crit",
                "pill": _("Auth"),
                "label": _("Authentication failing on %s", inst.name),
                "detail": _("Magento rejects the credentials. Update the access token on the instance."),
                "count": count,
                "drill": self._drill(
                    ISSUE_MODEL, _("Authentication errors: %s", inst.name),
                    ops["issue_scope"] + [("instance_id", "=", inst.id), ("is_auth_error", "=", True)],
                    action=ec,
                ),
            })
        auth_failing = {inst.id for inst in issues["auth"]}
        for inst in scope.filtered(lambda i: i.state == "error" and i.id not in auth_failing):
            items.append({
                "key": "instance_error_%s" % inst.id,
                "tone": "crit",
                "pill": _("Error"),
                "label": _("%s cannot connect", inst.name),
                "detail": inst.last_connection_message or _("Test the connection from the instance form."),
                "count": False,
                "drill": self._drill(
                    "magento.instance", inst.name, [("id", "=", inst.id)],
                    views=[[False, "form"]], res_id=inst.id,
                ),
            })
        if issues["permanent"]:
            labels = self._entity_labels()
            by_entity = sorted(
                ((entity, b["permanent"]) for entity, b in issues["by_entity"].items() if b["permanent"]),
                key=lambda kv: kv[1], reverse=True,
            )
            items.append({
                "key": "permanent_issues", "tone": "crit", "pill": _("Failed"),
                "label": _("Permanent sync errors"),
                "detail": " · ".join("%s %s" % (labels.get(e, e), n) for e, n in by_entity),
                "count": issues["permanent"],
                "drill": self._drill(
                    ISSUE_MODEL, _("Permanent sync errors"),
                    ops["issue_scope"] + [("severity", "=", "permanent")], action=ec,
                ),
            })
        if counts["conflicts"]:
            items.append({
                "key": "conflicts", "tone": "crit", "pill": _("Conflict"),
                "label": _("Orders changed in Magento after confirmation"),
                "detail": _("Review each order; its lines were not overwritten."),
                "count": counts["conflicts"],
                "drill": self._drill(
                    "sale.order", _("Magento order conflicts"),
                    order_scope + [("magento_sync_conflict", "=", True)],
                ),
            })
        # Warnings: failed jobs, stale markers, drafts, unfinished setup.
        if jobs["failed"]:
            items.append({
                "key": "failed_jobs", "tone": "warn", "pill": _("Jobs"),
                "label": _("Jobs failed"),
                "detail": _("In the last %s days. Requeue them from the Job Queue.", ops["days"]),
                "count": jobs["failed"],
                "drill": self._drill(
                    JOB_MODEL, _("Failed jobs"),
                    ops["job_scope"] + [("state", "=", "failed"), ("date_done", ">=", ops["date_from"])],
                    action=ops["actions"][JOB_QUEUE_ACTION],
                ),
            })
        for inst, age in self._stale_markers(scope):
            items.append({
                "key": "stale_marker_%s" % inst.id,
                "tone": "warn",
                "pill": _("Stale"),
                "label": _("Sync marker stale on %s", inst.name),
                "detail": _("Orders marker last moved %s ago although auto-sync is on.", self._age_text(age)),
                "count": False,
                "drill": self._drill(
                    "magento.instance", inst.name, [("id", "=", inst.id)],
                    views=[[False, "form"]], res_id=inst.id,
                ),
            })
        if counts["drafts"]:
            items.append({
                "key": "drafts", "tone": "warn", "pill": _("Draft"),
                "label": _("Magento orders not yet confirmed"),
                "detail": _("Confirm them to reserve stock and invoice."),
                "count": counts["drafts"],
                "drill": self._drill(
                    "sale.order", _("Unconfirmed Magento orders"),
                    order_scope + [("state", "in", DRAFT_ORDER_STATES)],
                ),
            })
        for inst in scope.filtered(lambda i: i.state == "draft"):
            items.append({
                "key": "instance_draft_%s" % inst.id,
                "tone": "warn",
                "pill": _("Setup"),
                "label": _("%s is not connected yet", inst.name),
                "detail": _("Test the connection to start syncing."),
                "count": False,
                "drill": self._drill(
                    "magento.instance", inst.name, [("id", "=", inst.id)],
                    views=[[False, "form"]], res_id=inst.id,
                ),
            })
        return items[:ATTENTION_LIMIT]

    def _drill(self, model, name, domain, views=None, res_id=False, action=False):
        """Describe the records behind a figure; the console turns it into an act_window.

        ``action`` (an xml id the user may open) makes the console open that
        action, with its search view and help, restricted to ``domain``.
        """
        drill = {
            "res_model": model,
            "name": name,
            "domain": domain,
            "views": views or LIST_FORM,
        }
        if res_id:
            drill["res_id"] = res_id
        if action:
            drill["xml_id"] = action
        return drill
