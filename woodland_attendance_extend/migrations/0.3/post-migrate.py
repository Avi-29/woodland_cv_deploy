import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """hr.employee.hours_last_month had no @api.depends from its
    introduction (0.1) until today (0.2), so it only ever computed once, at
    install time, and never again as new attendance came in. The missing
    @api.depends is now fixed, but that only makes it recompute for future
    writes - every employee's already-stored value is still stuck at
    whatever it was back then. Force one recompute here so existing data
    catches up.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    employees = env['hr.employee'].with_context(active_test=False).search([])
    employees._compute_hours_last_month_over()
    employees.flush_recordset(['hours_last_month'])

    _logger.info(
        'woodland_attendance_extend 0.3 migration: recomputed hours_last_month '
        'for %d employee(s).', len(employees))
