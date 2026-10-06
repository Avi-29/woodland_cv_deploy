# -*- coding: utf-8 -*-
from dateutil.relativedelta import relativedelta
from odoo import _, fields, models
from odoo.exceptions import UserError


class HrApprovalExcelWizard(models.TransientModel):
    _name = 'hr.approval.excel.wizard'
    _description = 'Export Approval Sheets to Excel'

    export_month = fields.Date(
        string='Month',
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
        help="Pick any date — only year+month are used. Exports sheets whose "
             "Joining Date falls in this month.",
    )

    def action_export(self):
        self.ensure_one()
        month_start = self.export_month.replace(day=1)
        month_end = month_start + relativedelta(months=1, days=-1)
        sheets = self.env['hr.employee.approval'].search([
            ('joining_date', '>=', month_start),
            ('joining_date', '<=', month_end),
            ('state', '!=', 'rejected'),
        ])
        if not sheets:
            raise UserError(_('No approval sheets with a joining date in %s.')
                            % month_start.strftime('%B %Y'))
        return sheets.action_export_excel(month_start)
