# -*- coding: utf-8 -*-
import re
import pandas
import pytz
from datetime import date, datetime, timedelta
from odoo import api, fields, models
from odoo.http import request
from odoo.tools import date_utils

DHAKA_TZ = pytz.timezone('Asia/Dhaka')
# Fixed epoch from which raw "Absent" days start counting towards LWP totals.
REALTIME_ABSENT_EPOCH = date(2026, 5, 1)

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

    def _dhaka_today(self):
        """Current date in Asia/Dhaka, independent of server/user timezone."""
        return datetime.now(pytz.utc).astimezone(DHAKA_TZ).date()

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

    def _is_effective_absent_external(self, employee, ext_date_str):
        """Classify a single date OUTSIDE the currently displayed month
        (the day just before the 1st, or just after the last day) as
        effectively-absent or not, for the Sandwich Absent rule's
        month-boundary edge case. Mirrors the same day-classification
        priority used in get_employee_leave_data's main loop, but only
        needs a present/absent boolean for one date, so it queries
        directly instead of reusing the bulk per-month lookups.
        """
        ext_date = date.fromisoformat(ext_date_str)

        has_leave = bool(self.env['hr.leave'].search_count([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('request_date_from', '<=', ext_date_str),
            ('request_date_to', '>=', ext_date_str),
        ]))
        if has_leave:
            return False

        has_swap = bool(self.env['hr.swap'].search_count([
            ('employee_id', '=', employee.id),
            '|',
            ('swap_work_date', '=', ext_date_str),
            ('swap_off_date', '=', ext_date_str),
        ]))
        if has_swap:
            return False

        self.env.cr.execute("""
            SELECT 1
            FROM   hr_attendance
            WHERE  employee_id = %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Dhaka')::date = %s::date
            LIMIT 1
        """, (employee.id, ext_date_str))
        if self.env.cr.fetchone():
            return False

        self.env.cr.execute("""
            SELECT 1
            FROM   resource_calendar_leaves rcl
            WHERE  rcl.resource_id IS NULL
              AND  %s::date BETWEEN
                       (rcl.date_from AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Dhaka')::date
                   AND (rcl.date_to   AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Dhaka')::date
            LIMIT 1
        """, (ext_date_str,))
        is_public_holiday = bool(self.env.cr.fetchone())
        if is_public_holiday:
            return False

        raw_dod = employee.get_day_off_day(ext_date)
        if raw_dod:
            try:
                if ext_date.weekday() == int(raw_dod):
                    return False  # scheduled weekly off, not an absence
            except ValueError:
                pass

        return True  # no leave, no swap, no attendance, not a scheduled non-working day

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

    # ── live "present today" counter (header button) ──────────────────────────
    @api.model
    def get_present_today_count(self, department_id=None, worker_type=None):
        """Count of employees checked in today (Asia/Dhaka), scoped to the
        same department / worker-type filters as the dashboard toolbar.
        """
        domain = []
        if department_id:
            domain.append(('department_id', '=', department_id))
        if worker_type:
            domain.append(('worker_type', '=', worker_type))
        employees = self.env['hr.employee'].search(domain)
        if not employees:
            return {'present_count': 0, 'total_count': 0}

        today_str = self._dhaka_today().isoformat()
        self.env.cr.execute("""
            SELECT DISTINCT employee_id
            FROM   hr_attendance
            WHERE  employee_id IN %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Dhaka')::date::text = %s
        """, (tuple(employees.ids), today_str))
        present_ids = {r[0] for r in self.env.cr.fetchall()}
        return {'present_count': len(present_ids), 'total_count': len(employees)}

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
        today_dhaka = self._dhaka_today()
        today_str = today_dhaka.isoformat()

        employee_data = []
        for employee in employees:
            emp_leaves = leaves_by_emp.get(employee.id, [])
            emp_att = att_by_emp.get(employee.id, {})
            emp_late = late_by_emp.get(employee.id, set())
            emp_dayoff = dayoff_by_emp.get(employee.id, set())
            emp_swaps = swaps_by_emp.get(employee.id, {
                'work_dates': set(), 'off_dates': set(), 'off_to_work': {}
            })
            # Current day-off, for the summary field below (not date-specific).
            current_day_off_raw = employee.day_off_day
            # One query for the whole month instead of one per date below.
            day_off_map = employee.get_day_off_day_map(
                date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))

            # Build leave_date_map: date string → (code, color, leave_id, type_name)
            leave_date_map = {}
            leave_counts = {}
            for lv in emp_leaves:
                lv_range = pandas.date_range(
                    lv['request_date_from'],
                    lv['request_date_to'],
                    freq='D'
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
                is_future = d > today_str  # day hasn't happened yet (Asia/Dhaka)

                day_off_idx = None
                raw_dod = day_off_map[date.fromisoformat(d)]
                if raw_dod:
                    try:
                        day_off_idx = int(raw_dod)
                    except ValueError:
                        pass

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
                            'record_type': 'holiday',
                            'record_id': None,
                            'tooltip': public_holidays[d],
                        })
                        if not is_future:
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
                    if not is_future:
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
                            'marked_as_day_off': d in emp_dayoff,
                            'tooltip': 'Weekly Day Off – Present (OT/Swap eligible)'
                                       + (' – Marked as Day Off' if d in emp_dayoff else '')
                                       + ' – click to view attendance',
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
                            'state': present_mark,
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
                    if not is_future:
                        total_absent_count += 1

            # ── Sandwich Absent rule (mirrors enterprise_shift_payroll) ─────
            # An un-worked weekly day-off OR an un-worked public holiday is
            # also counted as absent when both its neighbouring days are
            # genuine absences. Neighbours outside the viewed month (the
            # 1st/last day's boundary) are looked up directly via
            # _is_effective_absent_external instead of being skipped, so
            # there's no need for a "barely present this month → sandwich
            # everything" blanket fallback the way payroll's month-scoped
            # calculation once needed one — every day-off/holiday here
            # always gets judged on its own actual neighbours.
            by_date = {ld['leave_date']: ld for ld in leave_data}

            def _is_effective_absent(dd):
                entry = by_date.get(dd)
                return bool(entry) and entry['record_type'] == 'absent'

            for d in dates:
                if d > today_str:
                    continue  # don't sandwich a day that hasn't happened yet
                entry = by_date[d]
                # Only a plain, un-worked weekly off or un-worked public
                # holiday can be flipped to Sandwich Absent.
                if entry['record_type'] not in ('dayoff', 'holiday'):
                    continue

                prev_d = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
                next_d = (date.fromisoformat(d) + timedelta(days=1)).isoformat()

                # Edge case: the 1st/last day of the viewed month has no
                # neighbour inside `dates` — look at the previous month's
                # last day / next month's first day instead of silently
                # skipping the check.
                if prev_d in by_date:
                    prev_absent = _is_effective_absent(prev_d)
                else:
                    prev_absent = self._is_effective_absent_external(employee, prev_d)

                if next_d in by_date:
                    next_absent = _is_effective_absent(next_d)
                else:
                    next_absent = self._is_effective_absent_external(employee, next_d)

                sandwich = prev_absent and next_absent

                if sandwich:
                    is_holiday_cell = entry['record_type'] == 'holiday'
                    entry.update({
                        'state': absent_mark,
                        'color': '#fff3cd',
                        'is_sandwich_absent': True,
                        'tooltip': (
                            'Sandwich Absent – public holiday between two absent days (payroll rule) – click to view'
                            if is_holiday_cell else
                            'Sandwich Absent – weekly off between two absent days (payroll rule) – click to request swap'
                        ),
                    })
                    total_present_count -= 1
                    total_absent_count += 1

            employee_data.append({
                'id': employee.id,
                'name': employee.name,
                'zk_badge_no': employee.zk_badge_no or '',
                'department': employee.department_id.name if employee.department_id else '—',
                'department_id': employee.department_id.id if employee.department_id else None,
                'day_off': DAY_OFF_MAP.get(str(current_day_off_raw) if current_day_off_raw else '', ''),
                'is_present_today': today_str in emp_att,
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

    # ── real "Absent" day counts (no leave filed, not a scheduled day off) ──
    def _get_real_absent_counts(self, employees, date_from, date_to):
        """Count genuine unexplained-absence days per employee within
        [date_from, date_to] — no validated leave, no attendance, and not
        a scheduled non-working day (weekly off / public holiday / swap
        off). Mirrors the day-classification used in
        get_employee_leave_data, but only tallies the final "Absent" case.
        """
        if not employees or date_from > date_to:
            return {e.id: 0 for e in employees}

        TZ = 'Asia/Dhaka'
        emp_ids = tuple(employees.ids)
        dates = pandas.date_range(date_from, date_to, freq='D').strftime("%Y-%m-%d").tolist()

        self.env.cr.execute("""
            SELECT hl.employee_id,
                   hl.request_date_from::text AS date_from,
                   hl.request_date_to::text   AS date_to
            FROM   hr_leave hl
            WHERE  hl.state = 'validate'
              AND  hl.employee_id IN %s
              AND  hl.request_date_from <= %s
              AND  hl.request_date_to   >= %s
        """, (emp_ids, date_to, date_from))
        leave_days_by_emp = {}
        for r in self.env.cr.dictfetchall():
            days = pandas.date_range(
                max(date.fromisoformat(r['date_from']), date_from),
                min(date.fromisoformat(r['date_to']), date_to),
                freq='D'
            ).strftime("%Y-%m-%d").tolist()
            leave_days_by_emp.setdefault(r['employee_id'], set()).update(days)

        self.env.cr.execute("""
            SELECT gs.day::date::text AS date
            FROM resource_calendar_leaves rcl
            CROSS JOIN LATERAL generate_series(
                (rcl.date_from AT TIME ZONE 'UTC' AT TIME ZONE %s)::date,
                (rcl.date_to   AT TIME ZONE 'UTC' AT TIME ZONE %s)::date,
                interval '1 day'
            ) AS gs(day)
            WHERE rcl.resource_id IS NULL
              AND gs.day::date BETWEEN %s AND %s
        """, (TZ, TZ, date_from, date_to))
        public_holidays = {r['date'] for r in self.env.cr.dictfetchall()}

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
            s = swaps_by_emp.setdefault(sw['employee_id'], {'work_dates': set(), 'off_dates': set()})
            s['work_dates'].add(sw['work_date'])
            s['off_dates'].add(sw['off_date'])

        self.env.cr.execute("""
            SELECT employee_id,
                   (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date::text AS check_date
            FROM   hr_attendance
            WHERE  employee_id IN %s
              AND  (check_in AT TIME ZONE 'UTC' AT TIME ZONE %s)::date BETWEEN %s AND %s
        """, (TZ, emp_ids, TZ, date_from, date_to))
        att_days_by_emp = {}
        for a in self.env.cr.dictfetchall():
            att_days_by_emp.setdefault(a['employee_id'], set()).add(a['check_date'])

        result = {}
        for employee in employees:
            emp_leave_days = leave_days_by_emp.get(employee.id, set())
            emp_att_days = att_days_by_emp.get(employee.id, set())
            emp_swaps = swaps_by_emp.get(employee.id, {'work_dates': set(), 'off_dates': set()})
            # One query for the whole range instead of one per date below.
            day_off_map = employee.get_day_off_day_map(date_from, date_to)

            absent_count = 0
            for d in dates:
                if d in emp_leave_days or d in emp_att_days:
                    continue
                d_date = date.fromisoformat(d)
                weekday = d_date.weekday()
                if (getattr(employee, 'worker_type', None) != 'daily'
                        and d in public_holidays and d not in emp_swaps['work_dates']):
                    continue
                if d in emp_swaps['off_dates']:
                    continue
                day_off_idx = None
                raw_dod = day_off_map[d_date]
                if raw_dod:
                    try:
                        day_off_idx = int(raw_dod)
                    except ValueError:
                        pass
                if (day_off_idx is not None and weekday == day_off_idx
                        and d not in emp_swaps['work_dates']):
                    continue
                absent_count += 1
            result[employee.id] = absent_count
        return result

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
            for d in pandas.date_range(d_from, d_to, freq='D'):
                emp_months[d.month][key] += 1

        # ── real-time Absent → LWP top-up (per month, Total follows) ────────
        # From REALTIME_ABSENT_EPOCH (2026-05-01) up to (but excluding) the
        # current Dhaka month, any unexplained absent day (no leave filed)
        # is added into that same month's LWP count. The current month is
        # skipped since it is still in progress and its absences aren't
        # final yet. Only relevant while viewing the current Dhaka year —
        # the Total column is just the sum of the (now topped-up) months,
        # so it stays consistent automatically.
        today_dhaka = self._dhaka_today()
        if y == today_dhaka.year:
            current_month_start = date(today_dhaka.year, today_dhaka.month, 1)
            range_start = max(REALTIME_ABSENT_EPOCH, year_start)
            range_end = min(current_month_start - timedelta(days=1), year_end)
            cursor = range_start
            while cursor <= range_end:
                if cursor.month == 12:
                    month_last = date(cursor.year, 12, 31)
                else:
                    month_last = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
                month_end = min(month_last, range_end)

                month_absent = self._get_real_absent_counts(employees, cursor, month_end)
                for emp_id, cnt in month_absent.items():
                    if not cnt:
                        continue
                    emp_months = monthly.setdefault(
                        emp_id, {m: {'sl': 0, 'cl': 0, 'lwp': 0} for m in range(1, 13)}
                    )
                    emp_months[cursor.month]['lwp'] += cnt

                cursor = (date(cursor.year + 1, 1, 1) if cursor.month == 12
                          else date(cursor.year, cursor.month + 1, 1))

        employee_data = []
        for employee in employees:
            emp_months = monthly.get(
                employee.id, {m: {'sl': 0, 'cl': 0, 'lwp': 0} for m in range(1, 13)}
            )
            months_list = [emp_months[m] for m in range(1, 13)]
            employee_data.append({
                'id': employee.id,
                'name': employee.name,
                'zk_badge_no': employee.zk_badge_no or '',
                'department': employee.department_id.name if employee.department_id else '—',
                'months': months_list,
                'total': {
                    'sl': sum(m['sl'] for m in months_list),
                    'cl': sum(m['cl'] for m in months_list),
                    'lwp': sum(m['lwp'] for m in months_list),
                },
            })

        return {
            'employee_data': employee_data,
            'total_count': total_count,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
        }

