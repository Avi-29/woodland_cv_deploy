# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class HrApprovalSheetWizard(models.TransientModel):
    _name = 'hr.approval.sheet.wizard'
    _description = 'Choose Approval Sheet Signatories'

    sign_1 = fields.Char(string='1st Signatory', default='Sr. Manager', required=True)
    sign_2 = fields.Char(string='2nd Signatory', default='D.G.M (Production)', required=True)
    sign_3 = fields.Char(string='3rd Signatory', default='General Manager', required=True)
    sign_4 = fields.Char(string='4th Signatory', default='Managing Director', required=True)

    def action_print_approval_sheet(self):
        # active_ids isn't set on every path that can open this wizard (e.g.
        # a single-record form action only guarantees active_id) — without
        # this fallback, sheets/docs silently ends up empty and the report's
        # t-foreach="docs" renders a page-less, blank PDF with no error.
        context = self.env.context
        active_ids = context.get('active_ids') or (
            [context['active_id']] if context.get('active_id') else [])
        sheets = self.env['hr.employee.approval'].browse(active_ids)
        if not sheets:
            raise UserError(_('No Employee Approval Sheet selected to print.'))
        report = self.env.ref('woodland_attendance_extend.action_report_approval_sheet')
        return report.report_action(sheets, data={
            'approval_sign_1': self.sign_1,
            'approval_sign_2': self.sign_2,
            'approval_sign_3': self.sign_3,
            'approval_sign_4': self.sign_4,
        })
