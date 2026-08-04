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
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['hr','hr_attendance','zk_adms_attendance'],

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
        'views/menu.xml'
    ],
}

