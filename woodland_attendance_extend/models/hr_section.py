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
