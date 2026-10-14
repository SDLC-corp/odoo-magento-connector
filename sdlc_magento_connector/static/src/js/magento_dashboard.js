/** @odoo-module **/

import { loadJS } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";  // Odoo 16: _t() takes no arguments
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formatInteger, formatMonetary } from "@web/views/fields/formatters";
import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

const { DateTime } = luxon;

/** Coloured-dot fallback for instances without a logo: Magento admin palette, not status colours. */
const INSTANCE_DOTS = ["#eb5202", "#514943", "#007bdb", "#79a22e", "#b84002", "#736963"];

export class MagentoConsole extends Component {
    static template = "sdlc_magento_connector.MagentoConsole";
    static props = ["*"];  // Odoo 16: action props are not standardised

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");

        const context = this.props.action?.context || {};
        const initialInstance = context.default_instance_id;
        this.state = useState({
            loading: true,
            error: null,
            data: null,
            navLinks: [],
            selectedIds: initialInstance ? [initialInstance] : [],
            days: Number(context.dashboard_days) || 30,
        });

        this.trendCanvas = useRef("trendCanvas");
        this.trendChart = null;

        onWillStart(async () => {
            const [navLinks] = await Promise.all([
                this.orm.call("magento.dashboard", "get_nav_links", []).catch(() => []),
                loadJS("/web/static/lib/Chart/Chart.js"),  // Odoo 16 has no chartjs bundle
            ]);
            this.state.navLinks = navLinks;
            await this.load();
        });

        useEffect(
            () => {
                this.renderTrendChart();
            },
            () => [this.state.data]
        );

        onWillUnmount(() => this.destroyChart());
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------

    async load() {
        this.state.loading = true;
        this.state.error = null;
        try {
            const data = await this.orm.call("magento.dashboard", "get_dashboard_data", [], {
                instance_ids: this.state.selectedIds,
                days: this.state.days,
            });
            this.state.selectedIds = data.selected_ids;
            this.state.data = data;
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || _t("Could not load the console.");
        } finally {
            this.state.loading = false;
        }
    }

    async toggleInstance(instanceId) {
        const ids = this.state.selectedIds;
        this.state.selectedIds = ids.includes(instanceId)
            ? ids.filter((id) => id !== instanceId)
            : [...ids, instanceId];
        await this.load();
    }

    async clearInstances() {
        if (this.state.selectedIds.length) {
            this.state.selectedIds = [];
            await this.load();
        }
    }

    async setPeriod(days) {
        if (this.state.days !== days) {
            this.state.days = days;
            await this.load();
        }
    }

    // ------------------------------------------------------------------
    // Navigation and drill-downs
    // ------------------------------------------------------------------

    /**
     * Open the records behind a figure, with the scope currently on screen.
     * A drill naming an action (`xml_id`: Error Center, Job Queue) opens that
     * action, keeping its views, search view and help, restricted to the domain.
     */
    async open(drill) {
        if (!drill) {
            return;
        }
        const action = {
            type: "ir.actions.act_window",
            name: drill.name,
            res_model: drill.res_model,
            views: drill.views,
            domain: drill.domain,
            target: "current",
            context: { create: false, ...(drill.context || {}) },
        };
        if (drill.res_id) {
            action.res_id = drill.res_id;
        }
        if (drill.xml_id) {
            try {
                const base = await this.action.loadAction(drill.xml_id);
                if (base?.res_model === drill.res_model) {
                    Object.assign(action, {
                        id: base.id,
                        xml_id: base.xml_id,
                        // `keep_views`: the drill asks for its own first view (e.g. a list
                        // grouped by order inside a pivot-first report action).
                        views: !drill.keep_views && base.views?.length ? base.views : action.views,
                        search_view_id: base.search_view_id,
                        help: base.help,
                    });
                }
            } catch {
                // Fall back to a plain list of the same records.
            }
        }
        await this.action.doAction(action);
    }

