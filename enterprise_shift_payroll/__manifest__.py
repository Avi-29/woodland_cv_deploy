{
    'name': 'Enterprise Shift Payroll',
    'version': '1.0',
    'depends': ['hr', 'hr_attendance','hr_holidays','woodland_attendance_extend','zk_adms_attendance'],
    'data': [
        'security/ir.model.access.csv',
        'views/payslip_report.xml',
        'views/attendance_export_wizard.xml',
        'views/bonus_deduction_report_wizard_views.xml',
        'views/daily_worker_summary_wizard_views.xml',
        'views/daily_payroll_views.xml',
        'views/payroll_views.xml',
        'views/payroll_adjustment_views.xml',
        'views/daily_payroll_adjustment_views.xml',
        'views/ot_wizard_views.xml',
        'views/bank_export_wizard_views.xml',
        'views/hr_employee_wage_dayoff_wizard_views.xml',
        'views/hr_employee_views.xml',
        'views/payroll_month_lock_views.xml',
        'views/menu.xml',
    ],
    'installable': True
}
