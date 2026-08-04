"""
zk.enrolled.user — stores enrollment data pushed from devices
=============================================================
Sources that populate this model:
  1. OPERLOG  table  — USER / FP / FACE / DEL_USER / DEL_FP lines
  2. BIODATA  table  — biometric template push (Type=9 = face, others = FP)
  3. USERPIC  (OPERLOG) — photo push (stored as base64 on employee record)

Employee linkage: via hr.employee.zk_badge_no == pin

Device tracking
---------------
``enrolled_device_ids``  — devices that have received this user's basic USERINFO
``zk.enrolled.fp.enrolled_device_ids``  — devices that have this fingerprint slot
``zk.enrolled.face.enrolled_device_ids`` — devices that have this face template

These sets are maintained automatically during upsert.  The sync wizard and
``enqueue_to_devices`` check them to avoid creating duplicate commands.
"""
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)

PRIVILEGE = [
    ('0', 'Normal user'),
    ('1', 'Enroller'),
    ('14', 'Admin'),
    ('3', 'Super admin'),
]

FINGER_IDS = [(str(i), f'Finger {i}') for i in range(10)]
FACE_IDS   = [('0', 'Face 0'), ('1', 'Face 1')]


class ZkEnrolledUser(models.Model):
    _name = 'zk.enrolled.user'
    _description = 'ZKTeco Enrolled User'
    _order = 'pin'

    pin = fields.Char(string='PIN / Badge No', required=True, index=True)
    name = fields.Char(string='Name on Device')

    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        compute='_compute_employee_id', store=True,
        ondelete='set null',
        help='Linked via hr.employee.zk_badge_no = PIN',
    )

    privilege    = fields.Selection(PRIVILEGE, string='Privilege', default='0')
    password     = fields.Char(string='Password')
    card         = fields.Char(string='Card Number')
    group        = fields.Char(string='Group', default='1')
    time_zone    = fields.Char(string='TimeZone', default='0')
    verify_style = fields.Integer(
        string='VerifyStyle', default=31,
        help='Bitmask: 1=FP 2=Card 4=PWD 8=Face 16=Palm. 31=all',
    )

    # ── Device tracking ──────────────────────────────────────────────────────
    source_device_id = fields.Many2one(
        'zk.device', string='First Enrolled On', ondelete='set null',
        help='Device that first reported this user (kept for backwards compat).',
    )

    enrolled_device_ids = fields.Many2many(
        'zk.device',
        'zk_enrolled_user_device_rel',
        'user_id', 'device_id',
        string='Enrolled On Devices',
        help="All devices that currently hold this user's USERINFO record.",
    )

    enrolled_device_count = fields.Integer(
        compute='_compute_device_count', string='# Devices',
    )

    last_updated = fields.Datetime(string='Last Updated')

    fingerprint_ids = fields.One2many('zk.enrolled.fp',   'user_id', string='Fingerprints')
    face_ids        = fields.One2many('zk.enrolled.face', 'user_id', string='Face Templates')

    fp_count   = fields.Integer(compute='_compute_counts', string='FPs')
    face_count = fields.Integer(compute='_compute_counts', string='Faces')

    _sql_constraints = [
        ('pin_uniq', 'unique(pin)', 'PIN must be unique!'),
    ]

    @api.depends('pin')
    def _compute_employee_id(self):
        Employee = self.env['hr.employee']
        for rec in self:
            rec.employee_id = Employee.get_by_badge(rec.pin) if rec.pin else False

    def _compute_counts(self):
        for rec in self:
            rec.fp_count   = len(rec.fingerprint_ids)
            rec.face_count = len(rec.face_ids)

    def _compute_device_count(self):
        for rec in self:
            rec.enrolled_device_count = len(rec.enrolled_device_ids)

    # ── OPERLOG: USER line ───────────────────────────────────────────────────
    @api.model
    def upsert_from_operlog(self, source_device, fields_dict: dict):
        pin = str(fields_dict.get('PIN', '')).strip()
        if not pin:
            return None
        user = self.search([('pin', '=', pin)], limit=1)
        vals = {
            'pin':              pin,
            'name':             fields_dict.get('Name', ''),
            'privilege':        str(fields_dict.get('Pri', '0')),
            'password':         fields_dict.get('Passwd', ''),
            'card':             fields_dict.get('Card', ''),
            'group':            fields_dict.get('Grp', '1'),
            'time_zone':        fields_dict.get('TZ', '0'),
            'verify_style':     int(fields_dict.get('Verify', 31) or 31),
            'source_device_id': source_device.id,
            'last_updated':     fields.Datetime.now(),
        }
        if user:
            user.write(vals)
        else:
            user = self.create(vals)

        # Register this device in the enrolled set
        if source_device.id not in user.enrolled_device_ids.ids:
            user.enrolled_device_ids = [(4, source_device.id)]

        _logger.debug('ZK OPERLOG USER: upsert PIN=%s from %s', pin, source_device.serial_number)
        return user

    # ── BIODATA table handler ────────────────────────────────────────────────
    @api.model
    def upsert_from_biodata(self, source_device, fields_dict: dict):
        """
        Handle BIODATA table pushes.
        Type: 1=FP, 9=Face/3D-Face, 10=Palm, others=FP
        """
        pin      = str(fields_dict.get('Pin', '')).strip()
        bio_type = str(fields_dict.get('Type', '1')).strip()
        valid    = str(fields_dict.get('Valid', '1')) == '1'
        template = fields_dict.get('Tmp', '')
        index    = str(fields_dict.get('Index', '0')).strip()

        if not pin:
            return

        user = self.search([('pin', '=', pin)], limit=1)
        if not user:
            user = self.create({
                'pin':              pin,
                'name':             f'User {pin}',
                'source_device_id': source_device.id,
                'last_updated':     fields.Datetime.now(),
            })

        if source_device.id not in user.enrolled_device_ids.ids:
            user.enrolled_device_ids = [(4, source_device.id)]

        if bio_type in ('9',):
            face_id = index if index in ('0', '1') else '0'
            existing = self.env['zk.enrolled.face'].search(
                [('user_id', '=', user.id), ('face_id', '=', face_id)], limit=1
            )
            vals = {
                'user_id':          user.id,
                'face_id':          face_id,
                'valid':            valid,
                'template':         template,
                'source_device_id': source_device.id,
                'bio_type':         bio_type,
                'major_ver':        fields_dict.get('MajorVer', ''),
                'minor_ver':        fields_dict.get('MinorVer', ''),
            }
            if existing:
                existing.write(vals)
                if source_device.id not in existing.enrolled_device_ids.ids:
                    existing.enrolled_device_ids = [(4, source_device.id)]
            else:
                face = self.env['zk.enrolled.face'].create(vals)
                face.enrolled_device_ids = [(4, source_device.id)]
            _logger.debug('ZK BIODATA Face PIN=%s type=%s from %s', pin, bio_type, source_device.serial_number)
        else:
            finger_id = index if index in [str(i) for i in range(10)] else '0'
            existing = self.env['zk.enrolled.fp'].search(
                [('user_id', '=', user.id), ('finger_id', '=', finger_id)], limit=1
            )
            vals = {
                'user_id':          user.id,
                'finger_id':        finger_id,
                'valid':            valid,
                'template':         template,
                'source_device_id': source_device.id,
                'bio_type':         bio_type,
            }
            if existing:
                existing.write(vals)
                if source_device.id not in existing.enrolled_device_ids.ids:
                    existing.enrolled_device_ids = [(4, source_device.id)]
            else:
                fp = self.env['zk.enrolled.fp'].create(vals)
                fp.enrolled_device_ids = [(4, source_device.id)]
            _logger.debug('ZK BIODATA FP PIN=%s finger=%s from %s', pin, finger_id, source_device.serial_number)

        user.write({'last_updated': fields.Datetime.now()})

    # ── Command builders ─────────────────────────────────────────────────────
    def build_userinfo_cmd(self) -> str:
        self.ensure_one()
        parts = [
            f'PIN={self.pin}',
            f'Name={self.name or ""}',
            f'Privilege={self.privilege or "0"}',
            f'Password={self.password or ""}',
            f'Card={self.card or ""}',
            f'Group={self.group or "1"}',
            f'TimeZone={self.time_zone or "0"}',
            f'VerifyStyle={self.verify_style}',
        ]
        return 'DATA UPDATE USERINFO ' + '\t'.join(parts)

    def enqueue_to_devices(self, target_device_ids, force=False,
                           include_fp=True, include_face=True):
        """
        Queue sync commands only for biometric data that is MISSING on each device.

        :param target_device_ids: list of zk.device IDs to target
        :param force: bypass the already-enrolled check (full re-sync)
        :param include_fp: include fingerprint commands
        :param include_face: include face commands
        :return: number of commands queued
        """
        CmdModel = self.env['zk.device.command']
        devices  = self.env['zk.device'].browse(target_device_ids)
        queued   = 0

        for user in self:
            already_has_user = set(user.enrolled_device_ids.ids)

            for device in devices:
                # ── USERINFO ─────────────────────────────────────────────
                if force or device.id not in already_has_user:
                    CmdModel.create({
                        'device_id':      device.id,
                        'command_type':   'enroll_user',
                        'command_string': user.build_userinfo_cmd(),
                        'note':           f'Sync user PIN={user.pin} ({user.name})',
                    })
                    queued += 1
                    # Optimistically mark — ACK confirmation happens via devicecmd handler
                    user.enrolled_device_ids = [(4, device.id)]
                    already_has_user.add(device.id)

                # ── Fingerprints ──────────────────────────────────────────
                if include_fp:
                    for fp in user.fingerprint_ids.filtered('valid'):
                        if force or device.id not in fp.enrolled_device_ids.ids:
                            CmdModel.create({
                                'device_id':      device.id,
                                'command_type':   'enroll_fp',
                                'command_string': fp.build_fp_cmd(),
                                'note':           f'Sync FP PIN={user.pin} F{fp.finger_id}',
                            })
                            queued += 1
                            fp.enrolled_device_ids = [(4, device.id)]

                # ── Faces ─────────────────────────────────────────────────
                if include_face:
                    for face in user.face_ids.filtered('valid'):
                        if force or device.id not in face.enrolled_device_ids.ids:
                            CmdModel.create({
                                'device_id':      device.id,
                                'command_type':   'enroll_face',
                                'command_string': face.build_face_cmd(),
                                'note':           f'Sync Face PIN={user.pin}',
                            })
                            queued += 1
                            face.enrolled_device_ids = [(4, device.id)]

        return queued

    def action_clear_device_enrollment(self):
        """
        Reset per-device enrollment tracking for selected users.
        Use this after a device is factory-reset so the next sync pushes everything.
        """
        for user in self:
            user.enrolled_device_ids = [(5,)]
            for fp in user.fingerprint_ids:
                fp.enrolled_device_ids = [(5,)]
            for face in user.face_ids:
                face.enrolled_device_ids = [(5,)]
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Enrollment tracking cleared'),
                'message': _('Reset for %d user(s). Next sync will push all data.') % len(self),
                'type': 'warning',
            },
        }

    def build_delete_cmd(self):
        self.ensure_one()
        return f"DATA DELETE USERINFO PIN={self.pin}"


