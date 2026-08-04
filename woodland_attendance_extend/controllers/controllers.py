# from odoo import http


# class WoodlandAttendanceExtend(http.Controller):
#     @http.route('/woodland_attendance_extend/woodland_attendance_extend', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/woodland_attendance_extend/woodland_attendance_extend/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('woodland_attendance_extend.listing', {
#             'root': '/woodland_attendance_extend/woodland_attendance_extend',
#             'objects': http.request.env['woodland_attendance_extend.woodland_attendance_extend'].search([]),
#         })

#     @http.route('/woodland_attendance_extend/woodland_attendance_extend/objects/<model("woodland_attendance_extend.woodland_attendance_extend"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('woodland_attendance_extend.object', {
#             'object': obj
#         })

