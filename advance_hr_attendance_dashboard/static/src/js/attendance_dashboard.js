/* @odoo-module */
import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
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
    return { year: get('year'), month: get('month'), day: get('day') };
}

class AttendanceDashboard extends Component {
    setup() {
        this.action       = useService('action');
        this.orm          = useService('orm');
        this.notification = useService('notification');
        this.root         = useRef('attendance-dashboard');

        const dhaka = todayInDhaka();

        this.state = useState({
            filteredDurationDates: [],
            employeeData:          [],
            loading:               false,
            searchValue:           '',
            selectedYear:          dhaka.year,
            selectedMonth:         dhaka.month,
            selectedDepartment:    null,
            selectedWorkerType:    null,
            page:                  1,
            perPage:               5,
            totalCount:            0,
            totalPages:            0,
            // leave-creation popup
            popup: {
                visible:    false,
                employeeId: null,
                empName:    '',
                dateFrom:   '',
                dateTo:     '',
                leaveTypes: [],
                selectedType: null,
            },
            // swap-day popup
            swapPopup: {
                visible:      false,
                employeeId:   null,
                empName:      '',
                workDate:     '',
                workType:     'weekend',
                offDate:      '',
                reason:       '',
            },
            // ── Export PDF modal ────────────────────────────────────────────
            exportModal: {
                visible:              false,
                departments:          [],   // [{id, name}]
                selectedDepts:        [],   // [] means all
                workerTypes:          [],   // [{id, name}]
                selectedWorkerTypes:  [],   // [] means all
                allDeptsChecked:      true,
                allTypesChecked:      true,
                generating:           false,
            },
            // ── Leave Summary grid (toggled in place of the attendance table) ──
            viewMode:         'attendance',   // 'attendance' | 'leaveSummary'
            leaveSummaryData: [],
            // ── Live "present today" counter (header button) ───────────────
            presentCount:     0,
            presentTotal:     0,
        });

        this._drag = {
            active:     false,
            employeeId: null,
            startDate:  null,
            endDate:    null,
            cells:      [],
        };

        this._searchTimer = null;
        this._departments = [];
        this._workerTypes = [];
        this._liveTimer = null;
        onMounted(() => {
            this._loadDepartments();
            this._loadWorkerTypes();
            this._fetchData();
            this._fetchPresentCount();
            this._liveTimer = setInterval(() => this._fetchPresentCount(), 60000);
        });
        onWillUnmount(() => {
            if (this._liveTimer) clearInterval(this._liveTimer);
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
        if (this.state.viewMode === 'leaveSummary') {
            return this._fetchLeaveSummary();
        }
        this.state.loading = true;
        try {
            const result = await this.orm.call(
                'hr.employee',
                'get_employee_leave_data',
                [
                    this.state.selectedYear,
                    this.state.selectedMonth,
                    this.state.searchValue,
                    this.state.page,
                    this.state.perPage,
                    this.state.selectedDepartment,
                    this.state.selectedWorkerType,
                ]
            );
            this.state.filteredDurationDates = result.filtered_duration_dates;
            this.state.employeeData          = result.employee_data;
            this.state.totalCount            = result.total_count;
            this.state.totalPages            = result.total_pages;
        } catch (e) {
            this.notification.add('Failed to load attendance data.', { type: 'danger' });
        } finally {
            this.state.loading = false;
        }
    }

    async _fetchLeaveSummary() {
        this.state.loading = true;
        try {
            const result = await this.orm.call(
                'hr.employee',
                'get_employee_leave_summary',
                [
                    this.state.selectedYear,
                    this.state.searchValue,
                    this.state.page,
                    this.state.perPage,
                    this.state.selectedDepartment,
                    this.state.selectedWorkerType,
                ]
            );
            this.state.leaveSummaryData = result.employee_data;
            this.state.totalCount       = result.total_count;
            this.state.totalPages       = result.total_pages;
        } catch (e) {
            this.notification.add('Failed to load leave summary.', { type: 'danger' });
        } finally {
            this.state.loading = false;
        }
    }

    // Live count of employees checked in today (Asia/Dhaka), scoped to the
    // current Department / Worker Type filters.
    async _fetchPresentCount() {
        try {
            const result = await this.orm.call(
                'hr.employee',
                'get_present_today_count',
                [this.state.selectedDepartment, this.state.selectedWorkerType]
            );
            this.state.presentCount = result.present_count;
            this.state.presentTotal = result.total_count;
        } catch (e) {
            // Silent — this is a secondary indicator, not worth an error toast.
        }
    }
    onClickPresentCounter() { this._fetchPresentCount(); }

    // Toggle between the attendance grid and the leave summary grid,
    // reusing the same toolbar filters and pagination as the dashboard.
    onToggleLeaveSummary() {
        this.state.viewMode = this.state.viewMode === 'leaveSummary' ? 'attendance' : 'leaveSummary';
        this.state.page = 1;
        this._fetchData();
    }

    // ══════════════════════════════════════════════ TOOLBAR EVENTS
    onChangeYear(ev)   { this.state.selectedYear  = parseInt(ev.target.value, 10); this.state.page = 1; this._fetchData(); }
    onChangeMonth(ev)  { this.state.selectedMonth = parseInt(ev.target.value, 10); this.state.page = 1; this._fetchData(); }
    onChangeDepartment(ev) {
        const val = ev.target.value;
        this.state.selectedDepartment = val ? parseInt(val, 10) : null;
        this.state.page = 1; this._fetchData(); this._fetchPresentCount();
    }
    onChangeWorkerType(ev) {
        const val = ev.target.value;
        this.state.selectedWorkerType = val || null;
        this.state.page = 1; this._fetchData(); this._fetchPresentCount();
    }
    onSearchInput(ev) {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
            this.state.searchValue = ev.target.value.trim();
            this.state.page = 1; this._fetchData();
        }, 350);
    }
    onSearchKeydown(ev) {
        if (ev.key === 'Enter') {
            clearTimeout(this._searchTimer);
            this.state.searchValue = ev.target.value.trim();
            this.state.page = 1; this._fetchData();
        }
    }
    onClickSearch() {
        clearTimeout(this._searchTimer);
        const inp = this.root.el.querySelector('#search-bar');
        this.state.searchValue = inp ? inp.value.trim() : '';
        this.state.page = 1; this._fetchData();
    }

