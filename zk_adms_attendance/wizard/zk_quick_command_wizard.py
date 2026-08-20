from odoo import models, fields, api, _
from odoo.exceptions import UserError
from datetime import datetime


# All supported ADMS commands with their template strings and descriptions
COMMAND_CATALOGUE = {
    # ── User management ──────────────────────────────────────────────────────
    'enroll_user': {
        'label':    'Enroll / Update User',
        'template': 'DATA UPDATE USERINFO PIN={pin}\tName={name}\tPrivilege=0\tPassword=\tCard=\tGroup=1\tTimeZone=0\tVerifyStyle=31',
        'params':   ['pin', 'name'],
        'help':     'Push a user record to the device. VerifyStyle 31 = all methods allowed.',
    },
    'delete_user': {
        'label':    'Delete User',
        'template': 'DATA DELETE USERINFO PIN={pin}',
        'params':   ['pin'],
        'help':     'Remove a user and all their templates from the device.',
    },
    'get_all_userinfo': {
        'label':    'Get All User Info',
        'template': 'DATA QUERY USERINFO',
        'params':   [],
        'help':     'Device will push all enrolled user records via OPERLOG.',
    },
    # ── Fingerprint ──────────────────────────────────────────────────────────
    'enroll_fp': {
        'label':    'Push Fingerprint Template',
        'template': 'DATA UPDATE FP PIN={pin}\tFINGERID={finger_id}\tValid=1\tTMP={template}\tSize=0',
        'params':   ['pin', 'finger_id', 'template'],
        'help':     'Push a base64 fingerprint template to the device. finger_id = 0-9.',
    },
    'delete_fp': {
        'label':    'Delete Fingerprint',
        'template': 'DATA DELETE FP PIN={pin}\tFINGERID={finger_id}',
        'params':   ['pin', 'finger_id'],
        'help':     'Delete a specific finger template from the device.',
    },
    'get_all_fp': {
        'label':    'Get All Fingerprints',
        'template': 'DATA QUERY FP',
        'params':   [],
        'help':     'Device will push all fingerprint templates via OPERLOG.',
    },
    # ── Face ─────────────────────────────────────────────────────────────────
    'enroll_face': {
        'label':    'Push Face Template',
        'template': 'DATA UPDATE BIODATA Pin={pin}\tNo=0\tValid=1\tTmp={template}\tSize=0',
        'params':   ['pin', 'template'],
        'help':     'Push a base64 face template to the device.',
    },
    'delete_face': {
        'label':    'Delete Face Template',
        'template': 'DATA DELETE FACE PIN={pin}\tFACEID=0',
        'params':   ['pin'],
        'help':     'Remove the face template for a user.',
    },
    # ── Photo ────────────────────────────────────────────────────────────────
    'enroll_userpic': {
        'label':    'Push User Photo',
        'template': 'DATA UPDATE USERPIC PIN={pin}\tFileName={pin}.jpg\tSize={size}\tContent={content}',
        'params':   ['pin', 'size', 'content'],
        'help':     'Push a base64 JPEG photo to the device for a given PIN — same picture on every device.',
    },
    # ── Attendance ───────────────────────────────────────────────────────────
    'get_attlog': {
        'label':    'Get Attendance Log',
        'template': 'DATA QUERY ATTLOG',
        'params':   [],
        'help':     'Device pushes all stored attendance logs.',
    },
    'get_new_attlog': {
        'label':    'Get New Attendance Log',
        'template': 'DATA QUERY ATTLOG StartTime={start_time}\tEndTime={end_time}',
        'params':   ['start_time', 'end_time'],
        'help':     'Get logs between dates. Format: 2024-01-01 00:00:00',
    },
    'clear_attlog': {
        'label':    'Clear Attendance Log',
        'template': 'DATA CLEAR ATTLOG',
        'params':   [],
        'help':     'WARNING: permanently deletes all attendance records from device memory.',
    },
    # ── Device control ───────────────────────────────────────────────────────
    'reboot': {
        'label':    'Reboot Device',
        'template': 'CONTROL DEVICE 0 0 1 0 0',
        'params':   [],
        'help':     'Immediately reboot the device.',
    },
    'enable_device': {
        'label':    'Enable Device (unlock)',
        'template': 'CONTROL DEVICE 1',
        'params':   [],
        'help':     'Re-enable a device that was disabled.',
    },
    'disable_device': {
        'label':    'Disable Device (lock)',
        'template': 'CONTROL DEVICE 0',
        'params':   [],
        'help':     'Lock the device — it will not allow any punches until re-enabled.',
    },
    'clear_admin': {
        'label':    'Clear Admin Privileges',
        'template': 'DATA CLEAR ADMIN',
        'params':   [],
        'help':     'Removes all admin-level users from the device.',
    },
    'reset_device': {
        'label':    'Factory Reset',
        'template': 'DATA CLEAR ALL',
        'params':   [],
        'help':     'WARNING: wipes all users, templates, and logs from device.',
    },
    # ── Time ─────────────────────────────────────────────────────────────────
    'set_time': {
        'label':    'Sync Device Time',
        'template': 'DATE {datetime}',
        'params':   ['datetime'],
        'help':     'Set device clock. Format: YYYY-MM-DD HH:MM:SS  (use server time).',
    },
    # ── Door relay ───────────────────────────────────────────────────────────
    'open_door': {
        'label':    'Open Door (relay)',
        'template': 'CONTROL DEVICE 2 0 {duration} 0 0',
        'params':   ['duration'],
        'help':     'Trigger door relay. duration = seconds to keep open (e.g. 5).',
    },
    # ── LCD message ──────────────────────────────────────────────────────────
    'write_lcd': {
        'label':    'Write LCD Message',
        'template': 'MESSAGE WRITECARD {message}',
        'params':   ['message'],
        'help':     'Display a short message on the device screen.',
    },
    # ── Custom ───────────────────────────────────────────────────────────────
    'custom': {
        'label':    'Custom Command',
        'template': '',
        'params':   [],
        'help':     'Enter any raw ADMS command string manually.',
    },
}

