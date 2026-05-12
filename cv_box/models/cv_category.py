# -*- coding: utf-8 -*-
from odoo import models, fields, api


class CvCategory(models.Model):
    """
    Work Category — defines WHAT kind of work this CV pool covers.
    Examples: Python Developer, Accountant, Graphic Designer, Driver …
    """
    _name = 'cv.category'
    _description = 'CV Work Category'
    _order = 'sequence, name'

    name = fields.Char(
        string='Category Name',
        required=True,
        translate=True,
    )
    sequence = fields.Integer(
        string='Sequence',
        default=10,
    )
    code = fields.Char(
        string='Short Code',
        size=20,
        help='Optional short code, e.g. "PY-DEV", "ACCT".',
    )
    description = fields.Text(
        string='Description',
        translate=True,
        help='Describe the skills/tasks that belong to this category.',
    )
    color = fields.Integer(
        string='Color Index',
        default=0,
    )
    active = fields.Boolean(
        string='Active',
        default=True,
    )

    # ── computed stats ──────────────────────────────────────────────
    cv_count = fields.Integer(
        string='# CVs',
        compute='_compute_cv_count',
        store=True,
    )

    @api.depends('name')   # recomputed whenever cv.box records change via inverse
    def _compute_cv_count(self):
        CvBox = self.env['cv.box']
        for rec in self:
            rec.cv_count = CvBox.search_count([('category_id', '=', rec.id)])

    def action_view_cvs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'CVs – {self.name}',
            'res_model': 'cv.box',
            'view_mode': 'kanban,list,form',
            'domain': [('category_id', '=', self.id)],
            'context': {'default_category_id': self.id},
        }
