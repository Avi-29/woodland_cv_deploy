from odoo import models, fields, api
from datetime import date, timedelta, datetime
from odoo.exceptions import UserError
import base64
import io
import pytz

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class DailyPayroll(models.Model):
    _name = 'daily.payroll'
    _description = 'Daily Payroll'
    _order = 'work_date desc, employee_id'

    employee_id   = fields.Many2one('hr.employee', required=True)
    work_date     = fields.Date(required=True, default=fields.Date.today)
    department_id = fields.Many2one(related='employee_id.department_id', store=True)
    shift_id      = fields.Many2one('hr.shift', string='Shift', index=True)

    present    = fields.Boolean()
    # day_off: True if attendance is marked as day_off → payment = 0
    is_day_off = fields.Boolean(string='Day Off', default=False)
    wage       = fields.Float(string='Daily Wage')
    amount     = fields.Float(string='Base Amount')

    hours_worked = fields.Float(string='Hours Worked', digits=(16, 2))

    # Hourly rate: daily_wage / 8
    hourly_rate  = fields.Float(string='Hourly Rate (Wage÷8)', digits=(16, 4), store=True)

    # OT sourced directly from hr.attendance.validated_overtime_hours — no approval needed
    ot_hours  = fields.Float(string='OT Hours (Validated)', digits=(16, 2))
    ot_amount = fields.Float(string='OT Amount', digits=(16, 2))

    total_amount = fields.Float(
        string='Total Amount',
        compute='_compute_total', store=True, digits=(16, 2)
    )
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')

    _sql_constraints = [
        ('unique_employee_day', 'unique(employee_id, work_date)',
         'Payroll already generated for this employee on this day!')
    ]

    @api.depends('amount', 'ot_amount', 'is_day_off')
    def _compute_total(self):
        for rec in self:
            if rec.is_day_off:
                rec.total_amount = 0.0
            else:
                rec.total_amount = rec.amount + rec.ot_amount

    # ------------------------------------------------------------------ helpers

    def _build_att_map(self, work_date):
        """Return {employee_id: attendance_info_dict} for a single date.

        Odoo stores check_in in UTC.  We convert the Asia/Dhaka day boundaries
        (00:00:00 and 23:59:59 local) to UTC before querying so that attendance
        records are matched to the correct local calendar date.
        """
        dhaka_tz = pytz.timezone('Asia/Dhaka')
        # Build naive local datetimes, localise, then convert to UTC
        local_start = dhaka_tz.localize(datetime(work_date.year, work_date.month, work_date.day, 0, 0, 0))
        local_end   = dhaka_tz.localize(datetime(work_date.year, work_date.month, work_date.day, 23, 59, 59))
        utc_start   = local_start.astimezone(pytz.utc).replace(tzinfo=None)  # naive UTC for Odoo ORM
        utc_end     = local_end.astimezone(pytz.utc).replace(tzinfo=None)

        attendances = self.env['hr.attendance'].search([
            ('check_in', '>=', utc_start),
            ('check_in', '<=', utc_end),
        ])
        has_validated_ot = 'validated_overtime_hours' in self.env['hr.attendance']._fields
        has_day_off_flag = 'marked_as_day_off'        in self.env['hr.attendance']._fields

        att_map = {}
        for att in attendances:
            eid = att.employee_id.id
            if eid in att_map:
                continue
            att_map[eid] = {
                'hours':        att.worked_hours or 0.0,
                'shift_id':     att.shift_id.id if hasattr(att, 'shift_id') and att.shift_id else False,
                'validated_ot': (att.validated_overtime_hours or 0.0) if has_validated_ot else 0.0,
                'is_day_off':   bool(att.marked_as_day_off) if has_day_off_flag else False,
            }
        return att_map

    def _payroll_vals_for_emp(self, emp, info):
        """
        Compute payroll field values for one employee given attendance info.
        Returns only the computed fields (no employee_id / work_date).
        """
        daily_wage  = emp.daily_wage or 0.0
        hourly_rate = daily_wage / 8.0

        if info and info.get('is_day_off'):
            return dict(
                shift_id=info.get('shift_id', False),
                present=False, is_day_off=True,
                wage=daily_wage, hourly_rate=hourly_rate,
                amount=0.0, hours_worked=round(info['hours'], 2),
                ot_hours=0.0, ot_amount=0.0,
            )
        elif info:
            validated_ot = round(info['validated_ot'], 2)
            return dict(
                shift_id=info.get('shift_id', False),
                present=True, is_day_off=False,
                wage=daily_wage, hourly_rate=hourly_rate,
                amount=daily_wage, hours_worked=round(info['hours'], 2),
                ot_hours=validated_ot,
                ot_amount=round(validated_ot * hourly_rate, 2),
            )
        else:
            return dict(
                shift_id=False,
                present=False, is_day_off=False,
                wage=daily_wage, hourly_rate=hourly_rate,
                amount=0.0, hours_worked=0.0,
                ot_hours=0.0, ot_amount=0.0,
            )

    # ------------------------------------------------------------------ public

    def generate_daily_payroll(self, work_date):
        """
        Generate **or recompute** daily payroll for *work_date*.

        State logic
        -----------
        - ``done``  (approved) → locked forever, never touched.
        - ``draft``             → recomputed in-place with fresh attendance data.
        - no record yet         → created fresh.
        """
        employees = self.env['hr.employee'].search([('salary_type', '=', 'daily')])
        att_map   = self._build_att_map(work_date)

        existing     = self.search([('work_date', '=', work_date)])
        done_emp_ids = set(existing.filtered(lambda r: r.state == 'done').mapped('employee_id').ids)
        draft_by_emp = {r.employee_id.id: r for r in existing if r.state == 'draft'}

        to_create = []
        for emp in employees:
            if emp.id in done_emp_ids:
                continue  # approved → never recompute

            vals = self._payroll_vals_for_emp(emp, att_map.get(emp.id))

            if emp.id in draft_by_emp:
                # Draft already exists → rewrite with latest attendance data
                draft_by_emp[emp.id].write(vals)
            else:
                # No record yet → create
                vals.update({'employee_id': emp.id, 'work_date': work_date})
                to_create.append(vals)

        if to_create:
            self.create(to_create)


