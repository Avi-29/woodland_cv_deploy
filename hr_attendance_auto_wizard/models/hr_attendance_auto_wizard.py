# -*- coding: utf-8 -*-
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DEFAULT_TZ = 'Asia/Dhaka'
DEFAULT_CHECK_IN_HOUR = 9   # 9:00 AM
DEFAULT_CHECK_OUT_HOUR = 18  # 6:00 PM


class HrAttendanceAutoWizard(models.TransientModel):
    _name = 'hr.attendance.auto.wizard'
    _description = 'Automatic Attendance Creation Wizard'

    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        help='Employee to generate attendance for.',
    )
    employee_day_off_day = fields.Selection(
        related='employee_id.day_off_day', string='Weekly Day Off', readonly=True,
    )
    date_from = fields.Date(string='Date From', required=True, default=fields.Date.context_today)
    date_to = fields.Date(string='Date To', required=True, default=fields.Date.context_today)
    line_ids = fields.One2many(
        'hr.attendance.auto.wizard.line', 'wizard_id', string='Days',
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('computed', 'Computed')],
        default='draft',
    )
    day_off_count = fields.Integer(string='Day-offs Skipped', readonly=True)
    existing_count = fields.Integer(string='Already Present', readonly=True)
    total_days = fields.Integer(compute='_compute_total_days', string='Total Days in Range')

    @api.depends('date_from', 'date_to')
    def _compute_total_days(self):
        for wizard in self:
            total = 0
            if wizard.date_from and wizard.date_to and wizard.date_to >= wizard.date_from:
                total = (wizard.date_to - wizard.date_from).days + 1
            wizard.total_days = total

    def _get_tz(self, employee):
        tz_name = employee.tz or DEFAULT_TZ
        try:
            return pytz.timezone(tz_name)
        except Exception:
            return pytz.timezone(DEFAULT_TZ)

    def _is_day_off(self, employee, date):
        """Return True if `date` falls on the employee's weekly day-off as of
        that date (hr.employee.get_day_off_day, '0' = Monday ... '6' = Sunday,
        matching date.weekday()), following history so a later change doesn't
        affect this date. If no day-off was set then, this always returns
        False -- no calendar fallback."""
        day_off = employee.get_day_off_day(date)
        return bool(day_off) and str(date.weekday()) == day_off

    def action_compute_days(self):
        self.ensure_one()
        self.line_ids.unlink()

        if not self.date_from or not self.date_to:
            raise UserError(_('Please set both Date From and Date To.'))
        if self.date_from > self.date_to:
            raise UserError(_('Date From must be on or before Date To.'))

        employee = self.employee_id
        local_tz = self._get_tz(employee)

        # Existing attendance dates (converted to employee's local date so we don't
        # accidentally recreate attendance that already exists for that local day).
        existing_atts = self.env['hr.attendance'].search([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', datetime.combine(self.date_from, time.min)),
            ('check_in', '<=', datetime.combine(self.date_to + timedelta(days=1), time.min)),
        ])
        existing_dates = set()
        for att in existing_atts:
            if not att.check_in:
                continue
            local_dt = pytz.utc.localize(att.check_in).astimezone(local_tz)
            existing_dates.add(local_dt.date())

        # Approved leaves overlapping the range.
        leaves = self.env['hr.leave'].search([
            ('employee_id', '=', employee.id),
            ('state', '=', 'validate'),
            ('date_from', '<=', datetime.combine(self.date_to, time.max)),
            ('date_to', '>=', datetime.combine(self.date_from, time.min)),
        ])
        leave_dates = set()
        for leave in leaves:
            d = leave.date_from.date()
            end = leave.date_to.date()
            while d <= end:
                leave_dates.add(d)
                d += timedelta(days=1)

        lines = []
        day_off_count = 0
        existing_count = 0
        current = self.date_from
        while current <= self.date_to:
            if self._is_day_off(employee, current):
                day_off_count += 1
                current += timedelta(days=1)
                continue
            if current in existing_dates:
                existing_count += 1
                current += timedelta(days=1)
                continue
            is_leave = current in leave_dates
            lines.append((0, 0, {
                'date': current,
                'is_leave': is_leave,
                # Absent (non-leave) working days are pre-checked so attendance
                # gets created for them by default; leave days start unchecked.
                'selected': not is_leave,
            }))
            current += timedelta(days=1)

        self.line_ids = lines
        self.day_off_count = day_off_count
        self.existing_count = existing_count
        self.state = 'computed'
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'hr.attendance.auto.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _get_day_hours(self, date, local_tz):
        """Return (check_in_local, check_out_local) tz-aware datetimes for a fixed
        default shift: 9:00 AM - 6:00 PM, in the employee's local timezone."""
        check_in_naive = datetime.combine(date, time.min) + timedelta(hours=DEFAULT_CHECK_IN_HOUR)
        check_out_naive = datetime.combine(date, time.min) + timedelta(hours=DEFAULT_CHECK_OUT_HOUR)
        check_in_local = local_tz.localize(check_in_naive)
        check_out_local = local_tz.localize(check_out_naive)
        return check_in_local, check_out_local

    def action_create_attendance(self):
        self.ensure_one()
        if self.state != 'computed':
            raise UserError(_('Please click "Compute Days" first.'))

        employee = self.employee_id
        local_tz = self._get_tz(employee)

        Attendance = self.env['hr.attendance']
        created = 0
        for line in self.line_ids.filtered('selected'):
            check_in_local, check_out_local = self._get_day_hours(line.date, local_tz)
            check_in_utc = check_in_local.astimezone(pytz.utc).replace(tzinfo=None)
            check_out_utc = check_out_local.astimezone(pytz.utc).replace(tzinfo=None)
            Attendance.create({
                'employee_id': employee.id,
                'check_in': check_in_utc,
                'check_out': check_out_utc,
            })
            created += 1

        message = _('%s attendance record(s) created for %s.') % (created, employee.name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Attendance Created'),
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }


class HrAttendanceAutoWizardLine(models.TransientModel):
    _name = 'hr.attendance.auto.wizard.line'
    _description = 'Automatic Attendance Wizard Line'
    _order = 'date'

    wizard_id = fields.Many2one('hr.attendance.auto.wizard', ondelete='cascade')
    date = fields.Date(string='Date')
    is_leave = fields.Boolean(string='On Approved Leave', readonly=True)
    selected = fields.Boolean(
        string='Create Attendance',
        help='Checked = attendance will be created for this day. '
             'Uncheck to treat it as absent and skip it.',
    )