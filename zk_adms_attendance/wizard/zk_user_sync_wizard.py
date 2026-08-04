from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ZkUserSyncWizard(models.TransientModel):
    _name = 'zk.user.sync.wizard'
    _description = 'Sync Enrolled Users to Devices'

    sync_mode = fields.Selection([
        ('selected_users',   'Selected users → selected devices'),
        ('all_users',        'All enrolled users → selected devices'),
        ('device_to_device', 'All users from one device → other devices'),
    ], string='Sync Mode', required=True, default='selected_users')

    user_ids = fields.Many2many(
        'zk.enrolled.user',
        string='Users to Sync',
        help='Leave empty with "all_users" mode to sync everyone',
    )
    source_device_id = fields.Many2one(
        'zk.device', string='Source Device',
        help='Sync all users enrolled on this device to the target devices',
    )
    target_device_ids = fields.Many2many(
        'zk.device',
        string='Target Devices',
        required=True,
        help='Devices that will receive the enrolled data',
    )
    include_fingerprints = fields.Boolean('Include Fingerprints', default=True)
    include_faces        = fields.Boolean('Include Faces', default=True)
    include_cards        = fields.Boolean('Include Card / Password', default=True)

    force_resync = fields.Boolean(
        string='Force Re-sync (ignore enrollment cache)',
        default=False,
        help='Normally the wizard skips biometric data that a device already has.\n'
             'Enable this only if a device was factory-reset or you suspect its data\n'
             'is out of sync. With 1 700 employees and 7 devices this could queue\n'
             '30 000+ commands — use sparingly.',
    )

    # Result
    state        = fields.Selection([('draft', 'Configure'), ('done', 'Done')], default='draft')
    queued_count = fields.Integer(string='Commands Queued', readonly=True)
    skipped_count = fields.Integer(string='Commands Skipped (already enrolled)', readonly=True)
    summary      = fields.Text(string='Summary', readonly=True)

    def action_sync(self):
        self.ensure_one()

        EnrolledUser = self.env['zk.enrolled.user']

        # ── Resolve users ────────────────────────────────────────────────────
        if self.sync_mode == 'selected_users':
            users = self.user_ids
            if not users:
                raise UserError(_('Please select at least one user to sync.'))
        elif self.sync_mode == 'all_users':
            users = EnrolledUser.search([])
        else:  # device_to_device
            if not self.source_device_id:
                raise UserError(_('Please select a source device.'))
            users = EnrolledUser.search(
                [('enrolled_device_ids', 'in', self.source_device_id.id)]
            )
            if not users:
                raise UserError(_('No enrolled users found on the source device.'))

        if not self.target_device_ids:
            raise UserError(_('Please select at least one target device.'))

        target_ids = self.target_device_ids.ids
        force      = self.force_resync

        # ── Dispatch via model method (handles dedup logic) ──────────────────
        queued = users.enqueue_to_devices(
            target_device_ids=target_ids,
            force=force,
            include_fp=self.include_fingerprints,
            include_face=self.include_faces,
        )

        # Compute how many were skipped for the summary
        max_possible = 0
        for user in users:
            for _device in self.target_device_ids:
                max_possible += 1                          # USERINFO
                max_possible += len(user.fingerprint_ids.filtered('valid'))
                max_possible += len(user.face_ids.filtered('valid'))
        skipped = max_possible - queued

        lines = []
        for user in users[:30]:
            lines.append(
                f'  PIN {user.pin} ({user.name or "unknown"}): '
                f'{len(user.fingerprint_ids)} FPs, {len(user.face_ids)} faces '
                f'| on {len(user.enrolled_device_ids)} device(s)'
            )

        summary_parts = [
            f'Queued {queued} command(s) for {len(self.target_device_ids)} device(s).',
            f'Skipped {skipped} command(s) — data already present on target device(s).',
            '',
        ]
        if force:
            summary_parts.insert(0, '⚠️  Force re-sync enabled — all data was re-queued.')
        if lines:
            summary_parts += ['Users synced (first 30):'] + lines
        if len(users) > 30:
            summary_parts.append(f'  … and {len(users) - 30} more.')

        self.write({
            'state':         'done',
            'queued_count':  queued,
            'skipped_count': skipped,
            'summary':       '\n'.join(summary_parts),
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