# ─────────────────────────────────────────────
#  Daily Excel Report Wizard
# ─────────────────────────────────────────────

class DailyPayrollExcelWizard(models.TransientModel):
    _name  = 'daily.payroll.excel.wizard'
    _description = 'Daily Payroll Excel Report Wizard'

    report_date   = fields.Date(required=True, default=fields.Date.today, string='Report Date')
    department_id = fields.Many2one('hr.department', string='Department',
                                    help='Leave empty to include all departments')
    shift_id      = fields.Many2one('hr.shift', string='Shift',
                                    help='Leave empty for all shifts')

    excel_file  = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename',      readonly=True)

    def _build_domain(self):
        domain = [('work_date', '=', self.report_date)]
        if self.department_id:
            domain.append(('department_id', '=', self.department_id.id))
        if self.shift_id:
            domain.append(('shift_id', '=', self.shift_id.id))
        return domain

    def action_export_excel(self):
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        records = self.env['daily.payroll'].search(
            self._build_domain(),
            order='department_id, shift_id, employee_id'
        )
        records = records.sorted(
            key=lambda r: (
                r.department_id.name or '',
                r.shift_id.name or '',
                r.employee_id.zk_badge_no or '99999',
            )
        )

        output = io.BytesIO()
        wb     = xlsxwriter.Workbook(output, {'in_memory': True})

        hdr_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 11,
                                     'bg_color': '#1F3864', 'font_color': '#FFFFFF',
                                     'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        grp_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 12,
                                     'bg_color': '#D9E1F2', 'border': 1,
                                     'align': 'center', 'valign': 'vcenter'})
        cell_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'})
        num_fmt    = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        rate_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'num_format': '#,##0.0000', 'valign': 'vcenter'})
        ot_fmt     = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#E2EFDA', 'num_format': '#,##0.00', 'valign': 'vcenter'})
        absent_fmt = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#FCE4D6', 'align': 'center', 'valign': 'vcenter'})
        dayoff_fmt = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#E2E2E2', 'align': 'center', 'valign': 'vcenter',
                                     'italic': True})
        tot_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1, 'num_format': '#,##0.00', 'valign': 'vcenter'})
        tot_lbl    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'})
        title_fmt  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 14,
                                     'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'})
        date_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'num_format': 'dd/mm/yyyy', 'valign': 'vcenter'})

        COLS   = [
            'SL', 'ID No', 'Employee', 'Date', 'Department', 'Shift', 'Status',
            'Hours\nWorked', 'Daily\nWage', 'Hourly Rate\n(Wage÷8)',
            'OT Hours\n(Validated)', 'OT\nAmount',
            'Base Pay', 'Total Amount', 'Remarks',
        ]
        WIDTHS = [6, 12, 24, 12, 28, 20, 12, 10, 12, 14, 14, 12, 12, 14, 20]

        groups = {}
        for r in records:
            dept  = r.department_id.name or 'No Department'
            shift = r.shift_id.name if r.shift_id else 'No Shift'
            groups.setdefault((dept, shift), []).append(r)

        title_parts = [self.report_date.strftime('%d %B %Y')]
        if self.department_id:
            title_parts.append(self.department_id.name)
        if self.shift_id:
            title_parts.append(self.shift_id.name)
        title_str   = 'Daily Payroll Report  |  ' + '  ·  '.join(title_parts)
        sheet_label = str(self.report_date)[:31]

        ws = wb.add_worksheet(sheet_label)
        ws.merge_range(0, 0, 1, len(COLS) - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)
        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        row = 2
        grand_base = grand_ot = grand_total = 0.0

        for (dept, shift) in sorted(groups.keys()):
            recs = groups[(dept, shift)]

            # Department row — centered, taller
            ws.merge_range(row, 0, row, len(COLS) - 1, f'{dept}   |   {shift}', grp_fmt)
            ws.set_row(row, 30)
            row += 1

            for c, h in enumerate(COLS):
                ws.write(row, c, h, hdr_fmt)
            ws.set_row(row, 42)
            row += 1

            g_base = g_ot = g_total = 0.0
            sl_no  = 1

            for r in recs:
                ws.set_row(row, 24)
                badge_no = r.employee_id.zk_badge_no or ''

                if r.is_day_off:
                    status_str = 'Day Off'
                    s_fmt      = dayoff_fmt
                elif r.present:
                    status_str = 'Present'
                    s_fmt      = cell_fmt
                else:
                    status_str = 'Absent'
                    s_fmt      = absent_fmt

                ws.write(row, 0,  sl_no,                       cell_fmt)
                ws.write(row, 1,  badge_no,                    cell_fmt)
                ws.write(row, 2,  r.employee_id.name or '',    cell_fmt)
                ws.write_datetime(row, 3, r.work_date,         date_fmt)
                ws.write(row, 4,  r.department_id.name or '',  cell_fmt)
                ws.write(row, 5,  r.shift_id.name if r.shift_id else '', cell_fmt)
                ws.write(row, 6,  status_str,                  s_fmt)
                ws.write(row, 7,  r.hours_worked,              num_fmt)
                ws.write(row, 8,  r.wage,                      num_fmt)
                ws.write(row, 9,  r.hourly_rate,               rate_fmt)
                ws.write(row, 10, r.ot_hours,                  num_fmt if r.ot_hours == 0 else ot_fmt)
                ws.write(row, 11, r.ot_amount,                 num_fmt if r.ot_amount == 0 else ot_fmt)
                ws.write(row, 12, r.amount,                    num_fmt)
                ws.write(row, 13, r.total_amount,              num_fmt)
                ws.write(row, 14, '',                          cell_fmt)

                g_base  += r.amount
                g_ot    += r.ot_amount
                g_total += r.total_amount
                sl_no   += 1
                row += 1

            ws.set_row(row, 22)
            for c in range(12):
                ws.write(row, c, '', tot_lbl)
            ws.write(row, 12, f'Subtotal  ({len(recs)} emp)', tot_lbl)
            ws.write(row, 11, g_ot,    tot_fmt)
            ws.write(row, 12, g_base,  tot_fmt)
            ws.write(row, 13, g_total, tot_fmt)
            ws.write(row, 14, '',      tot_lbl)

            grand_base  += g_base
            grand_ot    += g_ot
            grand_total += g_total
            row += 2

        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, 11, 'GRAND TOTAL', tot_lbl)
        ws.write(row, 11, grand_ot,    tot_fmt)
        ws.write(row, 12, grand_base,  tot_fmt)
        ws.write(row, 13, grand_total, tot_fmt)
        ws.write(row, 14, '',          tot_lbl)

        wb.close()
        output.seek(0)

        fname = (
            f"daily_payroll_{self.report_date}"
            f"{'_' + self.department_id.name.replace(' ', '_') if self.department_id else ''}"
            f"{'_' + self.shift_id.name.replace(' ', '_') if self.shift_id else ''}"
            f".xlsx"
        )
        self.write({'excel_file': base64.b64encode(output.read()), 'excel_fname': fname})
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ─────────────────────────────────────────────
#  Weekly Excel Report Wizard
# ─────────────────────────────────────────────

