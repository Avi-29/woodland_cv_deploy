from datetime import timedelta

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HrEmployeeWageChangeWizard(models.TransientModel):
    """Backs both the 'Set Wage' and 'Change Wage' buttons on the employee
    form. Writing the wage directly is disabled on that form — every
    change goes through here so it's dated (by payroll month, not by
    exact day) and kept in hr.employee.wage.history, letting payroll
    resolve the wage that was actually in effect for any given payroll
    month instead of whatever the wage happens to be today."""
    _name = 'hr.employee.wage.change.wizard'
    _description = 'Set or Change Employee Wage'

    employee_id = fields.Many2one('hr.employee', required=True, readonly=True)
    mode = fields.Selection([
        ('set', 'Set Wage'),
        ('change', 'Change Wage'),
    ], required=True, default='change')
    currency_id = fields.Many2one(
        related='employee_id.company_id.currency_id', readonly=True)
    current_wage = fields.Monetary(related='employee_id.wage', readonly=True)
    new_wage = fields.Monetary(string='New Wage', required=True)
    effective_date = fields.Date(
        string='Effective Month', required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help="Payroll months from this one onward will use the new wage; "
             "months before it keep resolving to whatever wage was in "
             "effect at the time. Pick any date — only the year+month matter.",
    )
    note = fields.Char(string='Reason / Note')

    @api.onchange('effective_date')
    def _onchange_effective_date(self):
        if self.effective_date:
            self.effective_date = self.effective_date.replace(day=1)

    def action_confirm(self):
        self.ensure_one()
        employee = self.employee_id

        if self.mode == 'set' and employee.wage:
            raise UserError(_(
                "%s already has a wage set. Use Change Wage instead.",
                employee.name))
        if self.mode == 'change' and not employee.wage:
            raise UserError(_(
                "%s has no wage set yet. Use Set Wage instead.", employee.name))

        effective_month = self.effective_date.replace(day=1)
        previous_wage = employee.wage or 0.0
        change_amount = self.new_wage - previous_wage
        if self.mode == 'set':
            change_type = 'initial'
        elif change_amount > 0:
            change_type = 'increase'
        elif change_amount < 0:
            change_type = 'decrease'
        else:
            change_type = 'no_change'

        History = self.env['hr.employee.wage.history'].sudo()
        open_line = History.search([
            ('employee_id', '=', employee.id),
            ('date_end', '=', False),
        ], limit=1, order='date_start desc')

        line_vals = {
            'wage': self.new_wage,
            'previous_wage': previous_wage,
            'change_amount': change_amount,
            'change_type': change_type,
            'note': self.note,
        }

        if open_line and open_line.date_start == effective_month:
            # Same effective month as the currently-open line — update it
            # in place instead of stacking a second row for that month.
            open_line.write(line_vals)
        else:
            if open_line:
                if effective_month <= open_line.date_start:
                    raise UserError(_(
                        "Effective month must be after %s, when the "
                        "current wage took effect.", open_line.date_start))
                open_line.date_end = effective_month - timedelta(days=1)
            History.create({
                **line_vals,
                'employee_id': employee.id,
                'date_start': effective_month,
            })

        employee.wage = self.new_wage
        return {'type': 'ir.actions.act_window_close'}
