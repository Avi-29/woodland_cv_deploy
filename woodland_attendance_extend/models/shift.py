from odoo import models, fields
from datetime import datetime, timedelta
import pytz


class HrShift(models.Model):
    _name = 'hr.shift'
    _description = 'Work Shift'

    name = fields.Char(required=True)
    start_time = fields.Float(required=True, string="Start Time (24h)")
    end_time = fields.Float(required=True, string="End Time (24h)")

    # True when shift crosses midnight (end < start, e.g. 22:00–06:00)
    is_night = fields.Boolean(string="Crosses Midnight")
    is_morning_shift = fields.Boolean(
        string='Morning Shift (7 AM)'
    )

    # For 12-hour shifts (Day 06:00–18:00 / Night 18:00–06:00) there is an
    # overlap of ±1 h at the boundaries.  We use this flag to prefer the
    # employee's own is_12_hour_shift field instead of guessing.
    is_12_hour = fields.Boolean(
        string="12-Hour Shift",
        help="Enable for Day (06:00-18:00) and Night (18:00-06:00) shifts "
             "so the system can resolve boundary overlaps using the employee "
             "flag 'Prefers 12-Hour Shift'."
    )

    # General shift gets break windows
    is_general = fields.Boolean(string="Is General Shift")
    break_start = fields.Float(string="Break Start")
    break_end = fields.Float(string="Break End")

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _float_to_dt(self, f_time, base_date, next_day=False):
        h = int(f_time)
        m = int(round((f_time - h) * 60))
        d = base_date + timedelta(days=1) if next_day else base_date
        return datetime(d.year, d.month, d.day, h, m)

    def get_checkin_window(self, roster_date):
        """
        Return the check-in acceptance window for this shift on roster_date.

        Window rule: ideal_start - 1 hour  <=  check_in  <=  ideal_start + 2 hours
        Late flag   : check_in > ideal_start + 15 minutes

        Returns a dict with naive-local datetimes:
            window_start  – earliest acceptable check-in
            window_end    – latest  acceptable check-in
            late_after    – check-ins after this are marked late
            shift_start   – exact shift start (naive local)
        """
        self.ensure_one()
        shift_start = self._float_to_dt(self.start_time, roster_date)

        return {
            'shift_start': shift_start,
            'window_start': shift_start - timedelta(hours=1),
            'window_end': shift_start + timedelta(hours=2),
            'late_after': shift_start + timedelta(minutes=15),
        }

    def get_break_windows(self, roster_date):
        """Only relevant for general shifts."""
        self.ensure_one()
        if not self.is_general:
            return None
        return {
            'break_start': self._float_to_dt(self.break_start, roster_date) - timedelta(minutes=15),
            'break_end': self._float_to_dt(self.break_end, roster_date) + timedelta(minutes=15),
        }
