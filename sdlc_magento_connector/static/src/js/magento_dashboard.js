/** @odoo-module **/

import { loadBundle } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formatInteger, formatMonetary } from "@web/views/fields/formatters";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

const { DateTime } = luxon;

/** Coloured-dot fallback for instances without a logo; warm, distinct, not status colours. */
const INSTANCE_DOTS = ["#c8103a", "#8c7b6b", "#3a5a8c", "#2f7d6d", "#a8572a", "#6b4c8a"];

export class MagentoConsole extends Component {
    static template = "sdlc_magento_connector.MagentoConsole";
    static props = { ...standardActionServiceProps };

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
                loadBundle("web.chartjs_lib"),
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

    /** Open the records behind a figure, with the scope currently on screen. */
    open(drill) {
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
            context: { create: false },
        };
        if (drill.res_id) {
            action.res_id = drill.res_id;
        }
        this.action.doAction(action);
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
        return _t("Last %s days", this.state.days);
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
        return _t("Synced %s", dt.toRelative() || formatDateTime(dt));
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
        const ink = token("--mg-ink", "#1a1a1a");
        const inkDim = token("--mg-ink-dim", "#6b5f55");
        const line = token("--mg-line", "#e3d8cc");
        const panel = token("--mg-panel", "#ffffff");
        const accent = token("--mg-accent", "#c8103a");
        const bars = token("--mg-bar-alt", "#bbaa97");
        const font = token("--mg-text", "sans-serif");

        const labels = trend.map((p) =>
            DateTime.fromISO(p.date).toLocaleString({ month: "short", day: "numeric" })
        );

        // eslint-disable-next-line no-undef
        this.trendChart = new Chart(canvas, {
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
                        backgroundColor: bars + "99",
                        hoverBackgroundColor: bars,
                        borderRadius: 3,
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
                interaction: { mode: "index", intersect: false },
                font: { family: font },
                plugins: {
                    legend: {
                        position: "top",
                        align: "end",
                        labels: { color: inkDim, boxWidth: 10, boxHeight: 10, font: { size: 11 } },
                    },
                    tooltip: {
                        backgroundColor: panel,
                        borderColor: line,
                        borderWidth: 1,
                        titleColor: ink,
                        bodyColor: inkDim,
                        callbacks: {
                            label: (ctx) =>
                                ctx.dataset.yAxisID === "revenue"
                                    ? `${ctx.dataset.label}: ${this.money(ctx.parsed.y, true)}`
                                    : `${ctx.dataset.label}: ${this.count(ctx.parsed.y)}`,
                        },
                    },
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { color: inkDim, font: { size: 10 }, maxTicksLimit: 10 },
                    },
                    revenue: {
                        position: "left",
                        beginAtZero: true,
                        grid: { color: line },
                        ticks: { color: inkDim, font: { size: 10 }, callback: (v) => this.money(v) },
                    },
                    orders: {
                        position: "right",
                        beginAtZero: true,
                        grid: { display: false },
                        ticks: { color: inkDim, font: { size: 10 }, precision: 0 },
                    },
                },
            },
        });
    }
}

registry.category("actions").add("sdlc_magento_dashboard", MagentoConsole);