    // ══════════════════════════════════════════════ PAGINATION
    onPrevPage()   { if (this.state.page > 1) { this.state.page--; this._fetchData(); } }
    onNextPage()   { if (this.state.page < this.state.totalPages) { this.state.page++; this._fetchData(); } }
    onGoToPage(ev) { const v = parseInt(ev.target.value, 10); if (v >= 1 && v <= this.state.totalPages) { this.state.page = v; this._fetchData(); } }
    onChangePerPage(ev) { this.state.perPage = parseInt(ev.target.value, 10) || 20; this.state.page = 1; this._fetchData(); }

    // ══════════════════════════════════════════════ CELL CLICK
    onClickCell(leave, employeeId) {
        // Normal (non-split) cells only — split cells are handled by onClickSplitTop / onClickSplitBot
        if (leave.is_day_off_or_holiday) return;
        if (leave.record_type === 'attendance') {
            this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.attendance', res_id: leave.record_id, views: [[false, 'form']], target: 'new' });
        } else if (leave.record_type === 'leave') {
            this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.leave', res_id: leave.record_id, views: [[false, 'form']], target: 'new' });
        } else if (leave.record_type === 'absent') {
            this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.leave', views: [[false, 'form']], target: 'new', context: { default_employee_id: employeeId, default_request_date_from: leave.leave_date, default_request_date_to: leave.leave_date, default_date_from: leave.leave_date + ' 00:00:00', default_date_to: leave.leave_date + ' 23:59:59' } });
        } else if (leave.record_type === 'dayoff') {
            this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.swap', views: [[false, 'form']], target: 'new', context: { default_employee_id: employeeId, default_swap_work_date: leave.leave_date, default_swap_work_type: 'weekend' } });
        } else if (leave.state === 'PH' || leave.state === 'ADJUST') {
            this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.swap', views: [[false, 'form']], target: 'new', context: { default_employee_id: employeeId, default_swap_work_date: leave.work_date || leave.leave_date, default_swap_off_date: leave.state === 'ADJUST' ? leave.leave_date : undefined, default_swap_work_type: leave.state === 'PH' ? 'holiday' : 'weekend' } });
        }
    }

