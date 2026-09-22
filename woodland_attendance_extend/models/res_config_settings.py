from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    attendance_late_grace_minutes = fields.Integer(
        string="Late Grace Period (minutes)", default=15,
        help="A check-in after shift start + this many minutes is marked late.")
    attendance_morning_late_grace_minutes = fields.Integer(
        string="Morning-Shift Late Grace Period (minutes)", default=75,
        help="Used instead of the standard grace period when both the shift and "
             "the employee's department are flagged as Morning Shift.")
    attendance_auto_checkout_hours = fields.Float(
        string="Auto-Checkout After (hours)", default=20.0,
        help="An open attendance with no checkout for this many hours is "
             "automatically closed.")
    attendance_daily_night_auto_checkout_hours = fields.Float(
        string="Auto-Checkout After — Daily Worker / Night Shift (hours)", default=13.0,
        help="Auto-checkout threshold used instead of the default when the "
             "employee is a Daily Worker and the attendance's shift crosses "
             "midnight (Night Shift).")


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    attendance_late_grace_minutes = fields.Integer(
        related='company_id.attendance_late_grace_minutes', readonly=False)
    attendance_morning_late_grace_minutes = fields.Integer(
        related='company_id.attendance_morning_late_grace_minutes', readonly=False)
    attendance_auto_checkout_hours = fields.Float(
        related='company_id.attendance_auto_checkout_hours', readonly=False)
    attendance_daily_night_auto_checkout_hours = fields.Float(
        related='company_id.attendance_daily_night_auto_checkout_hours', readonly=False)