class WeeklyPayrollExcelWizard(models.TransientModel):
    _name  = 'weekly.payroll.excel.wizard'
    _description = 'Weekly Payroll Excel Report Wizard'

    week_date     = fields.Date(required=True, default=fields.Date.today, string='Any Date in the Week')
    department_id = fields.Many2one('hr.department', string='Department')
    shift_id      = fields.Many2one('hr.shift',      string='Shift')

    include_bonus = fields.Boolean(string='Add Bonus', default=False)
    bonus_month   = fields.Date(
        string='Bonus Month',
        help='Pick any date — the full calendar month is used to count attendance.',
    )

    excel_file  = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename',       readonly=True)

    BONUS_THRESHOLD = 24
    BONUS_AMOUNT    = 500.0

    def _bonus_att_counts(self):
        """Return {employee_id: attendance_day_count} for bonus_month in Asia/Dhaka TZ."""
        import calendar
        d         = self.bonus_month
        first_day = d.replace(day=1)
        last_day  = d.replace(day=calendar.monthrange(d.year, d.month)[1])

        dhaka_tz    = pytz.timezone('Asia/Dhaka')
        utc_start   = dhaka_tz.localize(datetime(first_day.year, first_day.month, first_day.day, 0, 0, 0)).astimezone(pytz.utc).replace(tzinfo=None)
        utc_end     = dhaka_tz.localize(datetime(last_day.year,  last_day.month,  last_day.day, 23, 59, 59)).astimezone(pytz.utc).replace(tzinfo=None)

        daily_emp_ids = self.env['hr.employee'].search([('salary_type', '=', 'daily')]).ids
        if not daily_emp_ids:
            return {}

        has_day_off = 'marked_as_day_off' in self.env['hr.attendance']._fields
        attendances = self.env['hr.attendance'].search([
            ('check_in', '>=', utc_start),
            ('check_in', '<=', utc_end),
            ('employee_id', 'in', daily_emp_ids),
        ])

        counts = {}
        seen   = set()
        for att in attendances:
            if has_day_off and att.marked_as_day_off:
                continue
            local_date = pytz.utc.localize(att.check_in).astimezone(dhaka_tz).date()
            key = (att.employee_id.id, local_date)
            if key in seen:
                continue
            seen.add(key)
            counts[att.employee_id.id] = counts.get(att.employee_id.id, 0) + 1
        return counts

    def _week_range(self):
        d      = self.week_date
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    def action_generate_weekly(self):
        """
        Recompute daily payroll for every draft day in the selected week,
        then export the weekly Excel report in one click.

        State logic (delegated to generate_daily_payroll):
        - ``done``  (approved) -> locked, never touched.
        - ``draft``             -> refreshed from live attendance data.
        - no record             -> created fresh.
        """
        if self.include_bonus and not self.bonus_month:
            raise UserError('Please select a Bonus Month.')
        monday, sunday = self._week_range()
        payroll_model  = self.env['daily.payroll']
        current = monday
        while current <= sunday:
            payroll_model.generate_daily_payroll(current)
            current += timedelta(days=1)
        # Produce and return the Excel report immediately after recompute
        return self.action_export_weekly_excel()

    def action_export_weekly_excel(self):
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed.')
        if self.include_bonus and not self.bonus_month:
            raise UserError('Please select a Bonus Month.')

        monday, sunday = self._week_range()
        domain = [('work_date', '>=', monday), ('work_date', '<=', sunday)]
        if self.department_id:
            domain.append(('department_id', '=', self.department_id.id))
        if self.shift_id:
            domain.append(('shift_id', '=', self.shift_id.id))

        records = self.env['daily.payroll'].search(domain, order='employee_id, work_date')

        # Bonus attendance counts (only when requested)
        bonus_counts = self._bonus_att_counts() if self.include_bonus else {}

        week_days = [monday + timedelta(days=i) for i in range(7)]
        emp_data  = {}
        for r in records:
            eid = r.employee_id.id
            if eid not in emp_data:
                emp_data[eid] = {
                    'name':  r.employee_id.name or '',
                    'dept':  r.department_id.name or 'No Department',
                    'shift': r.shift_id.name if r.shift_id else 'No Shift',
                    'wage':  r.wage,
                    'badge': r.employee_id.zk_badge_no or '',
                    'days':  {},
                }
            emp_data[eid]['days'][r.work_date] = r

        output = io.BytesIO()
        wb     = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 14,
                                     'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'})
        hdr_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#1F3864', 'font_color': '#FFFFFF',
                                     'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        day_hdr    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 9,
                                     'bg_color': '#2E5090', 'font_color': '#FFFFFF',
                                     'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        bonus_hdr  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#375623', 'font_color': '#FFFFFF',
                                     'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        cell_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'})
        num_fmt    = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        cnt_fmt    = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'align': 'center', 'valign': 'vcenter'})
        present_f  = wb.add_format({'font_name': 'Arial', 'font_size': 9, 'border': 1,
                                     'bg_color': '#E2EFDA', 'align': 'center',
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        absent_f   = wb.add_format({'font_name': 'Arial', 'font_size': 9, 'border': 1,
                                     'bg_color': '#FCE4D6', 'align': 'center', 'valign': 'vcenter'})
        dayoff_f   = wb.add_format({'font_name': 'Arial', 'font_size': 9, 'border': 1,
                                     'bg_color': '#E2E2E2', 'align': 'center',
                                     'valign': 'vcenter', 'italic': True})
        tot_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        tot_cnt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1,
                                     'align': 'center', 'valign': 'vcenter'})
        tot_lbl    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'})
        bonus_fmt  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#E2EFDA', 'border': 1,
                                     'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})
        nobonus_f  = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#FCE4D6', 'num_format': '#,##0.00',
                                     'align': 'center', 'valign': 'vcenter'})
        grtot_fmt  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#D6E4BC', 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        tot_bonus  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#C6EFCE', 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})

        DAY_NAMES  = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        fixed_cols = ['SL', 'ID No', 'Employee', 'Department', 'Shift', 'Daily\nWage']

        # Summary columns — bonus columns inserted before Remarks when active
        base_summary = ['Present\nDays', 'Absent\nDays', 'Day Off\nDays',
                        'Total\nBase Pay', 'Total OT\nHours', 'Total OT\nPay', 'Grand\nTotal']
        bonus_summary = (
            [f'Att. Count\n({self.bonus_month.strftime("%b %Y")})',
             f'Bonus\n(৳{self.BONUS_AMOUNT:,.0f})',
             'Total +\nBonus']
            if self.include_bonus else []
        )
        summary_cols = base_summary + bonus_summary + ['Remarks']

        n_fixed    = len(fixed_cols)
        n_days     = 7
        n_summary  = len(summary_cols)
        total_cols = n_fixed + n_days + n_summary

        # Column widths: fixed + 7 day cols + summary
        bonus_extra_widths = [12, 12, 14] if self.include_bonus else []
        col_widths = [6, 12, 26, 28, 20, 12] + [16] * 7 + [11, 11, 11, 14, 14, 14, 14] + bonus_extra_widths + [20]

        sheet_label = f"Week {monday.strftime('%d%b')}-{sunday.strftime('%d%b%Y')}"[:31]
        ws = wb.add_worksheet(sheet_label)
        for i, w in enumerate(col_widths):
            ws.set_column(i, i, w)

        title_str = (
            f"Weekly Payroll Report  |  "
            f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b %Y')}"
            + (f"  |  Bonus: {self.bonus_month.strftime('%B %Y')}  (>{self.BONUS_THRESHOLD} days → ৳{self.BONUS_AMOUNT:,.0f})" if self.include_bonus else "")
        )
        ws.merge_range(0, 0, 1, total_cols - 1, title_str, title_fmt)
        ws.set_row(0, 30)
        ws.set_row(1, 10)

        row = 2
        # Header row 1: fixed cols (span 2 rows) + day-group header + summary cols (span 2 rows)
        for c, h in enumerate(fixed_cols):
            ws.merge_range(row, c, row + 1, c, h, hdr_fmt)
        ws.merge_range(row, n_fixed, row, n_fixed + n_days - 1, 'Daily Attendance & Pay by Date', hdr_fmt)

        # Base summary headers
        for i, h in enumerate(base_summary):
            ws.merge_range(row, n_fixed + n_days + i, row + 1, n_fixed + n_days + i, h, hdr_fmt)

        # Bonus headers (dark green)
        if self.include_bonus:
            b_start = n_fixed + n_days + len(base_summary)
            for i, h in enumerate(bonus_summary):
                ws.merge_range(row, b_start + i, row + 1, b_start + i, h, bonus_hdr)
            ws.merge_range(row, n_fixed + n_days + len(base_summary) + len(bonus_summary),
                           row + 1,
                           n_fixed + n_days + len(base_summary) + len(bonus_summary),
                           'Remarks', hdr_fmt)
        else:
            ws.merge_range(row, n_fixed + n_days + len(base_summary),
                           row + 1,
                           n_fixed + n_days + len(base_summary),
                           'Remarks', hdr_fmt)

        ws.set_row(row, 28)
        row += 1

        # Header row 2: day sub-headers
        for i, d in enumerate(week_days):
            ws.write(row, n_fixed + i, f"{DAY_NAMES[i]}\n{d.strftime('%d/%m')}", day_hdr)
        ws.set_row(row, 34)
        row += 1

        # Precompute column offsets for summary fields
        S = n_fixed + n_days   # summary base offset
        # indices within summary_cols:
        # 0=Present, 1=Absent, 2=DayOff, 3=BasePay, 4=OT_h, 5=OT_p, 6=GrandTotal
        # [7=AttCount, 8=Bonus, 9=Total+Bonus]  (only if include_bonus)
        # last = Remarks

        # Flat list of all employees, sorted by zk_badge_no (character field).
        # No department/shift grouping in the main sheet anymore.
        all_emps = sorted(
            ((eid, e) for eid, e in emp_data.items()),
            key=lambda item: item[1]['badge'] or 'zzzzzzzz'
        )

        grand_present = grand_absent = grand_dayoff = 0
        grand_base = grand_ot_h = grand_ot_p = grand_total = 0.0
        grand_bonus = grand_with_bonus = 0.0

        # Per-department roll-up, used to build the
        # "Weekly Summary" sheet — how much money each department pays.
        dept_summary = {}

        sl_no = 1
        for eid, e in all_emps:
            present_days = absent_days = dayoff_days = 0
            total_base = total_ot_h = total_ot_p = 0.0

            ws.set_row(row, 26)
            ws.write(row, 0, sl_no,       cell_fmt)
            ws.write(row, 1, e['badge'],  cell_fmt)
            ws.write(row, 2, e['name'],   cell_fmt)
            ws.write(row, 3, e['dept'],   cell_fmt)
            ws.write(row, 4, e['shift'],  cell_fmt)
            ws.write(row, 5, e['wage'],   num_fmt)

            for i, d in enumerate(week_days):
                col = n_fixed + i
                rec = e['days'].get(d)
                if rec is None:
                    ws.write(row, col, 'Absent', absent_f)
                    absent_days += 1
                elif rec.is_day_off:
                    ws.write(row, col, 'Day Off', dayoff_f)
                    dayoff_days += 1
                elif not rec.present:
                    ws.write(row, col, 'Absent', absent_f)
                    absent_days += 1
                else:
                    shift_name = rec.shift_id.name if rec.shift_id else ''
                    day_total  = rec.total_amount
                    label = f"{shift_name}\n{day_total:,.2f}" if shift_name else f"{day_total:,.2f}"
                    ws.write(row, col, label, present_f)
                    total_base += rec.amount
                    total_ot_h += rec.ot_hours
                    total_ot_p += rec.ot_amount
                    present_days += 1

            grand_t = total_base + total_ot_p

            ws.write(row, S + 0, present_days,  cnt_fmt)
            ws.write(row, S + 1, absent_days,   cnt_fmt)
            ws.write(row, S + 2, dayoff_days,   cnt_fmt)
            ws.write(row, S + 3, total_base,    num_fmt)
            ws.write(row, S + 4, total_ot_h,    num_fmt)
            ws.write(row, S + 5, total_ot_p,    num_fmt)
            ws.write(row, S + 6, grand_t,       num_fmt)

            emp_bonus   = 0.0
            total_bonus = grand_t
            if self.include_bonus:
                att_count   = bonus_counts.get(eid, 0)
                emp_bonus   = self.BONUS_AMOUNT if att_count > self.BONUS_THRESHOLD else 0.0
                total_bonus = grand_t + emp_bonus
                b_fmt       = bonus_fmt if emp_bonus > 0 else nobonus_f
                ws.write(row, S + 7, att_count,   cnt_fmt)
                ws.write(row, S + 8, emp_bonus,   b_fmt)
                ws.write(row, S + 9, total_bonus, grtot_fmt)
                ws.write(row, S + 10, '',          cell_fmt)
            else:
                ws.write(row, S + 7, '', cell_fmt)

            grand_present    += present_days
            grand_absent     += absent_days
            grand_dayoff     += dayoff_days
            grand_base       += total_base
            grand_ot_h       += total_ot_h
            grand_ot_p       += total_ot_p
            grand_total      += grand_t
            grand_bonus      += emp_bonus
            grand_with_bonus += total_bonus

            # Roll employee totals up into the department-level summary
            # (used only by the separate "Weekly Summary" sheet).
            dept = e['dept']
            if dept not in dept_summary:
                dept_summary[dept] = {
                    'employees': 0, 'present': 0, 'absent': 0, 'dayoff': 0,
                    'base': 0.0, 'ot_h': 0.0, 'ot_p': 0.0, 'total': 0.0,
                    'bonus': 0.0, 'with_bonus': 0.0,
                }
            ds = dept_summary[dept]
            ds['employees']  += 1
            ds['present']    += present_days
            ds['absent']     += absent_days
            ds['dayoff']     += dayoff_days
            ds['base']       += total_base
            ds['ot_h']       += total_ot_h
            ds['ot_p']       += total_ot_p
            ds['total']      += grand_t
            ds['bonus']      += emp_bonus
            ds['with_bonus'] += total_bonus

            sl_no += 1
            row += 1

        # Grand total row
        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, n_fixed + n_days - 1, 'GRAND TOTAL', tot_lbl)
        ws.write(row, S + 0, grand_present, tot_cnt)
        ws.write(row, S + 1, grand_absent,  tot_cnt)
        ws.write(row, S + 2, grand_dayoff,  tot_cnt)
        ws.write(row, S + 3, grand_base,    tot_fmt)
        ws.write(row, S + 4, grand_ot_h,    tot_fmt)
        ws.write(row, S + 5, grand_ot_p,    tot_fmt)
        ws.write(row, S + 6, grand_total,   tot_fmt)
        if self.include_bonus:
            ws.write(row, S + 7,  '',              tot_cnt)
            ws.write(row, S + 8,  grand_bonus,     tot_bonus)
            ws.write(row, S + 9,  grand_with_bonus,tot_bonus)
            ws.write(row, S + 10, '',               tot_lbl)
        else:
            ws.write(row, S + 7, '', tot_lbl)

        # ── Weekly Summary sheet — how much money each department needs
        #    to disburse for the week ────────────────────────────────
        sm = wb.add_worksheet('Weekly Summary')

        sm_title_fmt = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 14,
                                       'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'})
        sm_hdr_fmt   = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#1F3864', 'font_color': '#FFFFFF',
                                       'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        sm_bonus_hdr = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#375623', 'font_color': '#FFFFFF',
                                       'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        sm_cell_fmt  = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'})
        sm_num_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                       'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})
        sm_cnt_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                       'align': 'center', 'valign': 'vcenter'})
        sm_pay_fmt   = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#E2EFDA', 'border': 1,
                                       'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})
        sm_bonus_fmt = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#D6E4BC', 'border': 1,
                                       'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})
        sm_tot_lbl   = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'})
        sm_tot_num   = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#FFF2CC', 'border': 1,
                                       'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})
        sm_tot_pay   = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                       'bg_color': '#C6EFCE', 'border': 1,
                                       'num_format': '#,##0.00', 'align': 'center', 'valign': 'vcenter'})

        SM_COLS = [
            'SL', 'Department', 'Employees', 'Present\nDays', 'Absent\nDays',
            'Day Off\nDays', 'Base Pay', 'OT Pay', 'Total Amount',
        ]
        SM_WIDTHS = [5, 26, 11, 11, 11, 11, 14, 14, 16]
        if self.include_bonus:
            SM_COLS   += ['Bonus', 'Total + Bonus\n(Payable)']
            SM_WIDTHS += [14, 18]
        else:
            SM_COLS[-1] = 'Total Amount\n(Payable)'
            SM_WIDTHS[-1] = 18

        for i, w in enumerate(SM_WIDTHS):
            sm.set_column(i, i, w)

        sm_title = (
            f"Weekly Department Summary  |  "
            f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b %Y')}"
        )
        sm.merge_range(0, 0, 1, len(SM_COLS) - 1, sm_title, sm_title_fmt)
        sm.set_row(0, 30)
        sm.set_row(1, 10)

        sm_row = 2
        for c, h in enumerate(SM_COLS):
            fmt = sm_bonus_hdr if (self.include_bonus and c >= len(SM_COLS) - 2) else sm_hdr_fmt
            sm.write(sm_row, c, h, fmt)
        sm.set_row(sm_row, 32)
        sm_row += 1

        sm_sl = 1
        sm_grand = {
            'employees': 0, 'present': 0, 'absent': 0, 'dayoff': 0,
            'base': 0.0, 'ot_p': 0.0, 'total': 0.0, 'bonus': 0.0, 'with_bonus': 0.0,
        }

        for dept in sorted(dept_summary.keys()):
            d = dept_summary[dept]
            sm.set_row(sm_row, 20)
            sm.write(sm_row, 0, sm_sl,          sm_cell_fmt)
            sm.write(sm_row, 1, dept,            sm_cell_fmt)
            sm.write(sm_row, 2, d['employees'],  sm_cnt_fmt)
            sm.write(sm_row, 3, d['present'],    sm_cnt_fmt)
            sm.write(sm_row, 4, d['absent'],     sm_cnt_fmt)
            sm.write(sm_row, 5, d['dayoff'],     sm_cnt_fmt)
            sm.write(sm_row, 6, d['base'],       sm_num_fmt)
            sm.write(sm_row, 7, d['ot_p'],       sm_num_fmt)
            if self.include_bonus:
                sm.write(sm_row, 8, d['total'],      sm_num_fmt)
                sm.write(sm_row, 9, d['bonus'],       sm_bonus_fmt)
                sm.write(sm_row, 10, d['with_bonus'], sm_pay_fmt)
            else:
                sm.write(sm_row, 8, d['total'], sm_pay_fmt)

            for k in ('employees', 'present', 'absent', 'dayoff', 'base', 'ot_p', 'total', 'bonus', 'with_bonus'):
                sm_grand[k] += d[k]
            sm_sl += 1
            sm_row += 1

        sm.set_row(sm_row, 22)
        sm.merge_range(sm_row, 0, sm_row, 1,
                        f'GRAND TOTAL  ({len(dept_summary)} departments)', sm_tot_lbl)
        sm.write(sm_row, 2, sm_grand['employees'], sm_tot_num)
        sm.write(sm_row, 3, sm_grand['present'],   sm_tot_num)
        sm.write(sm_row, 4, sm_grand['absent'],    sm_tot_num)
        sm.write(sm_row, 5, sm_grand['dayoff'],    sm_tot_num)
        sm.write(sm_row, 6, sm_grand['base'],      sm_tot_num)
        sm.write(sm_row, 7, sm_grand['ot_p'],      sm_tot_num)
        if self.include_bonus:
            sm.write(sm_row, 8, sm_grand['total'],      sm_tot_num)
            sm.write(sm_row, 9, sm_grand['bonus'],      sm_tot_num)
            sm.write(sm_row, 10, sm_grand['with_bonus'], sm_tot_pay)
        else:
            sm.write(sm_row, 8, sm_grand['total'], sm_tot_pay)

        wb.close()
        output.seek(0)

        fname = (
            f"weekly_payroll_{monday}_{sunday}"
            f"{'_' + self.department_id.name.replace(' ', '_') if self.department_id else ''}"
            f"{'_' + self.shift_id.name.replace(' ', '_') if self.shift_id else ''}"
            f"{'_bonus_' + self.bonus_month.strftime('%Y%m') if self.include_bonus else ''}"
            f".xlsx"
        )
        self.write({'excel_file': base64.b64encode(output.read()), 'excel_fname': fname})
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }


