{
    'name': 'Enterprise Shift Payroll',
    'version': '1.0',
    'depends': ['hr', 'hr_attendance','hr_holidays','woodland_attendance_extend'],
    'data': [
        'security/ir.model.access.csv',
        'views/payslip_report.xml',
        'views/attendance_export_wizard.xml',
        'views/daily_payroll_views.xml',
        'views/payroll_views.xml',
        'views/ot_wizard_views.xml',
        'views/hr_employee_views.xml',
        'views/menu.xml',
    ],
    'installable': True
}
