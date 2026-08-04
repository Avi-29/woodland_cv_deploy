# -*- coding: utf-8 -*-
import re
import pandas
from datetime import date, timedelta
from odoo import api, fields, models
from odoo.http import request
from odoo.tools import date_utils

LEAVE_COLORS = {
    1: "#F06050", 2: "#F4A460", 3: "#F7CD1F", 4: "#6CC1ED",
    5: "#814968", 6: "#EB7E7F", 7: "#2C8397", 8: "#475577",
    9: "#D6145F", 10: "#30C381", 11: "#9365B8",
}

DAY_OFF_MAP = {
    '0': 'Monday', '1': 'Tuesday', '2': 'Wednesday', '3': 'Thursday',
    '4': 'Friday', '5': 'Saturday', '6': 'Sunday',
}


class HrEmployee(models.Model):
    _inherit = 'hr.employee'
    _check_company_auto = True

    # ── helpers ───────────────────────────────────────────────────────────────
    def _get_allowed_company_ids(self):
        cids = request.httprequest.cookies.get('cids', '')
        split_cids = re.split(r'[,-]', cids)
        result = [int(c) for c in split_cids if c.isdigit()]
        # Fallback: if no company from cookies, use current user's company
        if not result:
            result = [self.env.company.id]
        return result

    def _build_dates(self, year, month):
        """Return list of YYYY-MM-DD strings for the given year+month."""
        try:
            y, m = int(year), int(month)
        except (TypeError, ValueError):
            today = fields.Date.today()
            y, m = today.year, today.month
        start = date(y, m, 1)
        # last day of month
        if m == 12:
            end = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(y, m + 1, 1) - timedelta(days=1)
        return pandas.date_range(start, end, freq='D').strftime(
            "%Y-%m-%d").tolist()

    # ── leave types available (for popup dropdowns) ───────────────────────────
    @api.model
    def get_leave_types(self):
        types = self.env['hr.leave.type'].search([
            ('requires_allocation', '!=', 'yes'),
        ], order='name')
        return [{'id': lt.id, 'name': lt.name} for lt in types]

    # ── get available departments ────────────────────────────────────────────
    @api.model
    def get_departments(self):
        """Get list of available departments for filter."""
        allowed_company_ids = self._get_allowed_company_ids()
        depts = self.env['hr.department'].search(
            [],
            order='name'
        )
        return [{'id': d.id, 'name': d.name} for d in depts]

    # ── get available worker types ─────────────────────────────────────────────
    @api.model
    def get_worker_types(self):
        """Get list of available worker types for filter."""
        return [
            {'id': 'daily', 'name': 'Daily'},
            {'id': 'regular', 'name': 'Regular'},
        ]

    # ── quick create leave (from drag-select popup) ───────────────────────────
    @api.model
    def create_leave_from_dashboard(self, employee_id, date_from, date_to,
                                    holiday_status_id):
        """Create and submit a leave request. Returns the new leave id."""
        leave = self.env['hr.leave'].create({
            'employee_id': employee_id,
            'holiday_status_id': holiday_status_id,
            'request_date_from': date_from,
            'request_date_to': date_to,
            'date_from': date_from + ' 00:00:00',
            'date_to': date_to + ' 23:59:59',
        })
        return leave.id

    # ── main dashboard RPC ────────────────────────────────────────────────────
    @api.model
    def get_employee_leave_data(self, year, month,
                                search='', page=1, per_page=20, department_id=None,
                                worker_type=None):
        """
        Args:
            year         (int|str): e.g. 2025
            month        (int|str): 1-12
            search       (str)    : employee name or barcode/badge
            page         (int)    : 1-based
            per_page     (int)    : rows per page
            department_id (int|None): filter by department
            worker_type  (str|None): filter by worker type ('daily' or 'regular')
        """
        TZ = 'Asia/Dhaka'

        dates = self._build_dates(year, month)
        if not dates:
            return {
                'employee_data': [], 'filtered_duration_dates': [],
                'total_count': 0, 'page': page,
                'per_page': per_page, 'total_pages': 0,
            }

        allowed_company_ids = self.env.companies.ids or [self.env.company.id]
        domain = []
        if department_id:
            domain.append(('department_id', '=', department_id))
        if worker_type:
            domain.append(('worker_type', '=', worker_type))
        if search:
            domain += ['|',
                       ('name', 'ilike', search),
                       ('zk_badge_no', 'ilike', search)]

        all_employees = self.env['hr.employee'].search(domain)
        all_employees = all_employees.sorted(key=lambda e: int(e.zk_badge_no) if e.zk_badge_no and e.zk_badge_no.isdigit() else 0)
        total_count = len(all_employees)
        total_pages = max(1, -(-total_count // per_page))

        offset = (page - 1) * per_page
        employees = all_employees[offset: offset + per_page]

        date_from = min(dates)
        date_to = max(dates)

        emp_ids = tuple(employees.ids) if employees else (0,)

        # ── leaves ───────────────────────────────────────────────────────────
        # request_date_from/to are plain Date fields (no TZ), safe to use directly
        self.env.cr.execute("""
                    SELECT hl.id       AS leave_id,
                           hl.employee_id,
                           hl.request_date_from::text AS request_date_from,
                           hl.request_date_to::text   AS request_date_to,
                           hlt.leave_code,
                           hlt.color,
                           hlt.name   AS leave_type_name
                    FROM   hr_leave hl
                    JOIN   hr_leave_type hlt ON hlt.id = hl.holiday_status_id
                    WHERE  hl.state = 'validate'
                      AND  hl.employee_id IN %s
                      AND  hl.request_date_from <= %s
                      AND  hl.request_date_to   >= %s
                """, (emp_ids, date_to, date_from))
        leaves_by_emp = {}
        for lv in self.env.cr.dictfetchall():
            leaves_by_emp.setdefault(lv['employee_id'], []).append(lv)

        # ── public holidays in range ──────────────────────────────────────────
        # date_from/date_to on resource.calendar.leaves are Datetime (UTC).
        # Convert them to Asia/Dhaka before expanding into individual days.
        self.env.cr.execute("""
            SELECT
                gs.day::date::text AS date,
                rcl.name
            FROM resource_calendar_leaves rcl
            CROSS JOIN LATERAL generate_series(
                (rcl.date_from AT TIME ZONE 'UTC' AT TIME ZONE %s)::date,
                (rcl.date_to   AT TIME ZONE 'UTC' AT TIME ZONE %s)::date,
                interval '1 day'
            ) AS gs(day)
            WHERE rcl.resource_id IS NULL
              AND gs.day::date BETWEEN
                    (%s::timestamptz AT TIME ZONE %s)::date
                AND (%s::timestamptz AT TIME ZONE %s)::date
        """, (TZ, TZ, date_from, TZ, date_to, TZ))

        public_holidays = {
            r['date']: r['name']
            for r in self.env.cr.dictfetchall()
        }

        # ── swap days ─────────────────────────────────────────────────────────
        # swap_work_date and swap_off_date are plain Date fields — no TZ needed
        self.env.cr.execute("""
                    SELECT employee_id,
                           swap_work_date::text AS work_date,
                           swap_off_date::text  AS off_date
                    FROM   hr_swap
                    WHERE  employee_id IN %s
                      AND  (swap_work_date BETWEEN %s AND %s
                            OR swap_off_date BETWEEN %s AND %s)
                """, (emp_ids, date_from, date_to, date_from, date_to))
        swaps_by_emp = {}
        for sw in self.env.cr.dictfetchall():
            swaps_by_emp.setdefault(sw['employee_id'], {
                'work_dates': set(), 'off_dates': set(), 'off_to_work': {}
            })
            swaps_by_emp[sw['employee_id']]['work_dates'].add(sw['work_date'])
            swaps_by_emp[sw['employee_id']]['off_dates'].add(sw['off_date'])
            swaps_by_emp[sw['employee_id']]['off_to_work'][sw['off_date']] = sw['work_date']

        # ── attendance ────────────────────────────────────────────────────────
        # check_in is stored as UTC Datetime — convert to Asia/Dhaka for date bucketing
        self.env.cr.execute("""
                    SELECT id, employee_id,
                           (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date::text AS check_date,
                           is_late,
                           marked_as_day_off
                    FROM   hr_attendance
                    WHERE  employee_id IN %s
                      AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date
                           BETWEEN %s AND %s
                """, (TZ, emp_ids, TZ, date_from, date_to))
        att_by_emp = {}
        late_by_emp = {}
        dayoff_by_emp = {}
        for att in self.env.cr.dictfetchall():
            key = att['check_date']  # already a plain YYYY-MM-DD string
            att_by_emp.setdefault(att['employee_id'], {})[key] = att['id']
            if att['is_late']:
                late_by_emp.setdefault(att['employee_id'], set()).add(key)
            if att['marked_as_day_off']:
                dayoff_by_emp.setdefault(att['employee_id'], set()).add(key)

        # ── config marks ──────────────────────────────────────────────────────
        res_config = self.env['res.config.settings'].sudo().search([], limit=1, order='id desc')
        present_mark = (res_config.present if res_config else None) or 'P'
        absent_mark = (res_config.absent if res_config else None) or 'A'

        # ── build rows ────────────────────────────────────────────────────────
        employee_data = []
        for employee in employees:
            emp_leaves = leaves_by_emp.get(employee.id, [])
            emp_att = att_by_emp.get(employee.id, {})
            emp_late = late_by_emp.get(employee.id, set())
            emp_dayoff = dayoff_by_emp.get(employee.id, set())
            emp_swaps = swaps_by_emp.get(employee.id, {
                'work_dates': set(), 'off_dates': set(), 'off_to_work': {}
            })

            day_off_idx = None
            raw_dod = getattr(employee, 'day_off_day', None)
            if raw_dod:
                try:
                    day_off_idx = int(raw_dod)
                except ValueError:
                    pass

            # Build leave_date_map: date string → (code, color, leave_id, type_name)
            leave_date_map = {}
            leave_counts = {}
            for lv in emp_leaves:
                lv_range = pandas.date_range(
                    lv['request_date_from'],
                    lv['request_date_to'],
                    freq='d'
                ).strftime("%Y-%m-%d").tolist()
                for d in lv_range:
                    if d in dates:  # only include dates in the viewed month
                        leave_date_map[d] = (
                            lv['leave_code'] or 'L',
                            LEAVE_COLORS.get(lv['color'], '#6CC1ED'),
                            lv['leave_id'],
                            lv['leave_type_name'],
                        )

            leave_data = []
            total_absent_count = 0
            total_present_count = 0
            for d in dates:
                weekday = date.fromisoformat(d).weekday()  # 0=Mon … 6=Sun

                if d in leave_date_map:
                    code, color, leave_id, lt_name = leave_date_map[d]
                    leave_counts[code] = leave_counts.get(code, 0) + 1
                    if code == 'LWP':
                        total_absent_count += 1
                    else:
                        total_present_count += 1
                    leave_data.append({
                        'leave_date': d,
                        'state': code,
                        'color': color,
                        'record_type': 'leave',
                        'record_id': leave_id,
                        'tooltip': lt_name,
                    })

                elif getattr(employee, 'worker_type', None) != 'daily' and d in public_holidays and d not in emp_swaps['work_dates']:
                    if d in emp_att:
                        # Present on a public holiday → PH/P
                        total_present_count += 1
                        leave_data.append({
                            'leave_date': d,
                            'state': 'PH/P',
                            'color': '#80cbc4',
                            'record_type': 'attendance',
                            'record_id': emp_att[d],
                            'is_day_off_or_holiday': True,
                            'tooltip': f"{public_holidays[d]} – Present (OT/Swap eligible) – click to view attendance",
                        })
                    else:
                        leave_data.append({
                            'leave_date': d,
                            'state': 'PH',
                            'color': '#c8e6c9',
                            'record_type': None,
                            'record_id': None,
                            'tooltip': public_holidays[d],
                        })
                        total_present_count += 1

                elif d in emp_swaps['off_dates']:
                    total_present_count += 1
                    co_work_date = emp_swaps['off_to_work'].get(d, '')
                    leave_data.append({
                        'leave_date': d,
                        'state': 'ADJUST',
                        'color': '#e1bee7',
                        'record_type': None,
                        'record_id': None,
                        'work_date': co_work_date,
                        'tooltip': 'Adjust Day (Swap)' + (
                            f' – worked on {co_work_date}' if co_work_date else ''),
                    })

                elif (day_off_idx is not None
                      and weekday == day_off_idx
                      and d not in emp_swaps['work_dates']):
                    total_present_count += 1
                    if d in emp_att:
                        # Present on weekly day-off → OFF/P
                        leave_data.append({
                            'leave_date': d,
                            'state': 'OFF/P',
                            'color': '#78909c',
                            'record_type': 'attendance',
                            'record_id': emp_att[d],
                            'is_day_off_or_holiday': True,
                            'tooltip': 'Weekly Day Off – Present (OT/Swap eligible) – click to view attendance',
                        })
                    else:
                        leave_data.append({
                            'leave_date': d,
                            'state': 'OFF',
                            'color': '#b0bec5',
                            'record_type': 'dayoff',
                            'record_id': None,
                            'tooltip': 'Weekly Day Off – click to request swap',
                        })

                elif d in emp_att:
                    if d in emp_dayoff:
                        leave_data.append({
                            'leave_date': d,
                            'state': 'OFF',
                            'color': '#0000FF',
                            'record_type': 'attendance',
                            'record_id': emp_att[d],
                            'tooltip': 'Marked as Day Off – click to view',
                        })
                    elif d in emp_late:
                        leave_data.append({
                            'leave_date': d,
                            'state': 'L',
                            'color': '#ffb366',
                            'record_type': 'attendance',
                            'record_id': emp_att[d],
                            'tooltip': 'Late – click to view',
                        })
                        total_present_count += 1
                    else:
                        leave_data.append({
                            'leave_date': d,
                            'state': present_mark,
                            'color': '#d4edda',
                            'record_type': 'attendance',
                            'record_id': emp_att[d],
                            'tooltip': 'Present – click to view',
                        })
                        total_present_count += 1

                else:
                    leave_data.append({
                        'leave_date': d,
                        'state': absent_mark,
                        'color': '#fff3cd',
                        'record_type': 'absent',
                        'record_id': None,
                        'tooltip': 'Absent – click or drag to create leave',
                    })
                    total_absent_count += 1

            employee_data.append({
                'id': employee.id,
                'name': employee.name,
                'zk_badge_no': employee.zk_badge_no or '',
                'department': employee.department_id.name if employee.department_id else '—',
                'department_id': employee.department_id.id if employee.department_id else None,
                'day_off': DAY_OFF_MAP.get(str(raw_dod) if raw_dod else '', ''),
                'leave_data': leave_data[::-1],
                'total_absent_count': total_absent_count,
                'total_present_count': total_present_count,
                'leave_counts': leave_counts,
            })

        return {
            'employee_data': employee_data,
            'filtered_duration_dates': dates[::-1],
            'total_count': total_count,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
        }

    # ── yearly leave summary grid (12 months × SL/CL/LWP = 36 columns) ─────
    @api.model
    def get_employee_leave_summary(self, year, search='', page=1, per_page=20,
                                    department_id=None, worker_type=None):
        """
        Per-employee yearly leave-summary grid, paginated like
        get_employee_leave_data.

        For every matching employee, returns 12 month entries (Jan-Dec),
        each holding {sl, cl, lwp} day-counts for that month.

        request_date_from/request_date_to on hr.leave are plain Date
        fields (no UTC component stored), so they already represent the
        Asia/Dhaka calendar date and need no timezone conversion — same
        approach used in get_employee_leave_data above.
        """
        try:
            y = int(year)
        except (TypeError, ValueError):
            y = fields.Date.context_today(self).year

        domain = []
        if department_id:
            domain.append(('department_id', '=', department_id))
        if worker_type:
            domain.append(('worker_type', '=', worker_type))
        if search:
            domain += ['|',
                       ('name', 'ilike', search),
                       ('zk_badge_no', 'ilike', search)]

        all_employees = self.env['hr.employee'].search(domain)
        all_employees = all_employees.sorted(key=lambda e: int(e.zk_badge_no) if e.zk_badge_no and e.zk_badge_no.isdigit() else 0)
        total_count = len(all_employees)
        total_pages = max(1, -(-total_count // per_page))

        offset = (page - 1) * per_page
        employees = all_employees[offset: offset + per_page]
        emp_ids = tuple(employees.ids) if employees else (0,)

        year_start = date(y, 1, 1)
        year_end = date(y, 12, 31)

        self.env.cr.execute("""
            SELECT hl.employee_id,
                   hl.request_date_from::text AS date_from,
                   hl.request_date_to::text   AS date_to,
                   hlt.leave_code
            FROM   hr_leave hl
            JOIN   hr_leave_type hlt ON hlt.id = hl.holiday_status_id
            WHERE  hl.state = 'validate'
              AND  hl.employee_id IN %s
              AND  hl.request_date_from <= %s
              AND  hl.request_date_to   >= %s
        """, (emp_ids, year_end, year_start))

        # emp_id -> {month: {'sl': n, 'cl': n, 'lwp': n}}
        monthly = {}
        code_key = {'SL': 'sl', 'CL': 'cl', 'LWP': 'lwp'}
        for r in self.env.cr.dictfetchall():
            key = code_key.get(r['leave_code'])
            if not key:
                continue
            d_from = max(date.fromisoformat(r['date_from']), year_start)
            d_to = min(date.fromisoformat(r['date_to']), year_end)
            if d_from > d_to:
                continue
            emp_months = monthly.setdefault(
                r['employee_id'],
                {m: {'sl': 0, 'cl': 0, 'lwp': 0} for m in range(1, 13)}
            )
            for d in pandas.date_range(d_from, d_to, freq='d'):
                emp_months[d.month][key] += 1

        employee_data = []
        for employee in employees:
            emp_months = monthly.get(
                employee.id, {m: {'sl': 0, 'cl': 0, 'lwp': 0} for m in range(1, 13)}
            )
            employee_data.append({
                'id': employee.id,
                'name': employee.name,
                'zk_badge_no': employee.zk_badge_no or '',
                'department': employee.department_id.name if employee.department_id else '—',
                'months': [emp_months[m] for m in range(1, 13)],
            })

        return {
            'employee_data': employee_data,
            'total_count': total_count,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
        }

