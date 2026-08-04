from odoo import models, fields, api
from odoo.exceptions import UserError
import base64
import io
import logging

_logger = logging.getLogger(__name__)


class PayrollExcelWizard(models.TransientModel):
    _name = 'payroll.excel.wizard'
    _description = 'Export Payroll to Excel'

    batch_id   = fields.Many2one('payroll.batch', string='Batch', required=True)
    excel_file = fields.Binary(string='Excel File', readonly=True)
    file_name  = fields.Char(string='File Name', readonly=True)
    state      = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')

    def action_export(self):
        self.ensure_one()
        try:
            import xlsxwriter
        except ImportError:
            raise UserError("xlsxwriter is required. Install it with: pip install xlsxwriter")

        batch = self.batch_id
        if not batch.payslip_ids:
            raise UserError("No payslips found in this batch.")

        # Derive human-readable period from payroll_month
        period_label = batch.payroll_month.strftime('%B %Y') if batch.payroll_month else ''

        buffer = io.BytesIO()
        wb = xlsxwriter.Workbook(buffer, {'in_memory': True})
        ws = wb.add_worksheet(f"Payroll {batch.name}"[:31])  # sheet name max 31 chars

        # ── Colours ──────────────────────────────────────────────────────
        # Light green palette for headers
        TITLE_BG    = '#1A1A2E'   # deep navy for the title banner
        SUBHD_BG    = '#6BBF8E'   # medium green for group sub-headers
        HEADER_BG   = '#A8D5B5'   # light green for column headers
        TOTAL_BG    = '#E8F5E9'   # very light green for totals row
        WHITE       = '#FFFFFF'
        BLACK       = '#000000'
        DARK_GREEN  = '#1B5E20'   # text on light-green headers
        BORDER_CLR  = '#AAAAAA'

        money_fmt_str = '#,##0.00'
        pct_fmt_str   = '0.00"%"'

        # ── Format factory ───────────────────────────────────────────────
        def fmt(**kw):
            base = {
                'border':       1,
                'border_color': BORDER_CLR,
                'valign':       'vcenter',
            }
            base.update(kw)
            return wb.add_format(base)

        # Title row
        f_title = fmt(
            bold=True, font_size=13,
            font_color=WHITE, bg_color=TITLE_BG,
            align='center',
        )
        # Group sub-header (medium green)
        f_subhd = fmt(
            bold=True, font_size=10,
            font_color=WHITE, bg_color=SUBHD_BG,
            align='center',
        )
        # Column header (light green)
        f_hdr = fmt(
            bold=True, font_size=10,
            font_color=DARK_GREEN, bg_color=HEADER_BG,
            align='center', text_wrap=True,
        )
        # Data — centered
        f_data_c = fmt(align='center', font_size=10)
        # Data — left aligned (employee name)
        f_data_l = fmt(align='left', font_size=10)
        # Data — money
        f_money = fmt(align='right', font_size=10, num_format=money_fmt_str)
        # Data — percentage
        f_pct = fmt(align='center', font_size=10, num_format=pct_fmt_str)
        # Totals row
        f_total_lbl = fmt(bold=True, font_size=10, bg_color=TOTAL_BG, align='center')
        f_total_num = fmt(bold=True, font_size=10, bg_color=TOTAL_BG,
                          align='right', num_format=money_fmt_str)
        f_total_blank = fmt(bg_color=TOTAL_BG, align='center')

        # ── Column definitions ────────────────────────────────────────────
        # (header label, width, data_format, is_money, is_pct, slip_attr)
        columns = [
            ('SL',               5,  f_data_c,  False, False, None),
            ('Employee',         22, f_data_l,  False, False, 'employee_id.name'),
            ('Monthly Wage',     14, f_money,   True,  False, 'monthly_wage'),
            ('Period Days',      11, f_data_c,  False, False, 'working_days_in_period'),
            ('Per Day Rate',     13, f_money,   True,  False, 'per_day_rate'),
            ('Prorated Wage',    14, f_money,   True,  False, 'prorated_wage'),
            ('Basic %',          9,  f_pct,     False, True,  'basic_pct'),
            ('Basic Amount',     14, f_money,   True,  False, 'basic_amount'),
            ('HRA %',            8,  f_pct,     False, True,  'hra_pct'),
            ('HRA Amount',       13, f_money,   True,  False, 'hra_amount'),
            ('Travel %',         9,  f_pct,     False, True,  'travel_pct'),
            ('Travel Amount',    13, f_money,   True,  False, 'travel_amount'),
            ('Medical %',        9,  f_pct,     False, True,  'medical_pct'),
            ('Medical Amount',   14, f_money,   True,  False, 'medical_amount'),
            ('Gross Salary',     14, f_money,   True,  False, 'gross_salary'),
            ('Present Days',     12, f_data_c,  False, False, 'present_days'),
            ('Absent Days',      11, f_data_c,  False, False, 'absent_days'),
            ('Late Days',        10, f_data_c,  False, False, 'late_days'),
            ('Approved Leaves',  14, f_data_c,  False, False, 'approved_leave_days'),
            ('Unpaid Leaves',    13, f_data_c,  False, False, 'unpaid_leave_days'),
            ('Public Holidays',  14, f_data_c,  False, False, 'public_holiday_days'),
            ('Absent Deduction', 16, f_money,   True,  False, 'absent_deduction'),
            ('Late Deduction',   14, f_money,   True,  False, 'late_deduction'),
            ('Unpaid Deduction', 15, f_money,   True,  False, 'unpaid_leave_deduction'),
            ('Total Deductions', 16, f_money,   True,  False, 'total_deductions'),
            ('Net Salary',       14, f_money,   True,  False, 'net_salary'),
            ('Status',           14, f_data_c,  False, False, 'state'),
        ]

        num_cols = len(columns)

        # ── Row 0: Title ──────────────────────────────────────────────────
        ws.set_row(0, 28)
        ws.merge_range(0, 0, 0, num_cols - 1,
                       f"{batch.company_id.name}  |  Payroll: {batch.name}  |  Period: {period_label}",
                       f_title)

        # ── Row 1: Group sub-headers ──────────────────────────────────────
        ws.set_row(1, 20)
        groups = [
            ('Employee Info',       0,  1),
            ('Wage & Proration',    2,  5),
            ('Salary Components',   6,  14),
            ('Attendance Summary',  15, 20),
            ('Deductions',          21, 24),
            ('Payout',              25, 26),
        ]
        for label, col_start, col_end in groups:
            if col_start == col_end:
                ws.write(1, col_start, label, f_subhd)
            else:
                ws.merge_range(1, col_start, 1, col_end, label, f_subhd)

        # ── Row 2: Column headers ─────────────────────────────────────────
        ws.set_row(2, 30)
        for col_idx, (label, width, _dfmt, _im, _ip, _attr) in enumerate(columns):
            ws.write(2, col_idx, label, f_hdr)
            ws.set_column(col_idx, col_idx, width)

        # ── Data rows (starting row 3) ────────────────────────────────────
        payslips = batch.payslip_ids.sorted(key=lambda s: s.employee_id.name)
        state_labels = dict(batch.payslip_ids._fields['state'].selection)

        for sl_no, slip in enumerate(payslips, start=1):
            row = 2 + sl_no  # row index (0-based): row 3 = index 3

            # Build raw values list matching columns order
            raw_values = [
                sl_no,
                slip.employee_id.name,
                slip.monthly_wage,
                slip.working_days_in_period,
                slip.per_day_rate,
                slip.prorated_wage,
                slip.basic_pct,
                slip.basic_amount,
                slip.hra_pct,
                slip.hra_amount,
                slip.travel_pct,
                slip.travel_amount,
                slip.medical_pct,
                slip.medical_amount,
                slip.gross_salary,
                slip.present_days,
                slip.absent_days,
                slip.late_days,
                slip.approved_leave_days,
                slip.unpaid_leave_days,
                slip.public_holiday_days,
                slip.absent_deduction,
                slip.late_deduction,
                slip.unpaid_leave_deduction,
                slip.total_deductions,
                slip.net_salary,
                state_labels.get(slip.state, slip.state),
            ]

            ws.set_row(row, 18)
            for col_idx, (val, (_lbl, _w, dfmt, _im, _ip, _attr)) in enumerate(
                    zip(raw_values, columns)):
                ws.write(row, col_idx, val, dfmt)

        # ── Totals row ────────────────────────────────────────────────────
        total_row   = 2 + len(payslips) + 1   # 0-based index
        data_start  = 3                         # first data row (0-based)
        data_end    = 2 + len(payslips)         # last  data row (0-based)

        money_col_indices = {col_idx for col_idx, (_l, _w, _f, is_m, _ip, _a)
                             in enumerate(columns) if is_m}

        ws.set_row(total_row, 20)
        ws.write(total_row, 0, 'TOTAL', f_total_lbl)

        for col_idx in range(1, num_cols):
            if col_idx in money_col_indices:
                col_letter = chr(ord('A') + col_idx) if col_idx < 26 else (
                    chr(ord('A') + col_idx // 26 - 1) + chr(ord('A') + col_idx % 26)
                )
                formula = (f'=SUM({col_letter}{data_start + 1}:'
                           f'{col_letter}{data_end + 1})')
                ws.write_formula(total_row, col_idx, formula, f_total_num)
            else:
                ws.write(total_row, col_idx, '', f_total_blank)

        # ── Freeze top 3 rows + first 2 columns ──────────────────────────
        ws.freeze_panes(3, 2)

        # ── Save ─────────────────────────────────────────────────────────
        wb.close()
        buffer.seek(0)
        file_data = base64.b64encode(buffer.read())

        month_str = batch.payroll_month.strftime('%Y_%m') if batch.payroll_month else 'unknown'
        file_name = f"Payroll_{batch.name.replace(' ', '_')}_{month_str}.xlsx"

        self.write({
            'excel_file': file_data,
            'file_name':  file_name,
            'state':      'done',
        })

        return {
            'type':      'ir.actions.act_window',
            'res_model': 'payroll.excel.wizard',
            'view_mode': 'form',
            'res_id':    self.id,
            'target':    'new',
        }