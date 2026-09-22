from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date, datetime, time, timedelta
from calendar import monthrange
import base64
import io
import logging
import math
import pytz

try:
    import xlsxwriter
    from xlsxwriter.utility import xl_rowcol_to_cell
except ImportError:
    xlsxwriter = None
    xl_rowcol_to_cell = None

_logger = logging.getLogger(__name__)

DHAKA_TZ = pytz.timezone('Asia/Dhaka')


def _to_dhaka(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(DHAKA_TZ)


def _dhaka_day_to_utc_range(local_date):
    """Return (utc_start, utc_end) naive UTC datetimes for a full Dhaka calendar day."""
    aware_start = DHAKA_TZ.localize(datetime.combine(local_date, time.min))
    aware_end = DHAKA_TZ.localize(datetime.combine(local_date, time.max))
    return (
        aware_start.astimezone(pytz.utc).replace(tzinfo=None),
        aware_end.astimezone(pytz.utc).replace(tzinfo=None),
    )


MONTH_SELECTION = [
    ('1', 'January'),
    ('2', 'February'),
    ('3', 'March'),
    ('4', 'April'),
    ('5', 'May'),
    ('6', 'June'),
    ('7', 'July'),
    ('8', 'August'),
    ('9', 'September'),
    ('10', 'October'),
    ('11', 'November'),
    ('12', 'December'),
]


# ═══════════════════════════════════════════════════════════════════
#  Payslip
# ═══════════════════════════════════════════════════════════════════

class PayrollPayslip(models.Model):
    _name = 'payroll.payslip'
    _description = 'Employee Payslip'
    _order = 'payroll_month desc, employee_id'
    _rec_name = 'display_name'

    # ── identity ──────────────────────────────────────────────────────
    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company,
    )

    # ── Month ─────────────────────────────────────────────────────────
    payroll_month = fields.Date(
        string='Payroll Month',
        required=True,
        help="First day of the payroll month (auto-set to 1st of month).",
    )
    month = fields.Selection(
        MONTH_SELECTION,
        string='Month',
        compute='_compute_month',
        store=True,
        group_expand='_group_expand_month',
    )

    # Derived date range
    date_from = fields.Date(string='Period From', compute='_compute_date_range', store=True)
    date_to = fields.Date(string='Period To', compute='_compute_date_range', store=True)

    display_name = fields.Char(compute='_compute_display_name', store=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('computed', 'Computed'),
        ('validated', 'Validated'),
        ('waiting_payment', 'Waiting Payment'),
        ('paid', 'Paid'),
    ], default='draft', string='Status', index=True)

    # ── wage ──────────────────────────────────────────────────────────
    monthly_wage = fields.Float(string='Monthly Wage', digits=(16, 0))

    calendar_days_in_period = fields.Integer(string='Calendar Days in Month')
    day_off_count = fields.Integer(string='Weekly Off Days in Month')
    working_days_in_period = fields.Integer(string='Working Days in Month')
    per_day_rate = fields.Float(string='Per Day Rate (Wage÷CalDays)', digits=(16, 4))

    # ── salary components ─────────────────────────────────────────────
    basic_pct = fields.Float(string='Basic %', digits=(5, 2))
    hra_pct = fields.Float(string='HRA %', digits=(5, 2))
    travel_pct = fields.Float(string='Travel %', digits=(5, 2))
    medical_pct = fields.Float(string='Medical %', digits=(5, 2))

    basic_amount = fields.Float(string='Basic', digits=(16, 0))
    hra_amount = fields.Float(string='HRA', digits=(16, 0))
    travel_amount = fields.Float(string='Travel', digits=(16, 0))
    medical_amount = fields.Float(string='Medical', digits=(16, 0))
    gross_salary = fields.Float(string='Gross Salary', digits=(16, 0))

    # ── attendance ────────────────────────────────────────────────────
    public_holiday_days = fields.Integer(string='Public Holidays')
    present_days = fields.Integer(string='Present Days')
    approved_leave_days = fields.Integer(string='Approved Paid Leave Days')
    unpaid_leave_days = fields.Integer(string='Unpaid Leave Days')
    absent_days = fields.Integer(string='Absent Days')
    late_days = fields.Integer(string='Late Days')

    # ── overtime (stored for reference, NOT included in net pay) ─────
    # Overtime is paid separately via the OT Export wizard.
    overtime_hours = fields.Float(string='Overtime Hours', digits=(16, 2))
    hourly_rate = fields.Float(string='Hourly Rate', digits=(16, 4),
                               help='Per-day rate ÷ 8. Stored during main compute.')
    overtime_pay = fields.Float(string='Overtime Pay', digits=(16, 0),
                                help='Stored for reference only — not included in net salary.')

    # ── deductions ────────────────────────────────────────────────────
    absent_deduction = fields.Float(string='Absent Deduction', digits=(16, 0))
    late_deduction = fields.Float(string='Late Deduction', digits=(16, 0))
    unpaid_leave_deduction = fields.Float(string='Unpaid Leave Deduction', digits=(16, 0))
    total_deductions = fields.Float(string='Total Deductions', digits=(16, 0))

    # ── adjustments (from payroll.adjustment, summed per employee/month) ──
    last_month_due = fields.Float(
        string='Last Month Due', digits=(16, 0),
        help='Sum of "Last Month Due" entries for this employee/month. Added to net pay.',
    )
    penalty_amount = fields.Float(
        string='Penalty', digits=(16, 0),
        help='Penalty actually deducted this month (capped at available salary).',
    )
    advance_amount = fields.Float(
        string='Advance Payment', digits=(16, 0),
        help='Advance actually deducted this month (capped at available salary).',
    )

    # ── net (NO overtime included) ────────────────────────────────────
    net_salary = fields.Float(string='Net Salary', digits=(16, 0))
    notes = fields.Text(string='Notes')

    # ── bonus ─────────────────────────────────────────────────────────
    attendance_bonus = fields.Float(string='Attendance Bonus', digits=(16, 0))
    absent_deduct_per_day = fields.Float(string='Absent Deduct/Day', digits=(16, 4),
                                         help='1× or 2× per_day based on department bonus eligibility.')
    adjust_days = fields.Integer(string='ADJUST Days (used as present)')
    lwp_days = fields.Integer(string='LWP Days')
    genuine_absent_days = fields.Integer(
        string='Genuine Absent Days',
        help='No-show absences with no attendance/leave/adjust record. '
             'Eligible for the 2× cut in bonus-eligible departments.',
    )
    flat_absent_days = fields.Integer(
        string='Flat Absent Days',
        help='Sandwich-rule absences (currently the only contributor — a '
             'marked_as_day_off attendance is neutral, not counted here). '
             'Always cut at 1×, regardless of department bonus eligibility.',
    )
    sandwich_absent_days = fields.Integer(
        string='Sandwich Absent Days',
        help='An un-worked weekly day-off/holiday penalised because both '
             'neighbouring days were absent. Currently the sole component '
             'of Flat Absent Days.',
    )

    # ── month-lock guards ────────────────────────────────────────────
    # A locked payroll.month.lock record blocks all four ways a payslip
    # could change for that month: create, write (edit/state-change),
    # unlink, and recompute (_compute_payslip, guarded separately below
    # since it also needs to fail before doing expensive attendance
    # analysis, not just at the final self.write()).

    def _check_month_not_locked(self, action_label):
        Lock = self.env['payroll.month.lock']
        for rec in self:
            if rec.payroll_month and Lock._is_locked(rec.company_id, rec.payroll_month):
                raise UserError(
                    f"Payslip '{rec.display_name}' is in a locked payroll "
                    f"month ({rec.payroll_month.strftime('%B %Y')}) and "
                    f"cannot be {action_label}. Unlock the month first via "
                    f"Payroll → Configuration → Lock Payroll Month."
                )

    @api.model_create_multi
    def create(self, vals_list):
        Lock = self.env['payroll.month.lock']
        for vals in vals_list:
            month = fields.Date.to_date(vals.get('payroll_month'))
            if not month:
                continue
            company_id = vals.get('company_id')
            company = (self.env['res.company'].browse(company_id)
                       if company_id else self.env.company)
            if Lock._is_locked(company, month):
                raise UserError(
                    f"Payroll month {month.strftime('%B %Y')} is locked. "
                    f"A payslip cannot be created for it. Unlock the month "
                    f"first via Payroll → Configuration → Lock Payroll Month."
                )
        return super().create(vals_list)

    def write(self, vals):
        self._check_month_not_locked('modified')
        new_month = fields.Date.to_date(vals.get('payroll_month'))
        if new_month:
            Lock = self.env['payroll.month.lock']
            for rec in self:
                if Lock._is_locked(rec.company_id, new_month):
                    raise UserError(
                        f"Payroll month {new_month.strftime('%B %Y')} is "
                        f"locked. This payslip cannot be moved into it."
                    )
        return super().write(vals)

    def unlink(self):
        self._check_month_not_locked('deleted')
        return super().unlink()

    # ── computes ──────────────────────────────────────────────────────

    @api.depends('payroll_month')
    def _compute_date_range(self):
        for rec in self:
            if rec.payroll_month:
                first = rec.payroll_month.replace(day=1)
                last_day = monthrange(first.year, first.month)[1]
                rec.date_from = first
                rec.date_to = first.replace(day=last_day)
            else:
                rec.date_from = False
                rec.date_to = False

    @api.depends('payroll_month')
    def _compute_month(self):
        for rec in self:
            rec.month = str(rec.payroll_month.month) if rec.payroll_month else False

    @api.depends('employee_id', 'payroll_month')
    def _compute_display_name(self):
        for rec in self:
            emp = rec.employee_id.name or ''
            month = rec.payroll_month.strftime('%B %Y') if rec.payroll_month else ''
            rec.display_name = f"{emp} ({month})" if emp else 'New Payslip'

    @api.model
    def _group_expand_month(self, states, domain, order):
        return [key for key, _val in MONTH_SELECTION]

    @api.onchange('employee_id')
    def _onchange_employee(self):
        if self.employee_id:
            ref_date = self.payroll_month or fields.Date.context_today(self)
            self.monthly_wage = self.employee_id.get_wage(ref_date) or 0.0

    @api.onchange('payroll_month')
    def _onchange_payroll_month(self):
        if self.payroll_month:
            self.payroll_month = self.payroll_month.replace(day=1)

    # ── working-day helpers ───────────────────────────────────────────

    def _working_days_for_period(self, employee, date_from, date_to):
        calendar_days = (date_to - date_from).days + 1
        day_off_map = employee.get_day_off_day_map(date_from, date_to)
        off_count = 0
        cursor = date_from
        while cursor <= date_to:
            day_off = day_off_map[cursor]
            if day_off and cursor.weekday() == int(day_off):
                off_count += 1
            cursor += timedelta(days=1)
        return calendar_days, off_count, calendar_days - off_count

    def _get_public_holiday_dates(self, date_from, date_to):
        try:
            records = self.env['resource.calendar.leaves'].search([
                ('resource_id', '=', False),
                ('date_from', '<=', str(date_to) + ' 23:59:59'),
                ('date_to', '>=', str(date_from)),
            ])
            holiday_dates = set()
            for r in records:
                start = max(_to_dhaka(r.date_from).date(), date_from)
                end = min(_to_dhaka(r.date_to).date(), date_to)
                cursor = start
                while cursor <= end:
                    holiday_dates.add(cursor)
                    cursor += timedelta(days=1)
            return holiday_dates
        except Exception:
            _logger.warning("Public holiday model not found.")
            return set()

    def _get_all_working_dates(self, employee, date_from, date_to):
        day_off_map = employee.get_day_off_day_map(date_from, date_to)
        dates = set()
        cursor = date_from
        while cursor <= date_to:
            day_off = day_off_map[cursor]
            if not day_off or cursor.weekday() != int(day_off):
                dates.add(cursor)
            cursor += timedelta(days=1)
        return dates

    def _is_effective_absent_external(self, employee, ext_date):
        """Classify a single date OUTSIDE the current payroll period (the
        day just before date_from, or just after date_to) as
        effectively-absent or not, for the Sandwich Absent rule's
        month-boundary edge case.
        """
        utc_from, utc_to = _dhaka_day_to_utc_range(ext_date)
        has_attendance = bool(self.env['hr.attendance'].search_count([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', utc_from),
            ('check_in', '<=', utc_to),
        ]))
        if has_attendance:
            return False

        has_leave = bool(self.env['hr.leave'].search_count([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('date_from', '<=', str(ext_date)),
            ('date_to', '>=', str(ext_date)),
        ]))
        if has_leave:
            return False

        has_swap_off = bool(self.env['hr.swap'].search_count([
            ('employee_id', '=', employee.id),
            ('swap_off_date', '=', ext_date),
        ]))
        if has_swap_off:
            return False

        if ext_date in self._get_public_holiday_dates(ext_date, ext_date):
            return False

        day_off = employee.get_day_off_day(ext_date)
        if day_off and ext_date.weekday() == int(day_off):
            return False

        return True

    # ── main compute ──────────────────────────────────────────────────

    def action_compute(self):
        """Compute selected payslips, skipping any whose employee is
        archived/inactive or whose payroll month is locked. A
        notification lists any skipped payslips instead of hard-failing
        the whole batch."""
        archived_slips = self.filtered(lambda s: not s.employee_id.active)
        remaining_slips = self - archived_slips

        Lock = self.env['payroll.month.lock']
        locked_slips = remaining_slips.filtered(
            lambda s: Lock._is_locked(s.company_id, s.payroll_month)
        )
        computable_slips = remaining_slips - locked_slips

        for slip in computable_slips:
            slip._compute_payslip()

        messages = []
        if archived_slips:
            names = ', '.join(archived_slips.mapped('employee_id.name'))
            messages.append(
                f"Skipped {len(archived_slips)} payslip(s) for archived/inactive "
                f"employee(s): {names}"
            )
        if locked_slips:
            months = ', '.join(sorted(set(
                s.payroll_month.strftime('%B %Y') for s in locked_slips
            )))
            messages.append(
                f"Skipped {len(locked_slips)} payslip(s) in locked month(s): {months}"
            )
        if messages:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Some payslips were skipped',
                    'message': ' '.join(messages),
                    'type': 'warning',
                    'sticky': True,
                },
            }
        return True

    def _compute_payslip(self):
        self.ensure_one()
        employee = self.employee_id
        if not employee:
            raise UserError("No employee linked to this payslip.")
        if not employee.active:
            raise UserError(
                f"Employee '{employee.name}' is archived/inactive. "
                f"Cannot compute a payslip for this employee."
            )
        if self.env['payroll.month.lock']._is_locked(self.company_id, self.payroll_month):
            raise UserError(
                f"Payroll month {self.payroll_month.strftime('%B %Y')} is "
                f"locked and cannot be (re)computed. Unlock it first from "
                f"the Generate Monthly Payslips wizard."
            )

        date_from = self.date_from
        date_to = self.date_to
        wage = employee.get_wage(date_from) or 0.0

        struct = self.env['payroll.salary.structure'].get_structure(self.company_id)

        calendar_days, off_count, working_days = self._working_days_for_period(
            employee, date_from, date_to,
        )

        per_day = wage / calendar_days if calendar_days else 0.0
        hourly_rate = per_day / 8.0
        gross = wage

        basic = gross * struct.basic_pct / 100
        hra = gross * struct.hra_pct / 100
        travel = gross * struct.travel_pct / 100
        medical = gross * struct.medical_pct / 100

        pub_holidays = self._get_public_holiday_dates(date_from, date_to)
        all_working_dates = self._get_all_working_dates(employee, date_from, date_to)
        working_holidays = pub_holidays & all_working_dates
        scheduled_dates = all_working_dates - working_holidays
        pub_holiday_count = len(working_holidays)

        stats = self._analyse_attendance(employee, date_from, date_to, scheduled_dates, pub_holidays)

        dept_eligible = bool(
            employee.department_id and employee.department_id.eligible_for_bonus
        )
        # Eligible depts: genuine no-show absents → 2× per_day deduction.
        # Sandwich absents → always 1× for everyone. marked_as_day_off days
        # are neutral (see _analyse_attendance) — no deduction at all.
        absent_rate = per_day * 2 if dept_eligible else per_day

        genuine_absent_ded = stats['genuine_absent_days'] * absent_rate
        flat_absent_ded    = stats['flat_absent_days'] * per_day   # always 1×
        absent_ded  = genuine_absent_ded + flat_absent_ded
        # Late deduction is tiered, not per-instance: 1 day's pay is cut for
        # every 3 late arrivals in the period (3-5 late -> 1 day, 6-8 -> 2
        # days, etc.), not a flat half-day per late arrival.
        LATE_DAYS_PER_DEDUCTION_UNIT = 3
        late_ded    = (stats['late_days'] // LATE_DAYS_PER_DEDUCTION_UNIT) * per_day
        unpaid_ded  = stats['unpaid_leave_days'] * per_day
        total_ded   = absent_ded + late_ded + unpaid_ded

        is_dept_manager = bool(
            employee.department_id and employee.department_id.manager_id == employee
        )
        bonus = 0.0
        if dept_eligible and not is_dept_manager:
            perfect = (
                stats['absent_days'] == 0
                and stats['approved_leave_days'] == 0
                and stats['unpaid_leave_days'] == 0
            )
            if perfect:
                bonus = 500.0

        Adjustment = self.env['payroll.adjustment']
        adjustments = Adjustment.get_totals(employee.id, date_from)

        # ── Net salary does NOT include overtime pay ───────────────────
        # Overtime is calculated and paid separately via the OT Export wizard.
        # Last month due is added; penalty is deducted first, then advance,
        # each capped at whatever salary is actually left — if penalty and
        # advance together exceed the salary, only what's available gets
        # cut and the rest is tracked as "pending" on the adjustment
        # records instead of being silently lost.
        available = max(gross + bonus - total_ded + adjustments['last_month'], 0.0)

        penalty_ded = min(adjustments['penalty'], available)
        pending_penalty = adjustments['penalty'] - penalty_ded
        available -= penalty_ded

        advance_ded = min(adjustments['advance'], available)
        pending_advance = adjustments['advance'] - advance_ded
        available -= advance_ded

        net = available

        Adjustment.set_pending_amounts(employee.id, date_from, pending_penalty, pending_advance)

        self.write({
            'monthly_wage': wage,
            'calendar_days_in_period': calendar_days,
            'day_off_count': off_count,
            'working_days_in_period': len(scheduled_dates),
            'per_day_rate': per_day,

            'basic_pct': struct.basic_pct,
            'hra_pct': struct.hra_pct,
            'travel_pct': struct.travel_pct,
            'medical_pct': struct.medical_pct,

            'basic_amount': basic,
            'hra_amount': hra,
            'travel_amount': travel,
            'medical_amount': medical,
            'gross_salary': gross,

            'public_holiday_days': pub_holiday_count,
            'present_days': stats['present_days'],
            'approved_leave_days': stats['approved_leave_days'],
            'unpaid_leave_days': stats['unpaid_leave_days'],
            'absent_days': stats['absent_days'],
            'late_days': stats['late_days'],
            'adjust_days': stats['adjust_days'],
            'lwp_days': stats['lwp_days'],
            'genuine_absent_days': stats['genuine_absent_days'],
            'flat_absent_days': stats['flat_absent_days'],
            'sandwich_absent_days': stats['sandwich_absent_days'],

            'hourly_rate': hourly_rate,

            'absent_deduct_per_day': absent_rate,  # rate for genuine absents (1× or 2×); flat absents always 1×
            'absent_deduction': absent_ded,
            'late_deduction': late_ded,
            'unpaid_leave_deduction': unpaid_ded,
            'total_deductions': total_ded,
            'attendance_bonus': bonus,

            'last_month_due': adjustments['last_month'],
            'penalty_amount': penalty_ded,
            'advance_amount': advance_ded,

            # overtime_hours / overtime_pay intentionally NOT updated here;
            # they are populated by the separate OT wizard if needed for reference.
            'net_salary': net,
            'state': 'computed',
        })

    # ── attendance analysis ───────────────────────────────────────────

    def _analyse_attendance(self, employee, date_from, date_to, scheduled_dates, pub_holidays=None):
        """
        Classify every day of the period (not just scheduled working days —
        leave is checked before day-off/holiday status, so a leave that
        happens to fall on a weekly day-off or public holiday still counts
        as leave instead of being silently dropped).

        Priority per day (highest → lowest):
          1. Paid leave (approved, non-LWP)
          2. LWP leave  (leave_code = 'LWP' OR holiday_status_id.unpaid)
          3. ADJUST day (hr.swap swap_off_date whose required work was actually done)
             → counts as present; if required work NOT done → absent
          4. Present / late (attendance record exists, Dhaka-localised date)
             – marked_as_day_off attendance → neutral, matching the
               attendance dashboard: not present, not absent, no pay impact
               (that flag only means the check-out time was system-estimated
               after a long punch gap, not that the day was unworked)
          5. Absent, if this was otherwise a normal scheduled working day
             (no leave, no attendance, no adjust)
          6. Otherwise (a weekly day-off / public holiday with nothing else
             going on) → neutral, unless the sandwich rule below applies

        Sandwich rule (applies to ALL employees):
          An un-worked weekly day-off OR an un-worked public holiday is
          also counted as absent when both the day before and after it
          are genuine absences. If the day itself was actually attended,
          it is never flagged, regardless of the neighbours. If the
          candidate day is the 1st/last day of the payroll period, the
          neighbour on the other side of that boundary (previous month's
          last day / next month's 1st day) is looked up directly instead
          of being skipped.

        Returns dict with keys:
            present_days, absent_days, late_days,
            approved_leave_days, unpaid_leave_days (includes LWP),
            adjust_days, lwp_days
        """
        pub_holidays = pub_holidays or set()
        Attendance = self.env['hr.attendance']
        Leave = self.env['hr.leave']
        Swap = self.env['hr.swap']

        # ── 1. build attendance lookup keyed by Dhaka local date ─────
        utc_from, _ = _dhaka_day_to_utc_range(date_from)
        _, utc_to = _dhaka_day_to_utc_range(date_to)

        attendances = Attendance.search([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', utc_from),
            ('check_in', '<=', utc_to),
        ])

        att_by_date = {}  # dhaka_date → first attendance record
        for att in attendances:
            d = _to_dhaka(att.check_in).date() if att.check_in else None
            if d and d not in att_by_date:
                att_by_date[d] = att

        # ── 2. build leave sets ───────────────────────────────────────
        leaves = Leave.search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('date_from', '<=', str(date_to)),
            ('date_to', '>=', str(date_from)),
        ])

        paid_leave_dates = set()
        unpaid_leave_dates = set()
        lwp_dates = set()

        for leave in leaves:
            lf = max(leave.date_from.date(), date_from)
            lt = min(leave.date_to.date(), date_to)
            cursor = lf
            is_lwp = (
                (leave.holiday_status_id.leave_code or '').upper() == 'LWP'
                or leave.holiday_status_id.unpaid
            )
            while cursor <= lt:
                # No scheduled_dates gate here on purpose — leave takes
                # priority over day-off/holiday status (see docstring):
                # a leave that lands on a weekly off or public holiday
                # still counts as leave, it isn't silently dropped.
                if is_lwp:
                    unpaid_leave_dates.add(cursor)
                    lwp_dates.add(cursor)
                else:
                    paid_leave_dates.add(cursor)
                cursor += timedelta(days=1)

        # ── 3. build ADJUST day sets ──────────────────────────────────
        swaps = Swap.search([
            ('employee_id', '=', employee.id),
            ('swap_off_date', '>=', date_from),
            ('swap_off_date', '<=', date_to),
        ])

        adjust_valid_dates = set()
        adjust_absent_dates = set()

        for swap in swaps:
            off_date = swap.swap_off_date
            if off_date not in scheduled_dates:
                continue

            # "Required work actually done" check (see docstring point 3):
            # weekend/holiday swaps are earned by working swap_work_date;
            # extra_shift/overtime swaps are earned by the linked
            # attendance_id. Either missing, or marked_as_day_off, means
            # the work wasn't really done -> the ADJUST day is absent, not
            # present, even though the swap record itself still exists.
            if swap.swap_work_type in ('weekend', 'holiday'):
                work_done = False
                if swap.swap_work_date:
                    utc_w_from, utc_w_to = _dhaka_day_to_utc_range(swap.swap_work_date)
                    work_att = Attendance.search([
                        ('employee_id', '=', employee.id),
                        ('check_in', '>=', utc_w_from),
                        ('check_in', '<=', utc_w_to),
                    ], limit=1)
                    work_done = bool(work_att) and not work_att.marked_as_day_off
            else:  # extra_shift / overtime
                work_done = bool(swap.attendance_id) and not swap.attendance_id.marked_as_day_off

            if work_done:
                adjust_valid_dates.add(off_date)
            else:
                adjust_absent_dates.add(off_date)

        # ── 4. classify each scheduled day ───────────────────────────
        present = late = approved_leave = unpaid_leave = adjust = 0
        # Provisional present credit for a day-off/holiday with nothing
        # else going on (see the final `else` below) — kept separate from
        # `present` itself so `barely_present` further down still reflects
        # only real worked/adjusted attendance, not an auto-credit.
        dayoff_holiday_present = 0
        # Separate absent buckets so deduction rates can differ:
        #   genuine_absent  → 2× per_day for eligible depts, 1× otherwise
        #   flat_absent     → always 1× per_day (sandwich rule only —
        #                     marked_as_day_off is neutral, not counted here)
        genuine_absent = 0
        flat_absent = 0

        # Needed up front (not just for the sandwich pass below) so the
        # employee's actual scheduled day off can be recognised BEFORE the
        # generic "attendance exists" check — see is_weekly_off below.
        day_off_map = employee.get_day_off_day_map(date_from, date_to)

        day = date_from
        while day <= date_to:
            day_off = day_off_map[day]
            is_weekly_off = bool(day_off) and day.weekday() == int(day_off)

            if day in paid_leave_dates:
                approved_leave += 1

            elif day in unpaid_leave_dates:
                unpaid_leave += 1

            elif day in adjust_absent_dates:
                genuine_absent += 1

            elif day in adjust_valid_dates:
                present += 1
                adjust += 1

            elif is_weekly_off or day in pub_holidays:
                # The employee's actual scheduled day off, OR a public
                # holiday. Matches the attendance dashboard: present by
                # default whether attended or not, and regardless of
                # marked_as_day_off (that flag only means the check-out
                # time was system-estimated, not that the day was
                # unworked) — only the sandwich rule below can flip an
                # UNattended one to absent. Checked before the generic
                # attendance branch on purpose: marked_as_day_off is only
                # "neutral" on a common working day (below), not here —
                # a public holiday needs the exact same early check as a
                # day off, otherwise an attended-but-marked_as_day_off
                # holiday would wrongly fall through to the generic branch
                # and lose its present credit.
                dayoff_holiday_present += 1

            elif day in att_by_date:
                att = att_by_date[day]
                if att.marked_as_day_off:
                    # A common working day whose check-out was auto-
                    # estimated (stale/forgotten punch). Matches the
                    # dashboard: neutral, not present, not absent, no pay
                    # impact.
                    pass
                elif att.is_late:
                    present += 1
                    late += 1
                else:
                    present += 1

            elif day in scheduled_dates:
                # A normal working day with none of the above -> absent.
                genuine_absent += 1

            else:
                # Defensive fallback only — every day should already be
                # covered above (is_weekly_off/pub_holidays vs
                # scheduled_dates are complementary by construction in
                # _compute_payslip). Treated the same as a holiday/day-off
                # if ever reached.
                dayoff_holiday_present += 1

            day += timedelta(days=1)

        # ── 5. sandwich absent rule (ALL employees) ───────────────────
        # If both the working-day immediately before and after an
        # un-worked weekly day-off OR un-worked public holiday are
        # absent (no leave, no attendance, no adjust), that day-off/
        # holiday is penalised — always at 1× rate (flat_absent). A day
        # the employee actually attended is never a candidate, no matter
        # how "barely present" they were otherwise.
        #
        # Edge case: the neighbour check needs both neighbours to exist
        # inside the period — a candidate day on the very first/last day
        # of the month has one neighbour in the previous/next month, so
        # look that single day up directly (via _is_effective_absent_
        # external) instead of skipping the check and letting 1 day's
        # pay leak through un-deducted. If the employee was present
        # fewer than 6 days this period (working a public holiday also
        # counts towards that), skip the neighbour check and treat every
        # candidate day in the period as absent instead.
        sandwich_absent = 0
        MIN_PRESENT_FOR_DAY_OFF_PASS = 6

        # Each date resolves its own day-off via history (get_day_off_day)
        # instead of relying on a single value for the whole period. On any
        # date with neither a day-off nor a holiday, `candidate` below is
        # simply False, so there's no need to gate the loop up front.
        def is_effective_absent(day):
            return (
                day in scheduled_dates
                and day not in paid_leave_dates
                and day not in unpaid_leave_dates
                and day not in adjust_valid_dates
                and day not in adjust_absent_dates
                and day not in att_by_date
            )

        holiday_worked_count = len(pub_holidays & set(att_by_date.keys()))
        barely_present = (present + holiday_worked_count) < MIN_PRESENT_FOR_DAY_OFF_PASS

        # day_off_map already built before the main classification loop.
        cursor = date_from
        while cursor <= date_to:
            day_off = day_off_map[cursor]
            is_weekly_off = bool(day_off) and cursor.weekday() == int(day_off)
            is_unworked_holiday = cursor in pub_holidays and cursor not in att_by_date
            # Leave already claimed this day in the main loop above (leave
            # takes priority over day-off/holiday status) -> never a
            # sandwich candidate, it's not "un-worked with nothing going
            # on", it's already classified as leave.
            already_on_leave = cursor in paid_leave_dates or cursor in unpaid_leave_dates
            # A weekly off actually attended is never a candidate —
            # only an un-worked day-off/holiday can be sandwiched.
            candidate = not already_on_leave and (
                (is_weekly_off and cursor not in att_by_date) or is_unworked_holiday
            )

            if candidate:
                if barely_present:
                    sandwich_absent += 1
                else:
                    prev_day = cursor - timedelta(days=1)
                    next_day = cursor + timedelta(days=1)

                    prev_absent = (
                        is_effective_absent(prev_day) if prev_day >= date_from
                        else self._is_effective_absent_external(employee, prev_day)
                    )
                    next_absent = (
                        is_effective_absent(next_day) if next_day <= date_to
                        else self._is_effective_absent_external(employee, next_day)
                    )

                    if prev_absent and next_absent:
                        sandwich_absent += 1

            cursor += timedelta(days=1)

        flat_absent += sandwich_absent
        absent = genuine_absent + flat_absent
        # Every day-off/holiday defaults to present (dayoff_holiday_present)
        # except the ones the sandwich rule just turned into flat_absent —
        # this only affects the displayed present_days figure (to match the
        # attendance dashboard's count); it plays no part in absent/flat_
        # absent/deductions above, which are already final at this point.
        present += dayoff_holiday_present - sandwich_absent

        return {
            'present_days': present,
            'absent_days': absent,
            'genuine_absent_days': genuine_absent,   # eligible for 2× cut
            'flat_absent_days': flat_absent,          # always 1× (sandwich rule only)
            'sandwich_absent_days': sandwich_absent,  # sandwich-rule portion of flat_absent only
            'late_days': late,
            'approved_leave_days': approved_leave,
            'unpaid_leave_days': unpaid_leave,
            'adjust_days': adjust,
            'lwp_days': len(lwp_dates),
        }

    # ── state transitions ─────────────────────────────────────────────

    def action_validate(self):
        for slip in self:
            if slip.state != 'computed':
                raise UserError(f"Payslip '{slip.display_name}' must be computed first.")
            slip.state = 'validated'

    def action_waiting_payment(self):
        for slip in self:
            if slip.state != 'validated':
                raise UserError(f"Payslip '{slip.display_name}' must be validated first.")
            slip.state = 'waiting_payment'

    def action_mark_paid(self):
        for slip in self:
            slip.state = 'paid'

    def action_reset_draft(self):
        for slip in self:
            slip.state = 'draft'

    def action_print_payslip(self):
        return self.env.ref('enterprise_shift_payroll.action_report_payslip').report_action(self)


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Export Attendance Data (monthly summary)
# ═══════════════════════════════════════════════════════════════════

