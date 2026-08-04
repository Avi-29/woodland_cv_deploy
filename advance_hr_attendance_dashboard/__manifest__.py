# -*- coding: utf-8 -*-
{
    "name": "Advance HR Attendance Dashboard",
    "version": "19.0.3.0.0",
    "category": "Human Resources",
    "summary": "Production HR Attendance Dashboard – month/year filter, drag-to-leave, "
               "public holidays, swap days, leave breakdown, badge search, pagination.",
    "author": "Cybrosys Techno Solutions",
    "company": "Cybrosys Techno Solutions",
    "maintainer": "Cybrosys Techno Solutions",
    "website": "https://www.cybrosys.com",
    "depends": ["hr_holidays", "hr", "hr_attendance", "mail","woodland_attendance_extend"],
    "data": [
        # "security/ir.model.access.csv",
        "views/hr_leave_type_views.xml",
        "views/advance_hr_attendance_dashboard_menus.xml",
        "views/res_config_settings_views.xml",
        # "report/hr_attendance_reports.xml",
        # "report/hr_attendance_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "advance_hr_attendance_dashboard/static/src/xml/attendance_dashboard_templates.xml",
            "advance_hr_attendance_dashboard/static/src/js/attendance_dashboard.js",
            "advance_hr_attendance_dashboard/static/src/scss/attendance_dashboard.scss",
        ],
    },
    "external_dependencies": {"python": ["pandas"]},
    "images": ["static/description/banner.png"],
    "license": "AGPL-3",
    "installable": True,
    "auto_install": False,
    "application": False,
}