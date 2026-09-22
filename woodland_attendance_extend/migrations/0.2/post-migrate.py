import logging
from datetime import date

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Anchor date for backfilled history lines: far enough in the past to cover
# every existing attendance/payroll record, so nothing already computed
# changes as a result of this upgrade. Only day-off changes made after this
# migration create new, properly-dated history lines.
BACKFILL_ANCHOR = date(2000, 1, 1)


def migrate(cr, version):
    """Give every existing employee an open-ended day-off history line
    matching their current hr.employee.day_off_day, so
    hr.employee.get_day_off_day() has something to resolve for all past
    dates and behaves exactly like the old plain-field lookup until an
    admin actually changes someone's day off.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Employee = env['hr.employee'].with_context(active_test=False)
    History = env['hr.employee.dayoff.history']

    employees = Employee.search([])
    already_covered = set(
        History.search([('employee_id', 'in', employees.ids)]).mapped('employee_id.id')
    )

    vals_list = [
        {
            'employee_id': emp.id,
            'day_off_day': emp.day_off_day,
            'date_start': BACKFILL_ANCHOR,
            'note': 'Backfilled on module upgrade to 0.2 (pre-existing value).',
        }
        for emp in employees
        if emp.id not in already_covered
    ]

    if vals_list:
        History.create(vals_list)

    _logger.info(
        'woodland_attendance_extend 0.2 migration: backfilled day-off history '
        'for %d employee(s).', len(vals_list)
    )
