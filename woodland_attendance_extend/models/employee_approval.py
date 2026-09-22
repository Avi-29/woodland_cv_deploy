# -*- coding: utf-8 -*-
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
    present_address = fields.Char(string='Present Address (Vill)')
    present_post_office = fields.Char(string='Post Office')
    present_thana = fields.Char(string='Thana')
    present_district = fields.Char(string='District')

    # ── Recruitment / Position ───────────────────────────────────────────────
    department_id = fields.Many2one(
        'hr.department', string='Section',
        help='Section/department where recruitment is required.',
    )
    applied_job_id = fields.Many2one('hr.job', string='Applied Position')
    applied_salary = fields.Float(string='Applied Salary')
    examination_result = fields.Char(string='Examination Result')
    introducer = fields.Char(string='Introducer')
    approved_job_id = fields.Many2one('hr.job', string='Approved Position')
    approved_salary = fields.Float(string='Approved Salary')
    joining_date = fields.Date(string='Joining Date')

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
                # base Odoo).
                #
                # Deliberately NOT mapped here:
                # - permanent_*/present_* address, educational_qualification,
                #   examination_result, introducer: no matching hr.employee
                #   field exists yet.
                # - applied_salary/approved_salary: hr.employee.wage is a
                #   related field driven by hr.contract and is intentionally
                #   locked to the "Set Wage"/"Change Wage" wizards (see
                #   enterprise_shift_payroll/views/hr_employee_views.xml) so
                #   every change is dated in hr.employee.wage.history —
                #   writing it here would bypass that. Use those wizards on
                #   the created employee instead.
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
                })
                rec.employee_id = employee.id
            rec.write({
                'state': 'approved',
                'approved_by': self.env.user.id,
                'approved_date': fields.Datetime.now(),
            })

    def action_reject(self):
        self.write({'state': 'rejected'})

    def action_reset_to_draft(self):
        self.write({'state': 'draft'})
