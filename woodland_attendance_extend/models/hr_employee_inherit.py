from odoo import models, fields, api,_
from datetime import timedelta
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
    is_late_eligible = fields.Boolean(
        string="Eligible For Late Marking", default=True,
        help="If disabled, this employee's check-ins are never flagged as late, "
             "regardless of the shift's grace period.")
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
             "Working days = calendar days − occurrences of this weekday in the period. "
             "Changing this is logged in the Day-Off History below — past dates keep "
             "resolving to whichever day off was active at the time, so changing it "
             "does not retroactively alter already-computed payroll periods.")
    dayoff_history_ids = fields.One2many(
        'hr.employee.dayoff.history', 'employee_id',
        string="Weekly Day-Off History")

    document_count = fields.Integer(
        string="Documents", compute='_compute_document_count')

    # ------------------------------------------------------------------
    # Weekly Day-Off history
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        for emp, vals in zip(employees, vals_list):
            if 'day_off_day' in vals:
                emp._open_dayoff_history_line(
                    vals['day_off_day'],
                    emp.joining_date or fields.Date.context_today(emp),
                )
        return employees

    def write(self, vals):
        if 'day_off_day' in vals:
            effective_date = (
                self.env.context.get('dayoff_effective_date')
                or fields.Date.context_today(self)
            )
            for emp in self:
                if vals['day_off_day'] != emp.day_off_day:
                    emp._log_dayoff_change(vals['day_off_day'], effective_date)
        return super().write(vals)

    def _open_dayoff_history_line(self, day_off_day, date_start):
        self.ensure_one()
        self.env['hr.employee.dayoff.history'].sudo().create({
            'employee_id': self.id,
            'day_off_day': day_off_day,
            'date_start': date_start,
        })

    def _log_dayoff_change(self, new_day_off_day, effective_date):
        """Close the currently open weekly-day-off history line and open a
        new one dated from `effective_date`, so get_day_off_day() keeps
        resolving past dates to whichever day off was active back then."""
        self.ensure_one()
        History = self.env['hr.employee.dayoff.history'].sudo()
        open_line = History.search([
            ('employee_id', '=', self.id),
            ('date_end', '=', False),
        ], limit=1, order='date_start desc')
        if open_line and open_line.date_start == effective_date:
            # Already changed today — update in place instead of stacking rows.
            open_line.day_off_day = new_day_off_day
            return
        if open_line:
            open_line.date_end = effective_date - timedelta(days=1)
        self._open_dayoff_history_line(new_day_off_day, effective_date)

    def get_day_off_day(self, on_date):
        """Weekly day-off in effect on `on_date` ('0'=Monday..'6'=Sunday, or
        False for "no day off"), following the recorded history. Falls back
        to the live field for dates with no history line (e.g. not yet
        migrated).

        Callers resolving a whole date range (payroll, dashboards) should
        use get_day_off_day_map() instead of calling this in a loop -- one
        query per day adds up fast across a month and many employees.
        """
        self.ensure_one()
        line = self.env['hr.employee.dayoff.history'].sudo().search([
            ('employee_id', '=', self.id),
            ('date_start', '<=', on_date),
            '|', ('date_end', '=', False), ('date_end', '>=', on_date),
        ], limit=1, order='date_start desc')
        return line.day_off_day if line else self.day_off_day

    def get_day_off_day_map(self, date_from, date_to):
        """{date: day_off_day} for every date in [date_from, date_to],
        resolved via history in a single query instead of one query per
        date -- use this instead of get_day_off_day() when iterating a
        date range."""
        self.ensure_one()
        lines = self.env['hr.employee.dayoff.history'].sudo().search([
            ('employee_id', '=', self.id),
            ('date_start', '<=', date_to),
            '|', ('date_end', '=', False), ('date_end', '>=', date_from),
        ], order='date_start')

        day_map = {}
        cursor = date_from
        while cursor <= date_to:
            day_map[cursor] = self.day_off_day
            cursor += timedelta(days=1)

        for line in lines:
            start = max(line.date_start, date_from)
            end = min(line.date_end, date_to) if line.date_end else date_to
            cursor = start
            while cursor <= end:
                day_map[cursor] = line.day_off_day
                cursor += timedelta(days=1)

        return day_map

    @api.depends('attendance_ids.check_in', 'attendance_ids.check_out', 'attendance_ids.worked_hours')
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

    def _get_document_domain(self):
        """Domain matching every ir.attachment uploaded on this employee's
        own record, their Time Off requests (hr.leave), and their ADJUST /
        swap requests (hr.swap) -- the three places documents get attached
        for an employee today."""
        self.ensure_one()
        leave_ids = self.env['hr.leave'].search([('employee_id', '=', self.id)]).ids
        swap_ids = self.env['hr.swap'].search([('employee_id', '=', self.id)]).ids
        return [
            '|', '&', ('res_model', '=', 'hr.employee'), ('res_id', '=', self.id),
            '|', '&', ('res_model', '=', 'hr.leave'), ('res_id', 'in', leave_ids),
                 '&', ('res_model', '=', 'hr.swap'), ('res_id', 'in', swap_ids),
        ]

    def _compute_document_count(self):
        Attachment = self.env['ir.attachment']
        for emp in self:
            emp.document_count = Attachment.search_count(emp._get_document_domain())

    def action_view_employee_documents(self):
        self.ensure_one()
        kanban_view = self.env.ref('woodland_attendance_extend.view_ir_attachment_kanban_woodland_documents')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Documents — %s') % self.name,
            'res_model': 'ir.attachment',
            'view_mode': 'kanban,list,form',
            'views': [(kanban_view.id, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': self._get_document_domain(),
            'context': {'create': False, 'group_by': ['document_section']},
        }

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        domain = list(domain or [])
        if name:
            domain = ['|', ('name', operator, name), ('zk_badge_no', operator, name)] + domain
        employees = self.search(domain)
        employees = employees.sorted(
            key=lambda e: int(e.zk_badge_no) if e.zk_badge_no and e.zk_badge_no.isdigit() else float('inf'))[:limit]
        return [(emp.id, emp.display_name) for emp in employees]

class HrDepartment(models.Model):
    _inherit = 'hr.department'

    is_morning_shift = fields.Boolean("Morning Shift (7 AM)")
    eligible_for_bonus = fields.Boolean(
        string='Eligible for Attendance Bonus',
        default=True,
        help='If enabled, absent employees in this department lose 2 days salary '
             'per absence (instead of 1). Perfect attendance earns a 500 bonus.',
    )
