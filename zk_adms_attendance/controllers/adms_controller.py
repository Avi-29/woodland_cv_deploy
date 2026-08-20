"""
ZKTeco ADMS Push Protocol Controller
=====================================
Tables the device POSTs to /iclock/cdata:

  table=ATTLOG    attendance punches
  table=OPERLOG   operational events: OPLOG (device events), USER, FP, FACE,
                  USERPIC (employee photo), DEL_USER, DEL_FP
  table=BIODATA   biometric template blobs (face/FP) — separate from OPERLOG
  table=ENROLL_FP fingerprint enroll notification (some firmware versions)

Heartbeat / command dispatch:
  GET  /iclock/getrequest?SN=...   → returns C:<id>:<cmd> if queued, else OK
  POST /iclock/devicecmd?SN=...    → ACK after executing command
"""

import logging
from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)
OK   = 'OK'
FAIL = 'FAIL'


def plain(body: str, status: int = 200) -> Response:
    return Response(body, status=status, mimetype='text/plain; charset=utf-8')


def _parse_kv_line(line: str) -> dict:
    """Parse a tab-separated KEY=VALUE string into a dict."""
    result = {}
    for token in line.strip().split('\t'):
        if '=' in token:
            k, _, v = token.partition('=')
            result[k.strip()] = v.strip()
    return result


def _get_or_create_device(sn, ip=None, firmware=None, push_version=None):
    return request.env['zk.device'].sudo().get_or_create_device(
        serial_number=sn,
        ip=ip or request.httprequest.remote_addr,
        firmware=firmware or '',
        push_version=push_version or '',
    )