class AttendanceExportWizard(models.TransientModel):
    _name = 'payroll.attendance.export.wizard'
    _description = 'Export Attendance Data'

    export_month = fields.Date(
        string='Export Month',
        required=True,
        default=lambda self: date.today().replace(day=1),
        help="Pick any date — only year+month are used.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def _get_cl_sl_days(self, employee, date_from, date_to, scheduled_dates):
        Leave = self.env['hr.leave']
        leaves = Leave.search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('date_from', '<=', str(date_to)),
            ('date_to', '>=', str(date_from)),
        ])
        cl = sl = 0
        for leave in leaves:
            code = (leave.holiday_status_id.leave_code or '').upper()
            if code not in ('CL', 'SL'):
                continue
            lf = max(_to_dhaka(leave.date_from).date(), date_from)
            lt = min(_to_dhaka(leave.date_to).date(), date_to)
            cursor = lf
            while cursor <= lt:
                if cursor in scheduled_dates:
                    if code == 'CL':
                        cl += 1
                    else:
                        sl += 1
                cursor += timedelta(days=1)
        return cl, sl

    def action_export_attendance(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.export_month.replace(day=1)

        payslips = self.env['payroll.payslip'].search([
            ('payroll_month', '=', month_start),
            ('state', '!=', 'draft'),
        ])

        if not payslips:
            raise UserError(f"No computed payslips found for {month_start.strftime('%B %Y')}.")

        def _badge_sort_key(slip):
            badge = slip.employee_id.zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        sorted_payslips = sorted(payslips, key=_badge_sort_key)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 14,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter',
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        cell_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter',
        })
        hi_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#E2EFDA', 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        hi_tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#C6EFCE', 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        money_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        money_tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })

        IDX_PAY_DAYS = 13

        # NOTE: Section (Designation/Job Title) now precedes Department,
        # per request — designation first, then department.
        # Last Month / Penalty / Advance Payment are money totals pulled
        # from payroll.adjustment (multiple entries per month are summed
        # there); shown here for reference alongside Pay Days.
        COLS = [
            'SL', 'Badge No', 'Employee Name', 'Designation', 'Department',
            'Total Working\nDay',
            'Actual Absent\n(Absent+CL+SL+LWP)',
            'Eligible Double\nCutting Day',
            'Total Cutting',
            'Leave Approve\n(CL+SL)',
            'Last Month',
            'Penalty',
            'Advance Payment',
            'Pay Days',
            'Attendance Bonus',
            'Remarks',
        ]
        WIDTHS = [5, 11, 26, 20, 24, 13, 22, 18, 13, 15, 13, 13, 15, 11, 14, 22]

        sheet_label = month_start.strftime('%b %Y')[:31]
        ws = wb.add_worksheet(sheet_label)

        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        title_str = f"Attendance Summary  |  {month_start.strftime('%B %Y')}"
        ws.merge_range(0, 0, 1, len(COLS) - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        for c, h in enumerate(COLS):
            ws.write(row, c, h, hdr_fmt)
        ws.set_row(row, 50)
        row += 1

        grand = {k: 0 for k in [
            'total_wd', 'act_absent', 'dbl_cut', 'tot_cut',
            'lv_approve', 'pay_days', 'bonus',
            'last_month', 'penalty', 'advance',
        ]}
        sl_no = 1

        for slip in sorted_payslips:
            emp = slip.employee_id
            dept_eligible = bool(
                emp.department_id and emp.department_id.eligible_for_bonus
            )

            scheduled_dates = slip._get_all_working_dates(
                emp, slip.date_from, slip.date_to
            )
            pub_holiday_dates = slip._get_public_holiday_dates(
                slip.date_from, slip.date_to
            )
            working_holidays = pub_holiday_dates & scheduled_dates
            scheduled_dates -= working_holidays

            cl_days, sl_days = self._get_cl_sl_days(
                emp, slip.date_from, slip.date_to, scheduled_dates
            )

            total_wd = slip.calendar_days_in_period
            act_absent = slip.absent_days + cl_days + sl_days + slip.lwp_days
            # Double-cut only applies to genuine (no-show) absences —
            # flat absences (day-off marker / sandwich rule) are always 1×.
            dbl_cut = slip.genuine_absent_days if dept_eligible else 0
            tot_cut = act_absent + dbl_cut
            lv_approve = cl_days + sl_days
            pay_days = max(total_wd - tot_cut + lv_approve, 0)
            bonus = slip.attendance_bonus
            last_month = slip.last_month_due
            penalty = slip.penalty_amount
            advance = slip.advance_amount
            remarks = slip.notes or ''

            ws.set_row(row, 22)
            ws.write(row, 0,  sl_no,                        cell_fmt)
            ws.write(row, 1,  emp.zk_badge_no or '',        cell_fmt)
            ws.write(row, 2,  emp.name or '',                cell_fmt)
            ws.write(row, 3,  emp.job_id.name or '',         cell_fmt)
            ws.write(row, 4,  emp.department_id.name or '',  cell_fmt)
            ws.write(row, 5,  total_wd,                      num_fmt)
            ws.write(row, 6,  act_absent,                    num_fmt)
            ws.write(row, 7,  dbl_cut,                       num_fmt)
            ws.write(row, 8,  tot_cut,                       num_fmt)
            ws.write(row, 9,  lv_approve,                    num_fmt)
            ws.write(row, 10, last_month,                    money_fmt)
            ws.write(row, 11, penalty,                       money_fmt)
            ws.write(row, 12, advance,                       money_fmt)
            ws.write(row, IDX_PAY_DAYS, pay_days,            hi_fmt)
            ws.write(row, 14, bonus,                         num_fmt)
            ws.write(row, 15, remarks,                       cell_fmt)

            for k, v in [
                ('total_wd', total_wd), ('act_absent', act_absent),
                ('dbl_cut', dbl_cut), ('tot_cut', tot_cut),
                ('lv_approve', lv_approve), ('pay_days', pay_days),
                ('bonus', bonus), ('last_month', last_month),
                ('penalty', penalty), ('advance', advance),
            ]:
                grand[k] += v

            sl_no += 1
            row += 1

        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, 4, f'GRAND TOTAL  ({sl_no - 1} employees)', tot_lbl)
        ws.write(row, 5,  grand['total_wd'],   tot_fmt)
        ws.write(row, 6,  grand['act_absent'], tot_fmt)
        ws.write(row, 7,  grand['dbl_cut'],    tot_fmt)
        ws.write(row, 8,  grand['tot_cut'],    tot_fmt)
        ws.write(row, 9,  grand['lv_approve'], tot_fmt)
        ws.write(row, 10, grand['last_month'], money_tot_fmt)
        ws.write(row, 11, grand['penalty'],    money_tot_fmt)
        ws.write(row, 12, grand['advance'],    money_tot_fmt)
        ws.write(row, IDX_PAY_DAYS, grand['pay_days'], hi_tot_fmt)
        ws.write(row, 14, grand['bonus'],      tot_fmt)
        ws.write(row, 15, '',                  tot_lbl)

        wb.close()
        output.seek(0)

        fname = f"attendance_export_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Export Last Month / Penalty / Advance (badge-id wise)
