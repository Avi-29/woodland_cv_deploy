from odoo import models, fields

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    salary_type = fields.Selection([
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], default='monthly', required=True)

    daily_wage = fields.Float("Daily Wage")
