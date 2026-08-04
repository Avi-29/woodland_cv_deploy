from odoo import models, fields,api,_
from datetime import datetime, timedelta, time
import pytz
from odoo.exceptions import ValidationError



class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    break_start = fields.Datetime(string="Break Start")
    break_end = fields.Datetime(string="Break End")
    is_late = fields.Boolean(string="Late", default=False)
    marked_as_day_off = fields.Boolean(string="Marked as Day-Off", default=False)
    shift_id = fields.Many2one('hr.shift', string="Shift", index=True)
    notes = fields.Text(string="Notes")
    hours_to_approve =fields.Float()
    zk_teco = fields.Char(related='employee_id.zk_badge_no')

    # worker_type still useful for filtering/grouping
    worker_type = fields.Selection(
        related='employee_id.worker_type',
        string='Worker Type',
        store=True,
        index=True,
    )

    @api.onchange('check_in', 'shift_id')
    def _onchange_shift_checkin(self):
        dhaka_tz = pytz.timezone('Asia/Dhaka')

        for rec in self:
            if not rec.check_in or not rec.shift_id:
                continue

            shift = rec.shift_id

            # Odoo stores datetime in UTC
            checkin_utc = fields.Datetime.to_datetime(rec.check_in)
            checkin_utc = pytz.UTC.localize(checkin_utc)

            # Convert to Dhaka timezone
            checkin_dhaka = checkin_utc.astimezone(dhaka_tz)

            start_hour = int(shift.start_time)
            start_minute = int(round((shift.start_time - start_hour) * 60))

            end_hour = int(shift.end_time)
            end_minute = int(round((shift.end_time - end_hour) * 60))

            # Build shift times in Dhaka timezone
            new_checkin_dhaka = checkin_dhaka.replace(
                hour=start_hour,
                minute=start_minute,
                second=0,
                microsecond=0
            )

            new_checkout_dhaka = checkin_dhaka.replace(
                hour=end_hour,
                minute=end_minute,
                second=0,
                microsecond=0
            )

            # Overnight shift
            if shift.end_time <= shift.start_time:
                new_checkout_dhaka += timedelta(days=1)

            # Convert back to UTC for storage
            rec.check_in = new_checkin_dhaka.astimezone(pytz.UTC).replace(tzinfo=None)
            rec.check_out = new_checkout_dhaka.astimezone(pytz.UTC).replace(tzinfo=None)

    @api.depends('check_in', 'check_out', 'shift_id')
    def _compute_worked_hours(self):
        for rec in self:
            if rec.check_in and rec.check_out:
                duration = (rec.check_out - rec.check_in).total_seconds() / 3600.0
                # Apply 1 hour deduction ONLY for General Shift
                if rec.shift_id and rec.shift_id.is_general == True:
                    if duration > 6:  # optional safety
                        duration -= 1

                rec.worked_hours = max(duration, 0)
            else:
                rec.worked_hours = 0

    @api.depends('worked_hours', 'employee_id', 'check_in')
    def _compute_overtime_hours(self):
        dhaka_tz = pytz.timezone('Asia/Dhaka')

        for rec in self:
            # ── gate: must be OT eligible ────────────────────────────────────
            if not rec.employee_id.is_ot_eligible:
                rec.overtime_hours = 0
                continue

            if not rec.check_in:
                rec.overtime_hours = 0
                continue

            # ── Dhaka-local attendance date ──────────────────────────────────
            attendance_date = (
                rec.check_in
                .replace(tzinfo=pytz.utc)
                .astimezone(dhaka_tz)
                .date()
            )

            # ── day-off / public holiday branch ─────────────────────────────
            if rec._is_day_off_or_holiday(attendance_date):
                rec.overtime_hours = rec.worked_hours
                rec.hours_to_approve = rec.worked_hours
                rec.overtime_status = 'to_approve'
                continue

            # ── normal threshold logic ───────────────────────────────────────
            if rec.employee_id.is_12_hour_shift:
                threshold = 12.75
                ideal_hours = 12
            else:
                threshold = 8.75
                ideal_hours = 8

            if (
                    rec.shift_id
                    and rec.shift_id.is_night
                    and rec.employee_id.department_id
                    and rec.employee_id.department_id.is_morning_shift
            ):
                threshold = 9.75

            if rec.worked_hours > threshold:
                rec.overtime_hours = rec.worked_hours - ideal_hours
                rec.hours_to_approve = rec.overtime_hours
                rec.overtime_status = 'to_approve'
            else:
                rec.overtime_hours = 0

    def _is_day_off_or_holiday(self, attendance_date):
        """
        Returns True if attendance_date (date, Dhaka-local) falls on:
          1. The employee's weekly day_off_day, OR
          2. A global public holiday in resource.calendar.leaves
        """
        self.ensure_one()

        # 1. Weekly day_off_day
        if (
                self.employee_id.day_off_day
                and str(attendance_date.weekday()) == self.employee_id.day_off_day
        ):
            return True

        # 2. Public holiday — convert Dhaka day boundaries back to UTC naive
        #    before querying (resource.calendar.leaves stores UTC)
        dhaka_tz = pytz.timezone('Asia/Dhaka')
        utc_start = (
            dhaka_tz
            .localize(datetime.combine(attendance_date, time.min))
            .astimezone(pytz.utc)
            .replace(tzinfo=None)
        )
        utc_end = (
            dhaka_tz
            .localize(datetime.combine(attendance_date, time.max))
            .astimezone(pytz.utc)
            .replace(tzinfo=None)
        )

        return bool(self.env['resource.calendar.leaves'].sudo().search([
            ('resource_id', '=', False),
            ('date_from', '<=', utc_end),
            ('date_to', '>=', utc_start),
        ], limit=1))

    def _update_overtime(self, attendance_domain=None):
        pass

    @api.depends('check_in', 'check_out', 'overtime_hours')
    def _compute_overtime_status(self):
        for rec in self:
            if rec.overtime_hours >0:
                rec.overtime_status='to_approve'

    def action_approve_overtime(self):
        for rec in self:
            # Block if consumed by overtime swap
            swap = self.env['hr.swap'].search([
                ('attendance_id', '=', rec.id),
                ('swap_work_type', '=', 'overtime'),
            ], limit=1)
            if swap:
                raise ValidationError(
                    _('Cannot approve overtime — this attendance is already consumed '
                      'by swap %(ref)s. The employee will take an ADJUST day instead.',
                      ref=swap.name))

            # Block if a weekend/holiday swap exists for this attendance date
            date_swap = self.env['hr.swap'].search([
                ('employee_id', '=', rec.employee_id.id),
                ('swap_work_date', '=', rec.check_in_date),
                ('swap_work_type', 'in', ('weekend', 'holiday')),
            ], limit=1)
            if date_swap:
                raise ValidationError(
                    _('Cannot approve overtime for %(emp)s on %(date)s — '
                      'a %(type)s swap (%(ref)s) already grants an ADJUST day for this date.',
                      emp=rec.employee_id.name,
                      date=rec.check_in_date,
                      type=dict(rec.env['hr.swap']._fields['swap_work_type'].selection).get(date_swap.swap_work_type),
                      ref=date_swap.name))

            rec.validated_overtime_hours = rec.hours_to_approve
            rec.overtime_status = 'approved'

    def action_refuse_overtime(self):
        for rec in self:
            rec.validated_overtime_hours = 0
            rec.overtime_status = 'refused'

    def action_open_attendance_report_wizard(self):
        """Open the wizard to generate and download the daily attendance Excel."""
        return {
            'name': 'Download Daily Attendance Report',
            'type': 'ir.actions.act_window',
            'res_model': 'attendance.report.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                # Pre-fill today's date; user can change it in the wizard
                'default_report_date': fields.Date.today(),
            },
        }

    def action_remove_dayoff_attendance(self):
        count = 0
        for rec in self:
            if rec.marked_as_day_off:
                rec.marked_as_day_off = False
                count += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Done'),
                'message': _(f'{count} record(s) Day Off flag cleared.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_remove_late_attendance(self):
        count = 0
        for rec in self:
            if rec.is_late:
                rec.is_late = False
                count += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Done'),
                'message': _(f'{count} record(s) Late flag cleared.'),
                'type': 'success',
                'sticky': False,
            }
        }