    // Top-left triangle of a split cell (OFF or PH half) → open hr.swap form
    onClickSplitTop(ev, leave, employeeId) {
        ev.stopPropagation();
        const workType = leave.state === 'PH/P' ? 'holiday' : 'weekend';
        this.action.doAction({
            type: 'ir.actions.act_window',
            res_model: 'hr.swap',
            views: [[false, 'form']],
            target: 'new',
            context: {
                default_employee_id: employeeId,
                default_swap_work_date: leave.leave_date,
                default_swap_work_type: workType,
            },
        });
    }

    // Bottom-right triangle of a split cell (P half) → open attendance form
    onClickSplitBot(ev, leave, employeeId) {
        ev.stopPropagation();
        this.action.doAction({
            type: 'ir.actions.act_window',
            res_model: 'hr.attendance',
            res_id: leave.record_id,
            views: [[false, 'form']],
            target: 'new',
        });
    }

    onClickEmployee(employeeId) {
        this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.employee', res_id: employeeId, views: [[false, 'form']], target: 'current' });
    }

    // ══════════════════════════════════════════════ DRAG-SELECT
    _empName(id) { const emp = this.state.employeeData.find(e => e.id === id); return emp ? emp.name : ''; }

    onMouseDownCell(ev, leave, employeeId) {
        if (leave.record_type !== 'absent') return;
        ev.preventDefault();
        this._drag.active = true; this._drag.employeeId = employeeId;
        this._drag.startDate = leave.leave_date; this._drag.endDate = leave.leave_date;
        this._highlightDrag(employeeId, leave.leave_date, leave.leave_date);
    }
    onMouseEnterCell(ev, leave, employeeId) {
        if (!this._drag.active || employeeId !== this._drag.employeeId || leave.record_type !== 'absent') return;
        this._drag.endDate = leave.leave_date;
        this._highlightDrag(employeeId, this._drag.startDate, this._drag.endDate);
    }
    onMouseUpCell(ev, leave, employeeId) {
        if (!this._drag.active) return;
        this._drag.active = false; this._clearDragHighlight();
        let d1 = this._drag.startDate, d2 = this._drag.endDate;
        if (d1 > d2) { [d1, d2] = [d2, d1]; }
        this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.leave', views: [[false, 'form']], target: 'new', context: { default_employee_id: employeeId, default_request_date_from: d1, default_request_date_to: d2, default_date_from: d1 + ' 00:00:00', default_date_to: d2 + ' 23:59:59' } });
    }
    _highlightDrag(empId, start, end) {
        this._clearDragHighlight();
        const [d1, d2] = start <= end ? [start, end] : [end, start];
        this.root.el.querySelectorAll('tr[data-emp-id]').forEach(row => {
            if (parseInt(row.dataset.empId, 10) !== empId) return;
            row.querySelectorAll('td[data-date]').forEach(td => {
                const d = td.dataset.date;
                if (d >= d1 && d <= d2 && td.dataset.rtype === 'absent') { td.classList.add('ahad_drag_select'); this._drag.cells.push(td); }
            });
        });
    }
    _clearDragHighlight() { this._drag.cells.forEach(td => td.classList.remove('ahad_drag_select')); this._drag.cells = []; }

    // ══════════════════════════════════════════════ LEAVE POPUP
    async _openLeavePopup(empId, empName, dateFrom, dateTo) {
        if (!this._leaveTypes) this._leaveTypes = await this.orm.call('hr.employee', 'get_leave_types', []);
        Object.assign(this.state.popup, { visible: true, employeeId: empId, empName, dateFrom, dateTo, leaveTypes: this._leaveTypes, selectedType: this._leaveTypes.length ? this._leaveTypes[0].id : null });
    }
    onPopupTypeChange(ev)     { this.state.popup.selectedType = parseInt(ev.target.value, 10); }
    onPopupDateFromChange(ev) { this.state.popup.dateFrom = ev.target.value; }
    onPopupDateToChange(ev)   { this.state.popup.dateTo   = ev.target.value; }
    onPopupCancel()           { this.state.popup.visible = false; }
    async onPopupSubmit() {
        const p = this.state.popup;
        if (!p.selectedType) { this.notification.add('Please select a leave type.', { type: 'warning' }); return; }
        try {
            await this.orm.call('hr.employee', 'create_leave_from_dashboard', [p.employeeId, p.dateFrom, p.dateTo, p.selectedType]);
            this.notification.add('Leave request created.', { type: 'success' });
            this.state.popup.visible = false; this._fetchData();
        } catch (e) { this.notification.add(e.message || 'Could not create leave.', { type: 'danger' }); }
    }

