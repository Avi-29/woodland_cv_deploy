{
    'name': 'ZKTeco ADMS Attendance',
    'version': '19.0.3.0.0',
    'category': 'Human Resources/Attendance',
    'summary': 'ZKTeco ADMS push protocol — attendance, enrollment sync, command queue',
    'author': 'Your Company',
    'depends': ['base', 'web', 'hr'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/zk_export_wizard_views.xml',
        'wizard/zk_wizard_views.xml',
        'views/hr_employee_views.xml',
        'views/zk_device_views.xml',
        'views/zk_attendance_views.xml',
        'views/zk_enrolled_views.xml',
        'report/zk_attendance_report.xml',
        'views/zk_menu_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'zk_adms_attendance/static/src/css/zk_dashboard.css',
            'zk_adms_attendance/static/src/xml/zk_monitor_template.xml',
            'zk_adms_attendance/static/src/js/zk_monitor.js',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