# ─────────────────────────────────────────────
#  Generate Daily Payroll Wizard
# ─────────────────────────────────────────────

class DailyPayrollWizard(models.TransientModel):
    _name = 'daily.payroll.wizard'
    _description = 'Generate Daily Payroll Wizard'

    work_date = fields.Date(required=True, default=fields.Date.today)

    def action_generate(self):
        self.env['daily.payroll'].generate_daily_payroll(self.work_date)


# ─────────────────────────────────────────────
#  Monthly Attendance Bonus Wizard
# ─────────────────────────────────────────────

class DailyPayrollBonusWizard(models.TransientModel):
    _name        = 'daily.payroll.bonus.wizard'
    _description = 'Monthly Attendance Bonus Report Wizard'

    bonus_month = fields.Date(
        required=True,
        default=fields.Date.today,
        string='Month',
        help='Pick any date — the full calendar month is used.',
    )

    excel_file  = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename',       readonly=True)

    BONUS_THRESHOLD = 24
    BONUS_AMOUNT    = 500.0

    def _month_range(self):
        import calendar
        d          = self.bonus_month
        first_day  = d.replace(day=1)
        last_day   = d.replace(day=calendar.monthrange(d.year, d.month)[1])
        return first_day, last_day

    def _count_attendances(self, first_day, last_day):
        """
        Count distinct calendar days (Asia/Dhaka) each daily-worker was present
        within [first_day, last_day].  Returns {employee_id: int}.
        """
        dhaka_tz    = pytz.timezone('Asia/Dhaka')
        local_start = dhaka_tz.localize(datetime(first_day.year, first_day.month, first_day.day, 0, 0, 0))
        local_end   = dhaka_tz.localize(datetime(last_day.year,  last_day.month,  last_day.day, 23, 59, 59))
        utc_start   = local_start.astimezone(pytz.utc).replace(tzinfo=None)
        utc_end     = local_end.astimezone(pytz.utc).replace(tzinfo=None)

        # Only daily-wage employees
        daily_emp_ids = self.env['hr.employee'].search(
            [('salary_type', '=', 'daily')]
        ).ids
        if not daily_emp_ids:
            return {}

        has_day_off = 'marked_as_day_off' in self.env['hr.attendance']._fields

        attendances = self.env['hr.attendance'].search([
            ('check_in', '>=', utc_start),
            ('check_in', '<=', utc_end),
            ('employee_id', 'in', daily_emp_ids),
        ])

        counts = {}
        seen   = set()          # (employee_id, local_date) dedup
        for att in attendances:
            if has_day_off and att.marked_as_day_off:
                continue        # day-off records do not count as attendance
            # Convert check_in (naive UTC) → Dhaka date
            utc_dt    = pytz.utc.localize(att.check_in)
            dhaka_dt  = utc_dt.astimezone(dhaka_tz)
            local_date = dhaka_dt.date()
            key = (att.employee_id.id, local_date)
            if key in seen:
                continue
            seen.add(key)
            counts[att.employee_id.id] = counts.get(att.employee_id.id, 0) + 1

        return counts

    def action_export_bonus_excel(self):
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        first_day, last_day = self._month_range()
        att_counts          = self._count_attendances(first_day, last_day)

        daily_employees = self.env['hr.employee'].search([('salary_type', '=', 'daily')])
        if not daily_employees:
            raise UserError('No daily-wage employees found.')

        # Build per-department rows
        dept_rows = {}
        for emp in daily_employees:
            dept_name = emp.department_id.name if emp.department_id else 'No Department'
            count     = att_counts.get(emp.id, 0)
            bonus     = self.BONUS_AMOUNT if count > self.BONUS_THRESHOLD else 0.0
            dept_rows.setdefault(dept_name, []).append({
                'badge':  emp.zk_badge_no or '',
                'name':   emp.name or '',
                'dept':   dept_name,
                'count':  count,
                'bonus':  bonus,
            })
        for dept_name in dept_rows:
            dept_rows[dept_name].sort(key=lambda r: r['badge'] or '99999')

        # ── Excel ────────────────────────────────────────────────────────────
        output = io.BytesIO()
        wb     = xlsxwriter.Workbook(output, {'in_memory': True})

        title_fmt  = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 14,
                                     'font_color': '#1F3864', 'align': 'center', 'valign': 'vcenter'})
        hdr_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 11,
                                     'bg_color': '#1F3864', 'font_color': '#FFFFFF',
                                     'border': 1, 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        grp_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 12,
                                     'bg_color': '#D9E1F2', 'border': 1,
                                     'align': 'center', 'valign': 'vcenter'})
        cell_fmt   = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter'})
        num_fmt    = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter'})
        bonus_fmt  = wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#E2EFDA', 'num_format': '#,##0.00',
                                     'align': 'center', 'valign': 'vcenter', 'bold': True})
        nobonus_fmt= wb.add_format({'font_name': 'Arial', 'font_size': 10, 'border': 1,
                                     'bg_color': '#FCE4D6', 'num_format': '#,##0.00',
                                     'align': 'center', 'valign': 'vcenter'})
        tot_fmt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1,
                                     'num_format': '#,##0.00', 'valign': 'vcenter'})
        tot_cnt    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1,
                                     'align': 'center', 'valign': 'vcenter'})
        tot_lbl    = wb.add_format({'bold': True, 'font_name': 'Arial', 'font_size': 10,
                                     'bg_color': '#FFF2CC', 'border': 1, 'valign': 'vcenter'})
        note_fmt   = wb.add_format({'italic': True, 'font_name': 'Arial', 'font_size': 9,
                                     'font_color': '#595959', 'align': 'center', 'valign': 'vcenter'})

        COLS   = ['SL', 'ID No', 'Employee Name', 'Department',
                  f'Attendance\nCount', 'Eligible\n(>25 days)', f'Bonus\nAmount (৳)']
        WIDTHS = [6, 12, 28, 28, 14, 14, 16]

        month_label = first_day.strftime('%B %Y')
        ws          = wb.add_worksheet(month_label[:31])

        ws.merge_range(0, 0, 1, len(COLS) - 1,
                       f'Monthly Attendance Bonus Report  |  {month_label}  |  Threshold: >{self.BONUS_THRESHOLD} days  →  ৳{self.BONUS_AMOUNT:,.0f}',
                       title_fmt)
        ws.set_row(0, 36)
        ws.set_row(1, 10)
        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        row = 2
        grand_eligible = 0
        grand_bonus    = 0.0

        for dept_name in sorted(dept_rows.keys()):
            rows = dept_rows[dept_name]

            ws.merge_range(row, 0, row, len(COLS) - 1, dept_name, grp_fmt)
            ws.set_row(row, 28)
            row += 1

            for c, h in enumerate(COLS):
                ws.write(row, c, h, hdr_fmt)
            ws.set_row(row, 40)
            row += 1

            g_eligible = 0
            g_bonus    = 0.0
            sl_no      = 1

            for r in rows:
                ws.set_row(row, 22)
                eligible    = r['count'] > self.BONUS_THRESHOLD
                bonus_val   = r['bonus']
                eligible_str = 'Yes' if eligible else 'No'

                ws.write(row, 0, sl_no,           cell_fmt)
                ws.write(row, 1, r['badge'],       cell_fmt)
                ws.write(row, 2, r['name'],        cell_fmt)
                ws.write(row, 3, r['dept'],        cell_fmt)
                ws.write(row, 4, r['count'],       num_fmt)
                ws.write(row, 5, eligible_str,     bonus_fmt if eligible else nobonus_fmt)
                ws.write(row, 6, bonus_val,        bonus_fmt if eligible else nobonus_fmt)

                if eligible:
                    g_eligible += 1
                g_bonus += bonus_val
                sl_no   += 1
                row += 1

            # Dept subtotal
            ws.set_row(row, 22)
            ws.merge_range(row, 0, row, 3, f'Subtotal  ({len(rows)} employees,  {g_eligible} eligible)', tot_lbl)
            ws.write(row, 4, '',          tot_cnt)
            ws.write(row, 5, g_eligible,  tot_cnt)
            ws.write(row, 6, g_bonus,     tot_fmt)

            grand_eligible += g_eligible
            grand_bonus    += g_bonus
            row += 2

        # Grand total
        ws.set_row(row, 22)
        ws.merge_range(row, 0, row, 3, 'GRAND TOTAL', tot_lbl)
        ws.write(row, 4, '',              tot_cnt)
        ws.write(row, 5, grand_eligible,  tot_cnt)
        ws.write(row, 6, grand_bonus,     tot_fmt)
        row += 2

        # Legend note
        ws.merge_range(row, 0, row, len(COLS) - 1,
                       f'* Green = eligible (attendance > {self.BONUS_THRESHOLD} days).  '
                       f'Red = not eligible.  Attendance counted in Asia/Dhaka timezone.  '
                       f'Day-off records excluded.',
                       note_fmt)

        wb.close()
        output.seek(0)

        fname = f"attendance_bonus_{first_day.strftime('%Y_%m')}.xlsx"
        self.write({'excel_file': base64.b64encode(output.read()), 'excel_fname': fname})
        return {
            'type': 'ir.actions.act_url',
            'url':  f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }