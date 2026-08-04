from odoo import models, fields, api
from datetime import timedelta, date as date_cls
import logging
import pytz

_logger = logging.getLogger(__name__)

DHAKA_TZ = pytz.timezone('Asia/Dhaka')


class ZkAttendanceProcessor(models.Model):
    _name = 'zk.attendance.processor'
    _description = 'ZK Single Punch Processor'

    # ------------------------------------------------------------------
    # Timezone helpers
    # ------------------------------------------------------------------

    def _to_local(self, utc_naive):
        """Convert UTC-naive datetime → Asia/Dhaka-aware datetime."""
        return pytz.utc.localize(utc_naive).astimezone(DHAKA_TZ)

    def _local_date(self, utc_naive):
        return self._to_local(utc_naive).date()

    def _local_naive(self, utc_naive):
        """UTC-naive → local-naive (for arithmetic against window datetimes)."""
        return self._to_local(utc_naive).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Dynamic shift detection
    # ------------------------------------------------------------------

    def _detect_shift(self, local_punch_dt, employee):
        """
        Find which hr.shift the punch falls into based on check-in windows.

        Window rule: shift_start - 1h  <=  punch  <=  shift_start + 2h

        For 12-hour shifts (Day / Night) with overlapping boundaries we
        use employee.is_12_hour_shift to restrict matching to is_12_hour
        shifts only, avoiding false positives from the regular 8-h shifts.

        Returns (shift_record, windows_dict) or (False, None).
        """
        all_shifts = self.env['hr.shift'].search([])

        # Separate 12-h shifts from normal shifts
        twelve_hour_shifts = all_shifts.filtered(lambda s: s.is_12_hour)
        normal_shifts = all_shifts.filtered(lambda s: not s.is_12_hour)

        # Build candidate list
        if employee.is_12_hour_shift:
            candidates = twelve_hour_shifts
        else:
            candidates = normal_shifts

        # The "local date" we use for window calculation depends on the
        # punch time.  For night-crossing shifts the shift may have started
        # the previous calendar day, so we try today AND yesterday.
        punch_date = local_punch_dt.date()
        check_dates = [punch_date, punch_date - timedelta(days=1)]

        best_shift = False
        best_windows = None

        for shift in candidates:
            for d in check_dates:
                wins = shift.get_checkin_window(d)
                if wins['window_start'] <= local_punch_dt <= wins['window_end']:
                    best_shift = shift
                    best_windows = wins
                    break
            if best_shift:
                break

        return best_shift, best_windows

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    @api.model
    def process_raw_punches(self):
        raw_punches = self.env['zk.attendance.log'].search(
            [('state', '=', 'new')], order='punch_time asc',limit=500
        )
        for punch in raw_punches:
            if not punch.employee_id:
                punch.write({'state': 'ignored', 'error_msg': 'No employee linked'})
                continue
            try:
                self._route_punch(punch.employee_id, punch)
            except Exception as e:
                _logger.exception("Error processing punch %s", punch.id)
                punch.write({'state': 'error', 'error_msg': str(e)})

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route_punch(self, employee, punch):
        Attendance = self.env['hr.attendance']

        local_punch_naive = self._local_naive(punch.punch_time)
        local_punch_date = self._local_date(punch.punch_time)

        # --- Find open attendance ---
        open_att = Attendance.search([
            ('employee_id', '=', employee.id),
            ('check_out', '=', False),
        ], limit=1, order='check_in desc')


        # Debounce: ignore if punch is within 15 min of the open check-in
        if open_att:
            # General-shift break handling
            shift = open_att.shift_id
            if shift and shift.is_general:
                checkin_local_naive = self._local_naive(open_att.check_in)
                checkin_date = self._local_date(open_att.check_in)
                break_wins = shift.get_break_windows(checkin_date)
                if break_wins:
                    if not open_att.break_start:
                        if break_wins['break_start'] <= local_punch_naive <= break_wins['break_end']:
                            open_att.write({'break_start': punch.punch_time})
                            punch.write({'state': 'processed', 'processed_punch_type': 'break_out'})
                            return
                    elif not open_att.break_end:
                        open_att.write({'break_end': punch.punch_time})
                        punch.write({'state': 'processed', 'processed_punch_type': 'break_in'})
                        return
            diff_minutes = (punch.punch_time - open_att.check_in).total_seconds() / 60.0
            if diff_minutes < 15:
                punch.write({
                    'state': 'error',
                    'error_msg': 'Ignored: within 15-minute debounce of check-in',
                })
                return
            elif diff_minutes < 360:
                punch.write({
                    'state': 'error',
                    'error_msg': 'Inside 6 hours',
                })
                return

        # --- No open attendance → Check for recent checkout or Check-in ---
        if not open_att:
            # Look for the latest closed attendance record for this employee
            latest_closed_att = Attendance.search([
                ('employee_id', '=', employee.id),
                ('check_out', '!=', False),
            ], limit=1, order='check_out desc')

            # If latest checkout is within 1 hour of current punch → update checkout instead of new check-in
            if latest_closed_att:
                diff_minutes = (punch.punch_time - latest_closed_att.check_out).total_seconds() / 60.0
                if diff_minutes <= 60:
                    latest_closed_att.write({'check_out': punch.punch_time})
                    punch.write({
                        'state': 'processed',
                        'processed_punch_type': 'check_out',
                    })
                    return

            # No recent checkout → normal check-in
            self._do_checkin(punch, employee, local_punch_naive)
            return

        # --- Open attendance exists ---

        # Edge case: >20 h since check-in → auto-close and start fresh
        hours_open = (punch.punch_time - open_att.check_in).total_seconds() / 3600.0
        if hours_open > 20.0:
            open_att.write({
                'check_out': open_att.check_in + timedelta(hours=20),
                'marked_as_day_off': True,
                'notes': 'Auto-closed: punch received after 20 h without checkout.',
            })
            self._do_checkin(punch, employee, local_punch_naive)
            return



        # Default → checkout
        open_att.write({'check_out': punch.punch_time})
        punch.write({'state': 'processed', 'processed_punch_type': 'check_out'})

    # ------------------------------------------------------------------
    # Check-in action
    # ------------------------------------------------------------------

    def _do_checkin(self, punch, employee, local_punch_naive):
        """
        Detect shift dynamically, mark late if needed, mark day-off if
        the punch falls into no shift window.
        """
        shift, windows = self._detect_shift(local_punch_naive, employee)

        is_late = False
        marked_as_day_off = False
        notes = False

        if shift:
            if (    shift.is_morning_shift
                    and employee.department_id
                    and employee.department_id.is_morning_shift
            ):
                late_after = windows['shift_start'] + timedelta(hours=1, minutes=15)
            else:
                late_after = windows['late_after']

            is_late = local_punch_naive > late_after


        self.env['hr.attendance'].create({
            'employee_id': employee.id,
            'check_in': punch.punch_time,
            'shift_id': shift.id if shift else False,
            'is_late': is_late,
            'marked_as_day_off': marked_as_day_off,
            'notes': notes,
        })
        punch.write({'state': 'processed','processed_punch_type':'check_in'})

    # ------------------------------------------------------------------
    # Cron: auto-checkout long-open attendances
    # ------------------------------------------------------------------

    @api.model
    def cron_auto_checkout_long_attendance(self):
        now = fields.Datetime.now()
        limit_time = now - timedelta(hours=20)

        attendances = self.env['hr.attendance'].search([
            ('check_out', '=', False),
            ('check_in', '<=', limit_time),
        ])

        if not attendances:
            return True

        for att in attendances:
            att.with_context(no_check_overlap=True).write({
                'check_out': att.check_in + timedelta(hours=20),
                'marked_as_day_off': True,
                'notes': 'Auto Checkout: 20h limit reached (cron)',
            })

        _logger.info(
            'cron_auto_checkout_long_attendance: closed %d stale attendance(s)',
            len(attendances)
        )
        return True

    @api.constrains('check_in', 'check_out', 'employee_id')
    def _check_validity(self):
        # Allow cron auto-checkout to skip overlap validation
        if self.env.context.get('no_check_overlap'):
            return
        return super()._check_validity()
