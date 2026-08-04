from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import csv
import io
from datetime import datetime


class ZkAttendanceExportWizard(models.TransientModel):
    _name = 'zk.attendance.export.wizard'
    _description = 'Export ZKTeco Attendance Logs'

    date_from = fields.Date(
        string='From',
        required=True,
        default=lambda self: fields.Date.today().replace(day=1),
    )
    date_to = fields.Date(
        string='To',
        required=True,
        default=fields.Date.today,
    )
    device_ids = fields.Many2many(
        'zk.device',
        string='Devices',
        help='Leave empty to export all devices',
    )
    punch_type = fields.Selection(
        [('all', 'All types'),
         ('0', 'Check In only'),
         ('1', 'Check Out only')],
        string='Punch Type',
        default='all',
    )
    state_filter = fields.Selection(
        [('all', 'All'),
         ('new', 'New'),
         ('processed', 'Processed'),
         ('ignored', 'Ignored (unmapped)')],
        string='Status',
        default='all',
    )
    file_format = fields.Selection(
        [('csv', 'CSV'), ('txt', 'TXT (tab-separated)')],
        string='Format',
        default='csv',
    )

    # Output fields
    export_file = fields.Binary(string='Download', readonly=True)
    export_filename = fields.Char(string='Filename', readonly=True)
    export_count = fields.Integer(string='Records exported', readonly=True)
    state = fields.Selection(
        [('draft', 'Configure'), ('done', 'Done')],
        default='draft',
    )

    def action_export(self):
        self.ensure_one()

        domain = [
            ('punch_time', '>=', datetime.combine(self.date_from, datetime.min.time())),
            ('punch_time', '<=', datetime.combine(self.date_to, datetime.max.time())),
        ]
        if self.device_ids:
            domain.append(('device_id', 'in', self.device_ids.ids))
        if self.punch_type != 'all':
            domain.append(('punch_type', '=', self.punch_type))
        if self.state_filter != 'all':
            domain.append(('state', '=', self.state_filter))

        records = self.env['zk.attendance.log'].search(domain, order='punch_time asc')

        if not records:
            raise UserError(_('No records found for the selected criteria.'))

        delimiter = ',' if self.file_format == 'csv' else '\t'
        ext = 'csv' if self.file_format == 'csv' else 'txt'

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter)

        # Header
        writer.writerow([
            'Device', 'Device Serial', 'PIN', 'Employee Name',
            'Punch Time (UTC)', 'Punch Type', 'Verify Method',
            'Work Code', 'Status',
        ])

        punch_labels = dict(self.env['zk.attendance.log']._fields['punch_type'].selection)
        verify_labels = dict(self.env['zk.attendance.log']._fields['verify_type'].selection)

        for r in records:
            writer.writerow([
                r.device_id.name if r.device_id else '',
                r.device_serial or '',
                r.pin or '',
                r.employee_name or '',
                r.punch_time.strftime('%Y-%m-%d %H:%M:%S') if r.punch_time else '',
                punch_labels.get(r.punch_type, r.punch_type),
                verify_labels.get(r.verify_type, r.verify_type),
                r.work_code or '',
                r.state,
            ])

        csv_bytes = buf.getvalue().encode('utf-8-sig')  # BOM for Excel compatibility
        filename = (
            f'zk_attendance_'
            f'{self.date_from.strftime("%Y%m%d")}_'
            f'{self.date_to.strftime("%Y%m%d")}.{ext}'
        )

        self.write({
            'export_file': base64.b64encode(csv_bytes),
            'export_filename': filename,
            'export_count': len(records),
            'state': 'done',
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
