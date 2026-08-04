from odoo import models, fields, api
from odoo.exceptions import ValidationError


class PayrollSalaryStructure(models.Model):
    """
    Configurable salary component percentages.
    One record per company (singleton per company).
    """
    _name = 'payroll.salary.structure'
    _description = 'Salary Structure Configuration'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )

    basic_pct   = fields.Float(string='Basic (%)',   default=50.0, digits=(5, 2))
    hra_pct     = fields.Float(string='HRA (%)',     default=25.0, digits=(5, 2))
    travel_pct  = fields.Float(string='Travel (%)',  default=15.0, digits=(5, 2))
    medical_pct = fields.Float(string='Medical (%)', default=10.0, digits=(5, 2))

    total_pct = fields.Float(
        string='Total (%)', compute='_compute_total', store=True,
    )

    _sql_constraints = [
        ('unique_company', 'unique(company_id)',
         'A salary structure already exists for this company.'),
    ]

    @api.depends('basic_pct', 'hra_pct', 'travel_pct', 'medical_pct')
    def _compute_total(self):
        for rec in self:
            rec.total_pct = rec.basic_pct + rec.hra_pct + rec.travel_pct + rec.medical_pct

    @api.constrains('basic_pct', 'hra_pct', 'travel_pct', 'medical_pct')
    def _check_total(self):
        for rec in self:
            total = rec.basic_pct + rec.hra_pct + rec.travel_pct + rec.medical_pct
            if abs(total - 100.0) > 0.01:
                raise ValidationError(
                    f"Salary components must sum to 100%. Current total: {total:.2f}%"
                )

    @api.model
    def get_structure(self, company=None):
        company = company or self.env.company
        struct = self.search([('company_id', '=', company.id)], limit=1)
        if not struct:
            raise ValidationError(
                "No salary structure configured for this company. "
                "Please set it up under Payroll > Configuration > Salary Structure."
            )
        return struct