# ═══════════════════════════════════════════════════════════════════

class LastMonthPenaltyAdvanceExportWizard(models.TransientModel):
    _name = 'payroll.last.month.penalty.advance.wizard'
    _description = 'Export Last Month / Penalty / Advance'

    export_month = fields.Date(
        string='Month',
        required=True,
        default=lambda self: date.today().replace(day=1),
        help="Pick any date — only year+month are used.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def action_export(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.export_month.replace(day=1)
        month_end = month_start.replace(day=monthrange(month_start.year, month_start.month)[1])

        adjustments = self.env['payroll.adjustment'].search([
            ('payroll_month', '>=', month_start),
            ('payroll_month', '<=', month_end),
        ])

        if not adjustments:
            raise UserError(
                f"No Last Month/Penalty/Advance entries found for {month_start.strftime('%B %Y')}."
            )

        totals_by_employee = {}
        for adj in adjustments:
            bucket = totals_by_employee.setdefault(
                adj.employee_id, {'last_month': 0.0, 'penalty': 0.0, 'advance': 0.0}
            )
            if adj.adjustment_type in bucket:
                bucket[adj.adjustment_type] += adj.amount

        def _badge_sort_key(employee):
            badge = employee.zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        sorted_employees = sorted(totals_by_employee.keys(), key=_badge_sort_key)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 14,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter',
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        cell_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter',
        })
        money_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter',
        })

        COLS = ['SL', 'Badge No', 'Employee Name', 'Designation', 'Department',
                'Last Month', 'Penalty', 'Advance Payment']
        WIDTHS = [6, 12, 26, 20, 24, 14, 14, 16]

        sheet_label = month_start.strftime('%b %Y')[:31]
        ws = wb.add_worksheet(sheet_label)

        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        title_str = f"Last Month / Penalty / Advance  |  {month_start.strftime('%B %Y')}"
        ws.merge_range(0, 0, 1, len(COLS) - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        for c, h in enumerate(COLS):
            ws.write(row, c, h, hdr_fmt)
        ws.set_row(row, 30)
        row += 1

        grand_last_month = grand_penalty = grand_advance = 0.0
        sl_no = 1

        for emp in sorted_employees:
            totals = totals_by_employee[emp]
            ws.set_row(row, 22)
            ws.write(row, 0, sl_no,                       cell_fmt)
            ws.write(row, 1, emp.zk_badge_no or '',        cell_fmt)
            ws.write(row, 2, emp.name or '',               cell_fmt)
            ws.write(row, 3, emp.job_title or (emp.job_id.name or ''), cell_fmt)
            ws.write(row, 4, emp.department_id.name or '', cell_fmt)
            ws.write(row, 5, totals['last_month'],         money_fmt)
            ws.write(row, 6, totals['penalty'],            money_fmt)
            ws.write(row, 7, totals['advance'],            money_fmt)

            grand_last_month += totals['last_month']
            grand_penalty += totals['penalty']
            grand_advance += totals['advance']

            sl_no += 1
            row += 1

        ws.merge_range(row, 0, row, 4, f'GRAND TOTAL  ({sl_no - 1} employees)', tot_lbl)
        ws.write(row, 5, grand_last_month, tot_fmt)
        ws.write(row, 6, grand_penalty,    tot_fmt)
        ws.write(row, 7, grand_advance,    tot_fmt)

        wb.close()
        output.seek(0)

        fname = f"last_month_penalty_advance_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Generate Monthly Payslips
# ═══════════════════════════════════════════════════════════════════

class GenerateMonthPayslipsWizard(models.TransientModel):
    _name = 'payroll.generate.month.payslips.wizard'
    _description = 'Generate Monthly Payslips'

    payroll_month = fields.Date(
        string='Payroll Month',
        required=True,
        default=lambda self: date.today().replace(day=1),
        help="Pick any date — only the year+month matter.",
    )
    recompute_existing = fields.Boolean(
        string='Recompute Existing Payslips',
        default=True,
        help="If checked, existing payslips for this month will be recomputed.",
    )

    def action_generate(self):
        self.ensure_one()

        month_start = self.payroll_month.replace(day=1)

        # No lock check here — payroll.payslip.create()/_compute_payslip()
        # already refuse to touch a locked month, and the per-employee
        # try/except below reports that the same way as any other error.

        # `active` defaults to True in search domains, so archived
        # employees are already excluded here — kept explicit for clarity.
        employees = self.env['hr.employee'].search([
            ('active', '=', True),
            ('worker_type', '=', 'regular'),
        ])
        if not employees:
            raise UserError(
                "No active employees with worker_type = 'regular' found."
            )

        Payslip = self.env['payroll.payslip']
        created = recomputed = errors = skipped = 0
        error_msgs = []

        for emp in employees:
            if not emp.active:
                # Defensive guard in case of stale iteration state.
                skipped += 1
                continue

            existing = Payslip.search([
                ('employee_id', '=', emp.id),
                ('payroll_month', '=', month_start),
            ], limit=1)

            if existing:
                if self.recompute_existing:
                    try:
                        existing._compute_payslip()
                        recomputed += 1
                    except Exception as e:
                        error_msgs.append(f"{emp.name}: {e}")
                        errors += 1
                continue

            slip = Payslip.create({
                'employee_id': emp.id,
                'payroll_month': month_start,
                'company_id': emp.company_id.id or self.env.company.id,
                'monthly_wage': emp.wage or 0.0,
            })
            try:
                slip._compute_payslip()
                created += 1
            except Exception as e:
                error_msgs.append(f"{emp.name}: {e}")
                errors += 1

        msg_parts = [f"{created} payslip(s) created & computed."]
        if recomputed:
            msg_parts.append(f"{recomputed} recomputed (existing payslips).")
        if skipped:
            msg_parts.append(f"{skipped} archived/inactive employee(s) skipped.")
        if errors:
            msg_parts.append(f"{errors} error(s): " + " | ".join(error_msgs))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': f"Monthly Payslips — {month_start.strftime('%B %Y')}",
                'message': " ".join(msg_parts),
                'type': 'warning' if errors else 'success',
                'sticky': bool(errors),
            },
        }


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Export Payroll Worksheet (no OT columns)
# ═══════════════════════════════════════════════════════════════════

