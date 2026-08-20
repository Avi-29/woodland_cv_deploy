from odoo import models, fields


class EmployeeDepartureHistory(models.Model):
    _name = 'employee.departure.history'
    _description = 'Employee Departure / Reactivation History'
    _order = 'event_date desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        ondelete='cascade', index=True,
    )
    enrolled_user_id = fields.Many2one(
        'zk.enrolled.user', string='ZKTeco Enrollment', ondelete='set null',
    )
    badge_no = fields.Char(string='Badge No', help='Snapshot of the badge at the time of this event.')
    action = fields.Selection([
        ('departed', 'Departed (removed from devices)'),
        ('reactivated', 'Reactivated (resynced to devices)'),
    ], string='Action', required=True, index=True)
    event_date = fields.Date(string='Date', required=True, default=fields.Date.context_today)
    performed_by = fields.Many2one(
        'res.users', string='Performed By', default=lambda self: self.env.user,
    )
    departure_reason_id = fields.Many2one('hr.departure.reason', string='Departure Reason')
    note = fields.Text(string='Note')
    commands_queued = fields.Integer(
        string='Device Commands Queued',
        help='How many ADMS commands were queued to devices for this event.',
    )
