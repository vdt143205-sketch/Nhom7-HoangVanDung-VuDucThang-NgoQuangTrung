# -*- coding: utf-8 -*-
"""
F3: Auto Escalation — Tự động cảnh báo và tái phân công task quá hạn.

Cron chạy mỗi ngày lúc 8:00 sáng:
- Task quá hạn > WARN_DAYS  → gửi cảnh báo Telegram cho quản lý
- Task quá hạn > REASSIGN_DAYS → tự động reassign sang NV rảnh hơn
"""
from odoo import models, fields, api
import logging
import requests
from datetime import date

_logger = logging.getLogger(__name__)

# Ngưỡng cảnh báo (ngày quá hạn)
WARN_DAYS = 1       # sau 1 ngày quá hạn → Telegram cảnh báo
REASSIGN_DAYS = 5   # sau 5 ngày quá hạn → tự reassign


class TaskEscalation(models.Model):
    """Mixin chứa logic leo thang (escalation) cho task quá hạn."""
    _inherit = 'task.management.task'

    days_overdue = fields.Integer(
        string='Số ngày quá hạn',
        compute='_compute_days_overdue',
        store=False,
    )
    escalation_sent = fields.Boolean(
        string='Đã gửi cảnh báo',
        default=False,
        help='True nếu đã gửi Telegram cảnh báo quá hạn lần đầu',
    )

    @api.depends('deadline', 'state')
    def _compute_days_overdue(self):
        today = date.today()
        for task in self:
            if task.deadline and task.state in ('todo', 'in_progress'):
                delta = (today - task.deadline).days
                task.days_overdue = max(delta, 0)
            else:
                task.days_overdue = 0

    # ── Cron entry point ──────────────────────────────────────────────────────
    @api.model
    def _cron_check_overdue_tasks(self):
        """[F3] Scheduled Action — chạy hàng ngày lúc 8:00 AM.

        1. Lấy tất cả task đang mở đã quá hạn.
        2. Gửi Telegram cảnh báo nếu quá WARN_DAYS.
        3. Tự reassign nếu quá REASSIGN_DAYS.
        """
        today = date.today()
        overdue_tasks = self.search([
            ('deadline', '<', today),
            ('state', 'in', ['todo', 'in_progress']),
        ])

        if not overdue_tasks:
            _logger.info("[F3-CRON] Không có task nào quá hạn hôm nay.")
            return

        _logger.info(f"[F3-CRON] Tìm thấy {len(overdue_tasks)} task quá hạn.")

        for task in overdue_tasks:
            days = (today - task.deadline).days

            # Gửi cảnh báo Telegram lần đầu
            if days >= WARN_DAYS and not task.escalation_sent:
                self._send_escalation_alert(task, days)
                task.escalation_sent = True

            # Tái phân công nếu quá hạn lâu
            if days >= REASSIGN_DAYS:
                self._auto_reassign(task, days)

    # ── Gửi cảnh báo Telegram ─────────────────────────────────────────────────
    def _send_escalation_alert(self, task, days_overdue):
        """Gửi thông báo quá hạn tới Telegram group quản lý."""
        ICP = self.env['ir.config_parameter'].sudo()
        if not ICP.get_param('task_management.telegram_notify_enabled'):
            _logger.info(f"[F3] Telegram tắt, bỏ qua cảnh báo task {task.id}")
            return

        bot_token = ICP.get_param('task_management.telegram_bot_token')
        chat_id = ICP.get_param('task_management.telegram_chat_id')
        if not bot_token or not chat_id:
            _logger.warning("[F3] Thiếu bot_token/chat_id, không gửi cảnh báo.")
            return

        nv_name = task.nhan_vien_id.ho_va_ten if task.nhan_vien_id else "Chưa gán"
        kh_name = task.partner_id.name if task.partner_id else "N/A"
        deadline_str = str(task.deadline) if task.deadline else "Không có"

        text = (
            f"⚠️ *CẢNH BÁO: Task quá hạn {days_overdue} ngày!*\n"
            f"📋 Task: {task.name}\n"
            f"👤 Nhân viên: {nv_name}\n"
            f"🧑‍💼 Khách hàng: {kh_name}\n"
            f"📅 Hạn chót: {deadline_str}\n"
            f"📊 Trạng thái: {task.state}\n"
            f"⏰ Quá hạn: {days_overdue} ngày"
        )

        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(
                url,
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                timeout=10,
            )
            if resp.status_code == 200:
                _logger.info(f"[F3] Đã gửi cảnh báo Telegram cho task {task.id}")
            else:
                _logger.error(f"[F3] Telegram lỗi: {resp.status_code} - {resp.text}")
        except Exception as e:
            _logger.error(f"[F3] Telegram exception: {e}")

    # ── Tái phân công tự động ─────────────────────────────────────────────────
    def _auto_reassign(self, task, days_overdue):
        """Tự động chuyển task sang NV khác rảnh hơn (nếu có)."""
        # Tìm NV rảnh nhất (không phải NV hiện tại)
        nhan_vien_list = self.env['nhan_vien'].search([
            ('trang_thai_lam_viec', '=', 'dang_lam'),
        ])
        if not nhan_vien_list:
            _logger.info(f"[F3] Không có NV nào đang làm việc để reassign task {task.id}")
            return

        best, min_tasks = None, float('inf')
        for nv in nhan_vien_list:
            if nv == task.nhan_vien_id:
                continue  # không reassign cho chính người đang phụ trách
            task_count = self.env['task.management.task'].search_count([
                ('nhan_vien_id', '=', nv.id),
                ('state', 'in', ['todo', 'in_progress']),
            ])
            if task_count < min_tasks:
                min_tasks, best = task_count, nv

        if not best:
            _logger.info(f"[F3] Không tìm được NV thay thế cho task {task.id}")
            return

        old_nv = task.nhan_vien_id.ho_va_ten if task.nhan_vien_id else "Chưa gán"
        new_nv = best.ho_va_ten

        task.write({'nhan_vien_id': best.id})
        task.message_post(
            body=(
                f"🔄 <b>[Tự động - F3 Escalation]</b> Task đã quá hạn {days_overdue} ngày.<br/>"
                f"Tái phân công từ <b>{old_nv}</b> → <b>{new_nv}</b>."
            ),
        )
        _logger.info(
            f"[F3] Task {task.id} reassigned: {old_nv} → {new_nv} "
            f"(quá hạn {days_overdue} ngày)"
        )

        # Gửi thông báo Telegram về việc reassign
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('task_management.telegram_notify_enabled'):
            bot_token = ICP.get_param('task_management.telegram_bot_token')
            chat_id = ICP.get_param('task_management.telegram_chat_id')
            if bot_token and chat_id:
                text = (
                    f"🔄 *Task đã được tái phân công tự động*\n"
                    f"📋 Task: {task.name}\n"
                    f"❌ Từ: {old_nv}\n"
                    f"✅ Sang: {new_nv}\n"
                    f"⏰ Lý do: quá hạn {days_overdue} ngày"
                )
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                        timeout=10,
                    )
                except Exception as e:
                    _logger.error(f"[F3] Telegram reassign notify lỗi: {e}")