class ImportPayslipsWizard(models.TransientModel):
    _name = 'payroll.import.payslips.wizard'
    _description = 'Export Payroll Worksheet'

    export_month = fields.Date(
        string='Export Payroll Month',
        help="Select a month to export payroll data as Excel.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def action_export_payroll(self):
        self.ensure_one()
        if not self.export_month:
            raise UserError("Please select a month to export.")
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.export_month.replace(day=1)

        payslips = self.env['payroll.payslip'].search([
            ('payroll_month', '=', month_start),
            ('state', '!=', 'draft'),
        ], order='employee_id')

        if not payslips:
            raise UserError(f"No payslips found for {month_start.strftime('%B %Y')}.")

        def _badge_sort_key(slip):
            badge = slip.employee_id.zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        sorted_payslips = sorted(payslips, key=_badge_sort_key)
        # Employees with zero wage FOR THIS PAYSLIP are not payroll-relevant
        # this month — skip them entirely (not exported anywhere). Checked
        # against the payslip's own stored monthly_wage, not the employee's
        # current wage, since those can now differ (wage history) — using
        # the current wage would wrongly drop/keep rows based on a later
        # raise or an since-changed wage that has nothing to do with this
        # specific month.
        sorted_payslips = [s for s in sorted_payslips if s.monthly_wage]

        if not sorted_payslips:
            raise UserError(
                f"No payslips with a non-zero wage found for {month_start.strftime('%B %Y')}."
            )

        days_map = self.env['payroll.adjustment'].get_last_month_days_map(month_start)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        # Plain/uncoloured, bordered look to match the company's own
        # "GLASSWARE SALARY ... MAIN" register template — no bg_color
        # anywhere in this sheet.
        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 12,
            'align': 'center', 'valign': 'vcenter', 'border': 1,
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 9,
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        cell_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Calibri', 'font_size': 10, 'border': 1, 'valign': 'vcenter'
        })
        num_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'valign': 'vcenter'
        })
        tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1, 'num_format': '#,##0', 'valign': 'vcenter'
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1, 'valign': 'vcenter'
        })
        sig_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'align': 'center', 'valign': 'bottom',
        })

        # Column layout matches the "GLASSWARE SALARY ... MAIN" register
        # exactly, A-Z (labels, order and widths, including its own
        # spelling of "CONVENCE"/"QUALIFICLATION"/"Attendes incentive"):
        #   TAX and EDU. QUALIFICLATION have no source in Odoo yet -> left
        #   blank for manual entry, same as this file's own "Income"/"PF"
        #   columns on the Summary sheet.
        COLS = [
            'SER NO', 'ID NO', 'NAME', 'DESIGNATION', 'JOINING DATE',
            'EDU. QUALIFICLATION', 'SECTION', 'TOTAL WORKING DAYS', 'ABSENT',
            'LEAVE APPROVED', 'LAST MONTH', 'PAY DAYS', 'BASIC SALARY',
            'HOUSE RENT', 'CONVENCE', 'MEDICAL', 'OTHERS',
            'Prsnt Gross Salary', 'Attendes incentive', 'Prsnt Net Salary',
            'TAX', 'LATE DEDUCT', 'AD', 'H&S PRODUCT SALE ADVANCE',
            'Total Deduct', 'Net Pay', 'SIG',
        ]
        WIDTHS = [4.85, 6.17, 22.67, 8.85, 8.17, 10.35, 6.17, 6.5, 6.5, 6.17,
                  6.35, 5.85, 5.5, 5.17, 4.17, 3.85, 4.5, 8.67, 8.85, 8.5,
                  5.85, 7.85, 7.85, 6.5, 8.35, 9.35, 15.85]
        IDX = {label: i for i, label in enumerate(COLS)}

        sheet_label = month_start.strftime('%b %Y')[:31]
        ws = wb.add_worksheet(sheet_label)

        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        # Row 0: group bands (MONTH/ATTENDANCE/SALARY/DEDUCT/PAYABLE/YEAR),
        # row 1: the detailed column headers -- same two-row header shape
        # as the reference file's rows 6-7.
        ws.set_row(0, 20)
        ws.merge_range(0, IDX['SER NO'], 0, IDX['ID NO'], 'MONTH', hdr_fmt)
        ws.write(0, IDX['NAME'], month_start.strftime('%B').upper(), hdr_fmt)
        ws.merge_range(0, IDX['SECTION'], 0, IDX['PAY DAYS'], 'ATTENDANCE', hdr_fmt)
        ws.merge_range(0, IDX['BASIC SALARY'], 0, IDX['OTHERS'], '', hdr_fmt)
        ws.merge_range(0, IDX['Prsnt Gross Salary'], 0, IDX['Prsnt Net Salary'], 'SALARY', hdr_fmt)
        ws.merge_range(0, IDX['TAX'], 0, IDX['Total Deduct'], 'DEDUCT', hdr_fmt)
        ws.write(0, IDX['Net Pay'], 'PAYABLE', hdr_fmt)
        ws.write(0, IDX['SIG'], f'YEAR-{month_start.year}', hdr_fmt)

        ws.set_row(1, 44)
        for c, h in enumerate(COLS):
            ws.write(1, c, h, hdr_fmt)

        attendance_wizard = self.env['payroll.attendance.export.wizard']

        def _row_calc_for(slip, emp, last_month_days=0.0):
            """Everything derived (not stored directly on the payslip)
            needed for one row of this export:
              - total_wd   : TOTAL WORKING DAYS = calendar days in period
              - absent_col : ABSENT = genuine (no-show) absent + LWP days
              - leave_approve : LEAVE APPROVED = CL + SL days
              - pay_days   : same formula as the Attendance export, plus
                             last month's adjustment day count
              - prsnt_net  : Prsnt Net Salary = gross+bonus+last month,
                             before TAX/AD/H&S (matches the Summary
                             sheet's own "net + penalty + advance" shape)
              - total_deduct : Total Deduct = AD + H&S (TAX not tracked)
            """
            dept_eligible = bool(
                emp.department_id and emp.department_id.eligible_for_bonus
            )
            scheduled_dates = slip._get_all_working_dates(
                emp, slip.date_from, slip.date_to
            )
            pub_holiday_dates = slip._get_public_holiday_dates(
                slip.date_from, slip.date_to
            )
            scheduled_dates -= (pub_holiday_dates & scheduled_dates)
            cl_days, sl_days = attendance_wizard._get_cl_sl_days(
                emp, slip.date_from, slip.date_to, scheduled_dates
            )
            total_wd = slip.calendar_days_in_period
            act_absent = slip.absent_days + cl_days + sl_days + slip.lwp_days
            dbl_cut = slip.genuine_absent_days if dept_eligible else 0
            tot_cut = act_absent + dbl_cut
            leave_approve = cl_days + sl_days
            pay_days = max(total_wd - tot_cut + leave_approve + last_month_days, 0)

            advance = slip.advance_amount
            penalty = slip.penalty_amount
            late_ded = slip.late_deduction
            # Prsnt Net Salary is shown BEFORE late deduction (add it back
            # out of net_salary, which already has it subtracted), and
            # Total Deduct now includes it explicitly -- so Prsnt Net
            # Salary - Total Deduct still equals Net Pay (net_salary),
            # with late deduction now a real, visible step instead of
            # being silently pre-baked into net_salary.
            total_deduct = advance + penalty + late_ded
            prsnt_net = slip.net_salary + advance + penalty + late_ded

            return {
                'total_wd': total_wd,
                'absent_col': slip.genuine_absent_days + slip.lwp_days + slip.sandwich_absent_days,
                'leave_approve': leave_approve,
                'pay_days': pay_days,
                'advance': advance,
                'penalty': penalty,
                'total_deduct': total_deduct,
                'prsnt_net': prsnt_net,
            }

        def _write_slip_row(target_ws, r, sl_no, slip, calc, last_month_days):
            emp = slip.employee_id
            target_ws.set_row(r, 22)
            target_ws.write(r, IDX['SER NO'], sl_no,                       cell_fmt)
            target_ws.write(r, IDX['ID NO'], emp.zk_badge_no or '',        cell_fmt)
            target_ws.write(r, IDX['NAME'], emp.name or '',                 cell_fmt)
            target_ws.write(r, IDX['DESIGNATION'], emp.job_title or '',     cell_fmt)
            target_ws.write(r, IDX['JOINING DATE'],
                             emp.contract_date_start.strftime('%d-%m-%Y') if emp.contract_date_start else '',
                             cell_fmt)
            target_ws.write(r, IDX['EDU. QUALIFICLATION'], '',              cell_fmt)
            target_ws.write(r, IDX['SECTION'], emp.department_id.name or '', cell_fmt)
            target_ws.write(r, IDX['TOTAL WORKING DAYS'], calc['total_wd'], cell_fmt)
            target_ws.write(r, IDX['ABSENT'], calc['absent_col'],          cell_fmt)
            target_ws.write(r, IDX['LEAVE APPROVED'], calc['leave_approve'], cell_fmt)
            target_ws.write(r, IDX['LAST MONTH'], last_month_days,         cell_fmt)
            target_ws.write(r, IDX['PAY DAYS'], calc['pay_days'],          cell_fmt)
            target_ws.write(r, IDX['BASIC SALARY'], slip.basic_amount,     num_fmt)
            target_ws.write(r, IDX['HOUSE RENT'], slip.hra_amount,        num_fmt)
            target_ws.write(r, IDX['CONVENCE'], slip.travel_amount,       num_fmt)
            target_ws.write(r, IDX['MEDICAL'], slip.medical_amount,       num_fmt)
            target_ws.write(r, IDX['OTHERS'], 0,                          num_fmt)
            target_ws.write(r, IDX['Prsnt Gross Salary'], slip.gross_salary, num_fmt)
            target_ws.write(r, IDX['Attendes incentive'], slip.attendance_bonus, num_fmt)
            target_ws.write(r, IDX['Prsnt Net Salary'], calc['prsnt_net'], num_fmt)
            target_ws.write(r, IDX['TAX'], 0,                             num_fmt)
            target_ws.write(r, IDX['LATE DEDUCT'], slip.late_deduction,   num_fmt)
            target_ws.write(r, IDX['AD'], calc['advance'],                num_fmt)
            target_ws.write(r, IDX['H&S PRODUCT SALE ADVANCE'], calc['penalty'], num_fmt)
            target_ws.write(r, IDX['Total Deduct'], calc['total_deduct'], num_fmt)
            target_ws.write(r, IDX['Net Pay'], slip.net_salary,           num_fmt)
            # Bank-transfer employees don't need a wet-ink signature — show
            # their account number for reference instead; cash-paid
            # employees (no bank_acc_no) get a blank cell to sign in.
            target_ws.write(r, IDX['SIG'], emp.bank_acc_no or '',         cell_fmt)

        row = 2
        # grand_totals / dept_summary only cover rows that land on this
        # sheet — Low Attendance employees (pay_days < 6) are excluded
        # from both, so this sheet's own GRAND TOTAL and the Department
        # Summary / Summary sheets built from dept_summary all reconcile
        # against the same "main list" population, not a hidden extra
        # group of employees no one can see the rows for.
        grand_totals = {
            'basic': 0.0, 'hra': 0.0, 'travel': 0.0, 'medical': 0.0,
            'gross': 0.0, 'pay_days': 0, 'present': 0, 'leave': 0,
            'absent': 0,
            'absent_ded': 0.0, 'late_ded': 0.0, 'unpaid_ded': 0.0,
            'total_ded': 0.0, 'attendance_ded_capped': 0.0, 'bonus': 0.0,
            'last_month_days': 0.0,
            'last_month': 0.0, 'penalty': 0.0, 'advance': 0.0, 'net': 0.0,
            'total_wd': 0, 'absent_col': 0, 'leave_approve': 0,
            'prsnt_net': 0.0, 'total_deduct': 0.0,
        }
        # Per-department totals, collected alongside the main loop below,
        # used only to build the "Department Summary" sheet — the main
        # sheet itself is one flat badge-sorted list (no grouping).
        dept_summary = {}
        # Employees with under 6 pay days this period are pulled out of the
        # main list and reported on their own sheet. net_salary == 0 isn't
        # used for this: a normal full-attendance employee can also land at
        # net 0 purely from an advance/penalty deduction, which would be a
        # contradictory reason to call them "low attendance".
        MIN_PAY_DAYS_FOR_MAIN_SHEET = 6
        low_attendance_rows = []
        # Every slip processed this run, regardless of which sheet its row
        # ends up on — used below to find new joiners without re-deriving
        # pay_days a second time.
        all_rows = []

        main_sl = 0
        for slip in sorted_payslips:
            emp = slip.employee_id
            dept = emp.department_id.name or 'No Department'
            last_month_days = days_map.get(emp.id, 0.0)
            calc = _row_calc_for(slip, emp, last_month_days)
            pay_days = calc['pay_days']
            all_rows.append((slip, calc, last_month_days))

            # Low Attendance employees are excluded here too — not just
            # from this sheet's own rows, but from Department Summary /
            # grand totals as well, so nothing reconciles against money
            # tied to someone who isn't in the main list.
            if pay_days < MIN_PAY_DAYS_FOR_MAIN_SHEET:
                low_attendance_rows.append((slip, calc, last_month_days))
                continue

            # Deductions can add up to more than the employee is owed
            # (e.g. the 2x absent-day cut). net_salary is already floored
            # at 0 on the payslip, so total_gross - net_salary gives the
            # deduction actually applied, capped at what was owed.
            # Split into the attendance-based share (absent/late/unpaid,
            # capped) and the penalty/advance share (already capped at
            # the payslip level) so Department Summary can show attendance
            # deduction netted out of Gross Salary instead of lumped into
            # Total Deduction.
            total_gross_emp = slip.gross_salary + slip.attendance_bonus + slip.last_month_due
            capped_deduction = total_gross_emp - slip.net_salary
            penalty_advance_ded = slip.penalty_amount + slip.advance_amount
            attendance_ded_capped = capped_deduction - penalty_advance_ded

            dept_totals = dept_summary.setdefault(dept, {
                'gross': 0.0, 'pay_days': 0, 'present': 0, 'leave': 0,
                'absent': 0,
                'absent_ded': 0.0, 'late_ded': 0.0, 'unpaid_ded': 0.0,
                'total_ded': 0.0, 'attendance_ded_capped': 0.0, 'bonus': 0.0,
                'last_month_days': 0.0,
                'last_month': 0.0, 'penalty': 0.0, 'advance': 0.0, 'net': 0.0,
                'employees': 0,
            })
            dept_totals['gross']      += slip.gross_salary
            dept_totals['pay_days']   += pay_days
            dept_totals['present']    += slip.present_days
            dept_totals['leave']      += slip.approved_leave_days
            dept_totals['absent']     += slip.absent_days
            dept_totals['absent_ded'] += slip.absent_deduction
            dept_totals['late_ded']   += slip.late_deduction
            dept_totals['unpaid_ded'] += slip.unpaid_leave_deduction
            dept_totals['total_ded']  += slip.total_deductions
            dept_totals['attendance_ded_capped'] += attendance_ded_capped
            dept_totals['bonus']      += slip.attendance_bonus
            dept_totals['last_month_days'] += last_month_days
            dept_totals['last_month'] += slip.last_month_due
            dept_totals['penalty']    += slip.penalty_amount
            dept_totals['advance']    += slip.advance_amount
            dept_totals['net']        += slip.net_salary
            dept_totals['employees']  += 1

            grand_totals['basic']      += slip.basic_amount
            grand_totals['hra']        += slip.hra_amount
            grand_totals['travel']     += slip.travel_amount
            grand_totals['medical']    += slip.medical_amount
            grand_totals['gross']      += slip.gross_salary
            grand_totals['pay_days']   += pay_days
            grand_totals['present']    += slip.present_days
            grand_totals['leave']      += slip.approved_leave_days
            grand_totals['absent']     += slip.absent_days
            grand_totals['absent_ded'] += slip.absent_deduction
            grand_totals['late_ded']   += slip.late_deduction
            grand_totals['unpaid_ded'] += slip.unpaid_leave_deduction
            grand_totals['total_ded']  += slip.total_deductions
            grand_totals['attendance_ded_capped'] += attendance_ded_capped
            grand_totals['bonus']      += slip.attendance_bonus
            grand_totals['last_month_days'] += last_month_days
            grand_totals['last_month'] += slip.last_month_due
            grand_totals['penalty']    += slip.penalty_amount
            grand_totals['advance']    += slip.advance_amount
            grand_totals['net']        += slip.net_salary
            grand_totals['total_wd']      += calc['total_wd']
            grand_totals['absent_col']    += calc['absent_col']
            grand_totals['leave_approve'] += calc['leave_approve']
            grand_totals['prsnt_net']     += calc['prsnt_net']
            grand_totals['total_deduct']  += calc['total_deduct']

            main_sl += 1
            _write_slip_row(ws, row, main_sl, slip, calc, last_month_days)
            row += 1

        ws.set_row(row, 22)
        ws.merge_range(row, IDX['SER NO'], row, IDX['SECTION'], 'GRAND TOTAL', tot_lbl)
        ws.write(row, IDX['TOTAL WORKING DAYS'], grand_totals['total_wd'],      tot_fmt)
        ws.write(row, IDX['ABSENT'], grand_totals['absent_col'],                tot_fmt)
        ws.write(row, IDX['LEAVE APPROVED'], grand_totals['leave_approve'],     tot_fmt)
        ws.write(row, IDX['LAST MONTH'], grand_totals['last_month_days'],      tot_fmt)
        ws.write(row, IDX['PAY DAYS'], grand_totals['pay_days'],               tot_fmt)
        ws.write(row, IDX['BASIC SALARY'], grand_totals['basic'],              tot_fmt)
        ws.write(row, IDX['HOUSE RENT'], grand_totals['hra'],                  tot_fmt)
        ws.write(row, IDX['CONVENCE'], grand_totals['travel'],                 tot_fmt)
        ws.write(row, IDX['MEDICAL'], grand_totals['medical'],                 tot_fmt)
        ws.write(row, IDX['OTHERS'], 0,                                       tot_fmt)
        ws.write(row, IDX['Prsnt Gross Salary'], grand_totals['gross'],        tot_fmt)
        ws.write(row, IDX['Attendes incentive'], grand_totals['bonus'],        tot_fmt)
        ws.write(row, IDX['Prsnt Net Salary'], grand_totals['prsnt_net'],      tot_fmt)
        ws.write(row, IDX['TAX'], 0,                                          tot_fmt)
        ws.write(row, IDX['LATE DEDUCT'], grand_totals['late_ded'],           tot_fmt)
        ws.write(row, IDX['AD'], grand_totals['advance'],                      tot_fmt)
        ws.write(row, IDX['H&S PRODUCT SALE ADVANCE'], grand_totals['penalty'], tot_fmt)
        ws.write(row, IDX['Total Deduct'], grand_totals['total_deduct'],       tot_fmt)
        ws.write(row, IDX['Net Pay'], grand_totals['net'],                     tot_fmt)
        ws.write(row, IDX['SIG'], '',                                         tot_lbl)
        row += 2

        # Signature footer, matching the reference file's own
        # PREPARED BY / CHECKED BY / APPROVED BY row.
        ws.write(row, IDX['NAME'], 'PREPARED BY', sig_fmt)
        ws.write(row, IDX['LAST MONTH'], 'CHECKED BY', sig_fmt)
        ws.write(row, IDX['Net Pay'], 'APPROVED BY', sig_fmt)

        # ── Low Attendance sheet — employees with under 6 pay days this
        #    period, kept out of the main list ───────────────────────────
        zw = wb.add_worksheet('Low Attendance')
        for i, w in enumerate(WIDTHS):
            zw.set_column(i, i, w)
        zw.merge_range(
            0, 0, 1, len(COLS) - 1,
            f"Employees with Under {MIN_PAY_DAYS_FOR_MAIN_SHEET} Pay Days  |  "
            f"{month_start.strftime('%B %Y')}",
            title_fmt,
        )
        zw.set_row(0, 30)
        zw.set_row(1, 10)

        zrow = 2
        for c, h in enumerate(COLS):
            zw.write(zrow, c, h, hdr_fmt)
        zw.set_row(zrow, 42)
        zrow += 1

        low_totals = {
            'basic': 0.0, 'hra': 0.0, 'travel': 0.0, 'medical': 0.0,
            'gross': 0.0, 'pay_days': 0, 'present': 0, 'leave': 0, 'absent': 0,
            'absent_ded': 0.0, 'late_ded': 0.0, 'unpaid_ded': 0.0, 'total_ded': 0.0,
            'bonus': 0.0, 'last_month_days': 0.0, 'last_month': 0.0,
            'penalty': 0.0, 'advance': 0.0, 'net': 0.0,
            'total_wd': 0, 'absent_col': 0, 'leave_approve': 0,
            'prsnt_net': 0.0, 'total_deduct': 0.0,
        }
        for zsl_no, (slip, calc, last_month_days) in enumerate(low_attendance_rows, start=1):
            _write_slip_row(zw, zrow, zsl_no, slip, calc, last_month_days)
            zrow += 1

            low_totals['basic']      += slip.basic_amount
            low_totals['hra']        += slip.hra_amount
            low_totals['travel']     += slip.travel_amount
            low_totals['medical']    += slip.medical_amount
            low_totals['gross']      += slip.gross_salary
            low_totals['pay_days']   += calc['pay_days']
            low_totals['present']    += slip.present_days
            low_totals['leave']      += slip.approved_leave_days
            low_totals['absent']     += slip.absent_days
            low_totals['absent_ded'] += slip.absent_deduction
            low_totals['late_ded']   += slip.late_deduction
            low_totals['unpaid_ded'] += slip.unpaid_leave_deduction
            low_totals['total_ded']  += slip.total_deductions
            low_totals['bonus']      += slip.attendance_bonus
            low_totals['last_month_days'] += last_month_days
            low_totals['last_month'] += slip.last_month_due
            low_totals['penalty']    += slip.penalty_amount
            low_totals['advance']    += slip.advance_amount
            low_totals['net']        += slip.net_salary
            low_totals['total_wd']      += calc['total_wd']
            low_totals['absent_col']    += calc['absent_col']
            low_totals['leave_approve'] += calc['leave_approve']
            low_totals['prsnt_net']     += calc['prsnt_net']
            low_totals['total_deduct']  += calc['total_deduct']

        zw.set_row(zrow, 22)
        zw.merge_range(zrow, IDX['SER NO'], zrow, IDX['SECTION'], 'GRAND TOTAL', tot_lbl)
        zw.write(zrow, IDX['TOTAL WORKING DAYS'], low_totals['total_wd'],      tot_fmt)
        zw.write(zrow, IDX['ABSENT'], low_totals['absent_col'],                tot_fmt)
        zw.write(zrow, IDX['LEAVE APPROVED'], low_totals['leave_approve'],     tot_fmt)
        zw.write(zrow, IDX['LAST MONTH'], low_totals['last_month_days'],      tot_fmt)
        zw.write(zrow, IDX['PAY DAYS'], low_totals['pay_days'],               tot_fmt)
        zw.write(zrow, IDX['BASIC SALARY'], low_totals['basic'],              tot_fmt)
        zw.write(zrow, IDX['HOUSE RENT'], low_totals['hra'],                  tot_fmt)
        zw.write(zrow, IDX['CONVENCE'], low_totals['travel'],                 tot_fmt)
        zw.write(zrow, IDX['MEDICAL'], low_totals['medical'],                 tot_fmt)
        zw.write(zrow, IDX['OTHERS'], 0,                                     tot_fmt)
        zw.write(zrow, IDX['Prsnt Gross Salary'], low_totals['gross'],        tot_fmt)
        zw.write(zrow, IDX['Attendes incentive'], low_totals['bonus'],        tot_fmt)
        zw.write(zrow, IDX['Prsnt Net Salary'], low_totals['prsnt_net'],      tot_fmt)
        zw.write(zrow, IDX['TAX'], 0,                                        tot_fmt)
        zw.write(zrow, IDX['LATE DEDUCT'], low_totals['late_ded'],           tot_fmt)
        zw.write(zrow, IDX['AD'], low_totals['advance'],                      tot_fmt)
        zw.write(zrow, IDX['H&S PRODUCT SALE ADVANCE'], low_totals['penalty'], tot_fmt)
        zw.write(zrow, IDX['Total Deduct'], low_totals['total_deduct'],       tot_fmt)
        zw.write(zrow, IDX['Net Pay'], low_totals['net'],                     tot_fmt)
        zw.write(zrow, IDX['SIG'], '',                                       tot_lbl)

        # ── New Joiners sheet — employees whose contract started within
        #    this payroll cycle (26th of the previous month through the
        #    25th of this month — the company's joining cutoff, not the
        #    calendar month), purely informational: they stay on the main
        #    sheet exactly as computed; nothing here changes that ────────
        prev_month_last_day = month_start - timedelta(days=1)
        new_joiner_from = prev_month_last_day.replace(day=26)
        new_joiner_to = month_start.replace(day=25)

        new_joiner_rows = [
            (slip, calc, last_month_days)
            for slip, calc, last_month_days in all_rows
            if slip.employee_id.contract_date_start
            and new_joiner_from <= slip.employee_id.contract_date_start <= new_joiner_to
        ]

        nj = wb.add_worksheet('New Joiners')
        NJ_COLS = ['SL', 'ID', 'Name', 'Job Title', 'Department',
                   'Total Pay Days', 'Gross Salary', 'Net Salary', 'Bank Account']
        NJ_WIDTHS = [5, 12, 24, 18, 24, 14, 16, 16, 22]
        for i, w in enumerate(NJ_WIDTHS):
            nj.set_column(i, i, w)

        nj.merge_range(
            0, 0, 1, len(NJ_COLS) - 1,
            f"New Joiners  |  {new_joiner_from.strftime('%d %b')} – {new_joiner_to.strftime('%d %b %Y')}",
            title_fmt,
        )
        nj.set_row(0, 30)
        nj.set_row(1, 10)

        nj_row = 2
        for c, h in enumerate(NJ_COLS):
            nj.write(nj_row, c, h, hdr_fmt)
        nj.set_row(nj_row, 30)
        nj_row += 1

        nj_pay_days_total = 0
        nj_gross_total = 0.0
        nj_net_total = 0.0

        for nj_sl, (slip, calc, last_month_days) in enumerate(new_joiner_rows, start=1):
            emp = slip.employee_id
            pay_days = calc['pay_days']
            nj.set_row(nj_row, 20)
            nj.write(nj_row, 0, nj_sl,                  cell_fmt)
            nj.write(nj_row, 1, emp.zk_badge_no or '',  cell_fmt)
            nj.write(nj_row, 2, emp.name or '',          cell_fmt)
            nj.write(nj_row, 3, emp.job_title or '',      cell_fmt)
            nj.write(nj_row, 4, emp.department_id.name or '', cell_fmt)
            nj.write(nj_row, 5, pay_days,                cell_fmt)
            nj.write(nj_row, 6, slip.gross_salary,        num_fmt)
            nj.write(nj_row, 7, slip.net_salary,          num_fmt)
            nj.write(nj_row, 8, emp.bank_acc_no or '',    cell_fmt)
            nj_row += 1

            nj_pay_days_total += pay_days
            nj_gross_total    += slip.gross_salary
            nj_net_total      += slip.net_salary

        nj.set_row(nj_row, 22)
        nj.merge_range(nj_row, 0, nj_row, 4, f'TOTAL  ({len(new_joiner_rows)} new joiners)', tot_lbl)
        nj.write(nj_row, 5, nj_pay_days_total, tot_fmt)
        nj.write(nj_row, 6, nj_gross_total,    tot_fmt)
        nj.write(nj_row, 7, nj_net_total,      tot_fmt)
        nj.write(nj_row, 8, '',                 tot_lbl)

        # ── Department Summary sheet — how much money each department
        #    needs to disburse this month ────────────────────────────
        sm = wb.add_worksheet('Department Summary')

        sm_title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 12,
            'align': 'center', 'valign': 'vcenter', 'border': 1,
        })
        sm_hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True
        })
        sm_cell_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1, 'valign': 'vcenter'
        })
        sm_num_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter'
        })
        sm_pay_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter'
        })
        sm_tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1, 'valign': 'vcenter'
        })
        sm_tot_num = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter'
        })
        sm_tot_pay = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10,
            'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter'
        })

        # Gross Salary is shown net of the attendance-based deduction
        # (absent/late/unpaid, capped at what the employee was owed) —
        # that portion no longer sits inside Total Deduction below.
        # Total Gross Salary = (Gross Salary, already net of attendance
        # deduction) + Last Month + Attendance Bonus.
        # Total Deduction = Penalty + Advance only (each already capped
        # at the payslip level).
        SM_COLS = [
            'SL', 'Department', 'Employees', 'Gross Salary',
            'Last Month', 'Attendance Bonus', 'Total Gross Salary',
            'Penalty', 'Advance Payment', 'Total Deduction',
            'Net Salary (Payable)'
        ]
        SM_WIDTHS = [5, 28, 12, 16, 14, 16, 18, 14, 16, 16, 18]
        for i, w in enumerate(SM_WIDTHS):
            sm.set_column(i, i, w)

        sm.merge_range(
            0, 0, 1, len(SM_COLS) - 1,
            f"Department Payroll Summary  |  {month_start.strftime('%B %Y')}",
            sm_title_fmt,
        )
        sm.set_row(0, 30)
        sm.set_row(1, 10)

        sm_row = 2
        for c, h in enumerate(SM_COLS):
            sm.write(sm_row, c, h, sm_hdr_fmt)
        sm.set_row(sm_row, 30)
        sm_row += 1

        sm_sl = 1
        for dept in sorted(dept_summary.keys()):
            d = dept_summary[dept]
            # Net of the attendance-based deduction, capped per employee at
            # their own total gross before summing — see attendance_ded_capped
            # in the main loop above.
            gross_after_attendance = d['gross'] - d['attendance_ded_capped']
            total_gross = gross_after_attendance + d['last_month'] + d['bonus']
            total_deduction = d['penalty'] + d['advance']
            sm.set_row(sm_row, 20)
            sm.write(sm_row, 0, sm_sl,                  sm_cell_fmt)
            sm.write(sm_row, 1, dept,                    sm_cell_fmt)
            sm.write(sm_row, 2, d['employees'],           sm_num_fmt)
            sm.write(sm_row, 3, gross_after_attendance,   sm_num_fmt)
            sm.write(sm_row, 4, d['last_month'],          sm_num_fmt)
            sm.write(sm_row, 5, d['bonus'],               sm_num_fmt)
            sm.write(sm_row, 6, total_gross,              sm_num_fmt)
            sm.write(sm_row, 7, d['penalty'],             sm_num_fmt)
            sm.write(sm_row, 8, d['advance'],             sm_num_fmt)
            sm.write(sm_row, 9, total_deduction,          sm_num_fmt)
            sm.write(sm_row, 10, d['net'],                 sm_pay_fmt)
            sm_sl += 1
            sm_row += 1

        grand_gross_after_attendance = grand_totals['gross'] - grand_totals['attendance_ded_capped']
        grand_total_gross = grand_gross_after_attendance + grand_totals['last_month'] + grand_totals['bonus']
        grand_total_deduction = grand_totals['penalty'] + grand_totals['advance']
        sm.set_row(sm_row, 22)
        sm.merge_range(sm_row, 0, sm_row, 2,
                        f'GRAND TOTAL  ({len(dept_summary)} departments)', sm_tot_lbl)
        sm.write(sm_row, 3, grand_gross_after_attendance, sm_tot_num)
        sm.write(sm_row, 4, grand_totals['last_month'], sm_tot_num)
        sm.write(sm_row, 5, grand_totals['bonus'],      sm_tot_num)
        sm.write(sm_row, 6, grand_total_gross,          sm_tot_num)
        sm.write(sm_row, 7, grand_totals['penalty'],    sm_tot_num)
        sm.write(sm_row, 8, grand_totals['advance'],    sm_tot_num)
        sm.write(sm_row, 9, grand_total_deduction,      sm_tot_num)
        sm.write(sm_row, 10, grand_totals['net'],        sm_tot_pay)

        # ── Summary sheet — this month vs previous month, department-wise,
        #    with a DIFFERENCE block. Income/PF/Advance/AD/H&S Product Sale
        #    aren't tracked in this system yet, so they're left as blank,
        #    editable cells; Total Reduction and Net Pay are live formulas
        #    that recalculate once accounts fill those cells in. ─────────
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
        prev_payslips = self.env['payroll.payslip'].search([
            ('payroll_month', '=', prev_month_start),
            ('state', '!=', 'draft'),
        ]).filtered(lambda s: s.monthly_wage)
        prev_days_map = self.env['payroll.adjustment'].get_last_month_days_map(prev_month_start)

        prev_dept_summary = {}
        for slip in prev_payslips:
            emp = slip.employee_id
            dept = emp.department_id.name or 'No Department'
            pay_days = _row_calc_for(slip, emp, prev_days_map.get(emp.id, 0.0))['pay_days']
            # Same low-attendance barrier as the current month's main list
            # (MIN_PAY_DAYS_FOR_MAIN_SHEET) -- otherwise the previous-month
            # column here would include people the current month's column
            # deliberately excludes, making the two not actually comparable.
            if pay_days < MIN_PAY_DAYS_FOR_MAIN_SHEET:
                continue
            d = prev_dept_summary.setdefault(dept, {
                'employees': 0, 'pay_days': 0, 'gross': 0.0, 'bonus': 0.0, 'net': 0.0,
                'penalty': 0.0, 'advance': 0.0, 'late_ded': 0.0,
            })
            d['employees'] += 1
            d['pay_days']  += pay_days
            d['gross']     += slip.gross_salary
            d['bonus']     += slip.attendance_bonus
            d['net']       += slip.net_salary
            d['penalty']   += slip.penalty_amount
            d['advance']   += slip.advance_amount
            d['late_ded']  += slip.late_deduction

        cur_gross_total = sum(d['gross'] for d in dept_summary.values())
        prev_gross_total = sum(d['gross'] for d in prev_dept_summary.values())

        sy = wb.add_worksheet('Summary')
        # No separate "Advance" column — "AD" itself is the Advance Payment
        # total (fetched below from payroll.adjustment via each payslip's
        # already-capped advance_amount), and "H&S Product Sale" carries
        # the Penalty total the same way.
        SUMMARY_COLS = [
            'SECTION', 'No Of Employee', 'Pay Days', 'Total GROSS SALARY',
            'Attendence Incentive', 'Prsnt. Net Salary', 'Income Tax', 'PF',
            'LATE DEDUCT', 'AD', 'H&S PRODUCT SALE ADVANCE', 'Total Reduction',
            'Net Pay',
        ]
        SUMMARY_WIDTHS = [13.67, 5.35, 5.5, 8.85, 8.0, 8.85, 2.85, 5.5,
                          7.85, 6.85, 5.85, 6.5, 8.85]
        SUMMARY_IDX = {label: i for i, label in enumerate(SUMMARY_COLS)}
        for i, w in enumerate(SUMMARY_WIDTHS):
            sy.set_column(i, i, w)

        manual_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        formula_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })

        # Fixed department order/list, matching the reference workbook's
        # own Sheet3 exactly (including its spacing) — always shown, even
        # for a department with no employees this period, so "Total :"
        # and everything below it lands on the same row every month.
        # Departments outside this list (new ones added later) are
        # appended afterwards rather than silently dropped.
        FIXED_DEPARTMENT_ORDER = [
            'ADMIN', 'SALES', 'ACCOUNTS', 'MARKETING', 'AUTO MOBILE',
            'BATCH PLANT', 'ELECTRICAL', 'LABORATORY', 'MECHANICAL',
            'ORE DRESSING', 'OXYGEN PLANT', 'PRINTING  & PACKEGING',
            'PRODUCTION', 'SECURITY', 'STOCK', 'STORE',
        ]

        def _dept_row_order(dept_dict):
            extra = sorted(d for d in dept_dict if d not in FIXED_DEPARTMENT_ORDER)
            return FIXED_DEPARTMENT_ORDER + extra

        def _write_summary_table(target_ws, start_row, heading, dept_dict):
            target_ws.merge_range(start_row, 0, start_row, len(SUMMARY_COLS) - 1, heading, title_fmt)
            target_ws.set_row(start_row, 15.6)
            r = start_row + 1
            for c, h in enumerate(SUMMARY_COLS):
                target_ws.write(r, c, h, hdr_fmt)
            target_ws.set_row(r, 25.2)
            r += 1
            first_data_row = r

            tot_employees = tot_pay_days = 0
            tot_gross = tot_bonus = tot_prsnt_net = 0.0

            empty_dept = {
                'employees': 0, 'pay_days': 0, 'gross': 0.0, 'bonus': 0.0,
                'net': 0.0, 'penalty': 0.0, 'advance': 0.0, 'late_ded': 0.0,
            }
            for dept in _dept_row_order(dept_dict):
                d = dept_dict.get(dept, empty_dept)
                target_ws.set_row(r, 13.55)
                # Prsnt. Net Salary here is BEFORE Penalty/Advance/Late
                # Deduct — those are subtracted below via Total Reduction
                # instead, so they aren't double-deducted (slip.net_salary
                # already has them subtracted once).
                penalty = d.get('penalty', 0.0)
                advance = d.get('advance', 0.0)
                late_ded = d.get('late_ded', 0.0)
                prsnt_net_salary = d['net'] + penalty + advance + late_ded

                target_ws.write(r, SUMMARY_IDX['SECTION'], dept,               cell_fmt)
                target_ws.write(r, SUMMARY_IDX['No Of Employee'], d['employees'], num_fmt)
                target_ws.write(r, SUMMARY_IDX['Pay Days'], d['pay_days'],      num_fmt)
                target_ws.write(r, SUMMARY_IDX['Total GROSS SALARY'], d['gross'], num_fmt)
                target_ws.write(r, SUMMARY_IDX['Attendence Incentive'], d['bonus'], num_fmt)
                target_ws.write(r, SUMMARY_IDX['Prsnt. Net Salary'], prsnt_net_salary, num_fmt)
                # Income / PF aren't tracked in this system yet — left as
                # blank, editable cells for accounts to fill in manually.
                target_ws.write(r, SUMMARY_IDX['Income Tax'], 0, manual_fmt)
                target_ws.write(r, SUMMARY_IDX['PF'], 0, manual_fmt)
                target_ws.write(r, SUMMARY_IDX['LATE DEDUCT'], late_ded, num_fmt)
                target_ws.write(r, SUMMARY_IDX['AD'], advance, num_fmt)
                target_ws.write(r, SUMMARY_IDX['H&S PRODUCT SALE ADVANCE'], penalty, num_fmt)
                target_ws.write_formula(
                    r, SUMMARY_IDX['Total Reduction'],
                    f"={xl_rowcol_to_cell(r, SUMMARY_IDX['LATE DEDUCT'])}"
                    f"+{xl_rowcol_to_cell(r, SUMMARY_IDX['AD'])}"
                    f"+{xl_rowcol_to_cell(r, SUMMARY_IDX['H&S PRODUCT SALE ADVANCE'])}",
                    formula_fmt, late_ded + advance + penalty,
                )
                target_ws.write_formula(
                    r, SUMMARY_IDX['Net Pay'],
                    f"={xl_rowcol_to_cell(r, SUMMARY_IDX['Prsnt. Net Salary'])}"
                    f"+{xl_rowcol_to_cell(r, SUMMARY_IDX['Income Tax'])}"
                    f"-{xl_rowcol_to_cell(r, SUMMARY_IDX['Total Reduction'])}",
                    formula_fmt, d['net'],
                )
                tot_employees  += d['employees']
                tot_pay_days   += d['pay_days']
                tot_gross      += d['gross']
                tot_bonus      += d['bonus']
                tot_prsnt_net  += prsnt_net_salary
                r += 1

            last_data_row = r - 1
            total_cols = (
                SUMMARY_IDX['Income Tax'], SUMMARY_IDX['PF'], SUMMARY_IDX['LATE DEDUCT'],
                SUMMARY_IDX['AD'], SUMMARY_IDX['H&S PRODUCT SALE ADVANCE'],
                SUMMARY_IDX['Total Reduction'], SUMMARY_IDX['Net Pay'],
            )
            target_ws.set_row(r, 13.55)
            target_ws.write(r, SUMMARY_IDX['SECTION'], 'Total :', tot_lbl)
            target_ws.write(r, SUMMARY_IDX['No Of Employee'], tot_employees, tot_fmt)
            target_ws.write(r, SUMMARY_IDX['Pay Days'], tot_pay_days,  tot_fmt)
            target_ws.write(r, SUMMARY_IDX['Total GROSS SALARY'], tot_gross,     tot_fmt)
            target_ws.write(r, SUMMARY_IDX['Attendence Incentive'], tot_bonus,      tot_fmt)
            target_ws.write(r, SUMMARY_IDX['Prsnt. Net Salary'], tot_prsnt_net, tot_fmt)
            if last_data_row >= first_data_row:
                for col in total_cols:
                    target_ws.write_formula(
                        r, col,
                        f"=SUM({xl_rowcol_to_cell(first_data_row, col)}:"
                        f"{xl_rowcol_to_cell(last_data_row, col)})",
                        tot_fmt, 0,
                    )
            else:
                for col in total_cols:
                    target_ws.write(r, col, 0, tot_fmt)
            return r + 1

        sy_row = _write_summary_table(
            sy, 0,
            f"SUMMARY {month_start.strftime('%B').upper()} - {month_start.year}",
            dept_summary,
        )
        sy_row += 1

        # ── DIFFERENCE block — exactly 7 rows (matching the reference
        #    Sheet3's rows 21-27), not a separate title block above it:
        #    col A = category label, B:C merged = headcount, D = amount,
        #    E merged over rows 1-4 = first-4 subtotal, E merged over
        #    rows 5-7 = last-3 subtotal, F:H merged over all 7 rows =
        #    the overall difference, I:J on row 1 = "DIFFERENCE" label.
        # ─────────────────────────────────────────────────────────────
        block_row0 = sy_row  # first of the 7 rows (0-based)

        # Re Active: employees whose employee.departure.history (from
        # zk_adms_attendance) shows a 'reactivated' event within this
        # joining cycle window, matched against this month's payslips.
        reactivated_emp_ids = set(
            self.env['employee.departure.history'].search([
                ('action', '=', 'reactivated'),
                ('event_date', '>=', new_joiner_from),
                ('event_date', '<=', new_joiner_to),
            ]).mapped('employee_id.id')
        )
        reactive_rows = [
            row for row in all_rows
            if row[0].employee_id.id in reactivated_emp_ids
        ]
        reactive_gross_total = sum(slip.gross_salary for slip, _pd, _lmd in reactive_rows)

        # Salary Increase / Decrease: compare this month's monthly_wage to
        # last month's payslip for the same employee. New joiners (no
        # payslip last month) have nothing to compare against and are
        # skipped here.
        prev_wage_by_emp = {s.employee_id.id: s.monthly_wage for s in prev_payslips}
        salary_increase_count = salary_decrease_count = 0
        salary_increase_amount = salary_decrease_amount = 0.0
        for slip, _pd, _lmd in all_rows:
            prev_wage = prev_wage_by_emp.get(slip.employee_id.id)
            if prev_wage is None:
                continue
            wage_diff = slip.monthly_wage - prev_wage
            if wage_diff > 0:
                salary_increase_count += 1
                salary_increase_amount += wage_diff
            elif wage_diff < 0:
                salary_decrease_count += 1
                salary_decrease_amount += -wage_diff

        # Computed rows: New Joining Member (contract_date_start within
        # this joining cycle — same list as the "New Joiners" sheet),
        # Re Active and Salary Increase/Decrease (above), and Pending
        # Manpower (the "Low Attendance" sheet's headcount/gross).
        # New Joining Officer / Pending Officer need a Member-vs-Officer
        # split that isn't defined yet — left blank for manual entry.
        difference_rows = [
            ('New Joining Member', len(new_joiner_rows), nj_gross_total, False),
            ('New Joining Officer', 0, 0.0, True),
            ('Re Active', len(reactive_rows), reactive_gross_total, False),
            ('Salary Increase', salary_increase_count, salary_increase_amount, False),
            ('Pending Manpower', len(low_attendance_rows), low_totals['gross'], False),
            ('Salary Decrease', salary_decrease_count, salary_decrease_amount, False),
            ('Pending Officer', 0, 0.0, True),
        ]
        for i, (label, emp_count, amount, is_manual) in enumerate(difference_rows):
            row = block_row0 + i
            sy.set_row(row, 13.55)
            sy.write(row, 0, label, cell_fmt)
            sy.merge_range(row, 1, row, 2, emp_count, manual_fmt if is_manual else num_fmt)
            sy.write(row, 3, amount, manual_fmt if is_manual else num_fmt)

        # First 4 rows (New Joining Member/Officer, Re Active, Salary
        # Increase) vs last 3 (Pending Manpower, Salary Decrease, Pending
        # Officer), and their difference — shown as merged boxes spanning
        # their respective rows, matching the reference layout exactly
        # (E: first-4/last-3 subtotal, F:H: overall difference spanning
        # all 7 rows, I:J row 1: "DIFFERENCE" label).
        first4_amount = sum(amount for _label, _cnt, amount, _m in difference_rows[:4])
        last3_amount = sum(amount for _label, _cnt, amount, _m in difference_rows[4:])

        sy.merge_range(block_row0, 4, block_row0 + 3, 4, first4_amount, tot_fmt)
        sy.merge_range(block_row0 + 4, 4, block_row0 + 6, 4, last3_amount, tot_fmt)
        sy.merge_range(block_row0, 5, block_row0 + 6, 7, first4_amount - last3_amount, tot_fmt)
        sy.merge_range(block_row0, 8, block_row0, 9, 'DIFFERENCE', title_fmt)

        # This month's total gross salary minus the previous month's
        # (across all departments, not just the first-4/last-3 groups).
        sy.merge_range(
            block_row0 + 1, 8, block_row0 + 6, 9,
            cur_gross_total - prev_gross_total, tot_fmt,
        )
        # Small box (K:L, row 4 only) — purpose still unconfirmed, left
        # blank/bordered rather than guessed at.
        sy.merge_range(block_row0 + 3, 10, block_row0 + 3, 11, '', cell_fmt)

        sy_row = block_row0 + 7 + 1
        sy_row = _write_summary_table(
            sy, sy_row,
            f"SUMMARY {prev_month_start.strftime('%B').upper()} - {prev_month_start.year}",
            prev_dept_summary,
        )

        wb.close()
        output.seek(0)

        fname = f"payroll_export_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Bank Transfer Export