    // ══════════════════════════════════════════════ SWAP POPUP
    _openSwapPopup(empId, empName, workDate, workType) {
        Object.assign(this.state.swapPopup, { visible: true, employeeId: empId, empName, workDate, workType, offDate: '', reason: '' });
    }
    onSwapWorkTypeChange(ev) { this.state.swapPopup.workType = ev.target.value; }
    onSwapOffDateChange(ev)  { this.state.swapPopup.offDate  = ev.target.value; }
    onSwapReasonChange(ev)   { this.state.swapPopup.reason   = ev.target.value; }
    onSwapPopupCancel()      { this.state.swapPopup.visible  = false; }
    async onSwapPopupSubmit() {
        const s = this.state.swapPopup;
        if (!s.offDate) { this.notification.add('Please select a compensatory off date.', { type: 'warning' }); return; }
        try {
            await this.orm.call('hr.swap', 'create', [{ employee_id: s.employeeId, swap_work_date: s.workDate, swap_work_type: s.workType, swap_off_date: s.offDate, reason: s.reason || '' }]);
            this.notification.add('Swap day request created.', { type: 'success' });
            this.state.swapPopup.visible = false; this._fetchData();
        } catch (e) { this.notification.add(e.message || 'Could not create swap request.', { type: 'danger' }); }
    }
    onOpenSwapInOdoo() {
        const s = this.state.swapPopup;
        this.state.swapPopup.visible = false;
        this.action.doAction({ type: 'ir.actions.act_window', res_model: 'hr.swap', views: [[false, 'form']], target: 'new', context: { default_employee_id: s.employeeId, default_swap_work_date: s.workDate, default_swap_work_type: s.workType } });
    }

    // ══════════════════════════════════════════════ EXPORT PDF MODAL
    _OnClickPdfReport() {
        // Open the export options modal — pre-populate with all depts / types
        const em = this.state.exportModal;
        em.departments         = [...(this._departments || [])];
        em.workerTypes         = [...(this._workerTypes || [])];
        em.selectedDepts       = em.departments.map(d => d.id);
        em.selectedWorkerTypes = em.workerTypes.map(t => t.id);
        em.allDeptsChecked     = true;
        em.allTypesChecked     = true;
        em.generating          = false;
        em.visible             = true;
    }

    onExportModalCancel() {
        this.state.exportModal.visible = false;
    }

    // Toggle individual department checkbox
    onExportDeptToggle(ev, deptId) {
        const em  = this.state.exportModal;
        const idx = em.selectedDepts.indexOf(deptId);
        if (idx === -1) em.selectedDepts.push(deptId);
        else            em.selectedDepts.splice(idx, 1);
        em.allDeptsChecked = (em.selectedDepts.length === em.departments.length);
    }

    // Toggle "Select All" departments
    onExportDeptSelectAll(ev) {
        const em = this.state.exportModal;
        em.allDeptsChecked = ev.target.checked;
        em.selectedDepts   = em.allDeptsChecked ? em.departments.map(d => d.id) : [];
    }

    // Toggle individual worker type checkbox
    onExportTypeToggle(ev, typeId) {
        const em  = this.state.exportModal;
        const idx = em.selectedWorkerTypes.indexOf(typeId);
        if (idx === -1) em.selectedWorkerTypes.push(typeId);
        else            em.selectedWorkerTypes.splice(idx, 1);
        em.allTypesChecked = (em.selectedWorkerTypes.length === em.workerTypes.length);
    }

