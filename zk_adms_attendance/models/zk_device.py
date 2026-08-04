from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class ZkDevice(models.Model):
    _name = 'zk.device'
    _description = 'ZKTeco Device'
    _order = 'name'

    name = fields.Char(string='Device Name', required=True)
    serial_number = fields.Char(string='Serial Number', required=True, index=True)
    ip_address = fields.Char(string='IP Address')
    location = fields.Char(string='Location / Door')
    device_model = fields.Char(string='Device Model')
    firmware_version = fields.Char(string='Firmware Version')
    push_version = fields.Char(string='Push Version')
    timezone = fields.Char(string='Device Timezone', default='Asia/Dhaka')

    active = fields.Boolean(default=True)

    state = fields.Selection([
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('unknown', 'Unknown'),
    ], string='Status', default='unknown', readonly=True)

    last_activity = fields.Datetime(string='Last Activity', readonly=True)
    last_heartbeat = fields.Datetime(string='Last Heartbeat', readonly=True)

    attendance_count = fields.Integer(
        string='Attendance Records',
        compute='_compute_attendance_count',
    )

    # ADMS configuration returned to device
    server_tz_offset = fields.Integer(
        string='Server TZ Offset (minutes)',
        default=360,  # UTC+6 for Bangladesh
        help='Timezone offset in minutes sent to device in heartbeat response',
    )
    heartbeat_interval = fields.Integer(
        string='Heartbeat Interval (s)',
        default=30,
        help='How often the device should send heartbeats',
    )
    realtime = fields.Boolean(
        string='Real-time Push',
        default=True,
        help='If enabled, device pushes records immediately on punch',
    )

    _sql_constraints = [
        ('serial_uniq', 'unique(serial_number)', 'Serial number must be unique!'),
    ]

    def _compute_attendance_count(self):
        for rec in self:
            rec.attendance_count = self.env['zk.attendance.log'].search_count(
                [('device_id', '=', rec.id)]
            )

    def action_view_attendance(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Attendance — {self.name}',
            'res_model': 'zk.attendance.log',
            'view_mode': 'list,form',
            'domain': [('device_id', '=', self.id)],
            'context': {'default_device_id': self.id},
        }

    def mark_online(self, ip=None, firmware=None, push_version=None):
        """Called when device sends any valid request."""
        vals = {
            'state': 'online',
            'last_activity': fields.Datetime.now(),
        }
        if ip:
            vals['ip_address'] = ip
        if firmware:
            vals['firmware_version'] = firmware
        if push_version:
            vals['push_version'] = push_version
        self.write(vals)

    def mark_heartbeat(self):
        self.write({
            'state': 'online',
            'last_heartbeat': fields.Datetime.now(),
            'last_activity': fields.Datetime.now(),
        })

    @api.model
    def cron_check_offline(self):
        """Mark devices offline if no heartbeat for 3× heartbeat_interval."""
        from datetime import timedelta
        now = fields.Datetime.now()
        devices = self.search([('state', '=', 'online')])
        for device in devices:
            if not device.last_activity:
                continue
            threshold = timedelta(seconds=device.heartbeat_interval * 3)
            if (now - device.last_activity) > threshold:
                device.state = 'offline'
                _logger.info('Device %s marked offline (no heartbeat)', device.serial_number)

    @api.model
    def get_or_create_device(self, serial_number, name=None, ip=None, firmware=None, push_version=None):
        """Get existing device or create a new one from serial number."""
        device = self.search([('serial_number', '=', serial_number)], limit=1)
        if not device:
            device = self.create({
                'name': name or f'ZKTeco [{serial_number}]',
                'serial_number': serial_number,
                'ip_address': ip or '',
            })
            _logger.info('Auto-registered new ZKTeco device: %s', serial_number)
        device.mark_online(ip=ip, firmware=firmware, push_version=push_version)
        return device