#  (SL, Badge No, Name, Bank Account No, Net Pay — sorted by badge)
# ═══════════════════════════════════════════════════════════════════

class PayrollBankExportWizard(models.TransientModel):
    _name = 'payroll.bank.export.wizard'
    _description = 'Export Net Pay for Bank Transfer'

    export_month = fields.Date(
        string='Export Payroll Month',
        help="Select a month to export the bank transfer list as Excel.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def action_export_bank_list(self):
        self.ensure_one()
        if not self.export_month:
            raise UserError("Please select a month to export.")
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.export_month.replace(day=1)

        payslips = self.env['payroll.payslip'].search([
            ('payroll_month', '=', month_start),
            ('state', '!=', 'draft'),
        ])

        if not payslips:
            raise UserError(f"No payslips found for {month_start.strftime('%B %Y')}.")

        def _badge_sort_key(slip):
            badge = slip.employee_id.zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        sorted_payslips = sorted(payslips, key=_badge_sort_key)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 14,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter',
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        cell_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'valign': 'vcenter',
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter',
        })
        tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'num_format': '#,##0', 'valign': 'vcenter',
        })

        COLS = ['SL', 'ID No', 'Name', 'Bank Account No', 'Net Pay']
        WIDTHS = [6, 12, 28, 22, 15]

        ws = wb.add_worksheet(month_start.strftime('%b %Y')[:31])
        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        ws.merge_range(
            0, 0, 1, len(COLS) - 1,
            f"Bank Transfer List  |  {month_start.strftime('%B %Y')}",
            title_fmt,
        )
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        for c, h in enumerate(COLS):
            ws.write(row, c, h, hdr_fmt)
        ws.set_row(row, 30)
        row += 1

        total_net = 0.0
        for sl_no, slip in enumerate(sorted_payslips, start=1):
            emp = slip.employee_id
            ws.write(row, 0, sl_no,                   cell_fmt)
            ws.write(row, 1, emp.zk_badge_no or '',    cell_fmt)
            ws.write(row, 2, emp.name or '',           cell_fmt)
            ws.write(row, 3, emp.bank_acc_no or '',    cell_fmt)
            ws.write(row, 4, slip.net_salary,          num_fmt)
            total_net += slip.net_salary
            row += 1

        ws.merge_range(row, 0, row, 3, f'GRAND TOTAL  ({len(sorted_payslips)} employees)', tot_lbl)
        ws.write(row, 4, total_net, tot_fmt)

        wb.close()
        output.seek(0)

        fname = f"bank_transfer_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Overtime Pay Export