class ZkEnrolledFp(models.Model):
    _name = 'zk.enrolled.fp'
    _description = 'ZKTeco Enrolled Fingerprint'
    _order = 'user_id, finger_id'

    user_id   = fields.Many2one('zk.enrolled.user', required=True, ondelete='cascade', index=True)
    finger_id = fields.Selection(FINGER_IDS, string='Finger', required=True, default='0')
    valid     = fields.Boolean(default=True)
    template  = fields.Text(string='Template (base64)')
    bio_type  = fields.Char(string='Bio Type', default='1')
    source_device_id = fields.Many2one('zk.device', string='Captured On', ondelete='set null')

    enrolled_device_ids = fields.Many2many(
        'zk.device',
        'zk_enrolled_fp_device_rel',
        'fp_id', 'device_id',
        string='Synced To Devices',
        help='Devices that already have this fingerprint template.',
    )

    _sql_constraints = [
        ('user_finger_uniq', 'unique(user_id, finger_id)', 'Finger already enrolled for this user!'),
    ]

    def build_fp_cmd(self) -> str:
        self.ensure_one()
        parts = [
            f'Pin={self.user_id.pin}',
            f'FINGERID={self.finger_id}',
            f'Valid={1 if self.valid else 0}',
            f'Tmp={self.template or ""}',
            'Size=0',
        ]
        return 'DATA UPDATE FP ' + '\t'.join(parts)

    @api.model
    def upsert_from_operlog(self, source_device, user_record, fields_dict: dict):
        pin    = str(fields_dict.get('PIN', ''))
        finger = str(fields_dict.get('FINGERID', '0'))
        valid  = fields_dict.get('Valid', '1') == '1'
        tmpl   = fields_dict.get('TMP', '')
        existing = self.search([('user_id', '=', user_record.id), ('finger_id', '=', finger)], limit=1)
        vals = {
            'user_id':          user_record.id,
            'finger_id':        finger,
            'valid':            valid,
            'template':         tmpl,
            'source_device_id': source_device.id,
        }
        if existing:
            existing.write(vals)
            if source_device.id not in existing.enrolled_device_ids.ids:
                existing.enrolled_device_ids = [(4, source_device.id)]
        else:
            fp = self.create(vals)
            fp.enrolled_device_ids = [(4, source_device.id)]
        _logger.debug('ZK OPERLOG FP PIN=%s finger=%s from %s', pin, finger, source_device.serial_number)


