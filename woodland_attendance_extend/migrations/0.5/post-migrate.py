import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Attendance-bonus eligibility moved from hr.department to hr.section.

    Since 0.4 each section was an hr.department of the same name (now
    mostly archived). Copy that department's "Eligible for Attendance
    Bonus" flag to the section, and its Manager plus "Managers (No Bonus)"
    into the section's "Managers (No Bonus)" — keeping only people who are
    still in that section.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Department = env['hr.department'].with_context(active_test=False)
    sections = env['hr.section'].with_context(active_test=False).search([])

    copied, missing = 0, []
    for sec in sections:
        old = Department.search([('name', '=ilike', sec.name)], limit=1)
        if not old:
            missing.append(sec.name)
            continue
        managers = (old.manager_id | old.bonus_excluded_manager_ids).filtered(
            lambda e: e.section_id == sec)
        sec.write({
            'eligible_for_bonus': old.eligible_for_bonus,
            'bonus_excluded_manager_ids': [(6, 0, managers.ids)],
        })
        copied += 1

    _logger.info(
        'woodland_attendance_extend 0.5 migration: copied bonus settings to '
        '%d section(s) from their old departments', copied)
    if missing:
        _logger.warning(
            'woodland_attendance_extend 0.5 migration: no old department for '
            'section(s) %s; left as bonus-eligible (default)', ', '.join(missing))
