import io
import base64
from datetime import datetime
from collections import defaultdict

import pytz

from odoo import models, fields
from odoo.exceptions import UserError

try:
    import xlsxwriter
except ImportError:
    raise UserError("xlsxwriter is required. Install it via: pip install xlsxwriter")

DHAKA_TZ = pytz.timezone("Asia/Dhaka")


class AttendanceReportWizard(models.TransientModel):
    _name        = "attendance.report.wizard"
    _description = "Daily Attendance Report Wizard"

    report_date = fields.Date(
        string="Report Date",
        required=True,
        default=fields.Date.today,
    )
    shift_ids = fields.Many2many(
        "hr.shift",
        string="Shifts",
        help="Leave empty to include all shifts.",
    )
    worker_type = fields.Selection([
        ('daily', 'Daily Worker'),
        ('regular', 'Regular Worker'),
    ], default='regular', string="Worker Type")
    department_id = fields.Many2many('hr.department')

    # ── Entry point ───────────────────────────────────────────────────────────
    def action_generate_report(self):
        self.ensure_one()
        xlsx_bytes = self._build_xlsx()
        attachment = self.env["ir.attachment"].create({
            "name":     f"Attendance_{self.report_date}.xlsx",
            "type":     "binary",
            "datas":    base64.b64encode(xlsx_bytes),
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {
            "type":   "ir.actions.act_url",
            "url":    f"/web/content/{attachment.id}?download=true",
            "target": "new",
        }

    # ── ORM helpers ───────────────────────────────────────────────────────────
    def _fetch_attendances(self):
        # Convert local (Dhaka) date → UTC
        local_start = DHAKA_TZ.localize(datetime.combine(self.report_date, datetime.min.time()))
        local_end = DHAKA_TZ.localize(datetime.combine(self.report_date, datetime.max.time()))

        date_start = local_start.astimezone(pytz.utc).replace(tzinfo=None)
        date_end = local_end.astimezone(pytz.utc).replace(tzinfo=None)

        domain = [
            ("check_in", ">=", date_start),
            ("check_in", "<=", date_end),
        ]

        if self.shift_ids:
            domain.append(("shift_id", "in", self.shift_ids.ids))

        # 🔥 IMPORTANT FIX (was wrong before)
        if self.department_id:
            domain.append(("employee_id.department_id", "in", self.department_id.ids))

        if self.worker_type:
            domain.append(("worker_type", "=", self.worker_type))

        return self.env["hr.attendance"].search(domain)

    def _fetch_absent_employees(self, present_emp_ids):
        domain = [("active", "=", True)]

        # 🔥 Apply SAME filters as attendance
        if self.department_id:
            domain.append(("department_id", "in", self.department_id.ids))

        if self.worker_type:
            domain.append(("worker_type", "=", self.worker_type))

        all_emps = self.env["hr.employee"].search(domain)

        absent = [e for e in all_emps if e.id not in present_emp_ids]

        return sorted(absent, key=lambda e: (e.department_id.name or "", e.name or ""))

    # ── Python grouping ───────────────────────────────────────────────────────
    def _group_attendances(self, attendances):
        grouped = defaultdict(lambda: defaultdict(list))
        for att in attendances:
            dept  = att.employee_id.department_id.name or "No Department"
            shift = att.shift_id.name if att.shift_id else "No Shift"
            grouped[dept][shift].append(att)
        return {
            dept: dict(sorted(shifts.items()))
            for dept, shifts in sorted(grouped.items())
        }

    def _group_absent(self, absent_list):
        grouped = defaultdict(list)
        for emp in absent_list:
            grouped[emp.department_id.name or "No Department"].append(emp)
        return dict(sorted(grouped.items()))

    # ── Safe value helpers ────────────────────────────────────────────────────
    @staticmethod
    def _s(val):
        """Return a plain Python str – never an Odoo recordset."""
        if not val:
            return ""
        # Many2one / recordset guard
        if hasattr(val, '_name'):
            return ""
        return str(val)

    @staticmethod
    def _fmt_dt(dt):
        if not dt:
            return ""
        if isinstance(dt, datetime):
            # Odoo stores datetimes as naive UTC; convert to Asia/Dhaka
            if dt.tzinfo is None:
                dt = pytz.utc.localize(dt)
            dt = dt.astimezone(DHAKA_TZ)
            return dt.strftime("%Y-%m-%d %H:%M")
        return str(dt)

    @staticmethod
    def _fmt_f(val):
        try:
            f = float(val)
            return round(f, 2) if f else ""
        except (TypeError, ValueError):
            return ""

    # ── Main builder ──────────────────────────────────────────────────────────
    def _build_xlsx(self):
        buf = io.BytesIO()
        wb  = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws  = wb.add_worksheet("Attendance Report")

        # ── Formats ───────────────────────────────────────────────────────────
        def fmt(**kw):
            base = {"font_name": "Arial", "font_size": 10, "border": 1}
            base.update(kw)
            return wb.add_format(base)

        f_title   = fmt(bold=True, font_size=14, font_color="FFFFFF",
                        bg_color="1F4E79", align="center", valign="vcenter")
        f_hdr     = fmt(bold=True, font_color="FFFFFF", bg_color="1F4E79",
                        align="center", valign="vcenter", text_wrap=True, border=1)
        f_dept    = fmt(bold=True, font_color="FFFFFF", bg_color="2E75B6",
                        valign="vcenter", border=0)
        f_shift   = fmt(bold=True, bg_color="9DC3E6",
                        valign="vcenter", border=0)
        f_present = fmt(bg_color="C6EFCE", font_size=9)
        f_late    = fmt(bg_color="FFFF00", font_size=9)
        f_dayoff  = fmt(bg_color="FF0000", font_color="FFFFFF", font_size=9)
        f_absent  = fmt(bg_color="FFC7CE", font_size=9)
        f_sum_lbl = fmt(bold=True)
        f_sum_g   = fmt(bold=True, bg_color="C6EFCE", align="center")
        f_sum_y   = fmt(bold=True, bg_color="FFFF00", align="center")
        f_sum_r   = fmt(bold=True, bg_color="FF0000", font_color="FFFFFF", align="center")
        f_sum_o   = fmt(bold=True, bg_color="FFC7CE", align="center")

        # Legend formats (no border)
        f_leg_g = wb.add_format({"font_name":"Arial","font_size":9,"bold":True,
                                  "bg_color":"C6EFCE","align":"center"})
        f_leg_o = wb.add_format({"font_name":"Arial","font_size":9,"bold":True,
                                  "bg_color":"FFC7CE","align":"center"})
        f_leg_y = wb.add_format({"font_name":"Arial","font_size":9,"bold":True,
                                  "bg_color":"FFFF00","align":"center"})
        f_leg_r = wb.add_format({"font_name":"Arial","font_size":9,"bold":True,
                                  "bg_color":"FF0000","font_color":"FFFFFF","align":"center"})

        COLS = [
            ("SL",            5),
            ("ZK Badge No",  14),
            ("Employee",      24),
            ("Check In",      18),
            ("Check Out",     18),
            ("Worked Hours",  13),
            ("Break Start",   18),
            ("Break End",     18),
            ("Status",        12),
            ("OT Hours",      12),
            ("OT Status",     14),
            ("Notes",         32),
        ]
        N = len(COLS)

        for ci, (_, w) in enumerate(COLS):
            ws.set_column(ci, ci, w)

        row = 0

        # ── Title ──────────────────────────────────────────────────────────────
        ws.merge_range(row, 0, row, N - 1,
                       f"Daily Attendance Report  –  {self.report_date}", f_title)
        ws.set_row(row, 28)
        row += 1

        # ── Legend ─────────────────────────────────────────────────────────────
        ws.write(row, 0, "■ Present",  f_leg_g)
        ws.write(row, 1, "■ Absent",   f_leg_o)
        ws.write(row, 2, "■ Late",     f_leg_y)
        ws.write(row, 3, "■ Day-Off",  f_leg_r)
        row += 2  # blank spacer

        # ── Data ───────────────────────────────────────────────────────────────
        attendances    = self._fetch_attendances()
        present_ids    = {att.employee_id.id for att in attendances}
        dept_shift_map = self._group_attendances(attendances)

        def write_col_headers(row):
            for ci, (hdr, _) in enumerate(COLS):
                ws.write(row, ci, hdr, f_hdr)
            ws.set_row(row, 18)
            return row + 1

        def write_band(row, text, fmt_, height):
            ws.merge_range(row, 0, row, N - 1, text, fmt_)
            ws.set_row(row, height)
            return row + 1

        # Present / Late / Day-Off grouped by Dept → Shift
        for dept_name, shifts in dept_shift_map.items():
            row = write_band(row, f"  Department: {dept_name}", f_dept, 20)
            for shift_name, records in shifts.items():
                row = write_band(row, f"      Shift: {shift_name}", f_shift, 18)
                row = write_col_headers(row)
                sl = 1
                for att in sorted(records, key=lambda a: a.employee_id.name or ""):
                    if att.marked_as_day_off:
                        rf = f_dayoff
                    elif att.is_late:
                        rf = f_late
                    else:
                        rf = f_present

                    status = ("Day-Off" if att.marked_as_day_off
                              else "Late" if att.is_late
                              else "Present")

                    # zk_badge_no – guard against recordset / False
                    badge = self._s(getattr(att.employee_id, "zk_badge_no", ""))

                    ot_status = self._s(getattr(att, "overtime_status", ""))
                    ot_status = ot_status.replace("_", " ").title()

                    values = [
                        sl,
                        badge,
                        self._s(att.employee_id.name),
                        self._fmt_dt(att.check_in),
                        self._fmt_dt(att.check_out),
                        self._fmt_f(att.worked_hours),
                        self._fmt_dt(att.break_start),
                        self._fmt_dt(att.break_end),
                        status,
                        self._fmt_f(getattr(att, "overtime_hours", 0)),
                        ot_status,
                        self._s(att.notes),
                    ]
                    for ci, v in enumerate(values):
                        ws.write(row, ci, v, rf)
                    ws.set_row(row, 16)
                    row += 1
                    sl += 1
            row += 1  # gap between departments

        # Absent grouped by Dept
        absent_list    = self._fetch_absent_employees(present_ids)
        absent_by_dept = self._group_absent(absent_list)

        if absent_by_dept:
            row = write_band(row, "  ABSENT EMPLOYEES", f_dept, 20)
            for dept_name, emps in absent_by_dept.items():
                row = write_band(row, f"      Department: {dept_name}", f_shift, 18)
                row = write_col_headers(row)
                sl = 1
                for emp in emps:
                    badge = self._s(getattr(emp, "zk_badge_no", ""))
                    values = [
                        sl,
                        badge,
                        self._s(emp.name),
                        "", "", "", "", "", "Absent", "", "", "",
                    ]
                    for ci, v in enumerate(values):
                        ws.write(row, ci, v, f_absent)
                    ws.set_row(row, 16)
                    row += 1
                    sl += 1
            row += 1

        # ── Summary ────────────────────────────────────────────────────────────
        present = sum(1 for a in attendances if not a.marked_as_day_off)
        late    = sum(1 for a in attendances if a.is_late and not a.marked_as_day_off)
        dayoff  = sum(1 for a in attendances if a.marked_as_day_off)
        absent  = len(absent_list)

        ws.merge_range(row, 0, row, N - 1, "Summary", f_hdr)
        ws.set_row(row, 20)
        row += 1

        for label, count, lf, vf in [
            ("Total Present",  present, f_sum_lbl, f_sum_g),
            ("Total Late",     late,    f_sum_lbl, f_sum_y),
            ("Total Day-Off",  dayoff,  f_sum_lbl, f_sum_r),
            ("Total Absent",   absent,  f_sum_lbl, f_sum_o),
        ]:
            ws.write(row, 0, label, lf)
            ws.write(row, 1, count, vf)
            row += 1

        wb.close()
        buf.seek(0)
        return buf.read()