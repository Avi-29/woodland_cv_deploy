"""
zk.device.command  —  ADMS Command Queue
=========================================
ZKTeco devices poll GET /iclock/getrequest every heartbeat interval.
This model holds pending commands for each device. When the device polls,
the controller reads the oldest pending command, returns it as plain text,
and marks it 'sent'. The device executes it and ACKs via POST /iclock/devicecmd,
which marks the command 'done'.

ADMS Command format returned to device:
    C:ID:COMMAND_STRING

Example:
    C:1:DATA UPDATE USERINFO PIN=3\tName=Alice\tPrivilege=0\tPassword=\tCard=\tGroup=1\tTimeZone=0\tVerifyStyle=31

The device returns the result as:
    ID=1\tReturn=0\tCMD=DATA UPDATE USERINFO ...
"""

from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

# ── Command type catalogue ───────────────────────────────────────────────────
COMMAND_TYPES = [
    # User management
    ('enroll_user',        'Enroll / Update User'),
    ('delete_user',        'Delete User'),
    ('get_all_userinfo',   'Get All User Info'),
    # Fingerprint
    ('enroll_fp',          'Enroll Fingerprint'),
    ('delete_fp',          'Delete Fingerprint'),
    ('get_all_fp',         'Get All Fingerprints'),
    # Face
    ('enroll_face',        'Enroll Face'),
    ('delete_face',        'Delete Face'),
    # Card
    ('enroll_card',        'Enroll Card'),
    ('delete_card',        'Delete Card'),
    # Attendance
    ('get_attlog',         'Get Attendance Log'),
    ('clear_attlog',       'Clear Attendance Log'),
    ('get_new_attlog',     'Get New Attendance Log'),
    # Device control
    ('reboot',             'Reboot Device'),
    ('enable_device',      'Enable Device'),
    ('disable_device',     'Disable Device'),
    ('clear_admin',        'Clear Admin Privileges'),
    ('reset_device',       'Reset to Factory Default'),
    # Time
    ('set_time',           'Sync Device Time'),
    # Door relay
    ('open_door',          'Open Door (Relay)'),
    # Messaging
    ('write_lcd',          'Write LCD Message'),
    # Custom
    ('custom',             'Custom Command'),
]

STATE = [
    ('pending',   'Pending'),
    ('sent',      'Sent (awaiting ACK)'),
    ('done',      'Done'),
    ('failed',    'Failed'),
    ('cancelled', 'Cancelled'),
]


class ZkDeviceCommand(models.Model):
    _name = 'zk.device.command'
    _description = 'ZKTeco Device Command Queue'
    _order = 'id asc'

    device_id = fields.Many2one(
        'zk.device', string='Device',
        required=True, ondelete='cascade', index=True,
    )
    command_type = fields.Selection(COMMAND_TYPES, string='Command Type', required=True)
    command_string = fields.Text(
        string='ADMS Command String',
        required=True,
        help='Raw ADMS command body, e.g. DATA UPDATE USERINFO PIN=1\\tName=Alice',
    )
    priority = fields.Integer(string='Priority', default=5,
                              help='Lower = higher priority. Commands sorted by id (FIFO) within same priority.')
    state = fields.Selection(STATE, string='State', default='pending', index=True)

    cmd_id = fields.Integer(
        string='ADMS CMD ID',
        help='Sequential ID sent to the device as C:<id>:<cmd>. Set on dispatch.',
    )
    result_code = fields.Char(string='Result Code', readonly=True,
                              help='Return code sent back by device (0 = success)')
    result_raw = fields.Text(string='Raw ACK', readonly=True)
    error_msg = fields.Char(string='Error', readonly=True)

    created_by = fields.Many2one('res.users', string='Queued By',
                                 default=lambda self: self.env.user)
    create_date = fields.Datetime(string='Queued At', readonly=True)
    sent_at = fields.Datetime(string='Sent At', readonly=True)
    done_at = fields.Datetime(string='Done At', readonly=True)

    note = fields.Char(string='Note', help='Human-readable description of this command')

    def action_cancel(self):
        for rec in self:
            if rec.state in ('pending', 'sent'):
                rec.state = 'cancelled'

    def action_retry(self):
        for rec in self:
            if rec.state in ('failed', 'cancelled'):
                rec.write({'state': 'pending', 'error_msg': False, 'result_code': False})

    @api.model
    def next_for_device(self, device):
        """
        Return the next pending command for this device and mark it 'sent'.
        Returns a recordset of 1 or 0 records.
        """
        cmd = self.search([
            ('device_id', '=', device.id),
            ('state', '=', 'pending'),
        ], order='priority asc, id asc', limit=1)

        if not cmd:
            return cmd

        # Assign a sequential cmd_id scoped to this device
        max_id = self.search([('device_id', '=', device.id)], order='cmd_id desc', limit=1).cmd_id or 0
        cmd.write({
            'state': 'sent',
            'cmd_id': max_id + 1,
            'sent_at': fields.Datetime.now(),
        })
        return cmd

    @api.model
    def ack_from_device(self, device, raw_body: str):
        """
        Process a devicecmd ACK from the device.
        Body format (tab-separated): ID=<n>\tReturn=<code>\tCMD=<original>
        """
        params = {}
        for part in raw_body.split('\t'):
            if '=' in part:
                k, _, v = part.partition('=')
                params[k.strip()] = v.strip()

        cmd_id_str = params.get('ID', '')
        return_code = params.get('Return', '')

        if not cmd_id_str:
            _logger.warning('ZK devicecmd ACK: no ID field from %s', device.serial_number)
            return

        try:
            cmd_id = int(cmd_id_str)
        except ValueError:
            _logger.warning('ZK devicecmd ACK: non-integer ID=%s', cmd_id_str)
            return

        cmd = self.search([
            ('device_id', '=', device.id),
            ('cmd_id', '=', cmd_id),
            ('state', '=', 'sent'),
        ], limit=1)

        if not cmd:
            _logger.warning('ZK devicecmd ACK: no matching cmd_id=%s for device %s', cmd_id, device.serial_number)
            return

        success = (return_code == '0')
        cmd.write({
            'state': 'done' if success else 'failed',
            'result_code': return_code,
            'result_raw': raw_body[:500],
            'done_at': fields.Datetime.now(),
            'error_msg': None if success else f'Device returned code {return_code}',
        })
        _logger.info('ZK CMD %s for device %s: %s (code %s)',
                     cmd.command_type, device.serial_number,
                     'OK' if success else 'FAILED', return_code)
