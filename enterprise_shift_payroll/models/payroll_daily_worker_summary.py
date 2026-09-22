from odoo import models, fields
from odoo.exceptions import UserError
from datetime import date
import base64
import io

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


# ═══════════════════════════════════════════════════════════════════
#  Wizard – Daily Worker Present/Absent Summary (date range → Excel)
# ═══════════════════════════════════════════════════════════════════
#
# For every "daily" worker_type employee, over the chosen [date_from,
# date_to] range (Asia/Dhaka calendar), reports:
#   - Total Days   = calendar days in the selected range
#   - Present Days = days classified present by the same attendance
#                    analysis the payslip itself uses (payroll.payslip
#                    ._analyse_attendance), so this reconciles with
#                    payroll.
#   - Absent Days  = days classified absent by that same analysis.
class PayrollDailyWorkerSummaryWizard(models.TransientModel):
    _name = 'payroll.daily.worker.summary.wizard'
    _description = 'Daily Worker Present/Absent Summary'

    date_from = fields.Date(
        string='Date From',
        required=True,
        default=lambda self: date.today().replace(day=1),
    )
    date_to = fields.Date(
        string='Date To',
        required=True,
        default=fields.Date.today,
    )
    excel_file = fields.Binary(string='Excel Report', readonly=True)
    excel_fname = fields.Char(string='Filename', readonly=True)

    def action_generate_report(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError('xlsxwriter is not installed. Run: pip install xlsxwriter')

        date_from = self.date_from
        date_to = self.date_to
        if date_from > date_to:
            raise UserError("'Date From' must not be after 'Date To'.")

        employees = self.env['hr.employee'].search([('worker_type', '=', 'daily')])
        if not employees:
            raise UserError("No daily workers found.")

        employees = employees.sorted(
            key=lambda e: int(e.zk_badge_no) if e.zk_badge_no and e.zk_badge_no.isdigit() else 0
        )

        Payslip = self.env['payroll.payslip']
        total_days = (date_to - date_from).days + 1

        pub_holidays = Payslip._get_public_holiday_dates(date_from, date_to)

        rows = []
        for employee in employees:
            all_working_dates = Payslip._get_all_working_dates(employee, date_from, date_to)
            scheduled_dates = all_working_dates - (pub_holidays & all_working_dates)
            stats = Payslip._analyse_attendance(employee, date_from, date_to, scheduled_dates, pub_holidays)
            rows.append({
                'badge': employee.zk_badge_no or '',
                'name': employee.name or '',
                'present_days': stats['present_days'],
                'absent_days': stats['absent_days'],
            })

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

        COLS = ['S/L', 'ID No', 'Name', 'Total Days', 'Present Days', 'Absent Days']
        WIDTHS = [6, 14, 26, 12, 14, 14]

        period_label = f"{date_from.strftime('%d %b %Y')} - {date_to.strftime('%d %b %Y')}"
        ws = wb.add_worksheet('Daily Worker Summary')

        ws.merge_range(0, 0, 1, len(COLS) - 1,
                        f'Daily Worker Present/Absent Summary  |  {period_label} (Asia/Dhaka)',
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

        for sl, r in enumerate(rows, start=1):
            ws.write(row, 0, sl, cell_fmt)
            ws.write(row, 1, r['badge'], cell_fmt)
            ws.write(row, 2, r['name'], name_fmt)
            ws.write(row, 3, total_days, cell_fmt)
            ws.write(row, 4, r['present_days'], cell_fmt)
            ws.write(row, 5, r['absent_days'], cell_fmt)
            row += 1

        wb.close()
        output.seek(0)

        fname = f"daily_worker_summary_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.xlsx"
        self.write({
            'excel_file': base64.b64encode(output.read()),
            'excel_fname': fname,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/excel_file?download=true&filename={fname}',
            'target': 'self',
        }
