# -*- coding: utf-8 -*-
###############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Cybrosys Techno Solutions (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from odoo import fields, models


class DbBackupLog(models.Model):
    """Stores the result of every automatic backup attempt so failures are
       visible from the UI instead of only in the server log."""
    _name = 'db.backup.log'
    _description = 'Database Backup Log'
    _order = 'create_date desc'
    _rec_name = 'backup_filename'

    config_id = fields.Many2one('db.backup.configure',
                                string='Backup Configuration',
                                required=True, ondelete='cascade')
    db_name = fields.Char(string='Database Name')
    backup_destination = fields.Selection([
        ('local', 'Local Storage'),
        ('google_drive', 'Google Drive'),
        ('ftp', 'FTP'),
        ('sftp', 'SFTP'),
        ('dropbox', 'Dropbox'),
        ('onedrive', 'Onedrive'),
        ('next_cloud', 'Next Cloud'),
        ('amazon_s3', 'Amazon S3')
    ], string='Destination')
    backup_filename = fields.Char(string='Backup Filename')
    state = fields.Selection([
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string='Status', default='running', required=True)
    message = fields.Text(string='Message',
                          help='Error details when the backup attempt fails')