    onKeyOpen(ev, drill) {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            this.open(drill);
        }
    }

    openAction(xmlId) {
        this.action.doAction(xmlId);
    }

    // ------------------------------------------------------------------
    // View helpers
    // ------------------------------------------------------------------

    get data() {
        return this.state.data;
    }

    get scopedInstances() {
        const data = this.data;
        if (!data) {
            return [];
        }
        const scope = new Set(data.scope_ids);
        return data.instances.filter((inst) => scope.has(inst.id));
    }

    get hasOrders() {
        return Boolean(this.data && this.data.totals.orders);
    }

    get periodLabel() {
        return sprintf(_t("Last %s days"), this.state.days);
    }

    isSelected(instanceId) {
        return this.state.selectedIds.includes(instanceId);
    }

    dotColor(instanceId) {
        return INSTANCE_DOTS[instanceId % INSTANCE_DOTS.length];
    }

    connectionTone(inst) {
        return { connected: "ok", error: "crit" }[inst.state] || "idle";
    }

    lastSync(inst) {
        if (!inst.last_sync_at) {
            return _t("Never synced");
        }
        const dt = deserializeDateTime(inst.last_sync_at);
        return sprintf(_t("Synced %s"), dt.toRelative() || formatDateTime(dt));
    }

    issuesLabel(inst) {
        const n = Number(inst.open_issues) || 0;
        return n === 1 ? _t("1 open issue") : sprintf(_t("%s open issues"), this.count(n));
    }

    /** Full split for the row tooltip. */
    entityDetail(row) {
        const parts = [];
        if (row.permanent) {
            parts.push(sprintf(_t("%s permanent"), this.count(row.permanent)));
        }
        if (row.transient) {
            parts.push(sprintf(_t("%s transient"), this.count(row.transient)));
        }
        return parts.join(" · ");
    }

    /** Short tag beside the entity name: the severity mix at a glance. */
    entityTag(row) {
        if (row.permanent && row.transient) {
            return sprintf(_t("%s perm."), this.count(row.permanent));
        }
        return row.permanent ? _t("permanent") : _t("transient");
    }

    /** Masthead: when the last daily digest was queued. */
    get lastDigestLabel() {
        const digest = this.data?.last_digest;
        if (!digest) {
            return _t("No digest sent yet");
        }
        const dt = deserializeDateTime(digest.sent_at);
        return sprintf(_t("Digest sent %s"), dt.toRelative() || formatDateTime(dt));
    }

    get lastDigestTitle() {
        const digest = this.data?.last_digest;
        return digest
            ? sprintf(_t("%(instance)s, %(when)s"), {
                  instance: digest.instance,
                  when: formatDateTime(deserializeDateTime(digest.sent_at)),
              })
            : _t("Turn on the 'Magento: Daily Digest' scheduled action, or use Send Digest Now on an instance.");
    }

    /** Net profit panel: signed component amount. */
    signedMoney(row) {
        const value = Number(row.value) || 0;
        return row.sign < 0 && value ? `− ${this.money(Math.abs(value))}` : this.money(value);
    }

    get marginLabel() {
        const margin = this.data?.net_profit?.margin;
        return margin === false || margin === undefined ? "" : sprintf(_t("%s margin"), `${margin}%`);
    }

    lastSyncTitle(inst) {
        return inst.last_sync_at ? formatDateTime(deserializeDateTime(inst.last_sync_at)) : "";
    }

    /** Company-currency amount; compact (1.2k) from a thousand up unless `exact`. */
    money(value, exact = false) {
        const amount = Number(value) || 0;
        const compact = !exact && Math.abs(amount) >= 1000;
        return formatMonetary(amount, {
            currencyId: this.data?.currency.id,
            humanReadable: compact,
            digits: [false, compact ? 1 : exact ? 2 : 0],
        });
    }

    count(value) {
        return formatInteger(Number(value) || 0);
    }

    figure(kpi) {
        return kpi.kind === "money" ? this.money(kpi.value) : this.count(kpi.value);
    }

    share(value, rows, key) {
        const max = Math.max(...rows.map((r) => Number(r[key]) || 0), 1);
        return Math.max(2, Math.min(100, Math.round(((Number(value) || 0) / max) * 100)));
    }

    // ------------------------------------------------------------------
    // Chart
    // ------------------------------------------------------------------

    destroyChart() {
        if (this.trendChart) {
            this.trendChart.destroy();
            this.trendChart = null;
        }
    }

    renderTrendChart() {
        this.destroyChart();
        const canvas = this.trendCanvas.el;
        const trend = this.data?.trend;
        if (!canvas || !trend?.length || !this.hasOrders) {
            return;
        }
        const css = getComputedStyle(canvas.closest(".mg-console"));
        const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
        const ink = token("--mg-ink", "#41362f");
        const inkDim = token("--mg-ink-dim", "#736963");
        const line = token("--mg-line", "#dad1c8");
        const panel = token("--mg-panel", "#ffffff");
        const accent = token("--mg-brand", "#eb5202");
        const bars = token("--mg-secondary", "#514943");
        const font = token("--mg-text", "sans-serif");

        const labels = trend.map((p) =>
            DateTime.fromISO(p.date).toLocaleString({ month: "short", day: "numeric" })
        );

        // eslint-disable-next-line no-undef
        // Odoo 16 ships Chart.js 2.9: axes are xAxes/yAxes lists, fonts and colours are
        // per-option (fontColor, fontSize), tooltip callbacks get (item, data).
        this.trendChart = new Chart(canvas, {
            type: "bar",
            data: {
                labels,
                datasets: [
                    {
                        type: "line",
                        label: _t("Revenue"),
                        data: trend.map((p) => Number(p.revenue) || 0),
                        yAxisID: "revenue",
                        borderColor: accent,
                        backgroundColor: accent + "1f",
                        borderWidth: 2,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        cubicInterpolationMode: "monotone",
                        fill: true,
                        order: 1,
                    },
                    {
                        type: "bar",
                        label: _t("Orders"),
                        data: trend.map((p) => p.orders),
                        yAxisID: "orders",
                        backgroundColor: bars + "59",
                        hoverBackgroundColor: bars,
                        maxBarThickness: 18,
                        order: 2,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches
                    ? false
                    : { duration: 300 },
                hover: { mode: "index", intersect: false },
                legend: {
                    position: "top",
                    align: "end",
                    labels: { fontColor: inkDim, fontFamily: font, fontSize: 11, boxWidth: 10 },
                },
                tooltips: {
                    mode: "index",
                    intersect: false,
                    backgroundColor: panel,
                    borderColor: line,
                    borderWidth: 1,
                    titleFontColor: ink,
                    bodyFontColor: inkDim,
                    callbacks: {
                        label: (item, chartData) => {
                            const dataset = chartData.datasets[item.datasetIndex];
                            return dataset.yAxisID === "revenue"
                                ? `${dataset.label}: ${this.money(item.yLabel, true)}`
                                : `${dataset.label}: ${this.count(item.yLabel)}`;
                        },
                    },
                },
                scales: {
                    xAxes: [{
                        gridLines: { display: false },
                        ticks: { fontColor: inkDim, fontFamily: font, fontSize: 10, maxTicksLimit: 10 },
                    }],
                    yAxes: [
                        {
                            id: "revenue",
                            position: "left",
                            gridLines: { color: line },
                            ticks: {
                                beginAtZero: true, fontColor: inkDim, fontFamily: font, fontSize: 10,
                                callback: (v) => this.money(v),
                            },
                        },
                        {
                            id: "orders",
                            position: "right",
                            gridLines: { display: false },
                            ticks: { beginAtZero: true, fontColor: inkDim, fontFamily: font, fontSize: 10, precision: 0 },
                        },
                    ],
                },
            },
        });
    }
}

registry.category("actions").add("sdlc_magento_dashboard", MagentoConsole);
