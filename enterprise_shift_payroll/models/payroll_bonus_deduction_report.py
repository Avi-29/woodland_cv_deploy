from odoo import models, fields
from odoo.exceptions import UserError
from datetime import date
import base64
import io

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None

# Same threshold as the Payroll Worksheet export (ImportPayslipsWizard) —
# employees below this many pay days are "Low Attendance" and are left
# out of that export's Department Summary. This report uses the same
# population so the two reconcile against each other.
MIN_PAY_DAYS_FOR_MAIN_SHEET = 6


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Bonus & Double-Deduction Department Summary
# ═══════════════════════════════════════════════════════════════════
#
# For the chosen month, one row per department (main employees only —
# same "pay_days >= 6" population as the Payroll Worksheet export's
# Department Summary, so Low Attendance employees don't skew this):
#   - employee count
#   - total attendance bonus
#   - total DOUBLE-CUT deduction only — genuine_absent_days ×
#     absent_deduct_per_day, and only for bonus-eligible departments
#     (where that rate is actually 2×). This deliberately excludes
#     late/unpaid-leave/flat-absent deductions — those aren't part of
#     the "double deduction" that only bonus-eligible depts get hit
#     with, so they're left out of this figure entirely.
#   - difference (net money) = total bonus - total double-cut deduction
class PayrollBonusDeductionReportWizard(models.TransientModel):
    _name = 'payroll.bonus.deduction.report.wizard'
    _description = 'Bonus & Double-Deduction Department Summary'

    report_month = fields.Date(
        string='Month',
        required=True,
        default=lambda self: date.today().replace(day=1),
        help="Pick any date — the full calendar month is used.",
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def _pay_days(self, slip):
        """Same pay_days formula as ImportPayslipsWizard's Department
        Summary — used only to exclude Low Attendance employees
        (pay_days < 6) from this report, same population it uses.
        """
        emp = slip.employee_id
        dept_eligible = bool(emp.department_id and emp.department_id.eligible_for_bonus)

        scheduled_dates = slip._get_all_working_dates(emp, slip.date_from, slip.date_to)
        pub_holiday_dates = slip._get_public_holiday_dates(slip.date_from, slip.date_to)
        scheduled_dates -= (pub_holiday_dates & scheduled_dates)

        cl_days, sl_days = self.env['payroll.attendance.export.wizard']._get_cl_sl_days(
            emp, slip.date_from, slip.date_to, scheduled_dates
        )

        total_wd = slip.calendar_days_in_period
        act_absent = slip.absent_days + cl_days + sl_days + slip.lwp_days
        dbl_cut = slip.genuine_absent_days if dept_eligible else 0
        tot_cut = act_absent + dbl_cut
        lv_approve = cl_days + sl_days
        return max(total_wd - tot_cut + lv_approve, 0)

    def _double_cut_deduction(self, slip):
        """Just the 2x genuine-absent penalty money — 0 for departments
        that aren't bonus-eligible (they're never charged at 2x)."""
        emp = slip.employee_id
        dept_eligible = bool(emp.department_id and emp.department_id.eligible_for_bonus)
        if not dept_eligible:
            return 0.0
        return slip.genuine_absent_days * slip.absent_deduct_per_day

    def action_generate_report(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        month_start = self.report_month.replace(day=1)

        payslips = self.env['payroll.payslip'].search([
            ('payroll_month', '=', month_start),
            ('state', '!=', 'draft'),
        ])
        if not payslips:
            raise UserError(f"No payslips found for {month_start.strftime('%B %Y')}.")

        # ── aggregate per department, main employees only ───────────
        dept_totals = {}
        for slip in payslips:
            if self._pay_days(slip) < MIN_PAY_DAYS_FOR_MAIN_SHEET:
                continue  # Low Attendance — excluded, same as the Payroll Worksheet export

            emp = slip.employee_id
            dept_name = emp.department_id.name if emp.department_id else 'No Department'
            dt = dept_totals.setdefault(dept_name, {
                'employees': 0, 'bonus': 0.0, 'deduction': 0.0,
            })
            dt['employees'] += 1
            dt['bonus'] += slip.attendance_bonus
            dt['deduction'] += self._double_cut_deduction(slip)

        if not dept_totals:
            raise UserError(
                f"No main employees (pay_days >= {MIN_PAY_DAYS_FOR_MAIN_SHEET}) "
                f"found for {month_start.strftime('%B %Y')}."
            )

        # ── Excel ────────────────────────────────────────────────────
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
        name_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1, 'valign': 'vcenter',
        })
        cell_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        money_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'num_format': '#,##0', 'align': 'center', 'valign': 'vcenter',
        })
        diff_pos_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'bg_color': '#E2EFDA', 'num_format': '#,##0',
            'align': 'center', 'valign': 'vcenter', 'bold': True,
        })
        diff_neg_fmt = wb.add_format({
            'font_name': 'Arial', 'font_size': 10, 'border': 1,
            'bg_color': '#FCE4D6', 'num_format': '#,##0',
            'align': 'center', 'valign': 'vcenter', 'bold': True,
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

        COLS = ['Department', 'Employee Count', 'Total Bonus',
                'Total Double-Cut Deduction', 'Difference (Net Money)']
        WIDTHS = [28, 16, 16, 20, 20]

        month_label = month_start.strftime('%B %Y')
        ws = wb.add_worksheet(month_label[:31])

        ws.merge_range(0, 0, 1, len(COLS) - 1,
                        f'Bonus & Double-Deduction Department Summary  |  {month_label}',
                        title_fmt)
        ws.set_row(0, 32)
        ws.set_row(1, 10)
        for i, w in enumerate(WIDTHS):
            ws.set_column(i, i, w)

        row = 2
        for c, h in enumerate(COLS):
            ws.write(row, c, h, hdr_fmt)
        ws.set_row(row, 24)
        row += 1

        grand_employees = 0
        grand_bonus = 0.0
        grand_deduction = 0.0

        for dept_name in sorted(dept_totals.keys()):
            dt = dept_totals[dept_name]
            difference = dt['bonus'] - dt['deduction']

            ws.set_row(row, 20)
            ws.write(row, 0, dept_name, name_fmt)
            ws.write(row, 1, dt['employees'], cell_fmt)
            ws.write(row, 2, dt['bonus'], money_fmt)
            ws.write(row, 3, dt['deduction'], money_fmt)
            ws.write(row, 4, difference, diff_pos_fmt if difference >= 0 else diff_neg_fmt)
            row += 1

            grand_employees += dt['employees']
            grand_bonus += dt['bonus']
            grand_deduction += dt['deduction']

        grand_difference = grand_bonus - grand_deduction
        ws.set_row(row, 22)
        ws.write(row, 0, 'GRAND TOTAL', tot_lbl)
        ws.write(row, 1, grand_employees, tot_fmt)
        ws.write(row, 2, grand_bonus, tot_fmt)
        ws.write(row, 3, grand_deduction, tot_fmt)
        ws.write(row, 4, grand_difference, tot_fmt)

        wb.close()
        output.seek(0)

        fname = f"bonus_deduction_dept_summary_{month_start.strftime('%Y%m')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }
