# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
import pytz
from datetime import datetime,time


class HrSwapDay(models.Model):
    """
    hr.swap  –  Employee Swap Day

    Four swap types:
        weekend      – work on weekly day-off  → get another day off
        holiday      – work on public holiday  → get another day off
        extra_shift  – worked 2 shifts in one day → pick the 2nd attendance
                       → get an ADJUST day (auto-approved)
        overtime     – single attendance with worked_hours ≥ 8
                       → get an ADJUST day (auto-approved)
    """
    _name = 'hr.swap'
    _description = 'Employee Swap Day'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'swap_work_date desc, extra_work_date desc'

    # ── Identity ──────────────────────────────────────────────────────────────
    name = fields.Char(
        string='Reference',
        readonly=True,
        default=lambda self: _('New'),
        copy=False,
    )
    employee_id = fields.Many2one(
        'hr.employee', string='Employee',
        required=True, tracking=True,
        default=lambda self: self.env['hr.employee'].search(
            [('user_id', '=', self.env.uid)], limit=1),
    )
    zk_badge_no = fields.Char(related='employee_id.zk_badge_no', store=True)
    department_id = fields.Many2one(
        related='employee_id.department_id',
        string='Department', store=True, readonly=True,
    )
    manager_id = fields.Many2one(
        related='employee_id.parent_id',
        string='Manager', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='employee_id.company_id',
        store=True, readonly=True,
    )

    # ── Swap type ─────────────────────────────────────────────────────────────
    swap_work_type = fields.Selection([
        ('weekend',     'Weekly Day Off'),
        ('holiday',     'Public Holiday'),
        ('extra_shift', 'Extra Shift Work'),
        ('overtime',    'Overtime Shift'),
    ], string='Swap Type', required=True, default='weekend', tracking=True,
    )

    # ── Weekend / Holiday fields (existing) ───────────────────────────────────
    swap_work_date = fields.Date(
        string='Will Work On',
        tracking=True,
        help='The weekend or public holiday on which the employee agrees to work.',
    )
    swap_work_type_day = fields.Selection([
        ('weekend', 'Weekly Day Off'),
        ('holiday', 'Public Holiday'),
    ], string='Type of Worked Day', tracking=True,
    )
    public_holiday_id = fields.Many2one(
        'resource.calendar.leaves',
        string='Public Holiday',
        help='Select the public holiday being swapped (optional reference).',
    )

    # ── Extra Shift / Overtime fields (new) ───────────────────────────────────
    extra_work_date = fields.Date(
        string='Date Worked Extra',
        tracking=True,
        help='The date on which the employee worked an extra shift or overtime.',
    )

    # dynamic domain is set via attrs/domain in the view;
    # here we just declare the field with a safe default domain
    attendance_id = fields.Many2one(
        'hr.attendance',
        string='Attendance Record',
        tracking=True,
        copy=False,
        help='Select the attendance record that represents the extra/overtime work.',
        domain="[('employee_id', '=', employee_id), ('is_used_for_swap', '=', False)]",
    )

    # computed info shown on form
    attendance_worked_hours = fields.Float(
        string='Worked Hours',
        related='attendance_id.worked_hours',
        readonly=True,
    )

    # ── Shared ────────────────────────────────────────────────────────────────
    swap_off_date = fields.Date(
        string='ADJUST / Compensatory Off Day',
        required=True, tracking=True,
        help='The day the employee will take as compensatory off.',
    )
    reason = fields.Text(string='Reason', tracking=True)


    # ── Computed helpers ──────────────────────────────────────────────────────
    @property
    def _is_attendance_type(self):
        return self.swap_work_type in ('extra_shift')

    # ── Sequence ──────────────────────────────────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'hr.swap') or _('New')
        records = super().create(vals_list)
        for rec in records:
            if rec._is_attendance_type and rec.attendance_id:
                rec.attendance_id.sudo().write({
                    'is_used_for_swap': True,
                    'swap_id': rec.id,
                })
        return records

    def write(self, vals):
        # if attendance_id changes, release old one and lock new one
        old_att = {rec.id: rec.attendance_id for rec in self}
        result = super().write(vals)
        if 'attendance_id' in vals:
            for rec in self:
                prev = old_att.get(rec.id)
                if prev and prev != rec.attendance_id:
                    prev.sudo().write({'is_used_for_swap': False, 'swap_id': False})
                if rec.attendance_id and rec._is_attendance_type:
                    rec.attendance_id.sudo().write({
                        'is_used_for_swap': True,
                        'swap_id': rec.id,
                    })
        return result

    def unlink(self):
        # release attendance lock when swap is deleted
        for rec in self:
            if rec.attendance_id:
                rec.attendance_id.sudo().write({
                    'is_used_for_swap': False,
                    'swap_id': False,
                })
        return super().unlink()


    # ── Constraints ───────────────────────────────────────────────────────────
    @api.constrains('employee_id', 'swap_work_type', 'swap_work_date',
                    'extra_work_date', 'swap_off_date', 'attendance_id')
    def _check_swap(self):
        for rec in self:

            # ── weekend / holiday ─────────────────────────────────────────────
            if rec.swap_work_type in ('weekend', 'holiday'):
                if not rec.swap_work_date:
                    raise ValidationError(
                        _('Please set the date the employee will work.'))
                if rec.swap_work_date == rec.swap_off_date:
                    raise ValidationError(
                        _('The work date and the off date cannot be the same day.'))

                # Block if any attendance on that day already has approved OT
                utc_start, utc_end = _dhaka_day_to_utc_range(rec.swap_work_date)
                approved_att = self.env['hr.attendance'].search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('check_in', '>=', utc_start),
                    ('check_in', '<=', utc_end),
                    ('overtime_status', '=', 'approved'),
                ], limit=1)
                if approved_att:
                    raise ValidationError(
                        _('%(emp)s already has an approved overtime on %(date)s (%(att)s). '
                          'Cannot create a swap ADJUST for the same day.',
                          emp=rec.employee_id.name,
                          date=rec.swap_work_date,
                          att=approved_att.display_name))

                duplicate = self.search([
                    ('id', '!=', rec.id),
                    ('employee_id', '=', rec.employee_id.id),
                    ('swap_work_date', '=', rec.swap_work_date),
                ], limit=1)
                if duplicate:
                    raise ValidationError(
                        _('%(emp)s already has a swap for %(date)s (%(ref)s).',
                          emp=rec.employee_id.name,
                          date=rec.swap_work_date,
                          ref=duplicate.name))
                attendance = self.env['hr.attendance'].search([
                    ('employee_id', '=', rec.employee_id.id),
                    ('check_in', '>=', utc_start),
                    ('check_in', '<=', utc_end),
                ], limit=1)
                if not attendance:
                    raise ValidationError(
                        _('%(emp)s has no attendance record on %(date)s. '
                          'The employee must be present on the day being swapped.',
                          emp=rec.employee_id.name,
                          date=rec.swap_work_date))

            # ── extra_shift ───────────────────────────────────────────────────
            elif rec.swap_work_type == 'extra_shift':
                if not rec.extra_work_date:
                    raise ValidationError(
                        _('Please select the date the employee worked the extra shift.'))
                if not rec.attendance_id:
                    raise ValidationError(
                        _('Please select the attendance record for the extra shift.'))

                # must be 2+ attendance records on that date
                utc_start, utc_end = _dhaka_day_to_utc_range(rec.extra_work_date)
                count = self.env['hr.attendance'].search_count([
                    ('employee_id', '=', rec.employee_id.id),
                    ('check_in', '>=', utc_start),
                    ('check_in', '<=', utc_end),
                ])
                if count < 2:
                    raise ValidationError(
                        _('Extra Shift swap requires at least 2 attendance records '
                          'on %(date)s for %(emp)s. Only %(count)d found.',
                          date=rec.extra_work_date,
                          emp=rec.employee_id.name,
                          count=count))

                att = rec.attendance_id
                att_date = _to_dhaka(att.check_in).date() if att.check_in else None
                if att.employee_id.id != rec.employee_id.id:
                    raise ValidationError(
                        _('The selected attendance does not belong to %(emp)s.',
                          emp=rec.employee_id.name))
                if att_date != rec.extra_work_date:
                    raise ValidationError(
                        _('The selected attendance is not on %(date)s.',
                          date=rec.extra_work_date))

                dup = self.search([
                    ('id', '!=', rec.id),
                    ('attendance_id', '=', rec.attendance_id.id),
                ], limit=1)
                if dup:
                    raise ValidationError(
                        _('This attendance record is already linked to swap %(ref)s.',
                          ref=dup.name))

            # ── overtime ──────────────────────────────────────────────────────
            elif rec.swap_work_type == 'overtime':
                if not rec.attendance_id:
                    raise ValidationError(
                        _('Please select the attendance record for the overtime shift.'))

                att = rec.attendance_id
                if att.employee_id.id != rec.employee_id.id:
                    raise ValidationError(
                        _('The selected attendance does not belong to %(emp)s.',
                          emp=rec.employee_id.name))

                # Zero out OT and mark approved — employee takes ADJUST instead
                att.sudo().write({
                    'overtime_hours': 0,
                    'hours_to_approve': 0,
                    'overtime_status': 'approved',
                    'validated_overtime_hours': 0,
                })

                dup = self.search([
                    ('id', '!=', rec.id),
                    ('attendance_id', '=', rec.attendance_id.id),
                ], limit=1)
                if dup:
                    raise ValidationError(
                        _('This attendance record is already linked to swap %(ref)s.',
                          ref=dup.name))


DHAKA_TZ = pytz.timezone('Asia/Dhaka')


def _to_dhaka(dt):
    """Convert a UTC-naive datetime (as stored by Odoo) to Asia/Dhaka."""
    if not dt:
        return None
    return pytz.utc.localize(dt).astimezone(DHAKA_TZ)


def _dhaka_day_to_utc_range(local_date):
    """
    Return (utc_start, utc_end) covering the full Asia/Dhaka calendar day
    for *local_date* so attendance search_count uses correct UTC boundaries.

    Asia/Dhaka = UTC+6, so:
        local 00:00  →  UTC 18:00 previous day
        local 23:59:59 →  UTC 17:59:59 same day
    """
    aware_start = DHAKA_TZ.localize(datetime.combine(local_date, time.min))
    aware_end = DHAKA_TZ.localize(datetime.combine(local_date, time.max))
    utc_start = aware_start.astimezone(pytz.utc).replace(tzinfo=None)
    utc_end = aware_end.astimezone(pytz.utc).replace(tzinfo=None)
    return utc_start, utc_end

class HrAttendance(models.Model):
    """Extends hr.attendance with swap tracking fields."""
    _inherit = 'hr.attendance'
    check_in_date = fields.Date(
        string='Check-in Date',
        compute='_compute_check_in_date',
        store=True,
        index=True,
        help='Local date of check-in in Asia/Dhaka timezone.',
    )
    is_used_for_swap = fields.Boolean(
        string='Used for Swap/Adjust',
        default=False,
        copy=False,
        tracking=True,
        help='True when this attendance record has been consumed by an '
             'hr.swap record (extra_shift or overtime type). '
             'Cannot be selected again until the swap is refused/deleted.',
    )
    swap_id = fields.Many2one(
        'hr.swap',
        string='Swap Reference',
        readonly=True,
        copy=False,
        ondelete='set null',
        help='The swap record that consumed this attendance.',
    )

    @api.depends('check_in')
    def _compute_check_in_date(self):
        for rec in self:
            local = _to_dhaka(rec.check_in)  # UTC → Asia/Dhaka
            rec.check_in_date = local.date() if local else False

    # ── Display name: "17 May (8am-12pm)" ────────────────────────────────────
    @api.depends('check_in', 'check_out')
    def _compute_display_name(self):

        def fmt_hour(dt):
            """Format a UTC-naive datetime as 12-hour Dhaka local time."""
            local = _to_dhaka(dt)
            if not local:
                return '—'
            h, m = local.hour, local.minute
            period = 'am' if h < 12 else 'pm'
            h12 = h % 12 or 12
            if m:
                return f"{h12}:{m:02d}{period}"
            return f"{h12}{period}"

        for rec in self:
            if rec.check_in:
                local_ci = _to_dhaka(rec.check_in)
                date_str = local_ci.strftime('%-d %b')  # "17 May"
                ci_str = fmt_hour(rec.check_in)
                co_str = fmt_hour(rec.check_out) if rec.check_out else 'ongoing'
                rec.display_name = f"{date_str} ({ci_str}-{co_str})"
            else:
                rec.display_name = f"Attendance #{rec.id}"