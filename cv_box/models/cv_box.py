# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class CvBox(models.Model):
    """
    CV Box — one record per CV / applicant.
    The category_id (Many2one → cv.category) answers: who can do which work.
    """
    _name = 'cv.box'
    _description = 'CV Box'
    _inherit = ['mail.thread', 'mail.activity.mixin','avatar.mixin']
    _order = 'create_date desc'
    _rec_name = 'name'

    # ── Basic Info ───────────────────────────────────────────────────
    name = fields.Char(
        string='Full Name',
        required=True,
        tracking=True,
    )
    reference = fields.Char(
        string='Reference',
        readonly=True,
        copy=False,
        default=lambda self: _('New'),
    )
    category_id = fields.Many2one(
        comodel_name='cv.category',
        string='Work Category',
        required=True,
        tracking=True,
        ondelete='restrict',
        help='Select the job role / skill area this CV belongs to.',
        index=True,
    )
    job_position = fields.Char(
        string='Applied Position',
        help='Specific job title the applicant is applying for.',
    )
    email = fields.Char(string='Email', tracking=True)
    phone = fields.Char(string='Phone')
    mobile = fields.Char(string='Mobile')

    # ── Experience & Skills ──────────────────────────────────────────
    experience_years = fields.Float(
        string='Experience (Years)',
        digits=(5, 1),
        default=0.0,
    )
    expected_salary = fields.Monetary(
        string='Expected Salary',
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )
    skills_summary = fields.Text(
        string='Skills Summary',
        help='Short bullet list of key skills.',
    )
    education = fields.Selection(
        selection=[
            ('ssc', 'SSC / O-Level'),
            ('hsc', 'HSC / A-Level'),
            ('diploma', 'Diploma'),
            ('bachelor', 'Bachelor\'s Degree'),
            ('master', 'Master\'s Degree'),
            ('phd', 'PhD / Doctorate'),
            ('other', 'Other'),
        ],
        string='Highest Education',
        default='bachelor',
    )
    education_detail = fields.Char(string='Degree / Major')

    # ── CV File Upload ───────────────────────────────────────────────
    cv_file = fields.Binary(
        string='Upload CV',
        attachment=True,
        help='Upload the CV file (PDF, DOCX, etc.).',
    )
    cv_filename = fields.Char(string='CV Filename')
    cv_mimetype = fields.Char(string='CV Mimetype', compute='_compute_cv_mimetype', store=True)


    color = fields.Integer(string='Color Index', default=0)

    # ── Notes ────────────────────────────────────────────────────────
    notes = fields.Html(string='Internal Notes')
    source = fields.Selection(
        selection=[
            ('linkedin', 'LinkedIn'),
            ('indeed', 'Indeed'),
            ('referral', 'Referral'),
            ('website', 'Company Website'),
            ('email', 'Email'),
            ('walk_in', 'Walk-in'),
            ('other', 'Other'),
        ],
        string='CV Source',
        default='other',
    )

    # ── Dates ────────────────────────────────────────────────────────
    date_received = fields.Date(
        string='Date Received',
        default=fields.Date.today,
    )
    date_available = fields.Date(string='Available From')


    # ────────────────────────────────────────────────────────────────
    # ORM Overrides
    # ────────────────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('reference', _('New')) == _('New'):
                vals['reference'] = self.env['ir.sequence'].next_by_code('cv.box') or _('New')
        return super().create(vals_list)

    # ────────────────────────────────────────────────────────────────
    # Computed fields
    # ────────────────────────────────────────────────────────────────
    @api.depends('cv_filename')
    def _compute_cv_mimetype(self):
        mime_map = {
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'odt': 'application/vnd.oasis.opendocument.text',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
        }
        for rec in self:
            if rec.cv_filename:
                ext = rec.cv_filename.rsplit('.', 1)[-1].lower()
                rec.cv_mimetype = mime_map.get(ext, 'application/octet-stream')
            else:
                rec.cv_mimetype = False

    # ────────────────────────────────────────────────────────────────
    # Constraints
    # ────────────────────────────────────────────────────────────────
    @api.constrains('email')
    def _check_email(self):
        for rec in self:
            if rec.email and '@' not in rec.email:
                raise ValidationError(_('Please enter a valid email address.'))


    def action_download_cv(self):
        """Return a URL action to download the binary CV file."""
        self.ensure_one()
        if not self.cv_file:
            raise ValidationError(_('No CV file uploaded for this record.'))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/cv.box/{self.id}/cv_file/{self.cv_filename}?download=true',
            'target': 'self',
        }
