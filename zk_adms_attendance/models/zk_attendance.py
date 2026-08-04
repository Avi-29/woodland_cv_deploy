from odoo import models, fields, api, _
import logging
import pytz
from datetime import datetime,timedelta

_logger = logging.getLogger(__name__)

PUNCH_TYPES = [
    ('0',   'Check In'),
    ('1',   'Check Out'),
    ('2',   'Break Out'),
    ('3',   'Break In'),
    ('4',   'Overtime In'),
    ('5',   'Overtime Out'),
    ('255', 'Other'),
]

VERIFY_TYPES = [
    ('0',  'Password'),
    ('1',  'Fingerprint'),
    ('3',  'Card'),
    ('4',  'Card + Password'),
    ('10', 'Palm'),
    ('15', 'Face'),
]

VALID_PUNCH  = {v[0] for v in PUNCH_TYPES}
VALID_VERIFY = {v[0] for v in VERIFY_TYPES}


class ZkAttendanceLog(models.Model):
    _name = 'zk.attendance.log'
    _description = 'ZKTeco Raw Attendance Log'
    _order = 'punch_time desc'

    device_id = fields.Many2one(
        'zk.device', string='Device',
        ondelete='set null', index=True,
    )
    device_serial = fields.Char(
        string='Device Serial', index=True,
        help='Stored directly in case device record is deleted',
    )

    # ── Employee link via hr.employee.zk_badge_no ───────────────────────────
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        ondelete='set null', index=True,
    )
    pin = fields.Char(string='PIN / Badge No', index=True)
    employee_name = fields.Char(
        string='Employee Name',
        compute='_compute_employee_name', store=True,
    )
    department_id=fields.Many2one(related='employee_id.department_id')

    punch_time  = fields.Datetime(string='Punch Time', required=True, index=True)
    punch_type  = fields.Selection(PUNCH_TYPES,  string='Punch Type',   default='0')
    verify_type = fields.Selection(VERIFY_TYPES, string='Verify Method', default='1')
    processed_punch_type = fields.Selection([
        ('check_in', 'Check In'),
        ('check_out', 'Check Out'),
        ('break_out', 'Break Out'),
        ('break_in', 'Break In'),
    ],string="Processed With")

    work_code = fields.Char(string='Work Code')
    reserved  = fields.Char(string='Reserved')
    raw_line  = fields.Text(string='Raw Line', help='Original tab-separated line from device')

    state = fields.Selection([
        ('new',       'New'),
        ('processed', 'Processed'),
        ('ignored',   'Ignored (no employee)'),
        ('error',     'Error'),
    ], default='new', index=True)
    error_msg = fields.Char(string='Error')


    _sql_constraints = [
        ('unique_punch',
         'unique(device_serial, pin, punch_time)',
         'Duplicate punch (same device + PIN + time) already exists!'),
    ]

    @api.depends('employee_id', 'pin')
    def _compute_employee_name(self):
        for rec in self:
            if rec.employee_id:
                rec.employee_name = rec.employee_id.name
            else:
                rec.employee_name = rec.pin or ''

    # ── Main entry point called by controller ───────────────────────────────

    @api.model
    def create_from_adms(self, device, records):
        """
        Persist attendance records pushed from a ZKTeco device via ATTLOG.

        Each item in `records` is a dict with keys:
            pin, time, status, verify, workcode, reserved, _raw
        Returns the count of newly created records.
        """
        created = 0
        EmployeeModel = self.env['hr.employee']
        device_tz = pytz.timezone(device.timezone or 'Asia/Dhaka')

        for rec in records:
            pin            = str(rec.get('pin',      '')).strip()
            punch_time_str = str(rec.get('time',     '')).strip()
            punch_type_raw = str(rec.get('status',   '0')).strip()
            verify_raw     = str(rec.get('verify',   '1')).strip()
            work_code      = rec.get('workcode', '')
            reserved       = rec.get('reserved', '')
            raw            = rec.get('_raw',     '')

            if not pin or not punch_time_str:
                continue

            # ── Parse device-local datetime → UTC ──────────────────────────
            try:
                naive_dt = datetime.strptime(punch_time_str, '%Y-%m-%d %H:%M:%S')
                local_dt = device_tz.localize(naive_dt)
                utc_dt   = local_dt.astimezone(pytz.utc).replace(tzinfo=None)
            except Exception as e:
                _logger.warning('ZK ADMS: bad punch_time "%s" from %s: %s',
                                punch_time_str, device.serial_number, e)
                continue

            # ── Normalise selection values ──────────────────────────────────
            punch_type  = punch_type_raw  if punch_type_raw  in VALID_PUNCH  else '255'
            verify_type = verify_raw      if verify_raw      in VALID_VERIFY else '1'

            # ── Look up employee by ZK badge no ────────────────────────────
            employee = EmployeeModel.get_by_badge(pin)

            # ── Duplicate guard ────────────────────────────────────────────
            last = self.search(
                [('pin', '=', pin)],
                order='punch_time desc',
                limit=1
            )

            if last and last.punch_time:
                if utc_dt - last.punch_time < timedelta(minutes=15):
                    continue

            try:
                self.create({
                    'device_id':    device.id,
                    'device_serial': device.serial_number,
                    'employee_id':  employee.id if employee else False,
                    'pin':          pin,
                    'punch_time':   utc_dt,
                    'punch_type':   punch_type,
                    'verify_type':  verify_type,
                    'work_code':    work_code,
                    'reserved':     reserved,
                    'raw_line':     raw,
                    'state':        'new' if employee else 'ignored',
                })
                created += 1
            except Exception as e:
                _logger.error('ZK ADMS: failed to save punch PIN=%s: %s', pin, e)

        _logger.info('ZK ADMS: saved %d new punches from device %s',
                     created, device.serial_number)
        return created

    def action_reprocess(self):
        self.write({'state': 'new', 'error_msg': False})

    def action_link_employee(self):
        """Try to re-link all Ignored records to employees by PIN."""
        EmployeeModel = self.env['hr.employee']
        relinked = 0
        for rec in self.filtered(lambda r: r.state == 'ignored' and r.pin):
            emp = EmployeeModel.get_by_badge(rec.pin)
            if emp:
                rec.write({'employee_id': emp.id, 'state': 'new'})
                relinked += 1
        return relinked
