import csv
import logging
import os
from collections import defaultdict

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def _norm_badge(badge):
    return (badge or '').strip().lstrip('0')


def migrate(cr, version):
    """Until now hr.department held what is really an employee's *section*
    (DRYER, LOG, P-G, ...), and the factory (PLYWOOD, MDF, ...) wasn't
    recorded anywhere. Using employee_factory.csv (badge -> factory, taken
    from the salary sheet), turn each employee's current department into
    their section_id and move them to the factory department.

    - Section name = the employee's current department name, falling back to
      the sheet's SECTION column for employees with no department.
    - Factory departments are matched case-insensitively (MELAMINE /
      Melamine are one) and created if missing; an existing department of
      that name (e.g. MDF, SECURITY) is reused, keeping its bonus/morning
      settings.
    - A section is linked to a factory only if all its employees are in that
      one factory; otherwise (e.g. GENERAL) it's left shared.
    - Old section-departments left with no employees are archived.
    - Employees not in the sheet are left untouched (logged).
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Department = env['hr.department'].with_context(active_test=False)
    Section = env['hr.section'].with_context(active_test=False)

    path = os.path.join(os.path.dirname(__file__), 'employee_factory.csv')
    with open(path, newline='', encoding='utf-8') as f:
        sheet = {_norm_badge(r['badge']): r for r in csv.DictReader(f)}

    employees = env['hr.employee'].with_context(active_test=False).search([])
    old_departments = employees.department_id

    dept_cache = {}

    def factory_department(name, company):
        key = (name.upper(), company.id)
        if key not in dept_cache:
            dept = Department.search([
                ('name', '=ilike', name),
                ('company_id', 'in', [company.id, False]),
            ], limit=1)
            if not dept:
                dept = Department.create({'name': name.upper(), 'company_id': company.id})
            elif not dept.active:
                dept.active = True
            dept_cache[key] = dept
        return dept_cache[key]

    section_cache = {}

    def section(name):
        key = name.upper()
        if key not in section_cache:
            sec = Section.search([('name', '=ilike', name)], limit=1)
            if not sec:
                sec = Section.create({'name': name})
            elif not sec.active:
                sec.active = True
            section_cache[key] = sec
        return section_cache[key]

    section_factories = defaultdict(set)
    unmatched = []
    moved = 0
    for emp in employees:
        row = sheet.get(_norm_badge(emp.zk_badge_no))
        if not row or not row['factory']:
            unmatched.append(emp.zk_badge_no or emp.name)
            continue
        section_name = (emp.department_id.name or row['section'] or '').strip()
        factory = factory_department(row['factory'].strip(), emp.company_id)
        vals = {'department_id': factory.id}
        if section_name:
            sec = section(section_name)
            vals['section_id'] = sec.id
            section_factories[sec].add(factory)
        emp.write(vals)
        moved += 1

    for sec, factories in section_factories.items():
        sec.department_id = factories.pop().id if len(factories) == 1 else False

    factories = env['hr.department'].browse([d.id for d in dept_cache.values()])
    emptied = (old_departments - factories).filtered(
        lambda d: not env['hr.employee'].with_context(active_test=False).search_count(
            [('department_id', '=', d.id)])
        and not d.child_ids)
    emptied.write({'active': False})

    _logger.info(
        'woodland_attendance_extend 0.4 migration: moved %d employee(s) to '
        '%d factory department(s) across %d section(s); archived %d emptied '
        'department(s): %s', moved, len(factories), len(section_factories),
        len(emptied), ', '.join(emptied.mapped('name')))
    if unmatched:
        _logger.warning(
            'woodland_attendance_extend 0.4 migration: %d employee(s) not in '
            'the salary sheet, left unchanged: %s',
            len(unmatched), ', '.join(str(u) for u in unmatched))
