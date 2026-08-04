from odoo import models, fields, api,_
import pytz



class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # Removed: shift_group (A/B/C/general) — no more roster groups

    worker_type = fields.Selection([
        ('daily', 'Daily Worker'),
        ('regular', 'Regular Worker'),
    ], default='regular', string="Worker Type")

    is_12_hour_shift = fields.Boolean(
        string="12-Hour Shift Employee",
        help="Enable for employees who work Day (06:00-18:00) or Night (18:00-06:00) "
             "12-hour shifts. This resolves check-in window overlaps at boundaries "
             "between those two shifts."
    )
    is_ot_eligible =fields.Boolean(string="Eligible For OT", default=True)
    fathers_name =fields.Char(string="Fathers's Name")
    mothers_name =fields.Char(string="Mothers's Name")
    joining_date=fields.Date(string="Joining Date")
    bank_acc_no=fields.Char(string="Bank Account No.")
    bank_name=fields.Char(string="Bank Name")
    hours_last_month = fields.Float(compute='_compute_hours_last_month_over',store=True)
    blood_group=fields.Char()
    day_off_day = fields.Selection([
        ('0', 'Monday'),
        ('1', 'Tuesday'),
        ('2', 'Wednesday'),
        ('3', 'Thursday'),
        ('4', 'Friday'),
        ('5', 'Saturday'),
        ('6', 'Sunday'),
    ], string="Weekly Day Off",
        help="Recurring weekly day off (e.g. Friday). "
             "Working days = calendar days − occurrences of this weekday in the period.")

    def _compute_hours_last_month_over(self):
        """
        Compute hours and overtime hours in the current month, if we are the 15th of october, will compute from 1 oct to 15 oct
        """
        now = fields.Datetime.now()
        now_utc = pytz.utc.localize(now)
        for employee in self:
            tz = pytz.timezone(employee.tz or 'UTC')
            now_tz = now_utc.astimezone(tz)
            start_tz = now_tz.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_naive = start_tz.astimezone(pytz.utc).replace(tzinfo=None)
            end_tz = now_tz
            end_naive = end_tz.astimezone(pytz.utc).replace(tzinfo=None)

            current_month_attendances = employee.attendance_ids.filtered(
                lambda att: att.check_in >= start_naive and att.check_out and att.check_out <= end_naive
            )
            hours = 0
            overtime_hours = 0
            for att in current_month_attendances:
                hours += att.worked_hours or 0
                overtime_hours += att.validated_overtime_hours or 0
            employee.hours_last_month = round(hours, 2)

    @api.onchange("worker_type")
    def onchange_salary_type(self):
        for rec in self:
            if rec.worker_type=='daily':
                rec.salary_type='daily'
            elif rec.worker_type=='regular':
                rec.salary_type='monthly'


    @api.depends('name', 'zk_badge_no')
    def _compute_display_name(self):
        for rec in self:
            name = rec.name or ''
            if rec.zk_badge_no:
                name += f" ({rec.zk_badge_no})"
            rec.display_name = name

    def action_open_last_month_attendances(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Attendances This Month"),
            "res_model": "hr.attendance",
            "view_mode": "list,form",
            "views": [
                (
                    self.env.ref(
                        'hr_attendance.hr_attendance_employee_simple_tree_view'
                    ).id,
                    "list"
                ),
                (False, "form"),
            ],
            "context": {
                "create": 0,
                "search_default_check_in_filter": 1,
                "default_employee_id": self.id,
                "display_extra_hours": self.display_extra_hours,
            },
            "domain": [('employee_id', '=', self.id)],
        }

class HrDepartment(models.Model):
    _inherit = 'hr.department'

    is_morning_shift = fields.Boolean("Morning Shift (7 AM)")
    eligible_for_bonus = fields.Boolean(
        string='Eligible for Attendance Bonus',
        default=True,
        help='If enabled, absent employees in this department lose 2 days salary '
             'per absence (instead of 1). Perfect attendance earns a 500 bonus.',
    )
