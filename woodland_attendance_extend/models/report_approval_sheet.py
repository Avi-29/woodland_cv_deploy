# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError

DEFAULT_SIGNATORIES = {
    'approval_sign_1': 'Sr. Manager',
    'approval_sign_2': 'D.G.M (Production)',
    'approval_sign_3': 'General Manager',
    'approval_sign_4': 'Managing Director',
}


class ReportApprovalSheet(models.AbstractModel):
    _name = 'report.woodland_attendance_extend.report_approval_sheet'
    _description = 'Employment Approval Note Sheet Report Values'

    def _get_report_values(self, docids, data=None):
        data = data or {}
        docs = self.env['hr.employee.approval'].browse(docids)
        if not docs:
            # Printing with no record selected (e.g. hitting Print from the
            # list view with nothing checked) used to silently render a
            # blank, content-less PDF - t-foreach="docs" just iterates zero
            # times. Fail loudly instead.
            raise UserError(_('Please select at least one Employee Approval Sheet to print.'))
        values = {
            'doc_ids': docids,
            'doc_model': 'hr.employee.approval',
            'docs': docs,
            'print_date': fields.Date.context_today(self).strftime('%d-%m-%Y'),
        }
        for key, default in DEFAULT_SIGNATORIES.items():
            values[key] = data.get(key) or default
        return values
