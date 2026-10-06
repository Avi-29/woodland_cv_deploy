# -*- coding: utf-8 -*-
from odoo import api, models, _
from odoo.exceptions import ValidationError

LIVE_LEAVE_STATES = ('draft', 'confirm', 'validate1', 'validate')


class HrLeaveSwapConflict(models.Model):
    _inherit = 'hr.leave'

    @api.constrains('employee_id', 'request_date_from', 'request_date_to', 'state')
    def _check_swap_conflict(self):
        for leave in self:
            if (leave.state not in LIVE_LEAVE_STATES
                    or not leave.request_date_from or not leave.request_date_to):
                continue
            conflict = self.env['hr.swap'].search([
                ('employee_id', '=', leave.employee_id.id),
                '|', '|',
                    '&', ('swap_work_date', '>=', leave.request_date_from),
                         ('swap_work_date', '<=', leave.request_date_to),
                    '&', ('extra_work_date', '>=', leave.request_date_from),
                         ('extra_work_date', '<=', leave.request_date_to),
                    '&', ('swap_off_date', '>=', leave.request_date_from),
                         ('swap_off_date', '<=', leave.request_date_to),
            ], limit=1)
            if conflict:
                raise ValidationError(_(
                    '%(emp)s already has a swap (%(ref)s) on a day within '
                    '%(date_from)s - %(date_to)s. A leave cannot be created '
                    'for a day that already has a swap.',
                    emp=leave.employee_id.name,
                    ref=conflict.name,
                    date_from=leave.request_date_from,
                    date_to=leave.request_date_to,
                ))
