# -*- coding: utf-8 -*-
from odoo import models, _


class HrEmployeeApprovalWage(models.Model):
    """Sets the new employee's wage from the approval sheet's approved
    salary. Lives here rather than on hr.employee.approval itself
    (woodland_attendance_extend) because hr.employee.wage.change.wizard
    is defined in this module, which depends on that one — not the
    other way around."""
    _inherit = 'hr.employee.approval'

    def action_approve(self):
        had_employee = {rec.id: bool(rec.employee_id) for rec in self}
        result = super().action_approve()
        for rec in self:
            if had_employee.get(rec.id) or not rec.employee_id or not rec.approved_salary:
                continue
            # Same wizard the "Set Wage" button on the employee form uses,
            # so the wage is dated and logged in hr.employee.wage.history
            # like every other wage change instead of writing
            # employee.wage directly.
            self.env['hr.employee.wage.change.wizard'].create({
                'employee_id': rec.employee_id.id,
                'mode': 'set',
                'new_wage': rec.approved_salary,
                'note': _('Initial wage from Employee Approval Sheet %s') % rec.name,
            }).action_confirm()
        return result
