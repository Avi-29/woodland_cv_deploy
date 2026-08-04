from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    zk_badge_no = fields.Char(
        string='ZKTeco Badge No (PIN)',
        index=True,
        copy=False,
        help='The numeric User ID / PIN enrolled on the ZKTeco device. '
             'Used to link attendance punches to this employee.',
    )
    zk_card_no = fields.Char(
        string='ZKTeco Card Number',
        index=True,
        copy=False,
        help='RFID / card number registered on the device.',
    )
    zk_enrolled_user_id = fields.Many2one(
        'zk.enrolled.user',
        string='ZKTeco Enrollment',
        compute='_compute_zk_enrolled_user',
        store=False,
        help='Linked enrollment record (computed from badge no).',
    )
    zk_attendance_count = fields.Integer(
        string='ZK Punches',
        compute='_compute_zk_attendance_count',
    )

    _sql_constraints = [
        ('zk_badge_no_uniq', 'unique(zk_badge_no)',
         'ZKTeco Badge No must be unique per employee!'),
    ]


    def action_archive_and_remove_from_devices(self):
        Device = self.env['zk.device']
        Cmd = self.env['zk.device.command']
        Enrolled = self.env['zk.enrolled.user']

        for emp in self:

            if emp.zk_badge_no:
            # 1️⃣ Find enrolled user
                user = Enrolled.search([
                    ('pin', '=', emp.zk_badge_no)
                ], limit=1)
                if user:
                # 2️⃣ Get all devices
                    devices = Device.search([('state', '=', 'online')])

                    # 3️⃣ Send DELETE command
                    for device in devices:
                        Cmd.create({
                            'device_id': device.id,
                            'command_type': 'delete_user',
                            'command_string': user.build_delete_cmd(),  # you must implement
                            'note': f'Delete user PIN={user.pin}',
                        })
                    user.unlink()
            # 4️⃣ Archive employee
            emp.active = False

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_remove_from_devices(self):
        Device = self.env['zk.device']
        Cmd = self.env['zk.device.command']
        Enrolled = self.env['zk.enrolled.user']

        for emp in self:
            if not emp.zk_badge_no:
                continue

            user = Enrolled.search([
                ('pin', '=', emp.zk_badge_no)
            ], limit=1)

            if not user:
                continue

            devices = Device.search([('state', '=', 'online')])

            for device in devices:
                Cmd.create({
                    'device_id': device.id,
                    'command_type': 'delete_user',
                    'command_string': user.build_delete_cmd(),
                    'note': f'Delete user PIN={user.pin}',
                })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': f'User removal command sent to all device(s).',
                'type': 'success',
                'sticky': False,
            }
        }

    def _compute_zk_enrolled_user(self):
        EnrolledUser = self.env['zk.enrolled.user']
        for emp in self:
            if emp.zk_badge_no:
                emp.zk_enrolled_user_id = EnrolledUser.search(
                    [('pin', '=', emp.zk_badge_no)], limit=1
                )
            else:
                emp.zk_enrolled_user_id = False

    def _compute_zk_attendance_count(self):
        AttLog = self.env['zk.attendance.log']
        for emp in self:
            if emp.zk_badge_no:
                emp.zk_attendance_count = AttLog.search_count(
                    [('pin', '=', emp.zk_badge_no)]
                )
            else:
                emp.zk_attendance_count = 0

    def action_view_zk_attendance(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'ZK Attendance — {self.name}',
            'res_model': 'zk.attendance.log',
            'view_mode': 'list,form',
            'domain': [('pin', '=', self.zk_badge_no)],
            'context': {},
        }

    def action_sync_to_devices(self):
        self.ensure_one()

        Enrolled = self.env['zk.enrolled.user']
        Device = self.env['zk.device']  # adjust if your model name differs

        if not self.zk_badge_no:
            raise UserError(_("Employee has no badge number."))

        # 1️⃣ Find or Create Enrollment
        enrolled = Enrolled.search(
            [('pin', '=', self.zk_badge_no)], limit=1
        )

        if not enrolled:
            enrolled = Enrolled.create({
                'name': self.name,
                'pin': self.zk_badge_no,
                'employee_id': self.id,
            })

        # 2️⃣ Get Devices
        devices = Device.search([('state', '=', 'online')])  # adjust domain

        if not devices:
            raise UserError(_("No connected devices found."))

        # 3️⃣ Send Command to Devices
        for device in devices:
            self.env['zk.device.command'].create({
                'device_id': device.id,
                'command_type': 'enroll_user',   # depends on your implementation
                'command_string': enrolled.build_userinfo_cmd(),
            })
        # 5️⃣ Notification
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('User synced to devices successfully.'),
                'type': 'success',
            },
        }

    @api.model
    def get_by_badge(self, pin: str):
        """Return employee record matching this ZKTeco PIN, or empty recordset."""
        return self.search([('zk_badge_no', '=', str(pin).strip())], limit=1)
