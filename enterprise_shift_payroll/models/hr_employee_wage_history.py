from odoo import models, fields


class HrEmployeeWageHistory(models.Model):
    """One row per wage change, dated by payroll month so payroll can
    resolve the wage that was actually in effect for any given period
    (hr.employee.get_wage()). previous_wage/change_amount/change_type are
    snapshotted by the Set/Change Wage wizard at creation time, so a
    salary increase/decrease summary can read them straight off this
    table (filtered by date_start / change_type) instead of having to
    diff consecutive rows itself."""
    _name = 'hr.employee.wage.history'
    _description = 'Employee Wage Change History'
    _order = 'employee_id, date_start desc'

    employee_id = fields.Many2one('hr.employee', required=True,
                                  ondelete='cascade', index=True)
    currency_id = fields.Many2one(
        related='employee_id.company_id.currency_id', readonly=True)
    wage = fields.Monetary(string='New Wage', required=True)
    date_start = fields.Date(
        string='Effective Month', required=True, index=True,
        help="First day of the payroll month this wage takes effect from.")
    date_end = fields.Date(
        help="Left empty while this is the employee's current wage.")
    previous_wage = fields.Monetary(
        string='Previous Wage', readonly=True,
        help="Wage before this change (0 for the first/initial entry).")
    change_amount = fields.Monetary(
        string='Change', readonly=True,
        help="New Wage minus Previous Wage.")
    change_type = fields.Selection([
        ('initial', 'Initial'),
        ('increase', 'Increase'),
        ('decrease', 'Decrease'),
        ('no_change', 'No Change'),
    ], string='Change Type', readonly=True)
    changed_by = fields.Many2one('res.users', string='Changed By',
                                 default=lambda self: self.env.user)
    note = fields.Char()

    _sql_constraints = [
        ('date_range_check', 'CHECK(date_end IS NULL OR date_end >= date_start)',
         'The end date cannot be before the start date.'),
    ]
