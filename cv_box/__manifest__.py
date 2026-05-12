# -*- coding: utf-8 -*-
{
    'name': 'CV Box',
    'version': '19.0.1.0.0',
    'category': 'Human Resources',
    'summary': 'Store and manage CVs with job categories',
    'description': """
CV Box
======
A complete CV management module for Odoo 19.

Features:
---------
* Upload and store CV files (PDF, DOCX, etc.)
* Organize CVs by Work Category (Many2one)
* Track applicant details, skills, experience
* Smart search and filter by category / status
* Kanban & List views
* Quick preview of uploaded CV
    """,
    'author': 'Your Company',
    'website': 'https://yourcompany.com',
    'license': 'LGPL-3',
    'depends': ['base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/cv_category_data.xml',
        'views/cv_category_views.xml',
        'views/cv_box_views.xml',
        'views/menu_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'cv_box/static/src/css/cv_box.css',
        ],
    },
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
