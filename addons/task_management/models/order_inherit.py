# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging
import requests
import json
from datetime import date

_logger = logging.getLogger(__name__)


class OrderTaskIntegration(models.Model):
    """Mở rộng model Order để tự động tạo Task khi tạo đơn hàng"""
    _inherit = 'khach_hang.order'

    # Liên kết với Task Management
    task_ids = fields.One2many('task.management.task', 'order_id', string='Công việc liên quan')

    # Mapping trạng thái đơn hàng -> trạng thái task
    ORDER_TO_TASK_STATE = {
        'draft': {'state': 'todo', 'progress': 0},
        'confirmed': {'state': 'todo', 'progress': 20},
        'shipping': {'state': 'in_progress', 'progress': 70},
        'done': {'state': 'done', 'progress': 100},
        'cancel': {'state': 'cancel', 'progress': 0},
    }

    def _find_available_nhan_vien(self):
        """Tìm nhân viên đang làm việc và có ít task chưa hoàn thành nhất"""
        nhan_vien_list = self.env['nhan_vien'].search([
            ('trang_thai_lam_viec', '=', 'dang_lam')
        ])
        if not nhan_vien_list:
            return False

        best = None
        min_tasks = float('inf')
        for nv in nhan_vien_list:
            task_count = self.env['task.management.task'].search_count([
                ('nhan_vien_id', '=', nv.id),
                ('state', 'in', ['todo', 'in_progress']),
            ])
            if task_count < min_tasks:
                min_tasks = task_count
                best = nv
        return best

    def _notify_telegram_new_task(self, task, nhan_vien, order):
        """External API: Gửi thông báo qua Telegram Bot khi task được tự động gán.

        Đây là ví dụ 'External API' theo yêu cầu Mức 3 (đồng bộ/gửi thông báo tới
        dịch vụ ngoài Telegram). Lỗi mạng/thiếu cấu hình không được làm hỏng luồng
        tạo đơn hàng, nên mọi thứ được bọc trong try/except và chạy độc lập.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        if not ICP.get_param('task_management.telegram_notify_enabled'):
            return

        bot_token = ICP.get_param('task_management.telegram_bot_token')
        chat_id = ICP.get_param('task_management.telegram_chat_id')
        if not bot_token or not chat_id:
            _logger.warning("Telegram notify bật nhưng thiếu bot_token/chat_id, bỏ qua.")
            return

        assignee_name = nhan_vien.ho_va_ten if nhan_vien else "Chưa gán"
        text = (
            f"🆕 *Công việc mới được tạo*\n"
            f"Đơn hàng: {order.name}\n"
            f"Khách hàng: {order.customer_id.name if order.customer_id else 'N/A'}\n"
            f"Công việc: {task.name}\n"
            f"Phụ trách: {assignee_name}\n"
            f"Hạn: {task.deadline or 'Chưa đặt'}"
        )

        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            response = requests.post(
                url,
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
                timeout=10,
            )
            if response.status_code != 200:
                _logger.error(f"Telegram API lỗi: {response.status_code} - {response.text}")
            else:
                _logger.info(f"Telegram notify OK cho task {task.id}")
        except Exception as e:
            _logger.error(f"Telegram API exception: {str(e)}")

    def _update_related_tasks(self, order_state):
        """Cập nhật trạng thái các task liên quan theo trạng thái đơn hàng"""
        if not self.task_ids:
            return
        if order_state == 'done':
            # Gọi action_done() để kích hoạt tạo care activity tự động
            self.task_ids.action_done()
        elif order_state == 'cancel':
            self.task_ids.action_cancel()
        else:
            task_vals = self.ORDER_TO_TASK_STATE.get(order_state, {})
            if task_vals:
                self.task_ids.write(task_vals)

    # ── F1: AI Smart Priority ─────────────────────────────────────────────────
    def _ai_suggest_priority(self, order, nhan_vien):
        """[F1] Dùng Gemini để đề xuất mức ưu tiên task dựa trên ngữ cảnh đơn.

        Priority:
          '0' = Thấp  |  '1' = Trung bình  |  '2' = Cao  |  '3' = Rất quan trọng

        Nếu Gemini không khả dụng → fallback rule-based (giá trị đơn + deadline).
        """
        try:
            config = self.env['chatbot.config'].search([('active', '=', True)], limit=1)
            if not config or not config.gemini_api_key:
                raise ValueError("Không có chatbot config")

            # Thu thập ngữ cảnh
            total_amount = order.total_amount or 0
            days_to_deadline = None
            if order.delivery_date:
                days_to_deadline = (order.delivery_date - date.today()).days

            order_count = 0
            if order.customer_id:
                order_count = self.env['khach_hang.order'].search_count([
                    ('customer_id', '=', order.customer_id.id),
                    ('state', '=', 'done'),
                ])

            nv_workload = 0
            if nhan_vien:
                nv_workload = self.env['task.management.task'].search_count([
                    ('nhan_vien_id', '=', nhan_vien.id),
                    ('state', 'in', ['todo', 'in_progress']),
                ])

            prompt = f"""Bạn là hệ thống ERP. Hãy phân tích và trả về mức ưu tiên cho task xử lý đơn hàng.

