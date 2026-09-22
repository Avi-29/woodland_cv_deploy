{
    'name': "woodland_attendance_extend",

    'summary': "Short (1 phrase/line) summary of the module's purpose",

    'description': """
Long description of module's purpose
    """,

    'author': "My Company",
    'website': "https://www.yourcompany.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.3',

    # any module necessary for this one to work correctly
    'depends': ['hr','hr_attendance','hr_holidays','zk_adms_attendance'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'wizard/attendance_daily_report.xml',
        'views/shift_views.xml',
        'views/hr_attendance_inherit.xml',
        'views/hr_swap_views.xml',
        'views/id_card.xml',
        'wizard/id_card_batch.xml',
        'wizard/approval_sheet_wizard.xml',
        'views/res_config_settings_views.xml',
        'views/employee_approval_views.xml',
        'views/ir_attachment_documents_views.xml',
        'views/menu.xml'
    ],

    'assets': {
        'web.assets_backend': [
            'woodland_attendance_extend/static/src/js/documents_kanban/*.js',
        ],
    },
}

