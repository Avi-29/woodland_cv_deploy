import re

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'
    _order = 'zk_badge_no_int asc'

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
    zk_departure_history_ids = fields.One2many(
        'employee.departure.history', 'employee_id',
        string='Departure History', readonly=True,
    )

    zk_badge_no_int = fields.Integer(
        string='ZK Badge No (numeric)',
        compute='_compute_zk_badge_no_int',
        store=True,
        index=True,
        help='Numeric mirror of zk_badge_no, kept in sync automatically - '
             'zk_badge_no is Char, so sorting/ordering on it (e.g. clicking '
             'a list column, or a view default_order) is lexicographic '
             '(1, 10, 11, 2, ...). Order by this field instead for real '
             'numeric order. Employees without a numeric badge sort last.',
    )

    _sql_constraints = [
        ('zk_badge_no_uniq', 'unique(zk_badge_no)',
         'ZKTeco Badge No must be unique per employee!'),
    ]

    @api.depends('zk_badge_no')
    def _compute_zk_badge_no_int(self):
        # The badge format isn't guaranteed to be a clean number - the
        # company can (and does) change convention over time, e.g.
        # "31", "F-31", "32F" - so pull out just the digits (wherever
        # they are) rather than requiring the whole value to be numeric.
        # "F-31" and "32F" then sort as 31 and 32, not as a tie.
        # No digits at all (blank, letters-only) falls back to the
        # sentinel so it always sorts last, and this never raises.
        for emp in self:
            digits = re.sub(r'\D', '', emp.zk_badge_no or '')
            try:
                emp.zk_badge_no_int = int(digits)
            except ValueError:
                emp.zk_badge_no_int = 999999999

    @api.constrains('zk_badge_no')
    def _check_zk_badge_no_unique(self):
        # Fires on both create() and write() whenever zk_badge_no changes.
        # The DB-level unique constraint (_sql_constraints above) can't
        # currently be relied on: it was silently never installed because
        # duplicate zk_badge_no rows already existed in the table when the
        # module last upgraded (Odoo logs a warning and moves on rather
        # than failing the upgrade). This check stops any *new* duplicates
        # going forward regardless of that. active_test=False so an
        # archived employee's badge can't be silently reused either -
        # duplicates must be resolved (re-badge one of them) rather than
        # just hidden by archiving.
        for emp in self:
            if not emp.zk_badge_no:
                continue
            duplicate = self.sudo().with_context(active_test=False).search([
                ('zk_badge_no', '=', emp.zk_badge_no),
                ('id', '!=', emp.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    'ZKTeco Badge No "%s" is already assigned to %s%s. '
                    'Badge numbers must be unique, even across archived employees.'
                ) % (
                    emp.zk_badge_no,
                    duplicate.name,
                    _(' (archived)') if not duplicate.active else '',
                ))

    def write(self, vals):
        res = super().write(vals)
        if 'image_1920' in vals:
            # Photo changed — forget which devices have the old picture so
            # the next sync (wizard or auto-resync) pushes the new one
            # everywhere instead of skipping devices that "already have a photo".
            users = self.env['zk.enrolled.user'].sudo().search([('employee_id', 'in', self.ids)])
            if users:
                users.write({'photo_synced_device_ids': [(5,)]})
        return res

    def action_remove_from_devices(self):
        Enrolled = self.env['zk.enrolled.user']

        for emp in self:
            if not emp.zk_badge_no:
                continue
            user = Enrolled.search([('pin', '=', emp.zk_badge_no)], limit=1)
            if user:
                user.action_queue_delete()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': 'User removal command sent to all device(s).',
                'type': 'success',
                'sticky': False,
            }
        }

    def _zk_process_departure(self, departure_reason_id=False, note=False):
        """
        Called from the departure wizard (see hr_departure_wizard.py) when
        an employee is archived: queue the device delete commands (same
        mechanism as action_remove_from_devices), deactivate the
        zk.enrolled.user record (and its fingerprints/faces) instead of
        deleting it — so re-activating the employee later can restore and
        re-sync it — and log the event to employee.departure.history.
        """
        Enrolled = self.env['zk.enrolled.user']
        History = self.env['employee.departure.history']

        for emp in self:
            if not emp.zk_badge_no:
                continue
            user = Enrolled.search([
                ('pin', '=', emp.zk_badge_no),
                ('active', '=', True),
            ], limit=1)
            if not user:
                continue

            queued = user.action_queue_delete()
            user.write({'active': False})
            user.fingerprint_ids.write({'active': False})
            user.face_ids.write({'active': False})

            History.create({
                'employee_id': emp.id,
                'enrolled_user_id': user.id,
                'badge_no': emp.zk_badge_no,
                'action': 'departed',
                'departure_reason_id': departure_reason_id or False,
                'note': note or False,
                'commands_queued': queued,
            })

    def action_unarchive_and_resync_devices(self):
        """
        Reactivate button: unarchive the employee, re-activate their
        zk.enrolled.user (+ fingerprints + faces), reset per-device
        enrollment tracking (the device actually deleted this data, so the
        next sync must push it fresh rather than skip it as "already
        there"), and queue a full resync (user info + fingerprints + faces
        + photo) to every device. Logs the event to employee.departure.history.
        """
        self.action_unarchive()

        Enrolled = self.env['zk.enrolled.user']
        Device = self.env['zk.device']
        History = self.env['employee.departure.history']
        devices = Device.search([])

        for emp in self:
            if not emp.zk_badge_no:
                continue
            user = Enrolled.with_context(active_test=False).search([
                ('pin', '=', emp.zk_badge_no),
            ], limit=1)
            if not user:
                continue

            user.write({'active': True})
            user.fingerprint_ids.with_context(active_test=False).write({'active': True})
            user.face_ids.with_context(active_test=False).write({'active': True})
            user.action_clear_device_enrollment()
            queued = user.enqueue_to_devices(
                target_device_ids=devices.ids,
                include_fp=True, include_face=True, include_photo=True,
            )

            History.create({
                'employee_id': emp.id,
                'enrolled_user_id': user.id,
                'badge_no': emp.zk_badge_no,
                'action': 'reactivated',
                'commands_queued': queued,
            })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Reactivated'),
                'message': _('Employee unarchived and resync queued to all devices.'),
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