Thông tin đơn hàng:
- Giá trị đơn: {total_amount:,.0f} VNĐ
- Số ngày còn đến hạn giao: {days_to_deadline if days_to_deadline is not None else 'Chưa đặt'}
- Số đơn đã hoàn thành của khách hàng này: {order_count}
- Workload hiện tại của nhân viên được gán: {nv_workload} task đang mở

Quy tắc ưu tiên:
- '3' (Rất quan trọng): Đơn > 50 triệu VNĐ HOẶC hạn dưới 2 ngày
- '2' (Cao): Đơn 10-50 triệu VNĐ HOẶC hạn 2-5 ngày
- '1' (Trung bình): Đơn 2-10 triệu VNĐ HOẶC hạn 5-14 ngày
- '0' (Thấp): Đơn dưới 2 triệu VNĐ HOẶC hạn trên 14 ngày

Chỉ trả về MỘT ký tự số duy nhất: 0, 1, 2, hoặc 3. Không giải thích."""

            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"gemini-2.0-flash:generateContent?key={config.gemini_api_key}")
            payload = {"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"maxOutputTokens": 5, "temperature": 0.1}}

            resp = requests.post(url, json=payload, timeout=8)
            resp.raise_for_status()
            ai_text = (resp.json()
                       .get('candidates', [{}])[0]
                       .get('content', {})
                       .get('parts', [{}])[0]
                       .get('text', '').strip())

            if ai_text in ('0', '1', '2', '3'):
                _logger.info(f"[F1-AI] Priority đề xuất bởi AI: {ai_text} cho đơn {order.name}")
                return ai_text
            _logger.warning(f"[F1-AI] Gemini trả về không hợp lệ: '{ai_text}', dùng fallback")

        except Exception as e:
            _logger.warning(f"[F1-AI] Gemini không khả dụng ({e}), dùng rule-based fallback")

        # ── Fallback: rule-based ──────────────────────────────────────────────
        total = order.total_amount or 0
        days = None
        if order.delivery_date:
            days = (order.delivery_date - date.today()).days

        if total > 50_000_000 or (days is not None and days < 2):
            return '3'
        if total > 10_000_000 or (days is not None and days < 5):
            return '2'
        if total > 2_000_000 or (days is not None and days < 14):
            return '1'
        return '0'

    # ── Task creation ─────────────────────────────────────────────────────────
    @api.model
    def create(self, vals):
        """Tự động tạo Task khi tạo đơn hàng mới"""
        _logger.info("========== ORDER CREATE START ==========")
        _logger.info(f"Creating order with vals: {vals}")

        order = super(OrderTaskIntegration, self).create(vals)
        _logger.info(f"Order created: ID={order.id}, Name={order.name}")

        # Tìm nhân viên đang làm việc và ít task nhất để auto-gán
        nhan_vien = self._find_available_nhan_vien()

        # [F1] AI đề xuất priority
        smart_priority = order._ai_suggest_priority(order, nhan_vien)

        # Tạo task tự động cho đơn hàng
        task_vals = {
            'name': f"Xử lý đơn hàng: {order.name}",
            'description': f"""
                <p><strong>Thông tin đơn hàng:</strong></p>
                <ul>
                    <li>Mã đơn hàng: {order.name}</li>
                    <li>Khách hàng: {order.customer_id.name if order.customer_id else 'N/A'}</li>
                    <li>Tổng tiền: {order.total_amount:,.0f} VNĐ</li>
                </ul>
                <p><strong>Công việc cần thực hiện:</strong></p>
                <ul>
                    <li>Xác nhận đơn hàng</li>
                    <li>Chuẩn bị hàng hóa</li>
                    <li>Giao hàng cho khách</li>
                </ul>
            """,
            'partner_id': order.customer_id.id if order.customer_id else False,
            'order_id': order.id,
            'nhan_vien_id': nhan_vien.id if nhan_vien else False,
            'deadline': order.delivery_date,
            'priority': smart_priority,  # [F1] AI-generated priority
            'state': 'todo',
            'progress': 0,
        }
        _logger.info(f"Creating task with vals: {task_vals}")

        task = self.env['task.management.task'].create(task_vals)
        _logger.info(
            f"Task created: ID={task.id}, Name={task.name}, "
            f"Priority={smart_priority}, "
            f"Assigned to: {nhan_vien.ho_va_ten if nhan_vien else 'Chưa gán'}"
        )

        order._notify_telegram_new_task(task, nhan_vien, order)

        _logger.info("========== ORDER CREATE END ==========")
        return order

    def action_confirm(self):
        """Override: Xác nhận đơn hàng và cập nhật task sang In Progress"""
        result = super(OrderTaskIntegration, self).action_confirm()
        for order in self:
            order._update_related_tasks('confirmed')
        return result

    def action_ship(self):
        """Override: Giao hàng và cập nhật task (tiến độ 70%)"""
        result = super(OrderTaskIntegration, self).action_ship()
        for order in self:
            order._update_related_tasks('shipping')
        return result

    def action_done(self):
        """Override: Hoàn thành đơn hàng và cập nhật task sang Done"""
        result = super(OrderTaskIntegration, self).action_done()
        for order in self:
            order._update_related_tasks('done')
        return result

    def action_cancel(self):
        """Override: Hủy đơn hàng và cập nhật task sang Cancel"""
        result = super(OrderTaskIntegration, self).action_cancel()
        for order in self:
            order._update_related_tasks('cancel')
        return result
