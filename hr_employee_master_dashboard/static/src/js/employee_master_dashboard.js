/* @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

// ── Timezone helper: get current date in Asia/Dhaka ─────────────────────────
function todayInDhaka() {
    const now = new Date();
    const parts = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Dhaka',
        year:  'numeric',
        month: '2-digit',
        day:   '2-digit',
    }).formatToParts(now);
    const get = (t) => parseInt(parts.find(p => p.type === t).value, 10);
    return `${get('year')}-${String(get('month')).padStart(2, '0')}-${String(get('day')).padStart(2, '0')}`;
}

// Exact figures only — no "1.2K" abbreviation, this is an official report.
function fmtExact(n) {
    return Math.round(n || 0).toLocaleString('en-US');
}

class EmployeeMasterDashboard extends Component {
    setup() {
        this.action       = useService('action');
        this.orm          = useService('orm');
        this.notification = useService('notification');

        this.state = useState({
            loading:            false,
            selectedDate:       todayInDhaka(),
            selectedDepartment: null,
            selectedWorkerType: null,
            kpis:               null,
        });

        this._departments = [];
        this._workerTypes = [];

        onMounted(() => {
            this._loadDepartments();
            this._loadWorkerTypes();
            this._fetchData();
        });
    }

    // ══════════════════════════════════════════════ INITIALIZATION
    async _loadDepartments() {
        try {
            this._departments = await this.orm.call('hr.employee', 'get_departments', []);
        } catch (e) {
            this._departments = [];
        }
    }

    async _loadWorkerTypes() {
        try {
            this._workerTypes = await this.orm.call('hr.employee', 'get_worker_types', []);
        } catch (e) {
            this._workerTypes = [];
        }
    }

    // ══════════════════════════════════════════════ DATA
    async _fetchData() {
        this.state.loading = true;
        try {
            const result = await this.orm.call(
                'hr.employee',
                'get_dashboard_kpis',
                [
                    this.state.selectedDate,
                    this.state.selectedDepartment,
                    this.state.selectedWorkerType,
                ]
            );
            this.state.kpis = result;
        } catch (e) {
            this.notification.add('Failed to load dashboard data.', { type: 'danger' });
        } finally {
            this.state.loading = false;
        }
    }

    // ══════════════════════════════════════════════ TOOLBAR EVENTS
    onChangeDate(ev) {
        this.state.selectedDate = ev.target.value || todayInDhaka();
        this._fetchData();
    }
    onChangeDepartment(ev) {
        const val = ev.target.value;
        this.state.selectedDepartment = val ? parseInt(val, 10) : null;
        this._fetchData();
    }
    onChangeWorkerType(ev) {
        const val = ev.target.value;
        this.state.selectedWorkerType = val || null;
        this._fetchData();
    }

    // ══════════════════════════════════════════════ DRILL-DOWN (click a card)
    async onClickCard(kpiKey, label) {
        try {
            const ids = await this.orm.call(
                'hr.employee',
                'get_kpi_employee_ids',
                [
                    kpiKey,
                    this.state.selectedDate,
                    this.state.selectedDepartment,
                    this.state.selectedWorkerType,
                ]
            );
            await this.action.doAction({
                type: 'ir.actions.act_window',
                name: label,
                res_model: 'hr.employee',
                views: [[false, 'list'], [false, 'form']],
                view_mode: 'list,form',
                domain: [['id', 'in', ids]],
                context: { active_test: false },
                target: 'current',
            });
        } catch (e) {
            this.notification.add('Could not open the employee list.', { type: 'danger' });
        }
    }

    async onClickDepartmentBar(row) {
        const domain = [['department_id', '=', row.department_id || false]];
        if (this.state.selectedWorkerType) {
            domain.push(['worker_type', '=', this.state.selectedWorkerType]);
        }
        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: row.department,
            res_model: 'hr.employee',
            views: [[false, 'list'], [false, 'form']],
            view_mode: 'list,form',
            domain,
            target: 'current',
        });
    }

    // ══════════════════════════════════════════════ HELPERS
    get departmentOptions() { return this._departments || []; }
    get workerTypeOptions() { return this._workerTypes || []; }

    fmt(n) { return fmtExact(n); }

    // Plain count for most cards; "present / total" for cards that carry a
    // `total` (the two worker-type presence cards) instead of measuring
    // everything against the grand total headcount.
    cardValueText(card) {
        if (card.format === 'text') return card.value;
        if (card.total !== undefined && card.total !== null) {
            return `${fmtExact(card.value)} / ${fmtExact(card.total)}`;
        }
        return fmtExact(card.value);
    }

    // ══════════════════════════════════════════════ HERO — Present Today
    get hero() {
        const k = this.state.kpis;
        if (!k) return null;
        const scheduled = (k.present || 0) + (k.absent || 0);
        const rate = k.attendance_rate || 0;
        // conic-gradient stop percentage as a rounded int for a crisp ring
        return {
            present: k.present || 0,
            scheduled,
            rate,
            ringDeg: Math.max(0, Math.min(100, Math.round(rate))),
            onLeave: k.on_leave || 0,
            dayOff: k.day_off_today || 0,
        };
    }

    // ══════════════════════════════════════════════ KPI CARDS (sectioned)
    get todayCards() {
        const k = this.state.kpis;
        if (!k) return [];
        return [
            { key: 'absent',    label: 'Absent',           value: k.absent,          icon: '⛔', tone: 'critical' },
            { key: 'leave',     label: 'On Leave',         value: k.on_leave,        icon: '🗓️', tone: 'warning' },
            { key: 'checkedin', label: 'Still Checked In', value: k.checked_in_now,  icon: '🟢', tone: 'good' },
            { key: 'late',      label: 'Late Today',       value: k.late_today,      icon: '⏰', tone: 'warning' },
            { key: 'dayoff',    label: 'On Day-Off',       value: k.day_off_today,   icon: '🌴', tone: 'neutral' },
        ];
    }

    get workforceCards() {
        const k = this.state.kpis;
        if (!k) return [];
        const regularPct = k.regular_total ? Math.round((k.regular_present / k.regular_total) * 100) : 0;
        const dailyPct = k.daily_total ? Math.round((k.daily_present / k.daily_total) * 100) : 0;
        return [
            {
                key: 'regular', label: 'Regular Present / Total', drillLabel: 'Regular Workers',
                value: k.regular_present, total: k.regular_total,
                sub: `${regularPct}% present`, icon: '🧰', tone: 'aqua',
            },
            {
                key: 'daily', label: 'Daily Present / Total', drillLabel: 'Daily Workers',
                value: k.daily_present, total: k.daily_total,
                sub: `${dailyPct}% present`, icon: '📅', tone: 'violet',
            },
            { key: 'joined',      label: 'New Joined (Month)', value: k.new_joined,    icon: '🆕', tone: 'aqua' },
            { key: 'reactivated', label: 'Reactivated (Month)', value: k.reactivated,  icon: '♻️', tone: 'good' },
            { key: 'archived',    label: 'Archived (Month)',   value: k.archived,      icon: '📦', tone: 'violet' },
        ];
    }

    get badgeCards() {
        const k = this.state.kpis;
        if (!k) return [];
        return [
            {
                key: 'last_regular', label: 'Highest Regular Badge', value: k.last_regular_badge,
                sub: k.last_regular_name, icon: '🏷️', tone: 'aqua', format: 'text',
            },
            {
                key: 'last_daily', label: 'Highest Daily Badge', value: k.last_daily_badge,
                sub: k.last_daily_name, icon: '🏷️', tone: 'violet', format: 'text',
            },
            {
                key: 'no_badge', label: 'Without a Badge Yet', value: k.no_badge,
                icon: '⚠️', tone: 'slate',
            },
        ];
    }

    // Grouped bar chart: Present / Absent / On Leave per worker type
    get presenceChartRows() {
        const k = this.state.kpis;
        if (!k) return [];
        const rows = k.presence_by_worker_type || [];
        const max = Math.max(1, ...rows.flatMap(r => [r.present, r.absent, r.on_leave]));
        return rows.map(r => ({
            ...r,
            presentPct: Math.round((r.present  / max) * 100),
            absentPct:  Math.round((r.absent   / max) * 100),
            leavePct:   Math.round((r.on_leave / max) * 100),
        }));
    }

    // Horizontal bar chart: headcount per department
    get departmentChartRows() {
        const k = this.state.kpis;
        if (!k) return [];
        const rows = k.headcount_by_department || [];
        const max = Math.max(1, ...rows.map(r => r.employee_count));
        return rows.map(r => ({ ...r, pct: Math.round((r.employee_count / max) * 100) }));
    }
}

EmployeeMasterDashboard.template = 'EmployeeMasterDashboard';
registry.category('actions').add('employee_master_dashboard', EmployeeMasterDashboard);
