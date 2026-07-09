# -*- coding: utf-8 -*-
"""
F10: AI Dashboard — Model tổng hợp dữ liệu & tóm tắt AI.

Cron hàng ngày gọi Gemini để sinh bản tóm tắt tình hình doanh nghiệp
bằng tiếng Việt, lưu vào chatbot.ai.summary để hiển thị trên dashboard.
"""
from odoo import models, fields, api
import logging
import requests
from datetime import date, timedelta

_logger = logging.getLogger(__name__)


class AISummary(models.Model):
    """Lưu bản tóm tắt AI hàng ngày về tình hình doanh nghiệp."""
    _name = 'chatbot.ai.summary'
    _description = 'AI Daily Summary'
    _order = 'summary_date desc'

    summary_date = fields.Date(string='Ngày', default=fields.Date.context_today, index=True)
    summary_text = fields.Text(string='Nội dung tóm tắt')

    # KPI Snapshot
    total_orders_today = fields.Integer(string='Đơn hàng hôm nay')
    total_revenue_today = fields.Float(string='Doanh thu hôm nay')
    overdue_tasks = fields.Integer(string='Task quá hạn')
    high_churn_customers = fields.Integer(string='KH rủi ro cao')
    negative_feedbacks = fields.Integer(string='Phản hồi tiêu cực')
    open_tasks = fields.Integer(string='Task đang mở')

    model_used = fields.Char(string='Model AI', default='gemini-2.0-flash')
    generation_time = fields.Float(string='Thời gian sinh (s)')

    @api.model
    def _cron_generate_daily_summary(self):
        """[F10] Cron hàng ngày — thu thập KPI và gọi Gemini tóm tắt."""
        import time
        today = date.today()

        # Tránh sinh lại nếu đã có
        existing = self.search([('summary_date', '=', today)], limit=1)
        if existing:
            _logger.info("[F10-CRON] Summary đã tồn tại cho hôm nay, bỏ qua.")
            return

        start = time.time()

        # ── Thu thập KPI ──────────────────────────────────────────────────────
        Order = self.env['khach_hang.order'].sudo()
        Task = self.env['task.management.task'].sudo()
        Customer = self.env['khach_hang.customer'].sudo()
        Feedback = self.env['khach_hang.feedback'].sudo()

        # Đơn hàng hôm nay
        today_orders = Order.search([
            ('create_date', '>=', str(today)),
            ('state', '!=', 'cancel'),
        ])
        total_orders_today = len(today_orders)
        total_revenue_today = sum(o.total_amount or 0 for o in today_orders)

        # Task quá hạn
        overdue_tasks = Task.search_count([
            ('deadline', '<', today),
            ('state', 'in', ['todo', 'in_progress']),
        ])

        # Task đang mở
        open_tasks = Task.search_count([
            ('state', 'in', ['todo', 'in_progress']),
        ])

        # KH rủi ro cao
        high_churn = Customer.search_count([('churn_risk_label', '=', 'high')])

        # Phản hồi tiêu cực trong 7 ngày
        week_ago = str(today - timedelta(days=7))
        neg_feedbacks = Feedback.search_count([
            ('sentiment', '=', 'negative'),
            ('create_date', '>=', week_ago),
        ])

        # Top NV theo task hoàn thành
        all_nv = self.env['nhan_vien'].sudo().search([
            ('trang_thai_lam_viec', '=', 'dang_lam')
        ], limit=5)
        nv_stats = []
        for nv in all_nv:
            done = Task.search_count([
                ('nhan_vien_id', '=', nv.id),
                ('state', '=', 'done'),
            ])
            nv_stats.append(f"  - {nv.ho_va_ten}: {done} task hoàn thành")

        # Top sản phẩm bán chạy (theo số đơn có chứa sản phẩm đó)
        products = self.env['khach_hang.product'].sudo().search([], limit=3)
        prod_stats = [f"  - {p.name}" for p in products]

        # ── Gọi Gemini sinh tóm tắt ───────────────────────────────────────────
        summary_text = self._generate_ai_summary(
            today=today,
            total_orders_today=total_orders_today,
            total_revenue_today=total_revenue_today,
            overdue_tasks=overdue_tasks,
            open_tasks=open_tasks,
            high_churn=high_churn,
            neg_feedbacks=neg_feedbacks,
            nv_stats=nv_stats,
            prod_stats=prod_stats,
        )

        gen_time = round(time.time() - start, 2)

        self.create({
            'summary_date': today,
            'summary_text': summary_text,
            'total_orders_today': total_orders_today,
            'total_revenue_today': total_revenue_today,
            'overdue_tasks': overdue_tasks,
            'open_tasks': open_tasks,
            'high_churn_customers': high_churn,
            'negative_feedbacks': neg_feedbacks,
            'generation_time': gen_time,
        })
        _logger.info(f"[F10-CRON] Summary tạo xong trong {gen_time}s")

    def _generate_ai_summary(self, today, total_orders_today, total_revenue_today,
                              overdue_tasks, open_tasks, high_churn, neg_feedbacks,
                              nv_stats, prod_stats):
        """Gọi Gemini sinh bản tóm tắt tình hình kinh doanh."""
        config = self.env['chatbot.config'].sudo().search(
            [('active', '=', True)], limit=1
        )
        if not config or not config.gemini_api_key:
            return self._fallback_summary(
                today, total_orders_today, total_revenue_today,
                overdue_tasks, open_tasks, high_churn, neg_feedbacks
            )

        nv_block = '\n'.join(nv_stats) if nv_stats else '  (chưa có dữ liệu)'
        prod_block = '\n'.join(prod_stats) if prod_stats else '  (chưa có dữ liệu)'

        prompt = f"""Bạn là trợ lý AI phân tích kinh doanh. Hãy viết bản tóm tắt tình hình hôm nay
cho ban quản lý dựa trên dữ liệu thực tế sau. Viết ngắn gọn, súc tích, chuyên nghiệp bằng tiếng Việt.
Dùng emoji để làm nổi bật điểm quan trọng.

=== DỮ LIỆU NGÀY {today} ===
📦 Đơn hàng mới hôm nay: {total_orders_today}
💰 Doanh thu hôm nay: {total_revenue_today:,.0f} VNĐ
⚠️  Task quá hạn: {overdue_tasks}
📋 Task đang mở: {open_tasks}
🔴 Khách hàng rủi ro cao: {high_churn}
😠 Phản hồi tiêu cực (7 ngày): {neg_feedbacks}

Nhân viên nổi bật:
{nv_block}

Sản phẩm trong hệ thống:
{prod_block}

Hãy:
1. Nhận xét tổng quan tình hình (2-3 câu)
2. Nêu 2-3 điểm cần chú ý/hành động ngay
3. Đề xuất 1-2 hành động cụ thể cho hôm nay"""

        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"gemini-2.0-flash:generateContent?key={config.gemini_api_key}")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0.4},
            }
            resp = requests.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            ai_text = (
                resp.json()
                .get('candidates', [{}])[0]
                .get('content', {})
                .get('parts', [{}])[0]
                .get('text', '').strip()
            )
            if ai_text:
                return ai_text
        except Exception as e:
            _logger.error(f"[F10] Gemini summary lỗi: {e}")

        return self._fallback_summary(
            today, total_orders_today, total_revenue_today,
            overdue_tasks, open_tasks, high_churn, neg_feedbacks
        )

    def _fallback_summary(self, today, orders, revenue, overdue, open_t, churn, neg):
        return (
            f"📊 Tóm tắt ngày {today}:\n"
            f"• Đơn hàng: {orders} | Doanh thu: {revenue:,.0f} VNĐ\n"
            f"• Task quá hạn: {overdue} | Task đang mở: {open_t}\n"
            f"• KH rủi ro cao: {churn} | Phản hồi tiêu cực: {neg}"
        )

    @api.model
    def get_latest_summary(self):
        """API cho dashboard — trả về summary mới nhất."""
        summary = self.search([], limit=1)
        if not summary:
            return None
        return {
            'date': str(summary.summary_date),
            'text': summary.summary_text,
            'kpi': {
                'orders_today': summary.total_orders_today,
                'revenue_today': summary.total_revenue_today,
                'overdue_tasks': summary.overdue_tasks,
                'open_tasks': summary.open_tasks,
                'high_churn': summary.high_churn_customers,
                'neg_feedbacks': summary.negative_feedbacks,
            }
        }
