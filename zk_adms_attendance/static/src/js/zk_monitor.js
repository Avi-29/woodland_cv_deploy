/** @odoo-module **/
// ZKTeco ADMS — Real-time Monitor Dashboard (Odoo 19 / OWL 3)

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";

const REFRESH_INTERVAL_MS = 10_000;

const PUNCH_TYPE_MAP = {
    "0": "Check In",  "1": "Check Out",
    "2": "Break Out", "3": "Break In",
    "4": "OT In",     "5": "OT Out",
};

const VERIFY_TYPE_MAP = {
    "0": "Password", "1": "Fingerprint", "3": "Card",
    "4": "Card+PIN", "10": "Face",       "15": "Palm",
};

export class ZkMonitorDashboard extends Component {
    static template = "zk_adms_attendance.MonitorDashboard";
    static props = {};

    setup() {
        this.orm          = useService("orm");
        this.notification = useService("notification");

        this.state = useState({
            devices:       [],
            recentPunches: [],
            stats:         { total: 0, today: 0, online: 0, offline: 0 },
            loading:       true,
            lastRefresh:   null,
        });

        this._timer = null;

        onWillStart(async () => { await this._loadData(); });
        onMounted(() => { this._timer = setInterval(() => this._loadData(), REFRESH_INTERVAL_MS); });
        onWillUnmount(() => { if (this._timer) clearInterval(this._timer); });
    }

    // ── Data fetching ────────────────────────────────────────────────────────

    async _loadData() {
        try {
            const [devices, punches, todayCount, totalCount] = await Promise.all([
                this.orm.searchRead(
                    "zk.device",
                    [["active", "=", true]],
                    ["name", "serial_number", "ip_address", "location",
                     "state", "last_activity", "last_heartbeat", "attendance_count"],
                    { order: "state asc, name asc" }
                ),
                this.orm.searchRead(
                    "zk.attendance.log", [],
                    ["punch_time", "pin", "employee_name", "device_id",
                     "punch_type", "verify_type", "state"],
                    { limit: 40, order: "punch_time desc" }
                ),
                this._todayCount(),
                this.orm.searchCount("zk.attendance.log", []),
            ]);

            this.state.devices       = devices;
            this.state.recentPunches = punches.map(p => this._decoratePunch(p));
            this.state.stats = {
                total:   totalCount,
                today:   todayCount,
                online:  devices.filter(d => d.state === "online").length,
                offline: devices.filter(d => d.state !== "online").length,
            };
            this.state.lastRefresh = new Date().toLocaleTimeString();
            this.state.loading     = false;
        } catch (err) {
            console.error("[ZkMonitor] refresh error:", err);
        }
    }

    async _todayCount() {
        const now     = new Date();
        const midnite = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const utcStr  = new Date(midnite.getTime() - midnite.getTimezoneOffset() * 60000)
                            .toISOString().replace("T", " ").slice(0, 19);
        return this.orm.searchCount("zk.attendance.log", [["punch_time", ">=", utcStr]]);
    }

    _decoratePunch(p) {
        return {
            ...p,
            punch_type_label:  PUNCH_TYPE_MAP[p.punch_type]  || p.punch_type,
            verify_type_label: VERIFY_TYPE_MAP[p.verify_type] || p.verify_type,
            device_name:       p.device_id ? p.device_id[1] : "Unknown",
            punch_time_fmt:    p.punch_time
                ? new Date(p.punch_time.replace(" ", "T") + "Z").toLocaleString()
                : "",
        };
    }

    // ── User actions ─────────────────────────────────────────────────────────

    async manualRefresh() {
        this.state.loading = true;
        await this._loadData();
        this.notification.add("Dashboard refreshed", { type: "info", sticky: false });
    }
}

registry.category("actions").add("zk_adms.monitor", ZkMonitorDashboard);
