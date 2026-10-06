# -*- coding: utf-8 -*-
import base64
import io

import xlsxwriter
from dateutil.relativedelta import relativedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class HrEmployeeApproval(models.Model):
    """
    hr.employee.approval – Employee Approval Sheet

    Data-backed version of the "Employment Approval Note Sheet" report
    (see report_approval_sheet.py / views/id_card.xml), which was
    previously a blank fillable PDF with no underlying model.
    """
    _name = 'hr.employee.approval'
    _description = 'Employee Approval Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    # ── Personal Info ────────────────────────────────────────────────────────
    badge_id = fields.Char(string='ID No.', required=True)
    name = fields.Char(string='Applicant Name', required=True, tracking=True)
    father_name = fields.Char(string="Father's Name")
    mother_name = fields.Char(string="Mother's Name")
    date_of_birth = fields.Date(string='Date of Birth')
    present_age = fields.Integer(
        string='Present Age', compute='_compute_present_age', store=True)
    mobile_no = fields.Char(string='Mobile No')
    identification_id = fields.Char(string='NID / ID No.')
    educational_qualification = fields.Char(string='Educational Qualification')

    @api.depends('date_of_birth')
    def _compute_present_age(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.present_age = (
                relativedelta(today, rec.date_of_birth).years
                if rec.date_of_birth else 0
            )

    # ── Permanent Address ────────────────────────────────────────────────────
    permanent_address = fields.Char(string='Permanent Address (Vill)')
    permanent_post_office = fields.Char(string='Post Office')
    permanent_thana = fields.Char(string='Thana')
    permanent_district = fields.Char(string='District')

    # ── Present Address ──────────────────────────────────────────────────────
    same_as_permanent_address = fields.Boolean(string='Same as Permanent Address')
    present_address = fields.Char(string='Present Address (Vill)')
    present_post_office = fields.Char(string='Post Office')
    present_thana = fields.Char(string='Thana')
    present_district = fields.Char(string='District')

    @api.onchange(
        'same_as_permanent_address',
        'permanent_address', 'permanent_post_office', 'permanent_thana', 'permanent_district',
    )
    def _onchange_same_as_permanent_address(self):
        for rec in self:
            if rec.same_as_permanent_address:
                rec.present_address = rec.permanent_address
                rec.present_post_office = rec.permanent_post_office
                rec.present_thana = rec.permanent_thana
                rec.present_district = rec.permanent_district

    def _present_address_display(self):
        """Present address as one line ('Vill, Post Office, Thana, District')
        for hr.employee.private_street, which has no separate post
        office/thana/district fields of its own."""
        self.ensure_one()
        parts = [
            self.present_address, self.present_post_office,
            self.present_thana, self.present_district,
        ]
        return ', '.join(p for p in parts if p)

    # ── Recruitment / Position ───────────────────────────────────────────────
    department_id = fields.Many2one(
        'hr.department', string='Section',
        help='Section/department where recruitment is required.',
    )
    applied_job_id = fields.Many2one('hr.job', string='Applied Position')
    applied_salary = fields.Monetary(string='Applied Salary', currency_field='currency_id')
    examination_result = fields.Char(string='Examination Result')
    introducer = fields.Char(string='Introducer')
    approved_job_id = fields.Many2one('hr.job', string='Approved Position')
    approved_salary = fields.Monetary(string='Approved Salary', currency_field='currency_id')
    joining_date = fields.Date(string='Joining Date')
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ── Approval workflow ─────────────────────────────────────────────────────
    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], string='Status', default='draft', tracking=True, copy=False)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', readonly=True, copy=False,
        help='Employee record created when this sheet was approved.',
    )
    approved_by = fields.Many2one(
        'res.users', string='Approved By', readonly=True, copy=False)
    approved_date = fields.Datetime(
        string='Approved On', readonly=True, copy=False)

    # ── Actions ───────────────────────────────────────────────────────────────
    def action_approve(self):
        for rec in self:
            if rec.state == 'approved':
                raise UserError(_('%s is already approved.') % rec.name)
            if not rec.employee_id:
                # Mapped 1:1 against hr.employee's own field list (base +
                # every module that extends it in this repo — fathers_name/
                # mothers_name/joining_date/identification_id come from
                # woodland_attendance_extend's own hr.employee extension and
                # base Odoo; badge_id maps to zk_badge_no from
                # zk_adms_attendance, the field used everywhere else in this
                # repo as the employee's badge/ID number).
                #
                # Deliberately NOT mapped here:
                # - permanent_*/present_* address: no matching hr.employee
                #   field exists yet, except present_* which is folded into
                #   private_street below (Bangladesh-style vill/post
                #   office/thana/district addresses don't map to Odoo's
                #   western city/state/zip private address fields).
                # - examination_result, introducer: no matching hr.employee
                #   field exists yet.
                # - applied_salary/approved_salary: hr.employee.wage lives
                #   in enterprise_shift_payroll, which depends on this
                #   module (not the other way around), so it can't be set
                #   here without an inverted dependency. See
                #   enterprise_shift_payroll's own extension of
                #   action_approve(), which sets it via the same "Set
                #   Wage" wizard the employee form's button uses, right
                #   after this method creates the employee.
                employee = self.env['hr.employee'].create({
                    'name': rec.name,
                    'fathers_name': rec.father_name,
                    'mothers_name': rec.mother_name,
                    'birthday': rec.date_of_birth,
                    'mobile_phone': rec.mobile_no,
                    'identification_id': rec.identification_id,
                    'department_id': rec.department_id.id,
                    'job_id': (rec.approved_job_id or rec.applied_job_id).id,
                    'joining_date': rec.joining_date,
                    'contract_date_start': rec.joining_date,
                    'zk_badge_no': rec.badge_id,
                    'study_field': rec.educational_qualification,
                    'private_street': rec._present_address_display(),
                })
                rec.employee_id = employee.id

                # Push the new badge to every online biometric device.
                # A device being offline shouldn't block the approval
                # itself — log it on the sheet so it can be retried later
                # (the same sync is also available on the employee form).
                try:
                    employee.action_sync_to_devices()
                except UserError as exc:
                    rec.message_post(body=_(
                        'Could not sync badge %(badge)s to attendance devices: %(error)s',
                        badge=rec.badge_id, error=str(exc),
                    ))
            rec.write({
                'state': 'approved',
                'approved_by': self.env.user.id,
                'approved_date': fields.Datetime.now(),
            })

    def action_export_excel(self, month_start=None):
        """Excel list of these sheets: SL, ID, Name, Designation, Section,
        Joining Date, Approved Salary — sorted by badge. Called from
        hr.approval.excel.wizard with the chosen month (used in the
        title/filename)."""
        if not self:
            raise UserError(_('Please select at least one Employee Approval Sheet to export.'))

        def _badge_sort_key(rec):
            try:
                return (0, int(rec.badge_id))
            except (ValueError, TypeError):
                return (1, rec.badge_id or '')

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})
        ws = wb.add_worksheet('Approval Sheets')

        title_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 12,
            'align': 'center', 'valign': 'vcenter', 'border': 1,
        })
        hdr_fmt = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
        })
        cell_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter',
        })
        name_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1, 'valign': 'vcenter',
        })
        num_fmt = wb.add_format({
            'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter', 'num_format': '#,##0',
        })
        tot_lbl = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'align': 'right', 'valign': 'vcenter',
        })
        tot_num = wb.add_format({
            'bold': True, 'font_name': 'Calibri', 'font_size': 10, 'border': 1,
            'align': 'center', 'valign': 'vcenter', 'num_format': '#,##0',
        })

        cols = ['SL', 'ID', 'Name', 'Designation', 'Section', 'Joining Date', 'Approved Salary']
        widths = [5, 10, 28, 20, 20, 13, 15]
        for i, w in enumerate(widths):
            ws.set_column(i, i, w)

        title = 'Employee Approval Sheet'
        if month_start:
            title += f"  |  {month_start.strftime('%B %Y')}"
        ws.merge_range(0, 0, 0, len(cols) - 1, title, title_fmt)
        ws.set_row(0, 24)
        for c, h in enumerate(cols):
            ws.write(1, c, h, hdr_fmt)
        ws.set_row(1, 20)

        row = 2
        total = 0.0
        for sl, rec in enumerate(sorted(self, key=_badge_sort_key), start=1):
            ws.write(row, 0, sl, cell_fmt)
            ws.write(row, 1, rec.badge_id or '', cell_fmt)
            ws.write(row, 2, rec.name or '', name_fmt)
            ws.write(row, 3, (rec.approved_job_id or rec.applied_job_id).name or '', cell_fmt)
            ws.write(row, 4, rec.department_id.name or '', cell_fmt)
            ws.write(row, 5, rec.joining_date.strftime('%d-%m-%Y') if rec.joining_date else '', cell_fmt)
            ws.write(row, 6, rec.approved_salary, num_fmt)
            total += rec.approved_salary
            row += 1

        ws.merge_range(row, 0, row, 5, 'TOTAL', tot_lbl)
        ws.write(row, 6, total, tot_num)

        wb.close()
        stamp = (month_start or fields.Date.context_today(self)).strftime(
            '%Y%m' if month_start else '%Y%m%d')
        fname = f"approval_sheets_{stamp}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': fname,
            'type': 'binary',
            'datas': base64.b64encode(output.getvalue()),
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_reject(self):
        self.write({'state': 'rejected'})

    def action_reset_to_draft(self):
        self.write({'state': 'draft'})
