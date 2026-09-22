from odoo import models, fields


DAY_OFF_SELECTION = [
    ('0', 'Monday'),
    ('1', 'Tuesday'),
    ('2', 'Wednesday'),
    ('3', 'Thursday'),
    ('4', 'Friday'),
    ('5', 'Saturday'),
    ('6', 'Sunday'),
]


class HrEmployeeDayoffHistory(models.Model):
    _name = 'hr.employee.dayoff.history'
    _description = 'Employee Weekly Day-Off History'
    _order = 'employee_id, date_start desc'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='cascade', index=True)
    day_off_day = fields.Selection(
        DAY_OFF_SELECTION, string="Weekly Day Off",
        help="Left empty means the employee had no weekly day-off during this period.")
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(
        help="Left empty while this is the employee's current day off.")
    changed_by = fields.Many2one(
        'res.users', string="Changed By", default=lambda self: self.env.user)
    note = fields.Char()

    _sql_constraints = [
        ('date_range_check', 'CHECK(date_end IS NULL OR date_end >= date_start)',
         'The end date cannot be before the start date.'),
    ]
