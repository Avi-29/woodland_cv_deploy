from odoo import models, fields, _

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    salary_type = fields.Selection([
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], default='monthly', required=True)

    daily_wage = fields.Float("Daily Wage")

    wage_history_ids = fields.One2many(
        'hr.employee.wage.history', 'employee_id', string='Wage History')

    def get_wage(self, on_date):
        """Wage in effect on `on_date`, following the recorded history
        (see hr.employee.wage.history / the Set Wage-Change Wage wizard).
        Falls back to the live field for dates with no history line."""
        self.ensure_one()
        line = self.env['hr.employee.wage.history'].sudo().search([
            ('employee_id', '=', self.id),
            ('date_start', '<=', on_date),
            '|', ('date_end', '=', False), ('date_end', '>=', on_date),
        ], limit=1, order='date_start desc')
        return line.wage if line else self.wage

    def action_set_wage(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Set Wage'),
            'res_model': 'hr.employee.wage.change.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_employee_id': self.id,
                'default_mode': 'set',
                'default_new_wage': self.wage,
            },
        }

    def action_change_wage(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Change Wage'),
            'res_model': 'hr.employee.wage.change.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_employee_id': self.id,
                'default_mode': 'change',
                'default_new_wage': self.wage,
            },
        }

    def action_change_dayoff(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Change Weekly Day Off'),
            'res_model': 'hr.employee.dayoff.change.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_employee_id': self.id,
                'default_new_day_off': self.day_off_day,
            },
        }
