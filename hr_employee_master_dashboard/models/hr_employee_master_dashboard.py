# -*- coding: utf-8 -*-
from datetime import datetime, time
from calendar import monthrange

from odoo import api, fields, models

TZ = 'Asia/Dhaka'


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # ── get available departments (for filter dropdown) ────────────────────
    @api.model
    def get_departments(self):
        depts = self.env['hr.department'].search([], order='name')
        return [{'id': d.id, 'name': d.name} for d in depts]

    # ── get available worker types (for filter dropdown) ────────────────────
    @api.model
    def get_worker_types(self):
        return [
            {'id': 'daily', 'name': 'Daily'},
            {'id': 'regular', 'name': 'Regular'},
        ]

    # ── shared day-classification helper ────────────────────────────────────
    def _dashboard_classify_day(self, employees, d, present_ids, leave_ids):
        """Classify each employee in `employees` for calendar date `d` into
        present / absent / on_leave / day_off, honoring each employee's
        weekly day-off. Returns a dict of bucket -> hr.employee recordset.
        Shared by get_dashboard_kpis (for counts) and get_kpi_employee_ids
        (for the exact ids behind a clicked KPI), so the two never diverge.
        """
        weekday = d.weekday()  # 0=Mon … 6=Sun
        Employee = self.env['hr.employee']
        buckets = {
            'present': Employee, 'absent': Employee,
            'on_leave': Employee, 'day_off': Employee,
        }
        for emp in employees:
            day_off_idx = None
            day_off = emp.get_day_off_day(d)
            if day_off:
                try:
                    day_off_idx = int(day_off)
                except ValueError:
                    day_off_idx = None
            if day_off_idx is not None and day_off_idx == weekday:
                buckets['day_off'] |= emp
                continue
            if emp.id in present_ids:
                buckets['present'] |= emp
            elif emp.id in leave_ids:
                buckets['on_leave'] |= emp
            else:
                buckets['absent'] |= emp
        return buckets

    def _dashboard_attendance_lookup(self, employees, d):
        """(present_ids, leave_ids) sets for `employees` on calendar date `d`."""
        emp_ids = tuple(employees.ids) if employees else (0,)

        self.env.cr.execute("""
            SELECT DISTINCT employee_id
            FROM   hr_attendance
            WHERE  employee_id IN %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date = %s
        """, (emp_ids, TZ, d))
        present_ids = {r[0] for r in self.env.cr.fetchall()}

        self.env.cr.execute("""
            SELECT DISTINCT employee_id
            FROM   hr_leave
            WHERE  employee_id IN %s
              AND  state = 'validate'
              AND  request_date_from <= %s
              AND  request_date_to   >= %s
        """, (emp_ids, d, d))
        leave_ids = {r[0] for r in self.env.cr.fetchall()}

        return present_ids, leave_ids

    def _dashboard_departure_history_emp_ids(self, action, month_start, month_end):
        """Distinct employee ids with a `action` ('departed'/'reactivated')
        event in employee.departure.history within [month_start, month_end].
        """
        history = self.env['employee.departure.history'].search([
            ('action', '=', action),
            ('event_date', '>=', month_start),
            ('event_date', '<=', month_end),
        ])
        return list(set(history.mapped('employee_id.id')))

    # ── main KPI dashboard RPC ───────────────────────────────────────────────
    @api.model
    def get_dashboard_kpis(self, selected_date, department_id=None, worker_type=None):
        """
        KPI + chart data for one selected date, scoped by optional department
        and worker-type filters.

        Present/Absent/On Leave are evaluated only for days the employee is
        actually scheduled to work (i.e. not their weekly day-off):
          - present  : an hr.attendance check-in exists that day (Asia/Dhaka
                       calendar date)
          - on_leave : no attendance, but an approved (state='validate')
                       hr.leave covers that day
          - absent   : neither of the above

        New Joined is counted for the calendar month containing
        `selected_date`, using `contract_date_start`. Archived / Reactivated
        are also counted for that same calendar month, sourced from
        employee.departure.history (zk_adms_attendance) — 'departed' /
        'reactivated' events respectively — so picking 5 Sept counts
        September's events, picking 31 Aug counts August's.

        Additional KPIs:
          - checked_in_now  : checked in that day, no check_out yet (still on-site)
          - late_today      : that day's attendance rows flagged is_late
          - day_off_today   : employees whose weekly day-off falls on this weekday
                               (excluded from present/absent/on_leave above)
          - attendance_rate : present / (present + absent), i.e. of everyone
                               actually scheduled to work that day
          - regular_total / daily_total : headcount by worker_type (independent
                               of the selected day — a snapshot, not a daily stat)
          - last_regular_badge / last_daily_badge : the highest zk_badge_no
                               (numeric) among employees of that worker_type
          - no_badge        : active employees with no zk_badge_no set yet
        """
        try:
            d = fields.Date.from_string(selected_date) if selected_date else fields.Date.context_today(self)
        except (TypeError, ValueError):
            d = fields.Date.context_today(self)

        month_start = d.replace(day=1)
        month_end = d.replace(day=monthrange(d.year, d.month)[1])

        domain = [('active', '=', True)]
        if department_id:
            domain.append(('department_id', '=', department_id))
        if worker_type:
            domain.append(('worker_type', '=', worker_type))

        employees = self.env['hr.employee'].search(domain)
        emp_ids = tuple(employees.ids) if employees else (0,)
        present_ids, leave_ids = self._dashboard_attendance_lookup(employees, d)

        # ── still clocked in (checked in that day, not checked out yet) ─────
        self.env.cr.execute("""
            SELECT COUNT(DISTINCT employee_id)
            FROM   hr_attendance
            WHERE  employee_id IN %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date = %s
              AND  check_out IS NULL
        """, (emp_ids, TZ, d))
        checked_in_now = self.env.cr.fetchone()[0] or 0

        # ── late arrivals that day (woodland_attendance_extend's is_late) ───
        self.env.cr.execute("""
            SELECT COUNT(DISTINCT employee_id)
            FROM   hr_attendance
            WHERE  employee_id IN %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date = %s
              AND  is_late IS TRUE
        """, (emp_ids, TZ, d))
        late_today = self.env.cr.fetchone()[0] or 0

        buckets = self._dashboard_classify_day(employees, d, present_ids, leave_ids)
        present = len(buckets['present'])
        absent = len(buckets['absent'])
        on_leave = len(buckets['on_leave'])
        day_off_today = len(buckets['day_off'])

        by_type = {}
        for wt, label in (('regular', 'Regular'), ('daily', 'Daily')):
            by_type[wt] = {
                'label': label,
                'present': len(buckets['present'].filtered(lambda e, wt=wt: e.worker_type == wt)),
                'absent': len(buckets['absent'].filtered(lambda e, wt=wt: e.worker_type == wt)),
                'on_leave': len(buckets['on_leave'].filtered(lambda e, wt=wt: e.worker_type == wt)),
            }

        scheduled_today = present + absent
        attendance_rate = round((present / scheduled_today) * 100, 1) if scheduled_today else 0.0

        # ── headcount by worker type (independent of the selected day) ──────
        regular_total = self.env['hr.employee'].search_count(domain + [('worker_type', '=', 'regular')])
        daily_total = self.env['hr.employee'].search_count(domain + [('worker_type', '=', 'daily')])

        # ── highest ZK badge no. per worker type (numeric, via zk_badge_no_int
        #    since zk_badge_no is Char and would otherwise sort "9" > "10") ──
        has_numeric_badge = [('zk_badge_no', '!=', False), ('zk_badge_no', '!=', '')]
        last_regular = self.env['hr.employee'].search(
            domain + [('worker_type', '=', 'regular')] + has_numeric_badge,
            order='zk_badge_no_int desc', limit=1
        )
        last_daily = self.env['hr.employee'].search(
            domain + [('worker_type', '=', 'daily')] + has_numeric_badge,
            order='zk_badge_no_int desc', limit=1
        )

        # ── employees with no ZK badge assigned yet (data-quality flag) ─────
        no_badge = self.env['hr.employee'].search_count(
            domain + ['|', ('zk_badge_no', '=', False), ('zk_badge_no', '=', '')]
        )

        # ── new joined this month (contract start date, not joining_date) ───
        new_joined = self.env['hr.employee'].search_count(domain + [
            ('contract_date_start', '>=', month_start),
            ('contract_date_start', '<=', month_end),
        ])

        # ── archived / reactivated this month (employee.departure.history) ──
        dept_wt_domain = []
        if department_id:
            dept_wt_domain.append(('department_id', '=', department_id))
        if worker_type:
            dept_wt_domain.append(('worker_type', '=', worker_type))

        def _history_count(action):
            emp_ids_hist = self._dashboard_departure_history_emp_ids(action, month_start, month_end)
            if not emp_ids_hist:
                return 0
            return self.env['hr.employee'].with_context(active_test=False).search_count(
                dept_wt_domain + [('id', 'in', emp_ids_hist)]
            )

        archived = _history_count('departed')
        reactivated = _history_count('reactivated')

        # ── headcount per department ─────────────────────────────────────────
        dept_rows = self.env['hr.employee']._read_group(
            domain,
            groupby=['department_id'],
            aggregates=['__count'],
        )
        headcount_by_department = sorted([
            {
                'department': dept.name if dept else 'No Department',
                'department_id': dept.id if dept else False,
                'employee_count': count,
            }
            for dept, count in dept_rows
        ], key=lambda r: r['employee_count'], reverse=True)

        return {
            'date': fields.Date.to_string(d),
            'total_employees': len(employees),
            'present': present,
            'absent': absent,
            'on_leave': on_leave,
            'day_off_today': day_off_today,
            'checked_in_now': checked_in_now,
            'late_today': late_today,
            'attendance_rate': attendance_rate,
            'regular_total': regular_total,
            'daily_total': daily_total,
            'regular_present': by_type['regular']['present'],
            'daily_present': by_type['daily']['present'],
            'last_regular_badge': last_regular.zk_badge_no or '—',
            'last_regular_name': last_regular.name or '',
            'last_daily_badge': last_daily.zk_badge_no or '—',
            'last_daily_name': last_daily.name or '',
            'no_badge': no_badge,
            'new_joined': new_joined,
            'archived': archived,
            'reactivated': reactivated,
            'presence_by_worker_type': [by_type['regular'], by_type['daily']],
            'headcount_by_department': headcount_by_department,
        }

    # ── click-through: exact employee ids behind one KPI card ───────────────
    @api.model
    def get_kpi_employee_ids(self, kpi_key, selected_date, department_id=None, worker_type=None):
        """Recompute just the employee ids behind one dashboard KPI, so a
        clicked card can open the employee list pre-filtered to exactly
        what the number on the card represents.
        """
        try:
            d = fields.Date.from_string(selected_date) if selected_date else fields.Date.context_today(self)
        except (TypeError, ValueError):
            d = fields.Date.context_today(self)

        month_start = d.replace(day=1)
        month_end = d.replace(day=monthrange(d.year, d.month)[1])

        base_domain = []
        if department_id:
            base_domain.append(('department_id', '=', department_id))
        if worker_type:
            base_domain.append(('worker_type', '=', worker_type))

        Employee = self.env['hr.employee']
        live_domain = base_domain + [('active', '=', True)]

        static_extra_domains = {
            'total': [],
            'regular': [('worker_type', '=', 'regular')],
            'daily': [('worker_type', '=', 'daily')],
            'no_badge': ['|', ('zk_badge_no', '=', False), ('zk_badge_no', '=', '')],
            'joined': [
                ('contract_date_start', '>=', month_start),
                ('contract_date_start', '<=', month_end),
            ],
        }
        if kpi_key in static_extra_domains:
            return Employee.search(live_domain + static_extra_domains[kpi_key]).ids

        if kpi_key in ('archived', 'reactivated'):
            action = 'departed' if kpi_key == 'archived' else 'reactivated'
            emp_ids_hist = self._dashboard_departure_history_emp_ids(action, month_start, month_end)
            if not emp_ids_hist:
                return []
            return Employee.with_context(active_test=False).search(
                base_domain + [('id', 'in', emp_ids_hist)]
            ).ids

        if kpi_key in ('last_regular', 'last_daily'):
            wt = 'regular' if kpi_key == 'last_regular' else 'daily'
            has_numeric_badge = [('zk_badge_no', '!=', False), ('zk_badge_no', '!=', '')]
            emp = Employee.search(
                live_domain + [('worker_type', '=', wt)] + has_numeric_badge,
                order='zk_badge_no_int desc', limit=1,
            )
            return emp.ids

        # Day-based buckets: present / absent / leave / day-off / checked-in / late
        employees = Employee.search(live_domain)
        emp_ids_tuple = tuple(employees.ids) if employees else (0,)

        if kpi_key == 'checkedin':
            self.env.cr.execute("""
                SELECT DISTINCT employee_id
                FROM   hr_attendance
                WHERE  employee_id IN %s
                  AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date = %s
                  AND  check_out IS NULL
            """, (emp_ids_tuple, TZ, d))
            return [r[0] for r in self.env.cr.fetchall()]

        if kpi_key == 'late':
            self.env.cr.execute("""
                SELECT DISTINCT employee_id
                FROM   hr_attendance
                WHERE  employee_id IN %s
                  AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date = %s
                  AND  is_late IS TRUE
            """, (emp_ids_tuple, TZ, d))
            return [r[0] for r in self.env.cr.fetchall()]

        present_ids, leave_ids = self._dashboard_attendance_lookup(employees, d)
        buckets = self._dashboard_classify_day(employees, d, present_ids, leave_ids)

        bucket_by_key = {
            'present': 'present',
            'absent': 'absent',
            'leave': 'on_leave',
            'dayoff': 'day_off',
        }
        if kpi_key in bucket_by_key:
            return buckets[bucket_by_key[kpi_key]].ids

        return []
