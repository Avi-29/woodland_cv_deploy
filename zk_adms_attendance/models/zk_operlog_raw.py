"""
zk.operlog.raw — bulk OPERLOG queue (USER / FP / FACE lines only)
==================================================================
`_handle_operlog` bulk-inserts every USER/FP/FACE line here instead of
upserting each one synchronously inside the HTTP request. A cron
(`process_batch`, see data/ir_cron.xml) drains this queue in bounded
batches, prefetching all referenced PINs with a single search instead of
one search per line — this is what actually fixes the "get_all_userinfo
on 2,500+ users" bottleneck, not just moving the same N+1 pattern later.

Every per-row mutation runs inside its own `cr.savepoint()` so one bad
row (bad data, a constraint violation, anything) can only ever mark
that single row 'error' and get skipped — it can never roll back or
permanently stall the rest of the batch the way an unguarded exception
inside a cron transaction would.

OPLOG/USERPIC/DEL_USER/DEL_FP lines are NOT queued here — they're low
volume and stay handled inline in the controller.
"""
from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


def _parse_kv_line(line: str) -> dict:
    """Parse a tab-separated KEY=VALUE string into a dict."""
    result = {}
    for token in (line or '').strip().split('\t'):
        if '=' in token:
            k, _, v = token.partition('=')
            result[k.strip()] = v.strip()
    return result


