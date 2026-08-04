from odoo import models, fields, api
from odoo.exceptions import UserError
from datetime import date, datetime, time, timedelta
from calendar import monthrange
import base64
import io
import logging
import pytz

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None

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
    monthly_wage = fields.Float(string='Monthly Wage', digits=(16, 2))

    calendar_days_in_period = fields.Integer(string='Calendar Days in Month')
    day_off_count = fields.Integer(string='Weekly Off Days in Month')
    working_days_in_period = fields.Integer(string='Working Days in Month')
    per_day_rate = fields.Float(string='Per Day Rate (Wage÷CalDays)', digits=(16, 4))

    # ── salary components ─────────────────────────────────────────────
    basic_pct = fields.Float(string='Basic %', digits=(5, 2))
    hra_pct = fields.Float(string='HRA %', digits=(5, 2))
    travel_pct = fields.Float(string='Travel %', digits=(5, 2))
    medical_pct = fields.Float(string='Medical %', digits=(5, 2))

    basic_amount = fields.Float(string='Basic', digits=(16, 2))
    hra_amount = fields.Float(string='HRA', digits=(16, 2))
    travel_amount = fields.Float(string='Travel', digits=(16, 2))
    medical_amount = fields.Float(string='Medical', digits=(16, 2))
    gross_salary = fields.Float(string='Gross Salary', digits=(16, 2))

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
    overtime_pay = fields.Float(string='Overtime Pay', digits=(16, 2),
                                help='Stored for reference only — not included in net salary.')

    # ── deductions ────────────────────────────────────────────────────
    absent_deduction = fields.Float(string='Absent Deduction', digits=(16, 2))
    late_deduction = fields.Float(string='Late Deduction', digits=(16, 2))
    unpaid_leave_deduction = fields.Float(string='Unpaid Leave Deduction', digits=(16, 2))
    total_deductions = fields.Float(string='Total Deductions', digits=(16, 2))

    # ── net (NO overtime included) ────────────────────────────────────
    net_salary = fields.Float(string='Net Salary', digits=(16, 2))
    notes = fields.Text(string='Notes')

    # ── bonus ─────────────────────────────────────────────────────────
    attendance_bonus = fields.Float(string='Attendance Bonus', digits=(16, 2))
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
        help='Day-off-marker and sandwich-rule absences. Always cut at 1×, '
             'regardless of department bonus eligibility.',
    )

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
            self.monthly_wage = self.employee_id.wage or 0.0

    @api.onchange('payroll_month')
    def _onchange_payroll_month(self):
        if self.payroll_month:
            self.payroll_month = self.payroll_month.replace(day=1)

    # ── working-day helpers ───────────────────────────────────────────

    def _count_weekday_occurrences(self, weekday_int, date_from, date_to):
        count = 0
        cursor = date_from
        while cursor <= date_to:
            if cursor.weekday() == weekday_int:
                count += 1
            cursor += timedelta(days=1)
        return count

    def _working_days_for_period(self, employee, date_from, date_to):
        calendar_days = (date_to - date_from).days + 1
        if employee.day_off_day:
            day_off_int = int(employee.day_off_day)
            off_count = self._count_weekday_occurrences(day_off_int, date_from, date_to)
        else:
            off_count = 0
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
        day_off_int = int(employee.day_off_day) if employee.day_off_day else None
        dates = set()
        cursor = date_from
        while cursor <= date_to:
            if day_off_int is None or cursor.weekday() != day_off_int:
                dates.add(cursor)
            cursor += timedelta(days=1)
        return dates

    # ── main compute ──────────────────────────────────────────────────

    def action_compute(self):
        """Compute selected payslips, skipping any whose employee is
        archived/inactive. A notification lists any skipped employees
        instead of hard-failing the whole batch."""
        archived_slips = self.filtered(lambda s: not s.employee_id.active)
        computable_slips = self - archived_slips

        for slip in computable_slips:
            slip._compute_payslip()

        if archived_slips:
            names = ', '.join(archived_slips.mapped('employee_id.name'))
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Some payslips were skipped',
                    'message': (
                        f"Skipped {len(archived_slips)} payslip(s) for archived/inactive "
                        f"employee(s): {names}"
                    ),
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

        date_from = self.date_from
        date_to = self.date_to
        wage = employee.wage or 0.0

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

        stats = self._analyse_attendance(employee, date_from, date_to, scheduled_dates)

        dept_eligible = bool(
            employee.department_id and employee.department_id.eligible_for_bonus
        )
        # Eligible depts: genuine no-show absents → 2× per_day deduction.
        # marked_as_day_off days and sandwich absents → always 1× for everyone.
        absent_rate = per_day * 2 if dept_eligible else per_day

        genuine_absent_ded = stats['genuine_absent_days'] * absent_rate
        flat_absent_ded    = stats['flat_absent_days'] * per_day   # always 1×
        absent_ded  = genuine_absent_ded + flat_absent_ded
        late_ded    = stats['late_days'] * (per_day / 2)
        unpaid_ded  = stats['unpaid_leave_days'] * per_day
        total_ded   = absent_ded + late_ded + unpaid_ded

        bonus = 0.0
        if dept_eligible:
            perfect = (
                stats['absent_days'] == 0
                and stats['approved_leave_days'] == 0
                and stats['unpaid_leave_days'] == 0
            )
            if perfect:
                bonus = 500.0

        # ── Net salary does NOT include overtime pay ───────────────────
        # Overtime is calculated and paid separately via the OT Export wizard.
        net = max(gross + bonus - total_ded, 0.0)

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

            'hourly_rate': hourly_rate,

            'absent_deduct_per_day': absent_rate,  # rate for genuine absents (1× or 2×); flat absents always 1×
            'absent_deduction': absent_ded,
            'late_deduction': late_ded,
            'unpaid_leave_deduction': unpaid_ded,
            'total_deductions': total_ded,
            'attendance_bonus': bonus,

            # overtime_hours / overtime_pay intentionally NOT updated here;
            # they are populated by the separate OT wizard if needed for reference.
            'net_salary': net,
            'state': 'computed',
        })

    # ── attendance analysis ───────────────────────────────────────────

    def _analyse_attendance(self, employee, date_from, date_to, scheduled_dates):
        """
        Classify every scheduled working day for the employee.

        Priority per day (highest → lowest):
          1. Paid leave (approved, non-LWP)
          2. LWP leave  (leave_code = 'LWP' OR holiday_status_id.unpaid)
          3. ADJUST day (hr.swap swap_off_date whose required work was actually done)
             → counts as present; if required work NOT done → absent
          4. Present / late (attendance record exists, Dhaka-localised date)
             – marked_as_day_off attendance → treated as ABSENT (1-day deduction)
          5. Absent (no attendance, no leave)

        Sandwich rule (applies to ALL employees):
          If both the day before and after a weekend/day-off are absent,
          that weekend day is also counted as absent.

        Returns dict with keys:
            present_days, absent_days, late_days,
            approved_leave_days, unpaid_leave_days (includes LWP),
            adjust_days, lwp_days
        """
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
                if cursor in scheduled_dates:
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
            adjust_valid_dates.add(off_date)

        # ── 4. classify each scheduled day ───────────────────────────
        present = late = approved_leave = unpaid_leave = adjust = 0
        # Separate absent buckets so deduction rates can differ:
        #   genuine_absent  → 2× per_day for eligible depts, 1× otherwise
        #   flat_absent     → always 1× per_day (marked_as_day_off + sandwich)
        genuine_absent = 0
        flat_absent = 0

        for day in scheduled_dates:
            if day in paid_leave_dates:
                approved_leave += 1

            elif day in unpaid_leave_dates:
                unpaid_leave += 1

            elif day in adjust_absent_dates:
                genuine_absent += 1

            elif day in adjust_valid_dates:
                present += 1
                adjust += 1

            elif day in att_by_date:
                att = att_by_date[day]
                if att.marked_as_day_off:
                    # ZK device marked this record as day-off → 1× cut always
                    flat_absent += 1
                elif att.is_late:
                    present += 1
                    late += 1
                else:
                    present += 1

            else:
                genuine_absent += 1

        # ── 5. sandwich absent rule (ALL employees) ───────────────────
        # If both the working-day immediately before and after a weekend
        # day are absent (no leave, no attendance, no adjust), that
        # weekend day is penalised — always at 1× rate (flat_absent).
        sandwich_absent = 0

        if employee.day_off_day:
            weekend_day = int(employee.day_off_day)

            cursor = date_from
            while cursor <= date_to:
                if cursor.weekday() == weekend_day:
                    prev_day = cursor - timedelta(days=1)
                    next_day = cursor + timedelta(days=1)

                    if date_from <= prev_day <= date_to and date_from <= next_day <= date_to:

                        def is_effective_absent(day):
                            return (
                                day in scheduled_dates
                                and day not in paid_leave_dates
                                and day not in unpaid_leave_dates
                                and day not in adjust_valid_dates
                                and day not in adjust_absent_dates
                                and day not in att_by_date
                            )

                        if is_effective_absent(prev_day) and is_effective_absent(next_day):
                            sandwich_absent += 1

                cursor += timedelta(days=1)

        flat_absent += sandwich_absent
        absent = genuine_absent + flat_absent

        return {
            'present_days': present,
            'absent_days': absent,
            'genuine_absent_days': genuine_absent,   # eligible for 2× cut
            'flat_absent_days': flat_absent,          # always 1× (day-off marker + sandwich)
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
        empty_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'bg_color': '#F2F2F2', 'align': 'center', 'valign': 'vcenter',
        })

        IDX_PAY_DAYS = 11

        # NOTE: Section (Designation/Job Title) now precedes Department,
        # per request — designation first, then department.
        COLS = [
            'SL', 'Badge No', 'Employee Name', 'Designation', 'Department',
            'Total Working\nDay',
            'Actual Absent\n(Absent+CL+SL+LWP)',
            'Eligible Double\nCutting Day',
            'Total Cutting',
            'Leave Approve\n(CL+SL)',
            'Last Month',
            'Pay Days',
            'Attendance Bonus',
            'Remarks',
        ]
        WIDTHS = [5, 11, 26, 20, 24, 13, 22, 18, 13, 15, 13, 11, 14, 22]

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
            ws.write(row, 10, '',                            empty_fmt)
            ws.write(row, IDX_PAY_DAYS, pay_days,            hi_fmt)
            ws.write(row, 12, bonus,                         num_fmt)
            ws.write(row, 13, remarks,                       cell_fmt)

            for k, v in [
                ('total_wd', total_wd), ('act_absent', act_absent),
                ('dbl_cut', dbl_cut), ('tot_cut', tot_cut),
                ('lv_approve', lv_approve), ('pay_days', pay_days),
                ('bonus', bonus),
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
        ws.write(row, 10, '',                  tot_fmt)
        ws.write(row, IDX_PAY_DAYS, grand['pay_days'], hi_tot_fmt)
        ws.write(row, 12, grand['bonus'],      tot_fmt)
        ws.write(row, 13, '',                  tot_lbl)

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

        groups = {}
        for slip in payslips:
            emp = slip.employee_id
            dept = emp.department_id.name or 'No Department'
            groups.setdefault(dept, []).append(slip)

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 14,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True
        })
        grp_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 12,
            'bg_color': '#D9E1F2', 'border': 1,
            'align': 'center', 'valign': 'vcenter'
        })
        cell_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'
        })
        num_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0.00', 'valign': 'vcenter'
        })
        tot_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'num_format': '#,##0.00', 'valign': 'vcenter'
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'
        })

        # OT Hours / OT Pay columns removed earlier.
        # ADJUST Days, LWP Days, Unpaid Leave and Late Days columns
        # removed per request — deductions derived from them are kept.
        COLS = [
            'SL', 'Badge No', 'Employee Name', 'Department',
            'Basic', 'HRA', 'Travel', 'Medical', 'Gross Salary',
            'Present Days', 'Approved Leave', 'Absent Days',
            'Absent Deduction', 'Late Deduction',
            'Unpaid Leave Deduction', 'Total Deductions',
            'Attendance Bonus', 'Net Salary'
        ]
        WIDTHS = [6, 12, 24, 28, 12, 12, 12, 12, 12,
                  12, 14, 12, 14, 14, 16, 12, 14, 12]

        sheet_label = month_start.strftime('%b %Y')[:31]
        ws = wb.add_worksheet(sheet_label)

        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        title_str = f"Monthly Payroll Report  |  {month_start.strftime('%B %Y')}"
        ws.merge_range(0, 0, 1, len(COLS) - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        grand_totals = {
            'gross': 0.0, 'present': 0, 'leave': 0,
            'absent': 0,
            'absent_ded': 0.0, 'late_ded': 0.0, 'unpaid_ded': 0.0,
            'total_ded': 0.0, 'bonus': 0.0, 'net': 0.0
        }
        # Collected alongside the main loop below, used to build the
        # "Department Summary" sheet (money payable per department).
        dept_summary = {}

        for dept in sorted(groups.keys()):
            recs = groups[dept]

            ws.merge_range(row, 0, row, len(COLS) - 1, dept, grp_fmt)
            ws.set_row(row, 30)
            row += 1

            for c, h in enumerate(COLS):
                ws.write(row, c, h, hdr_fmt)
            ws.set_row(row, 42)
            row += 1

            group_totals = {
                'gross': 0.0, 'present': 0, 'leave': 0,
                'absent': 0,
                'absent_ded': 0.0, 'late_ded': 0.0, 'unpaid_ded': 0.0,
                'total_ded': 0.0, 'bonus': 0.0, 'net': 0.0
            }
            sl_no = 1

            for slip in recs:
                emp = slip.employee_id

                ws.set_row(row, 24)
                ws.write(row, 0,  sl_no,                            cell_fmt)
                ws.write(row, 1,  emp.zk_badge_no or '',            cell_fmt)
                ws.write(row, 2,  emp.name or '',                    cell_fmt)
                ws.write(row, 3,  emp.department_id.name or '',      cell_fmt)
                ws.write(row, 4,  slip.basic_amount,                 num_fmt)
                ws.write(row, 5,  slip.hra_amount,                   num_fmt)
                ws.write(row, 6,  slip.travel_amount,                num_fmt)
                ws.write(row, 7,  slip.medical_amount,               num_fmt)
                ws.write(row, 8,  slip.gross_salary,                 num_fmt)
                ws.write(row, 9,  slip.present_days,                 cell_fmt)
                ws.write(row, 10, slip.approved_leave_days,          cell_fmt)
                ws.write(row, 11, slip.absent_days,                  cell_fmt)
                ws.write(row, 12, slip.absent_deduction,             num_fmt)
                ws.write(row, 13, slip.late_deduction,               num_fmt)
                ws.write(row, 14, slip.unpaid_leave_deduction,       num_fmt)
                ws.write(row, 15, slip.total_deductions,             num_fmt)
                ws.write(row, 16, slip.attendance_bonus,             num_fmt)
                ws.write(row, 17, slip.net_salary,                   num_fmt)

                group_totals['gross']      += slip.gross_salary
                group_totals['present']    += slip.present_days
                group_totals['leave']      += slip.approved_leave_days
                group_totals['absent']     += slip.absent_days
                group_totals['absent_ded'] += slip.absent_deduction
                group_totals['late_ded']   += slip.late_deduction
                group_totals['unpaid_ded'] += slip.unpaid_leave_deduction
                group_totals['total_ded']  += slip.total_deductions
                group_totals['bonus']      += slip.attendance_bonus
                group_totals['net']        += slip.net_salary

                sl_no += 1
                row += 1

            ws.set_row(row, 22)
            ws.merge_range(row, 0, row, 3, f'Subtotal  ({len(recs)} employees)', tot_lbl)
            ws.write(row, 4,  group_totals['gross'],      tot_fmt)
            ws.write(row, 5,  '',                          tot_lbl)
            ws.write(row, 6,  '',                          tot_lbl)
            ws.write(row, 7,  '',                          tot_lbl)
            ws.write(row, 8,  group_totals['gross'],      tot_fmt)
            ws.write(row, 9,  group_totals['present'],    tot_fmt)
            ws.write(row, 10, group_totals['leave'],      tot_fmt)
            ws.write(row, 11, group_totals['absent'],     tot_fmt)
            ws.write(row, 12, group_totals['absent_ded'], tot_fmt)
            ws.write(row, 13, group_totals['late_ded'],   tot_fmt)
            ws.write(row, 14, group_totals['unpaid_ded'], tot_fmt)
            ws.write(row, 15, group_totals['total_ded'],  tot_fmt)
            ws.write(row, 16, group_totals['bonus'],      tot_fmt)
            ws.write(row, 17, group_totals['net'],        tot_fmt)
            row += 2

            dept_summary[dept] = dict(group_totals)
            dept_summary[dept]['employees'] = len(recs)

            for key in grand_totals:
                grand_totals[key] += group_totals[key]

        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, 3, 'GRAND TOTAL', tot_lbl)
        ws.write(row, 4,  grand_totals['gross'],      tot_fmt)
        ws.write(row, 5,  '',                          tot_lbl)
        ws.write(row, 6,  '',                          tot_lbl)
        ws.write(row, 7,  '',                          tot_lbl)
        ws.write(row, 8,  grand_totals['gross'],      tot_fmt)
        ws.write(row, 9,  grand_totals['present'],    tot_fmt)
        ws.write(row, 10, grand_totals['leave'],      tot_fmt)
        ws.write(row, 11, grand_totals['absent'],     tot_fmt)
        ws.write(row, 12, grand_totals['absent_ded'], tot_fmt)
        ws.write(row, 13, grand_totals['late_ded'],   tot_fmt)
        ws.write(row, 14, grand_totals['unpaid_ded'], tot_fmt)
        ws.write(row, 15, grand_totals['total_ded'],  tot_fmt)
        ws.write(row, 16, grand_totals['bonus'],      tot_fmt)
        ws.write(row, 17, grand_totals['net'],        tot_fmt)

        # ── Department Summary sheet — how much money each department
        #    needs to disburse this month ────────────────────────────
        sm = wb.add_worksheet('Department Summary')

        sm_title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 14,
            'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'
        })
        sm_hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#1F3864', 'font_color': '#FFFFFF',
            'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True
        })
        sm_cell_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'
        })
        sm_num_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'
        })
        sm_pay_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#E2EFDA', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'
        })
        sm_tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'
        })
        sm_tot_num = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'
        })
        sm_tot_pay = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#C6EFCE', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'
        })

        SM_COLS = [
            'SL', 'Department', 'Employees', 'Gross Salary',
            'Total Deductions', 'Attendance Bonus', 'Net Salary (Payable)'
        ]
        SM_WIDTHS = [5, 28, 12, 16, 16, 16, 18]
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
            sm.set_row(sm_row, 20)
            sm.write(sm_row, 0, sm_sl,                  sm_cell_fmt)
            sm.write(sm_row, 1, dept,                    sm_cell_fmt)
            sm.write(sm_row, 2, d['employees'],           sm_num_fmt)
            sm.write(sm_row, 3, d['gross'],               sm_num_fmt)
            sm.write(sm_row, 4, d['total_ded'],           sm_num_fmt)
            sm.write(sm_row, 5, d['bonus'],               sm_num_fmt)
            sm.write(sm_row, 6, d['net'],                 sm_pay_fmt)
            sm_sl += 1
            sm_row += 1

        sm.set_row(sm_row, 22)
        sm.merge_range(sm_row, 0, sm_row, 2,
                        f'GRAND TOTAL  ({len(dept_summary)} departments)', sm_tot_lbl)
        sm.write(sm_row, 3, grand_totals['gross'],      sm_tot_num)
        sm.write(sm_row, 4, grand_totals['total_ded'],  sm_tot_num)
        sm.write(sm_row, 5, grand_totals['bonus'],      sm_tot_num)
        sm.write(sm_row, 6, grand_totals['net'],        sm_tot_pay)

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

        # ── group by department, sort by badge within dept ────────────
        dept_groups = {}
        for data in emp_data.values():
            dept = data['employee'].department_id.name or 'No Department'
            dept_groups.setdefault(dept, []).append(data)

        def _badge_key(d):
            badge = d['employee'].zk_badge_no or ''
            try:
                return (0, int(badge))
            except (ValueError, TypeError):
                return (1, badge)

        for dept in dept_groups:
            dept_groups[dept].sort(key=_badge_key)

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
        dept_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 11,
            'bg_color': '#D9E1F2', 'border': 1,
            'align': 'left', 'valign': 'vcenter',
        })
        cell_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 9, 'border': 1, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 9, 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
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
        # Subtotal
        sub_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#D9E1F2', 'border': 1, 'valign': 'vcenter',
        })
        sub_num = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#D9E1F2', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
        })
        sub_day = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 8,
            'bg_color': '#D9E1F2', 'border': 1,
            'num_format': '0.##', 'align': 'center', 'valign': 'vcenter',
        })
        sub_pay = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#C6EFCE', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
        })
        # Grand total
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter',
        })
        tot_num = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#FFF2CC', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
        })
        tot_pay = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 10,
            'bg_color': '#C6EFCE', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
        })
        ot_pay_fmt = wb.add_format({
            'bold': True, 'font_name': 'Arial', 'font_size': 9,
            'bg_color': '#E2EFDA', 'border': 1,
            'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter',
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
        grand_hours = 0.0
        grand_pay   = 0.0
        sl_no = 1
        total_emp_count = 0

        for dept_name in sorted(dept_groups.keys()):
            dept_rows = dept_groups[dept_name]

            # Department header
            ws.merge_range(row, 0, row, TOTAL_COLS - 1, f'  {dept_name}', dept_fmt)
            ws.set_row(row, 24)
            row += 1

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

            dept_hours = 0.0
            dept_pay   = 0.0

            for data in dept_rows:
                emp        = data['employee']
                days_dict  = data['days']
                wage       = emp.wage or 0.0
                eligible   = min(wage, 12500.0)
                hourly_rate = min(eligible / 208.0, 60.0) if eligible else 0.0
                total_hours = sum(days_dict.values())
                ot_pay      = total_hours * hourly_rate

                dept_hours += total_hours
                dept_pay   += ot_pay

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

                ws.write(row, COL_TOT,  total_hours, num_fmt)
                ws.write(row, COL_RATE, hourly_rate, num_fmt)
                ws.write(row, COL_PAY,  ot_pay,      ot_pay_fmt)

                sl_no += 1
                row += 1

            # Dept subtotal — merge fixed identity cols, sum day cols
            ws.set_row(row, 20)
            ws.merge_range(row, 0, row, 6,
                           f'Subtotal  ({len(dept_rows)} employees)', sub_lbl)
            for d in range(1, N_DAYS + 1):
                col = N_FIXED + d - 1
                dept_day_hours = sum(
                    data['days'].get(d, 0.0) for data in dept_rows
                )
                if dept_day_hours:
                    ws.write(row, col, dept_day_hours, sub_day)
                else:
                    ws.write(row, col, '', sub_lbl)
            ws.write(row, COL_TOT,  dept_hours, sub_num)
            ws.write(row, COL_RATE, '',          sub_lbl)
            ws.write(row, COL_PAY,  dept_pay,   sub_pay)
            row += 2

            grand_hours     += dept_hours
            grand_pay       += dept_pay
            total_emp_count += len(dept_rows)

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