    // Toggle "Select All" worker types
    onExportTypeSelectAll(ev) {
        const em = this.state.exportModal;
        em.allTypesChecked     = ev.target.checked;
        em.selectedWorkerTypes = em.allTypesChecked ? em.workerTypes.map(t => t.id) : [];
    }

    // Generate the PDF after user confirms selections
    async onExportConfirm() {
        const em = this.state.exportModal;
        em.generating = true;

        try {
            const monthNames = ['January','February','March','April','May','June',
                                'July','August','September','October','November','December'];
            const monthLabel = monthNames[this.state.selectedMonth - 1] + ' ' + this.state.selectedYear;

            // ── Determine which dept IDs to export ──────────────────────────
            // If all are selected we pass null (no dept filter from modal)
            // Otherwise we loop per-dept and combine
            const deptIds = (em.selectedDepts.length === em.departments.length || !em.departments.length)
                ? [null]                                    // all departments
                : em.selectedDepts.map(id => id);          // specific depts

            // ── Determine which worker types to export ───────────────────────
            const workerTypeValues = (em.selectedWorkerTypes.length === em.workerTypes.length || !em.workerTypes.length)
                ? [null]
                : em.selectedWorkerTypes.map(id => id);

            // ── Fetch all employees for each combination ─────────────────────
            const allEmployees = [];
            for (const deptId of deptIds) {
                for (const wType of workerTypeValues) {
                    const result = await this.orm.call(
                        'hr.employee',
                        'get_employee_leave_data',
                        [
                            this.state.selectedYear,
                            this.state.selectedMonth,
                            '',        // no search filter in export
                            1,
                            100000,
                            deptId,
                            wType,
                        ]
                    );
                    for (const emp of result.employee_data) {
                        if (!allEmployees.find(e => e.id === emp.id)) allEmployees.push(emp);
                    }
                }
            }

            // Re-fetch dates from a single call (they are the same for all)
            const datesResult = await this.orm.call(
                'hr.employee',
                'get_employee_leave_data',
                [this.state.selectedYear, this.state.selectedMonth, '', 1, 1, null, null]
            );
            const dates = datesResult.filtered_duration_dates;

            if (!allEmployees.length) {
                this.notification.add('No data to export for the selected options.', { type: 'warning' });
                em.generating = false;
                return;
            }

            // ── Group by department ──────────────────────────────────────────
            const deptMap = {};
            for (const emp of allEmployees) {
                const deptName = emp.department || '— No Department —';
                if (!deptMap[deptName]) deptMap[deptName] = [];
                deptMap[deptName].push(emp);
            }

            // ── Build thead ──────────────────────────────────────────────────
            let tHeadHtml = `<tr>
                <th style="white-space:nowrap;padding:4px 6px;">Employee</th>
                <th style="white-space:nowrap;padding:4px 6px;">Badge</th>
                <th style="white-space:nowrap;padding:4px 6px;">Day Off</th>
                <th style="white-space:nowrap;padding:4px 6px;background:#fff3cd;">Abs</th>`;
            for (const d of dates) {
                const [, mo, dy] = d.split('-');
                const mLabel = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][parseInt(mo,10)-1];
                tHeadHtml += `<th style="font-size:9px;padding:2px 3px;text-align:center;">${parseInt(dy,10)}<br/>${mLabel}</th>`;
            }
            tHeadHtml += `<th style="white-space:nowrap;padding:4px 6px;">Summary</th></tr>`;

            // ── Build tbody (grouped by department) ──────────────────────────
            let tBodyHtml = '';
            const colSpan = 5 + dates.length;
            for (const [deptName, emps] of Object.entries(deptMap)) {
                tBodyHtml += `<tr><td colspan="${colSpan}" style="background:#37474f;color:#fff;font-weight:bold;padding:6px 8px;font-size:11px;">📁 ${deptName}</td></tr>`;
                for (const emp of emps) {
                    tBodyHtml += `<tr>
                        <td style="white-space:nowrap;padding:3px 6px;font-size:10px;">${emp.name}</td>
                        <td style="white-space:nowrap;padding:3px 6px;font-size:10px;">${emp.zk_badge_no || '—'}</td>
                        <td style="white-space:nowrap;padding:3px 6px;font-size:10px;">${emp.day_off || '—'}</td>
                        <td style="padding:3px 6px;text-align:center;font-size:10px;font-weight:bold;background:#fff3cd;">${emp.total_absent_count}</td>`;
                    for (const ld of emp.leave_data) {
                        tBodyHtml += `<td style="text-align:center;padding:2px 3px;font-size:9px;background:${ld.color};">${ld.state || ''}</td>`;
                    }
                    let summary = '';
                    for (const [code, cnt] of Object.entries(emp.leave_counts || {})) {
                        summary += `<span style="display:inline-block;background:#e0e0e0;border-radius:3px;padding:1px 4px;margin:1px;font-size:9px;">${code}:${cnt}</span>`;
                    }
                    tBodyHtml += `<td style="padding:3px 6px;">${summary || '—'}</td></tr>`;
                }
            }

            em.visible    = false;
            em.generating = false;

            return this.action.doAction({
                type: 'ir.actions.report',
                report_type: 'qweb-pdf',
                report_name: 'advance_hr_attendance_dashboard.report_hr_attendance',
                report_file: 'advance_hr_attendance_dashboard.report_hr_attendance',
                data: { tHead: tHeadHtml, tBody: tBodyHtml, monthLabel },
            });
        } catch (e) {
            this.notification.add('Failed to generate PDF report.', { type: 'danger' });
            em.generating = false;
        }
    }

    // ══════════════════════════════════════════════ LEAVE SUMMARY GRID
    // Cell styling for an SL/CL/LWP count in the leave summary grid.
    leaveSummaryCellStyle(code, count) {
        const colors = { sl: '#6CC1ED', cl: '#30C381', lwp: '#F06050' };
        if (!count) return 'background:#f8f9fa;color:#94a3b8;';
        return `background:${colors[code]}40;color:${colors[code]};font-weight:700;`;
    }

    get leaveSummaryMonthLabels() {
        return ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    }

    // ══════════════════════════════════════════════ HELPERS
    formatDateHeader(d) {
        const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
        const [, mo, dy] = d.split('-');
        return `${parseInt(dy, 10)}\n${months[parseInt(mo, 10) - 1]}`;
    }

    get yearOptions() {
        const cur = todayInDhaka().year;
        const yrs = [];
        for (let y = cur - 3; y <= cur + 1; y++) yrs.push(y);
        return yrs;
    }

    get monthOptions() {
        return [
            {v:1,l:'January'},{v:2,l:'February'},{v:3,l:'March'},{v:4,l:'April'},
            {v:5,l:'May'},{v:6,l:'June'},{v:7,l:'July'},{v:8,l:'August'},
            {v:9,l:'September'},{v:10,l:'October'},{v:11,l:'November'},{v:12,l:'December'},
        ];
    }

    get departmentOptions()  { return this._departments || []; }
    get workerTypeOptions()  { return this._workerTypes || []; }

    get paginationInfo() {
        const s = (this.state.page - 1) * this.state.perPage + 1;
        const e = Math.min(this.state.page * this.state.perPage, this.state.totalCount);
        return this.state.totalCount ? `${s}–${e} of ${this.state.totalCount}` : '0';
    }

    // Helper: is a dept checked in export modal?
    isExportDeptChecked(deptId) {
        return this.state.exportModal.selectedDepts.includes(deptId);
    }
    // Helper: is a worker type checked in export modal?
    isExportTypeChecked(typeId) {
        return this.state.exportModal.selectedWorkerTypes.includes(typeId);
    }

    cellCursor(leave) {
        if (leave.record_type === 'absent')     return 'ahad_cursor_absent';
        if (leave.record_type === 'leave')      return 'ahad_clickable';
        if (leave.record_type === 'attendance') return 'ahad_clickable';
        if (leave.record_type === 'dayoff')     return 'ahad_clickable';
        if (leave.state === 'PH' || leave.state === 'ADJUST') return 'ahad_clickable';
        return '';
    }
}

AttendanceDashboard.template = 'AttendanceDashboard';
registry.category('actions').add('attendance_dashboard', AttendanceDashboard);