class ZkEnrolledFace(models.Model):
    _name = 'zk.enrolled.face'
    _description = 'ZKTeco Enrolled Face Template'
    _order = 'user_id, face_id'

    user_id   = fields.Many2one('zk.enrolled.user', required=True, ondelete='cascade', index=True)
    face_id   = fields.Selection(FACE_IDS, string='Face ID', required=True, default='0')
    valid     = fields.Boolean(default=True)
    template  = fields.Text(string='Template (base64)')
    bio_type  = fields.Char(string='Bio Type', default='9')
    major_ver = fields.Char(string='Major Ver')
    minor_ver = fields.Char(string='Minor Ver')
    source_device_id = fields.Many2one('zk.device', string='Captured On', ondelete='set null')

    enrolled_device_ids = fields.Many2many(
        'zk.device',
        'zk_enrolled_face_device_rel',
        'face_id', 'device_id',
        string='Synced To Devices',
        help='Devices that already have this face template.',
    )

    _sql_constraints = [
        ('user_face_uniq', 'unique(user_id, face_id)', 'Face already enrolled for this user!'),
    ]

    def build_face_cmd(self) -> str:
        parts = [
            f'Pin={self.user_id.pin}',
            f'No={self.face_id or 0}',
            f'Index=0',
            f'Valid={1 if self.valid else 0}',
            f'Duress=0',
            f'Type=9',
            f'MajorVer=40',
            f'MinorVer=1',
            f'Format=0',
            f'Tmp={self.template or ""}',
        ]
        return 'DATA UPDATE BIODATA ' + '\t'.join(parts)

    @api.model
    def upsert_from_operlog(self, source_device, user_record, fields_dict: dict):
        pin     = str(fields_dict.get('PIN', ''))
        face_id = str(fields_dict.get('FACEID', '0'))
        valid   = fields_dict.get('Valid', '1') == '1'
        tmpl    = fields_dict.get('TMP', '')
        existing = self.search([('user_id', '=', user_record.id), ('face_id', '=', face_id)], limit=1)
        vals = {
            'user_id':          user_record.id,
            'face_id':          face_id,
            'valid':            valid,
            'template':         tmpl,
            'source_device_id': source_device.id,
        }
        if existing:
            existing.write(vals)
            if source_device.id not in existing.enrolled_device_ids.ids:
                existing.enrolled_device_ids = [(4, source_device.id)]
        else:
            face = self.create(vals)
            face.enrolled_device_ids = [(4, source_device.id)]
        _logger.debug('ZK OPERLOG Face PIN=%s from %s', pin, source_device.serial_number)

    def build_delete_cmd(self):
        self.ensure_one()
        return f"DATA DELETE USERINFO PIN={self.user_id.pin}"
