from odoo import models, fields


class HrAttendanceCreateLog(models.Model):
    """Immutable audit trail of every hr.attendance creation: who created it,
    when, and for which employee. Rows are written with sudo() from
    hr.attendance.create(); users only get read access."""
    _name = 'hr.attendance.create.log'
    _description = 'Attendance Creation Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'employee_id'

    attendance_id = fields.Many2one(
        'hr.attendance', string='Attendance', ondelete='set null', index=True, readonly=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', ondelete='set null', index=True, readonly=True)
    # Kept as text so the log still says who it was for if the employee is deleted.
    employee_name = fields.Char(string='Employee Name', readonly=True)
    user_id = fields.Many2one(
        'res.users', string='Created By', ondelete='set null', index=True, readonly=True)
    check_in = fields.Datetime(string='Check In', readonly=True)
    check_out = fields.Datetime(string='Check Out', readonly=True)
    source = fields.Selection([
        ('manual', 'Manual'),
        ('biometric', 'Biometric Device'),
        ('mobile', 'Mobile App'),
        ('kiosk', 'Kiosk'),
        ('systray', 'Systray Check-in'),
        ('system', 'System'),
    ], string='Source', default='manual', readonly=True, index=True)
