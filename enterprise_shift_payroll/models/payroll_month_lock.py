from odoo import models, fields, api, _
from odoo.exceptions import UserError


class PayrollMonthLock(models.Model):
    """A locked (company, month) pair blocks create/write/unlink and
    recompute of any payroll.payslip in that month — see the guards in
    payroll.payslip (create/write/unlink overrides + the check at the
    top of _compute_payslip). Managed via payroll.month.lock.wizard,
    not directly."""
    _name = 'payroll.month.lock'
    _description = 'Payroll Month Lock'
    _order = 'month desc'
    _rec_name = 'month'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    month = fields.Date(
        string='Payroll Month', required=True,
        help="First day of the locked payroll month.",
    )
    locked_by = fields.Many2one(
        'res.users', string='Locked By', readonly=True,
        default=lambda self: self.env.user,
    )
    locked_date = fields.Datetime(
        string='Locked On', readonly=True, default=fields.Datetime.now,
    )
    note = fields.Char(string='Note')

    _sql_constraints = [
        ('month_company_uniq', 'UNIQUE(company_id, month)',
         'This payroll month is already locked for this company.'),
    ]

    @api.onchange('month')
    def _onchange_month(self):
        if self.month:
            self.month = self.month.replace(day=1)

    @api.model
    def _is_locked(self, company, month_date):
        """True if `month_date`'s month is locked for `company`. Runs as
        sudo so the lock check works for any caller regardless of their
        own access to this model — only locking/unlocking itself is
        access-restricted (see security/ir.model.access.csv)."""
        if not month_date:
            return False
        company_id = company.id if company else self.env.company.id
        return bool(self.sudo().search_count([
            ('company_id', '=', company_id),
            ('month', '=', month_date.replace(day=1)),
        ]))


class PayrollMonthLockWizard(models.TransientModel):
    """Payroll → Configuration → Lock Payroll Month. Pick a month, see
    whether it's currently locked, and lock/unlock it. The actual
    enforcement lives entirely in payroll.payslip's create/write/unlink
    overrides and its recompute guard — this wizard only manages the
    payroll.month.lock record."""
    _name = 'payroll.month.lock.wizard'
    _description = 'Lock / Unlock Payroll Month'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    month = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help="Pick any date — only the year+month matter.",
    )
    is_locked = fields.Boolean(compute='_compute_is_locked')
    note = fields.Char(string='Reason / Note')

    @api.onchange('month')
    def _onchange_month(self):
        if self.month:
            self.month = self.month.replace(day=1)

    @api.depends('company_id', 'month')
    def _compute_is_locked(self):
        Lock = self.env['payroll.month.lock']
        for wiz in self:
            wiz.is_locked = bool(
                wiz.month and Lock._is_locked(wiz.company_id, wiz.month)
            )

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_lock(self):
        self.ensure_one()
        month_start = self.month.replace(day=1)
        if self.env['payroll.month.lock']._is_locked(self.company_id, month_start):
            raise UserError(_("This payroll month is already locked."))
        self.env['payroll.month.lock'].create({
            'company_id': self.company_id.id,
            'month': month_start,
            'note': self.note,
        })
        return self._reopen()

    def action_unlock(self):
        self.ensure_one()
        month_start = self.month.replace(day=1)
        self.env['payroll.month.lock'].search([
            ('company_id', '=', self.company_id.id),
            ('month', '=', month_start),
        ]).unlink()
        return self._reopen()