class ZkOperlogRaw(models.Model):
    _name = 'zk.operlog.raw'
    _description = 'ZKTeco Raw OPERLOG Line (bulk queue)'
    _order = 'id asc'

    device_id = fields.Many2one('zk.device', ondelete='set null', index=True)
    device_serial = fields.Char(index=True)
    record_type = fields.Selection([
        ('USER', 'User'),
        ('FP', 'Fingerprint'),
        ('FACE', 'Face'),
    ], required=True, index=True)
    raw_kv = fields.Text(
        required=True,
        help='Tab-separated KEY=VALUE line content (everything after the record-type token).',
    )
    state = fields.Selection([
        ('new', 'New'),
        ('processed', 'Processed'),
        ('error', 'Error'),
    ], default='new', index=True)
    error_msg = fields.Char()

    def action_reprocess(self):
        self.write({'state': 'new', 'error_msg': False})

    def _mark_error(self, row, exc):
        _logger.warning('ZK OPERLOG row %s (%s): %s', row.id, row.record_type, exc)
        try:
            row.write({'state': 'error', 'error_msg': str(exc)[:250]})
        except Exception:
            # Even the error-marking write failed (e.g. cursor was left in a
            # bad state) — log and move on rather than let this raise too.
            _logger.exception('ZK OPERLOG: failed to mark row %s as error', row.id)

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    @api.model
    def process_batch(self, limit=500):
        rows = self.search([('state', '=', 'new')], order='id asc', limit=limit)
        if not rows:
            return

        self._process_user_rows(rows.filtered(lambda r: r.record_type == 'USER'))
        self._process_fp_rows(rows.filtered(lambda r: r.record_type == 'FP'))
        self._process_face_rows(rows.filtered(lambda r: r.record_type == 'FACE'))

    # ------------------------------------------------------------------
    # USER lines — one search for the whole batch, bulk-create the new ones
    # ------------------------------------------------------------------

    def _process_user_rows(self, rows):
        if not rows:
            return
        UserModel = self.env['zk.enrolled.user']

        parsed = []
        for row in rows:
            try:
                kv = _parse_kv_line(row.raw_kv)
                pin = str(kv.get('PIN', '')).strip()
                if not pin:
                    raise ValueError('Missing PIN')
                parsed.append((row, pin, kv))
            except Exception as e:
                self._mark_error(row, e)

        if not parsed:
            return

        pins = list({pin for _, pin, _ in parsed})
        try:
            existing_by_pin = {u.pin: u for u in UserModel.search([('pin', 'in', pins)])}
        except Exception as e:
            for row, _pin, _kv in parsed:
                self._mark_error(row, e)
            return

        create_batch = []  # list of (row, vals)
        for row, pin, kv in parsed:
            try:
                vals = {
                    'pin': pin,
                    'name': kv.get('Name', ''),
                    'privilege': str(kv.get('Pri', '0')),
                    'password': kv.get('Passwd', ''),
                    'card': kv.get('Card', ''),
                    'group': kv.get('Grp', '1'),
                    'time_zone': kv.get('TZ', '0'),
                    'verify_style': int(kv.get('Verify', 31) or 31),
                    'source_device_id': row.device_id.id,
                    'last_updated': fields.Datetime.now(),
                }
                existing = existing_by_pin.get(pin)
                if existing:
                    with self.env.cr.savepoint():
                        existing.write(vals)
                        if row.device_id and row.device_id.id not in existing.enrolled_device_ids.ids:
                            existing.enrolled_device_ids = [(4, row.device_id.id)]
                    row.write({'state': 'processed'})
                else:
                    create_batch.append((row, vals))
            except Exception as e:
                self._mark_error(row, e)

        if create_batch:
            try:
                with self.env.cr.savepoint():
                    new_users = UserModel.create([v for _, v in create_batch])
                for (row, _vals), user in zip(create_batch, new_users):
                    try:
                        if row.device_id:
                            user.enrolled_device_ids = [(4, row.device_id.id)]
                        row.write({'state': 'processed'})
                        # Keep the prefetch map current in case this PIN
                        # repeats later in the same batch (duplicate line).
                        existing_by_pin[user.pin] = user
                    except Exception as e:
                        self._mark_error(row, e)
            except Exception as e:
                # The bulk create itself failed (e.g. one row's data trips a
                # DB constraint) — fall back to one-at-a-time so a single
                # bad record doesn't lose everyone else in the batch.
                _logger.warning('ZK OPERLOG: bulk USER create failed (%s), falling back to per-row', e)
                for row, vals in create_batch:
                    try:
                        with self.env.cr.savepoint():
                            user = UserModel.create(vals)
                            if row.device_id:
                                user.enrolled_device_ids = [(4, row.device_id.id)]
                        row.write({'state': 'processed'})
                        existing_by_pin[user.pin] = user
                    except Exception as e2:
                        self._mark_error(row, e2)

        _logger.info('ZK OPERLOG batch: processed %d USER row(s)', len(parsed))

    # ------------------------------------------------------------------
    # FP lines — one search for (user_id, finger_id) pairs in the batch
    # ------------------------------------------------------------------

    def _process_fp_rows(self, rows):
        if not rows:
            return
        UserModel = self.env['zk.enrolled.user']
        FpModel = self.env['zk.enrolled.fp']

        parsed = []
        for row in rows:
            try:
                kv = _parse_kv_line(row.raw_kv)
                pin = str(kv.get('PIN', '')).strip()
                if not pin:
                    raise ValueError('Missing PIN')
                parsed.append((row, pin, kv))
            except Exception as e:
                self._mark_error(row, e)
        if not parsed:
            return

        pins = list({pin for _, pin, _ in parsed})
        try:
            user_by_pin = {u.pin: u for u in UserModel.search([('pin', 'in', pins)])}
            user_ids = [u.id for u in user_by_pin.values()]
            existing_fp = {
                (fp.user_id.id, fp.finger_id): fp
                for fp in FpModel.search([('user_id', 'in', user_ids)])
            } if user_ids else {}
        except Exception as e:
            for row, _pin, _kv in parsed:
                self._mark_error(row, e)
            return

        for row, pin, kv in parsed:
            try:
                with self.env.cr.savepoint():
                    user = user_by_pin.get(pin)
                    if not user:
                        # No USER line for this PIN yet in this batch/DB —
                        # create a placeholder the same way the old inline
                        # handler did.
                        user = UserModel.create({
                            'pin': pin, 'name': f'User {pin}',
                            'source_device_id': row.device_id.id,
                            'last_updated': fields.Datetime.now(),
                        })
                        user_by_pin[pin] = user

                    finger_id = str(kv.get('FINGERID', '0'))
                    vals = {
                        'user_id': user.id,
                        'finger_id': finger_id,
                        'valid': kv.get('Valid', '1') == '1',
                        'template': kv.get('TMP', ''),
                        'source_device_id': row.device_id.id,
                    }
                    existing = existing_fp.get((user.id, finger_id))
                    if existing:
                        existing.write(vals)
                        fp = existing
                    else:
                        fp = FpModel.create(vals)
                        existing_fp[(user.id, finger_id)] = fp
                    if row.device_id and row.device_id.id not in fp.enrolled_device_ids.ids:
                        fp.enrolled_device_ids = [(4, row.device_id.id)]
                row.write({'state': 'processed'})
            except Exception as e:
                self._mark_error(row, e)

        _logger.info('ZK OPERLOG batch: processed %d FP row(s)', len(parsed))

    # ------------------------------------------------------------------
    # FACE lines — same shape as FP
    # ------------------------------------------------------------------

    def _process_face_rows(self, rows):
        if not rows:
            return
        UserModel = self.env['zk.enrolled.user']
        FaceModel = self.env['zk.enrolled.face']

        parsed = []
        for row in rows:
            try:
                kv = _parse_kv_line(row.raw_kv)
                pin = str(kv.get('PIN', '')).strip()
                if not pin:
                    raise ValueError('Missing PIN')
                parsed.append((row, pin, kv))
            except Exception as e:
                self._mark_error(row, e)
        if not parsed:
            return

        pins = list({pin for _, pin, _ in parsed})
        try:
            user_by_pin = {u.pin: u for u in UserModel.search([('pin', 'in', pins)])}
            user_ids = [u.id for u in user_by_pin.values()]
            existing_face = {
                (face.user_id.id, face.face_id): face
                for face in FaceModel.search([('user_id', 'in', user_ids)])
            } if user_ids else {}
        except Exception as e:
            for row, _pin, _kv in parsed:
                self._mark_error(row, e)
            return

        for row, pin, kv in parsed:
            try:
                with self.env.cr.savepoint():
                    user = user_by_pin.get(pin)
                    if not user:
                        user = UserModel.create({
                            'pin': pin, 'name': f'User {pin}',
                            'source_device_id': row.device_id.id,
                            'last_updated': fields.Datetime.now(),
                        })
                        user_by_pin[pin] = user

                    face_id = str(kv.get('FACEID', '0'))
                    vals = {
                        'user_id': user.id,
                        'face_id': face_id,
                        'valid': kv.get('Valid', '1') == '1',
                        'template': kv.get('TMP', ''),
                        'source_device_id': row.device_id.id,
                    }
                    existing = existing_face.get((user.id, face_id))
                    if existing:
                        existing.write(vals)
                        face = existing
                    else:
                        face = FaceModel.create(vals)
                        existing_face[(user.id, face_id)] = face
                    if row.device_id and row.device_id.id not in face.enrolled_device_ids.ids:
                        face.enrolled_device_ids = [(4, row.device_id.id)]
                row.write({'state': 'processed'})
            except Exception as e:
                self._mark_error(row, e)

        _logger.info('ZK OPERLOG batch: processed %d FACE row(s)', len(parsed))
