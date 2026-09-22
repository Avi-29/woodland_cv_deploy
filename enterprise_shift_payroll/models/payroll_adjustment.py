from calendar import monthrange

from odoo import models, fields, api


class PayrollAdjustment(models.Model):
    _name = 'payroll.adjustment'
    _description = 'Payroll Adjustment (Last Month Due / Penalty / Advance)'
    _order = 'payroll_month desc, employee_id, id'

    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company,
    )

    payroll_month = fields.Date(
        string='Payroll Month',
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help="First day of the payroll month this adjustment applies to.",
    )

    adjustment_type = fields.Selection([
        ('last_month', 'Last Month Due'),
        ('penalty', 'Penalty'),
        ('advance', 'Advance Payment'),
    ], string='Type', required=True, default='last_month')

    amount = fields.Float(string='Amount', digits=(16, 0), required=True, default=0.0)
    last_month_days = fields.Float(
        string='Days',
        digits=(16, 2),
        help="Number of days being added for 'Last Month Due'. Amount is "
             "auto-calculated as (wage / calendar days in the payroll month) * days.",
    )
    penalty_days = fields.Float(
        string='Penalty Days',
        digits=(16, 2),
        help="Optional: number of days being deducted as penalty. When set, "
             "Amount is auto-calculated as (wage / calendar days in the "
             "payroll month) * days — you can still edit Amount directly "
             "afterwards, or skip Days entirely and just enter Amount.",
    )
    date = fields.Date(string='Entry Date', default=fields.Date.context_today)
    note = fields.Char(string='Reason / Note')

    pending_penalty = fields.Float(
        string='Pending Penalty', digits=(16, 0), readonly=True, copy=False,
        help="Penalty amount for this month that couldn't be deducted because "
             "salary ran out. Tracked here for reference; set on the most "
             "recent penalty entry for the employee/month when payslips are computed.",
    )
    pending_advance = fields.Float(
        string='Pending Advance', digits=(16, 0), readonly=True, copy=False,
        help="Advance amount for this month that couldn't be deducted because "
             "salary ran out. Tracked here for reference; set on the most "
             "recent advance entry for the employee/month when payslips are computed.",
    )

    @api.onchange('payroll_month')
    def _onchange_payroll_month(self):
        if self.payroll_month:
            self.payroll_month = self.payroll_month.replace(day=1)

    @api.onchange('adjustment_type', 'employee_id', 'payroll_month', 'last_month_days')
    def _onchange_last_month_days(self):
        if self.adjustment_type == 'last_month' and self.employee_id and self.payroll_month:
            self.amount = self._compute_days_amount(
                self.employee_id, self.payroll_month, self.last_month_days,
            )

    @api.onchange('adjustment_type', 'employee_id', 'payroll_month', 'penalty_days')
    def _onchange_penalty_days(self):
        if self.adjustment_type == 'penalty' and self.employee_id and self.payroll_month and self.penalty_days:
            self.amount = self._compute_days_amount(
                self.employee_id, self.payroll_month, self.penalty_days,
            )

    @api.model
    def _compute_days_amount(self, employee, payroll_month, days):
        if not employee or not payroll_month:
            return 0.0
        wage = employee.get_wage(payroll_month) or 0.0
        calendar_days = monthrange(payroll_month.year, payroll_month.month)[1]
        return (wage / calendar_days) * days if calendar_days else 0.0

    def _sync_days_amount(self):
        for rec in self:
            if rec.adjustment_type == 'last_month' and rec.employee_id and rec.payroll_month:
                # Last Month Due is always fully days-driven — Amount stays
                # readonly on the view, so it must always track Days.
                amount = rec._compute_days_amount(
                    rec.employee_id, rec.payroll_month, rec.last_month_days,
                )
                if amount != rec.amount:
                    rec.amount = amount
            elif (rec.adjustment_type == 'penalty' and rec.employee_id
                  and rec.payroll_month and rec.penalty_days):
                # Penalty supports both: entering Days auto-fills Amount,
                # but Amount can also be entered directly with no Days at
                # all — so only overwrite Amount here when Days is set.
                amount = rec._compute_days_amount(
                    rec.employee_id, rec.payroll_month, rec.penalty_days,
                )
                if amount != rec.amount:
                    rec.amount = amount

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_days_amount()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in
               ('adjustment_type', 'employee_id', 'payroll_month',
                'last_month_days', 'penalty_days')):
            self._sync_days_amount()
        return res

    @api.model
    def get_totals(self, employee_id, payroll_month):
        """Sum adjustments for one employee/month, split by type.

        Multiple entries of the same type in a month (e.g. several
        penalties) are added together automatically.
        """
        totals = {'last_month': 0.0, 'penalty': 0.0, 'advance': 0.0}
        if not employee_id or not payroll_month:
            return totals
        groups = self.read_group(
            domain=[
                ('employee_id', '=', employee_id),
                ('payroll_month', '=', payroll_month),
            ],
            fields=['amount:sum'],
            groupby=['adjustment_type'],
        )
        for g in groups:
            adj_type = g['adjustment_type']
            if adj_type in totals:
                totals[adj_type] = g['amount']
        return totals

    @api.model
    def set_pending_amounts(self, employee_id, payroll_month, pending_penalty, pending_advance):
        """Record how much of this month's penalty/advance couldn't be
        deducted (salary ran out) on the most recent entry of each type,
        so it stays visible for reference. Any other entries of that type
        for the same employee/month are cleared to avoid a stale figure
        showing on more than one row.
        """
        if not employee_id or not payroll_month:
            return
        domain_base = [('employee_id', '=', employee_id), ('payroll_month', '=', payroll_month)]

        penalty_recs = self.search(domain_base + [('adjustment_type', '=', 'penalty')],
                                    order='date desc, id desc')
        if penalty_recs:
            penalty_recs.write({'pending_penalty': 0.0})
            penalty_recs[0].pending_penalty = pending_penalty

        advance_recs = self.search(domain_base + [('adjustment_type', '=', 'advance')],
                                    order='date desc, id desc')
        if advance_recs:
            advance_recs.write({'pending_advance': 0.0})
            advance_recs[0].pending_advance = pending_advance

    @api.model
    def get_last_month_days_map(self, payroll_month):
        """Sum 'Last Month Due' days per employee for one payroll month."""
        if not payroll_month:
            return {}
        groups = self.read_group(
            domain=[
                ('payroll_month', '=', payroll_month),
                ('adjustment_type', '=', 'last_month'),
            ],
            fields=['last_month_days:sum'],
            groupby=['employee_id'],
        )
        return {
            g['employee_id'][0]: g['last_month_days']
            for g in groups if g['employee_id']
        }
