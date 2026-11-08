from odoo import models, fields


class HrSection(models.Model):
    _name = 'hr.section'
    _description = 'Employee Section'
    _order = 'name'

    name = fields.Char(required=True)
    department_id = fields.Many2one(
        'hr.department', string="Department",
        help="Optional. When set, the section is only offered to employees "
             "of this department.")
    active = fields.Boolean(default=True)
    employee_ids = fields.One2many('hr.employee', 'section_id', string="Employees")

    eligible_for_bonus = fields.Boolean(
        string='Eligible for Attendance Bonus',
        default=True,
        help='If enabled, absent employees in this section lose 2 days salary '
             'per absence (instead of 1). Perfect attendance earns a 500 bonus.',
    )
    bonus_excluded_manager_ids = fields.Many2many(
        'hr.employee', 'hr_section_bonus_excluded_rel',
        'section_id', 'employee_id',
        string='Managers (No Bonus)',
        domain="[('section_id', '=', id)]",
        help='Managers of this section who never get the attendance bonus. '
             'They still lose 2 days salary per absence. Only applies while '
             'the employee belongs to this section.',
    )
