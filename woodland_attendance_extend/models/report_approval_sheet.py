# -*- coding: utf-8 -*-
from odoo import models

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
        docs = self.env['hr.employee'].browse(docids)
        values = {
            'doc_ids': docids,
            'doc_model': 'hr.employee',
            'docs': docs,
        }
        for key, default in DEFAULT_SIGNATORIES.items():
            values[key] = data.get(key) or default
        return values
