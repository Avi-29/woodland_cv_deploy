# -*- coding: utf-8 -*-
{
    'name': 'Employee Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'KPI dashboard: present/absent by worker type for a selected date, headcount per department, new joiners and archived employees this month.',
    'author': 'Woodland',
    'depends': ['hr', 'hr_attendance', 'hr_holidays', 'woodland_attendance_extend', 'zk_adms_attendance'],
    'data': [
        'views/employee_master_dashboard_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'hr_employee_master_dashboard/static/src/xml/employee_master_dashboard_templates.xml',
            'hr_employee_master_dashboard/static/src/js/employee_master_dashboard.js',
            'hr_employee_master_dashboard/static/src/scss/employee_master_dashboard.scss',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'AGPL-3',
}
