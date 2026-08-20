from odoo import models


class HrDepartureWizard(models.TransientModel):
    _inherit = 'hr.departure.wizard'

    def action_register_departure(self):
        next_action = super().action_register_departure()

        employees = self.employee_ids
        if not employees:
            # employee_ids came back empty (context active_id/active_ids
            # not populated the way _get_default_employee_ids expects) —
            # fall back to whatever active_id/active_ids is in context so
            # the ZK cleanup still runs for the employee that was actually
            # being archived.
            ctx = self.env.context
            active_ids = ctx.get('active_ids') or ([ctx.get('active_id')] if ctx.get('active_id') else [])
            employees = self.env['hr.employee'].browse(active_ids)

        employees._zk_process_departure(
            departure_reason_id=self.departure_reason_id.id,
            note=self.departure_description or False,
        )
        return next_action
