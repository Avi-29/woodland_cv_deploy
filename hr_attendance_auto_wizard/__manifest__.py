{
    'name': 'HR Attendance Auto Wizard',
    'version': '19.0.1.0.0',
    'summary': 'Bulk-create attendance for a date range, skipping day-offs and absences',
    'description': """
Automatic Attendance Creation Wizard
=====================================
- Pick an employee and a date range.
- Compute: lists every working day in the range that has no attendance yet,
  skipping the employee's day-off days (per resource.calendar) automatically.
- Days on approved leave are shown but unchecked by default (skipped).
- Uncheck any day you don't want attendance created for (e.g. a genuine absence),
  or check a leave day if you want to override it.
- Create: builds hr.attendance records for all checked days using the
  employee's calendar working hours, timezone-corrected (Asia/Dhaka by default).

Requires an hr.employee field `day_off_day` (Selection, '0'=Monday..'6'=Sunday)
to already exist on the employee model, e.g. from another installed module such
as advance_hr_attendance_dashboard. Add that module to the depends list below
if it isn't already guaranteed to load first.
""",
    'category': 'Human Resources/Attendances',
    'author': 'Avishek',
    'depends': ['woodland_attendance_extend', 'hr_holidays', 'resource'],
    'data': [
        'security/ir.model.access.csv',
        'views/hr_attendance_auto_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