#
#  One row per employee, grouped by department.
#  Columns:
#    SL | Badge No | Employee | Job | Department |
#    Base Wage | Eligible Pay (max 12500) |
#    Day-1 … Day-N (validated OT hours per calendar day) |
#    Total OT Hours | Hourly Rate (min(eligible/208, 60)) | OT Pay
#
#  Source: hr.attendance where overtime_status = 'approved'
#          and employee worker_type = 'regular'
#  Hours field: validated_overtime_hours
# ═══════════════════════════════════════════════════════════════════

class OvertimeExportWizard(models.TransientModel):
    _name = 'payroll.overtime.export.wizard'
    _description = 'Export Overtime Pay'

    export_month = fields.Date(
        string='OT Month',
        required=True,
        default=lambda self: date.today().replace(day=1),
        help="Pick any date — only year+month are used.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    # ── main action ───────────────────────────────────────────────────

    def action_export_overtime(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.export_month.replace(day=1)
        last_day = monthrange(month_start.year, month_start.month)[1]
        month_end = month_start.replace(day=last_day)

        utc_from, _ = _dhaka_day_to_utc_range(month_start)
        _, utc_to   = _dhaka_day_to_utc_range(month_end)

        ot_records = self.env['hr.attendance'].search([
            ('check_in', '>=', utc_from),
            ('check_in', '<=', utc_to),
            ('overtime_status', '=', 'approved'),
            ('employee_id.worker_type', '=', 'regular'),
            ('employee_id.active', '=', True),
        ], order='employee_id, check_in')

        if not ot_records:
            raise UserError(
                f"No approved overtime records found for {month_start.strftime('%B %Y')}."
            )

        # ── aggregate per employee ────────────────────────────────────
        # emp_data[emp_id] = {
        #     'employee': rec,
        #     'days': {day_int: hours},   day_int in 1..last_day
        # }
        emp_data = {}
        for att in ot_records:
            emp = att.employee_id
            hours = att.validated_overtime_hours or 0.0
            if not hours:
                continue
            check_in_dhaka = _to_dhaka(att.check_in)
            if not check_in_dhaka:
                continue
            day_int = check_in_dhaka.date().day   # 1-based calendar day

            if emp.id not in emp_data:
                emp_data[emp.id] = {'employee': emp, 'days': {}}
            emp_data[emp.id]['days'][day_int] = (
                emp_data[emp.id]['days'].get(day_int, 0.0) + hours
            )

        emp_data = {k: v for k, v in emp_data.items() if v['days']}
        if not emp_data:
            raise UserError(
                f"No validated overtime hours found for {month_start.strftime('%B %Y')}."
            )

        # ── flat list, sorted by badge number ──────────────────────────
        def _badge_key(d):
            badge = d['employee'].zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        sorted_rows = sorted(emp_data.values(), key=_badge_key)

        # ── column layout ─────────────────────────────────────────────
        # Fixed columns before the day columns
        # 0  SL
        # 1  Badge No
        # 2  Employee Name
        # 3  Job Title
        # 4  Department
        # 5  Base Wage
        # 6  Eligible Pay (min(wage, 12500))
        # 7 … 7+last_day-1   Day 1 … Day N
        # 7+last_day          Total OT Hours
        # 7+last_day+1        Hourly Rate
        # 7+last_day+2        OT Pay

        N_FIXED  = 7
        N_DAYS   = last_day
        COL_TOT  = N_FIXED + N_DAYS
        COL_RATE = COL_TOT + 1
        COL_PAY  = COL_TOT + 2
        TOTAL_COLS = COL_PAY + 1

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        # ── formats ──────────────────────────────────────────────────
        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 13,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter',
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        day_hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 8,
            'bg_color': '#2E5FA3', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter',
        })
        cell_fmt = wb.add_format({
            'align': 'center',
            'font_name': 'Arial', 'font_size': 9, 'border': 1, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 9, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        int_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 9, 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        # Day cell with OT hours
        day_val_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 8, 'border': 1,
            'num_format': '0.##', 'align': 'center', 'valign': 'vcenter',
            'bg_color': '#EBF7E6',
        })
        # Day cell empty
        day_empty_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 8, 'border': 1,
            'align': 'center', 'valign': 'vcenter',
            'bg_color': '#F9F9F9',
        })
        # Grand total
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter',
        })
        tot_num = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        tot_pay = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#C6EFCE', 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        ot_pay_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#E2EFDA', 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })

        sheet_label = month_start.strftime('%b %Y')[:31]
        ws = wb.add_worksheet(sheet_label)

        # Column widths
        ws.set_column(0, 0, 5)    # SL
        ws.set_column(1, 1, 11)   # Badge No
        ws.set_column(2, 2, 26)   # Employee Name
        ws.set_column(3, 3, 20)   # Job Title
        ws.set_column(4, 4, 22)   # Department
        ws.set_column(5, 5, 12)   # Base Wage
        ws.set_column(6, 6, 13)   # Eligible Pay
        for d in range(N_DAYS):
            ws.set_column(N_FIXED + d, N_FIXED + d, 4)  # day columns narrow
        ws.set_column(COL_TOT,  COL_TOT,  11)   # Total OT Hours
        ws.set_column(COL_RATE, COL_RATE, 13)   # Hourly Rate
        ws.set_column(COL_PAY,  COL_PAY,  13)   # OT Pay

        # Title row
        title_str = (
            f"Overtime Pay Summary  |  {month_start.strftime('%B %Y')}"
            f"  |  Eligible Pay = min(Wage, 12,500)  |  Rate = Eligible ÷ 208  (max 60 BDT)"
        )
        ws.merge_range(0, 0, 1, TOTAL_COLS - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        grand_hours = 0
        grand_pay   = 0
        sl_no = 1

        # Column headers
        ws.write(row, 0, 'SL',              hdr_fmt)
        ws.write(row, 1, 'Badge No',        hdr_fmt)
        ws.write(row, 2, 'Employee Name',   hdr_fmt)
        ws.write(row, 3, 'Job Title',       hdr_fmt)
        ws.write(row, 4, 'Department',      hdr_fmt)
        ws.write(row, 5, 'Base\nWage',      hdr_fmt)
        ws.write(row, 6, 'Eligible\nPay\n(max 12,500)', hdr_fmt)
        for d in range(1, N_DAYS + 1):
            ws.write(row, N_FIXED + d - 1, str(d), day_hdr_fmt)
        ws.write(row, COL_TOT,  'Total OT\nHours', hdr_fmt)
        ws.write(row, COL_RATE, 'Hourly\nRate',    hdr_fmt)
        ws.write(row, COL_PAY,  'OT Pay',          hdr_fmt)
        ws.set_row(row, 40)
        row += 1

        for data in sorted_rows:
            emp        = data['employee']
            days_dict  = data['days']
            wage       = emp.get_wage(month_start) or 0.0
            eligible   = min(wage, 12500.0)
            hourly_rate = min(eligible / 208.0, 60.0) if eligible else 0.0
            total_hours = sum(days_dict.values())
            ot_pay      = total_hours * hourly_rate

            # whole numbers only — truncate (floor), never round up
            total_hours_i = math.floor(total_hours)
            hourly_rate_i = math.floor(hourly_rate)
            ot_pay_i      = math.floor(ot_pay)

            grand_hours += total_hours_i
            grand_pay   += ot_pay_i

            ws.set_row(row, 18)
            ws.write(row, 0, sl_no,                        int_fmt)
            ws.write(row, 1, emp.zk_badge_no or '',        cell_fmt)
            ws.write(row, 2, emp.name or '',               cell_fmt)
            ws.write(row, 3, emp.job_id.name or '',        cell_fmt)
            ws.write(row, 4, emp.department_id.name or '', cell_fmt)
            ws.write(row, 5, wage,                         num_fmt)
            ws.write(row, 6, eligible,                     num_fmt)

            for d in range(1, N_DAYS + 1):
                col = N_FIXED + d - 1
                if d in days_dict:
                    ws.write(row, col, days_dict[d], day_val_fmt)
                else:
                    ws.write(row, col, '',           day_empty_fmt)

            ws.write(row, COL_TOT,  total_hours_i, num_fmt)
            ws.write(row, COL_RATE, hourly_rate_i, num_fmt)
            ws.write(row, COL_PAY,  ot_pay_i,      ot_pay_fmt)

            sl_no += 1
            row += 1

        total_emp_count = len(sorted_rows)

        # Grand total
        ws.set_row(row, 24)
        ws.merge_range(row, 0, row, COL_TOT - 1,
                       f'GRAND TOTAL  ({total_emp_count} employees)', tot_lbl)
        ws.write(row, COL_TOT,  grand_hours, tot_num)
        ws.write(row, COL_RATE, '',           tot_lbl)
        ws.write(row, COL_PAY,  grand_pay,   tot_pay)

        wb.close()
        output.seek(0)

        fname = f"overtime_export_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }