# -*- coding: utf-8 -*-
from odoo import api, fields, models


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    document_section = fields.Selection([
        ('leave', 'Leave'),
        ('other', 'Other Documents'),
    ], string='Document Section', compute='_compute_document_section', store=True)

    @api.depends('res_model')
    def _compute_document_section(self):
        for attachment in self:
            attachment.document_section = 'leave' if attachment.res_model == 'hr.leave' else 'other'
