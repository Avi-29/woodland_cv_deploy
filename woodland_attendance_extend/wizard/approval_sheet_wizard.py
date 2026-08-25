# -*- coding: utf-8 -*-
from odoo import fields, models


class HrApprovalSheetWizard(models.TransientModel):
    _name = 'hr.approval.sheet.wizard'
    _description = 'Choose Approval Sheet Signatories'

    sign_1 = fields.Char(string='1st Signatory', default='Sr. Manager', required=True)
    sign_2 = fields.Char(string='2nd Signatory', default='D.G.M (Production)', required=True)
    sign_3 = fields.Char(string='3rd Signatory', default='General Manager', required=True)
    sign_4 = fields.Char(string='4th Signatory', default='Managing Director', required=True)

    def action_print_approval_sheet(self):
        employees = self.env['hr.employee'].browse(self.env.context.get('active_ids'))
        report = self.env.ref('woodland_attendance_extend.action_report_approval_sheet')
        return report.report_action(employees, data={
            'approval_sign_1': self.sign_1,
            'approval_sign_2': self.sign_2,
            'approval_sign_3': self.sign_3,
            'approval_sign_4': self.sign_4,
        })
