# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class HrBadgePrintWizard(models.TransientModel):
    _name = 'hr.badge.print.wizard'
    _description = 'Print Employee ID Badges by Badge No. Range'

    badge_no_from = fields.Char(string='From Badge No.', required=True)
    badge_no_to = fields.Char(string='To Badge No.', required=True)

    def _get_employees(self):
        self.ensure_one()
        try:
            low = int(self.badge_no_from)
            high = int(self.badge_no_to)
        except ValueError:
            raise UserError('Badge numbers must be numeric.')
        if low > high:
            low, high = high, low

        employees = self.env['hr.employee'].search([('zk_badge_no', '!=', False)])
        # zk_badge_no is stored as text, so filter numerically in Python
        # rather than relying on lexicographic string comparison.
        matched = employees.filtered(
            lambda e: e.zk_badge_no.isdigit() and low <= int(e.zk_badge_no) <= high
        ).sorted(key=lambda e: int(e.zk_badge_no))

        if not matched:
            raise UserError('No employees found with a badge number in that range.')
        return matched

    def action_print_badges(self):
        employees = self._get_employees()
        return self.env.ref(
            'woodland_attendance_extend.action_report_hr_id_card_a4'
        ).report_action(employees)