COMMAND_SELECTION = [(k, v['label']) for k, v in COMMAND_CATALOGUE.items()]


class ZkQuickCommandWizard(models.TransientModel):
    _name = 'zk.quick.command.wizard'
    _description = 'Send ADMS Command to Device(s)'

    device_ids = fields.Many2many(
        'zk.device', string='Target Devices', required=True,
    )
    command_type = fields.Selection(
        COMMAND_SELECTION, string='Command', required=True, default='get_all_userinfo',
    )
    command_help = fields.Char(
        string='Description', compute='_compute_help', store=False,
    )
    command_template = fields.Char(
        string='Template', compute='_compute_help', store=False,
    )

    # Parameter fields
    param_pin        = fields.Char(string='PIN / User ID')
    param_name       = fields.Char(string='User Name')
    param_finger_id  = fields.Selection(
        [(str(i), f'Finger {i}') for i in range(10)], string='Finger ID', default='0',
    )
    param_template   = fields.Text(string='Biometric Template (base64)')
    param_photo_content = fields.Text(string='Photo (base64 JPEG)')
    param_start_time = fields.Datetime(string='Start Time')
    param_end_time   = fields.Datetime(string='End Time')
    param_duration   = fields.Integer(string='Duration (seconds)', default=5)
    param_message    = fields.Char(string='LCD Message')
    param_datetime   = fields.Datetime(string='Date/Time', default=fields.Datetime.now)
    custom_cmd       = fields.Text(string='Custom Command String')

    note = fields.Char(string='Note / Label')

    # Results
    state          = fields.Selection([('draft','Configure'),('done','Done')], default='draft')
    queued_count   = fields.Integer(readonly=True)
    result_summary = fields.Text(readonly=True)

    @api.depends('command_type')
    def _compute_help(self):
        for rec in self:
            info = COMMAND_CATALOGUE.get(rec.command_type, {})
            rec.command_help     = info.get('help', '')
            rec.command_template = info.get('template', '')

    def _build_command_string(self):
        self.ensure_one()
        info = COMMAND_CATALOGUE.get(self.command_type, {})
        if self.command_type == 'custom':
            if not self.custom_cmd:
                raise UserError(_('Please enter a custom command string.'))
            return self.custom_cmd.strip()

        template = info.get('template', '')
        params   = info.get('params', [])

        subs = {}
        if 'pin'        in params: subs['pin']        = self.param_pin or ''
        if 'name'       in params: subs['name']       = self.param_name or ''
        if 'finger_id'  in params: subs['finger_id']  = self.param_finger_id or '0'
        if 'template'   in params: subs['template']   = self.param_template or ''
        if 'content'    in params: subs['content']    = self.param_photo_content or ''
        if 'size'       in params: subs['size']       = str(len(self.param_photo_content or ''))
        if 'duration'   in params: subs['duration']   = str(self.param_duration or 5)
        if 'message'    in params: subs['message']    = self.param_message or ''
        if 'datetime'   in params:
            dt = self.param_datetime or fields.Datetime.now()
            subs['datetime'] = dt.strftime('%Y-%m-%d %H:%M:%S')
        if 'start_time' in params:
            st = self.param_start_time or fields.Datetime.now()
            subs['start_time'] = st.strftime('%Y-%m-%d %H:%M:%S')
        if 'end_time'   in params:
            et = self.param_end_time or fields.Datetime.now()
            subs['end_time'] = et.strftime('%Y-%m-%d %H:%M:%S')

        try:
            return template.format(**subs)
        except KeyError as e:
            raise UserError(_(f'Missing parameter: {e}'))

    def action_send(self):
        self.ensure_one()
        cmd_str = self._build_command_string()
        CmdModel = self.env['zk.device.command']
        note = self.note or COMMAND_CATALOGUE.get(self.command_type, {}).get('label', '')
        queued = 0

        for device in self.device_ids:
            CmdModel.create({
                'device_id':      device.id,
                'command_type':   self.command_type,
                'command_string': cmd_str,
                'note':           note,
            })
            queued += 1

        self.write({
            'state':          'done',
            'queued_count':   queued,
            'result_summary': (
                f'Command "{note}" queued for {queued} device(s).\n\n'
                f'Command string:\n{cmd_str}\n\n'
                f'It will be sent on the next heartbeat poll (~{self.device_ids[:1].heartbeat_interval}s).'
                if self.device_ids else ''
            ),
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
