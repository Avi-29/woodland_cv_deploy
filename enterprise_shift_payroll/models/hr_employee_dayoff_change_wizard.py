from odoo import models, fields


class HrEmployeeDayoffChangeWizard(models.TransientModel):
    """Backs the 'Change Day Off' button on the employee form, letting the
    user pick the date the new weekly day off takes effect (instead of
    always defaulting to today, which is what a direct field edit does).
    Delegates to hr.employee's existing dayoff-history write() hook via
    a context flag — see woodland_attendance_extend/models/hr_employee_inherit.py."""
    _name = 'hr.employee.dayoff.change.wizard'
    _description = 'Change Employee Weekly Day Off'

    employee_id = fields.Many2one('hr.employee', required=True, readonly=True)
    current_day_off = fields.Selection(related='employee_id.day_off_day',
                                       readonly=True)
    new_day_off = fields.Selection([
        ('0', 'Monday'), ('1', 'Tuesday'), ('2', 'Wednesday'),
        ('3', 'Thursday'), ('4', 'Friday'), ('5', 'Saturday'), ('6', 'Sunday'),
    ], string='New Weekly Day Off', required=True)
    effective_date = fields.Date(
        string='Effective From', required=True,
        default=fields.Date.context_today,
        help="Payroll/attendance periods from this date onward will use "
             "the new day off; periods before it keep resolving to "
             "whatever day off was in effect at the time.",
    )
    note = fields.Char(string='Reason / Note')

    def action_confirm(self):
        self.ensure_one()
        self.employee_id.with_context(
            dayoff_effective_date=self.effective_date,
        ).write({'day_off_day': self.new_day_off})
        if self.note:
            line = self.env['hr.employee.dayoff.history'].sudo().search([
                ('employee_id', '=', self.employee_id.id),
                ('date_start', '=', self.effective_date),
            ], limit=1)
            if line:
                line.note = self.note
        return {'type': 'ir.actions.act_window_close'}