class AdmsController(http.Controller):

    # ------------------------------------------------------------------
    # 1.  GET /iclock/cdata  —  device initialisation
    # ------------------------------------------------------------------
    @http.route('/iclock/cdata', type='http', auth='public', methods=['GET'], csrf=False)
    def adms_get_options(self, **kw):
        sn = kw.get('SN', '').strip()
        if not sn:
            return plain(FAIL, 400)
        try:
            device = _get_or_create_device(
                sn,
                firmware=kw.get('DeviceType') or kw.get('OEMVendor'),
                push_version=kw.get('pushver'),
            )
            lines = [
                f'GET OPTION FROM: {sn}',
                'ATTLOGStamp=9999',
                'OPERLOGStamp=9999',
                'ATTPHOTOStamp=9999',
                'ErrorDelay=30',
                'Delay=10',
                'TransTimes=00:00;14:05',
                'TransInterval=1',
                'TransFlag=TransData AttLog\tOpLog\tEnrollUser\tChgUser\tEnrollFP\tChgFP\tFACE',
                f'TimeZone={device.server_tz_offset}',
                f'Realtime={1 if device.realtime else 0}',
                'Encrypt=0',
                'ServerVer=2.4.1',
                'PushProtVer=2.4.1',
                'PushOptionsFlag=1',
                f'HeartBeatInterval={device.heartbeat_interval}',
                'NK=0', 'PK=0', 'RE=0',
            ]
            return plain('\n'.join(lines))
        except Exception as e:
            _logger.exception('ZK ADMS INIT sn=%s: %s', sn, e)
            return plain(FAIL, 500)

    # ------------------------------------------------------------------
    # 2.  POST /iclock/cdata  —  data push dispatcher
    # ------------------------------------------------------------------
    @http.route('/iclock/cdata', type='http', auth='public', methods=['POST'], csrf=False)
    def adms_post_data(self, **kw):
        sn    = kw.get('SN', '').strip()
        table = kw.get('table', '').strip().upper()
        if not sn:
            return plain(FAIL, 400)

        try:
            device = _get_or_create_device(sn)
        except Exception as e:
            _logger.exception('ZK ADMS: cannot resolve device SN=%s: %s', sn, e)
            return plain(FAIL, 500)

        body = request.httprequest.data.decode('utf-8', errors='replace')
        _logger.debug('ZK ADMS POST table=%s SN=%s body_len=%d', table, sn, len(body))

        if table == 'ATTLOG':
            return self._handle_attlog(device, body)
        if table == 'OPERLOG':
            return self._handle_operlog(device, body)
        if table == 'BIODATA':
            return self._handle_biodata(device, body)
        if table in ('ENROLL_FP', 'ENROLL_USER', 'CHGUSER', 'CHGFP'):
            # Enrollment-complete notifications — no body to parse, just ACK
            _logger.debug('ZK ADMS %s notification from %s', table, sn)
            return plain(OK)

        _logger.debug('ZK ADMS: unhandled table=%s from SN=%s', table, sn)
        return plain(OK)

    # ── ATTLOG ──────────────────────────────────────────────────────────────
    def _handle_attlog(self, device, body: str):
        """
        ATTLOG line format (tab-separated):
          PIN  Time  Status  Verify  WorkCode  Reserved  [extra fields...]

        Real example from your device:
          2\t2026-03-31 14:13:37\t255\t15\t0\t0\t0\t0\t0\t0\t7
        """
        records = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) < 2:
                continue
            records.append({
                'pin':      parts[0],
                'time':     parts[1] if len(parts) > 1 else '',
                'status':   parts[2] if len(parts) > 2 else '0',
                'verify':   parts[3] if len(parts) > 3 else '1',
                'workcode': parts[4] if len(parts) > 4 else '',
                'reserved': parts[5] if len(parts) > 5 else '',
                '_raw':     line,
            })
        count = request.env['zk.attendance.log'].sudo().create_from_adms(device, records)
        _logger.debug('ZK ATTLOG: %d new punches from %s', count, device.serial_number)
        return plain(f'OK: {len(records)}')

    # ── OPERLOG ─────────────────────────────────────────────────────────────
    def _handle_operlog(self, device, body: str):
        """
        OPERLOG line types seen in the wild:

          OPLOG <id>\t<op>\t<datetime>\t<uid>\t<...>
              — device operation event (door open, admin login, etc.) → log only

          USER  PIN=n\tName=...\tPrivilege=n\t...
              — user info pushed after enrolment or edit

          FP    PIN=n\tFINGERID=n\tValid=1\tTMP=<b64>
              — fingerprint template

          FACE  PIN=n\tFACEID=n\tValid=1\tTMP=<b64>
              — face template

          USERPIC  PIN=n\tFileName=n.jpg\tSize=n\tContent=<b64-jpeg>
              — employee photo (we store on hr.employee)

          DEL_USER  PIN=n
          DEL_FP    PIN=n\tFINGERID=n
        """
        RawModel = request.env['zk.operlog.raw'].sudo()
        UserModel = request.env['zk.enrolled.user'].sudo()
        FpModel   = request.env['zk.enrolled.fp'].sudo()

        # USER/FP/FACE lines are the bulk-volume ones (thousands on a
        # get_all_userinfo pull) — queue them for the batch cron instead of
        # upserting one at a time inside this request. Everything else
        # (device events, photo, delete notifications) is low-volume and
        # stays handled inline below.
        bulk_vals = []

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue

            # First token is the record type
            parts = line.split(None, 1)  # split on first whitespace
            record_type = parts[0].strip().upper()
            rest = parts[1] if len(parts) > 1 else ""

            # ── Device operation log — just record in debug log ──────────────
            if record_type == 'OPLOG':
                _logger.debug('ZK OPLOG from %s: %s', device.serial_number, line[:120])
                continue

            if record_type in ('USER', 'FP', 'FACE', 'UFACE'):
                bulk_vals.append({
                    'device_id': device.id,
                    'device_serial': device.serial_number,
                    'record_type': 'FACE' if record_type == 'UFACE' else record_type,
                    'raw_kv': rest,
                })
                continue

            kv = _parse_kv_line(rest)

            if record_type == 'USERPIC':
                # Store photo on the linked hr.employee
                self._handle_userpic(device, kv)

            elif record_type == 'DEL_USER':
                pin = str(kv.get('PIN', '')).strip()
                _logger.debug('ZK OPERLOG DEL_USER PIN=%s from %s (kept in Odoo)', pin, device.serial_number)

            elif record_type == 'DEL_FP':
                pin    = str(kv.get('PIN', '')).strip()
                finger = str(kv.get('FINGERID', '0')).strip()
                try:
                    with request.env.cr.savepoint():
                        user = UserModel.search([('pin', '=', pin)], limit=1)
                        if user:
                            fp = FpModel.search([('user_id', '=', user.id), ('finger_id', '=', finger)], limit=1)
                            if fp:
                                fp.write({'valid': False})
                    _logger.debug('ZK OPERLOG DEL_FP PIN=%s F%s marked invalid', pin, finger)
                except Exception as e:
                    # Unguarded, a failure here would abort the whole request
                    # and lose every bulk_vals row queued from earlier lines
                    # in this same push (they're only create()'d at the end).
                    _logger.warning('ZK OPERLOG DEL_FP error PIN=%s F%s: %s', pin, finger, e)

            else:
                _logger.debug('ZK OPERLOG unknown type=%s from %s', record_type, device.serial_number)

        if bulk_vals:
            RawModel.create(bulk_vals)
            _logger.info('ZK OPERLOG: queued %d USER/FP/FACE row(s) from %s for batch processing',
                         len(bulk_vals), device.serial_number)

        return plain(OK)

    def _handle_userpic(self, device, kv: dict):
        """Store base64 JPEG from USERPIC push onto the linked hr.employee record."""
        pin     = str(kv.get('PIN', '')).strip()
        content = kv.get('Content', '')
        if not pin or not content:
            return
        try:
            with request.env.cr.savepoint():
                employee = request.env['hr.employee'].sudo().get_by_badge(pin)
                if employee:
                    # This write clears photo_synced_device_ids on every device
                    # (see hr_employee.py's write() override) since the photo is
                    # changing — then immediately mark the source device as
                    # already having it, so the next sync only pushes it out to
                    # every *other* device instead of redundantly back to this one.
                    employee.write({'image_1920': content})
                    user = request.env['zk.enrolled.user'].sudo().search([('pin', '=', pin)], limit=1)
                    if user:
                        user.photo_synced_device_ids = [(4, device.id)]
                    _logger.debug('ZK USERPIC: stored photo for employee %s (PIN=%s)', employee.name, pin)
                else:
                    _logger.debug('ZK USERPIC: no employee found for PIN=%s', pin)
        except Exception as e:
            _logger.warning('ZK USERPIC: failed to save photo PIN=%s: %s', pin, e)

    # ── BIODATA ─────────────────────────────────────────────────────────────
    def _handle_biodata(self, device, body: str):
        """
        BIODATA line format (tab-separated KEY=VALUE):
          Pin=2  No=0  Index=0  Valid=1  Duress=0  Type=9
          MajorVer=40  MinorVer=1  Format=0  Tmp=<base64>

        Type values:
          1  = fingerprint
          9  = 3D face / infrared face (UFS series)
          10 = palm
        """
        UserModel = request.env['zk.enrolled.user'].sudo()
        count = 0
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)

            record_type = parts[0].strip().upper()
            rest = parts[1] if len(parts) > 1 else ""
            if record_type != 'BIODATA':
                continue
            kv = _parse_kv_line(rest)
            try:
                with request.env.cr.savepoint():
                    UserModel.upsert_from_biodata(device, kv)
                count += 1
            except Exception as e:
                # Without the savepoint, a DB-level failure here (bad insert,
                # constraint violation, etc.) leaves the whole transaction
                # "aborted" at the Postgres level - every later query on this
                # same request/cursor (even an unrelated SELECT for the next
                # line) then fails with "current transaction is aborted,
                # commands ignored until end of transaction block" until a
                # rollback happens. cr.savepoint() rolls back only this one
                # line's work on failure, so the rest of the batch still runs.
                _logger.warning('ZK BIODATA parse error: %s | line: %s', e, line[:120])

        _logger.info('ZK BIODATA: processed %d record(s) from %s', count, device.serial_number)
        return plain(OK)

    # ------------------------------------------------------------------
    # 3.  GET /iclock/getrequest  —  heartbeat + command dispatch
    # ------------------------------------------------------------------
    @http.route('/iclock/getrequest', type='http', auth='public', methods=['GET'], csrf=False)
    def adms_get_request(self, **kw):
        sn = kw.get('SN', '').strip()
        if not sn:
            return plain(FAIL, 400)
        try:
            device = request.env['zk.device'].sudo().search(
                [('serial_number', '=', sn)], limit=1
            )
            if not device:
                return plain(OK)
            device.mark_heartbeat()
            cmd = request.env['zk.device.command'].sudo().next_for_device(device)
            if cmd:
                response_body = f'C:{cmd.cmd_id}:{cmd.command_string}'
                _logger.debug('ZK CMD dispatch to %s: %s', sn, response_body[:120])
                return plain(response_body)
        except Exception as e:
            _logger.warning('ZK getrequest error sn=%s: %s', sn, e)
        return plain(OK)

    # ------------------------------------------------------------------
    # 4.  POST /iclock/devicecmd  —  command ACK
    # ------------------------------------------------------------------
    @http.route('/iclock/devicecmd', type='http', auth='public', methods=['POST'], csrf=False)
    def adms_device_cmd(self, **kw):
        sn   = kw.get('SN', '').strip()
        body = request.httprequest.data.decode('utf-8', errors='replace').strip()
        try:
            device = request.env['zk.device'].sudo().search(
                [('serial_number', '=', sn)], limit=1
            )
            if device and body:
                request.env['zk.device.command'].sudo().ack_from_device(device, body)
        except Exception as e:
            _logger.warning('ZK devicecmd ACK error sn=%s: %s', sn, e)
        return plain(OK)

    # ------------------------------------------------------------------
    # 5.  Health check
    # ------------------------------------------------------------------
    @http.route('/iclock/ping', type='http', auth='public', methods=['GET'], csrf=False)
    def adms_ping(self, **kw):
        return plain(OK)
