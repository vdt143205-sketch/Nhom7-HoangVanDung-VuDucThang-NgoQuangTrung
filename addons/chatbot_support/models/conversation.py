# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging
import requests

_logger = logging.getLogger(__name__)

class ChatbotConversation(models.Model):
    _name = 'chatbot.conversation'
    _description = 'Lịch sử hội thoại Chatbot'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Tên cuộc hội thoại', compute='_compute_name', store=True)
    
    # User info
    partner_id = fields.Many2one('khach_hang.customer', string='Khách hàng', ondelete='cascade')
    session_id = fields.Char(string='Session ID', required=True, index=True)
    user_ip = fields.Char(string='IP Address')
    user_agent = fields.Char(string='User Agent')
    
    # Conversation metadata
    message_ids = fields.One2many('chatbot.message', 'conversation_id', string='Tin nhắn')
    message_count = fields.Integer(string='Số tin nhắn', compute='_compute_message_count', store=True)
    
    state = fields.Selection([
        ('active', 'Đang hoạt động'),
        ('closed', 'Đã đóng'),
        ('archived', 'Đã lưu trữ')
    ], string='Trạng thái', default='active', tracking=True)
    
    start_time = fields.Datetime(string='Bắt đầu', default=fields.Datetime.now, required=True)
    end_time = fields.Datetime(string='Kết thúc')
    duration = fields.Integer(string='Thời lượng (phút)', compute='_compute_duration', store=True)
    
    # Ratings
    rating = fields.Selection([
        ('1', '⭐ Rất tệ'),
        ('2', '⭐⭐ Tệ'),
        ('3', '⭐⭐⭐ Trung bình'),
        ('4', '⭐⭐⭐⭐ Tốt'),
        ('5', '⭐⭐⭐⭐⭐ Xuất sắc')
    ], string='Đánh giá')
    feedback = fields.Text(string='Phản hồi từ khách hàng')
    
    @api.depends('partner_id', 'create_date')
    def _compute_name(self):
        for record in self:
            if record.partner_id:
                record.name = f"Chat với {record.partner_id.name} - {record.create_date.strftime('%d/%m/%Y %H:%M')}"
            else:
                record.name = f"Chat khách vãng lai - {record.create_date.strftime('%d/%m/%Y %H:%M')}"
    
    @api.depends('message_ids')
    def _compute_message_count(self):
        for record in self:
            record.message_count = len(record.message_ids)
    
    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for record in self:
            if record.start_time and record.end_time:
                delta = record.end_time - record.start_time
                record.duration = int(delta.total_seconds() / 60)
            else:
                record.duration = 0
    
    def action_close_conversation(self):
        """Close the conversation"""
        self.ensure_one()
        self.write({
            'state': 'closed',
            'end_time': fields.Datetime.now()
        })

    # ── [F9] Cron: phân tích câu trả lời bị đánh giá thấp ──────────────────
    @api.model
    def _cron_analyze_low_rated_responses(self):
        """[F9] Cron hàng ngày: phân tích message bị đánh giá đến →
        Đề xuất tạo Knowledge Base draft để admin review."""
        Message = self.env['chatbot.message']
        # Lấy tất cả bot message được đánh giá thấp và chưa xử lý
        bad_msgs = Message.search([
            ('message_type', '=', 'bot'),
            ('rating', '=', 'down'),
            ('flagged_for_review', '=', False),
        ], limit=20)

        if not bad_msgs:
            _logger.info("[F9-CRON] Không có câu trả lời xấu nào cần xử lý.")
            return

        _logger.info(f"[F9-CRON] Tìm thấy {len(bad_msgs)} câu trả lời bị đánh giá xấu.")
        KB = self.env['chatbot.knowledge.base']

        for msg in bad_msgs:
            # Lấy câu hỏi liền trước của user
            user_msg = Message.search([
                ('conversation_id', '=', msg.conversation_id.id),
                ('message_type', '=', 'user'),
                ('create_date', '<', msg.create_date),
            ], order='create_date desc', limit=1)

            question_text = user_msg.content if user_msg else '(không rõ)'

            # Tạo draft KB entry
            existing_draft = KB.search([
                ('name', '=', f'[F9 Draft] {question_text[:60]}')
            ], limit=1)

            if not existing_draft:
                KB.create({
                    'name': f'[F9 Draft] {question_text[:80]}',
                    'content': (
                        f'Câu hỏi của khách: {question_text}\n\n'
                        f'Câu trả lời của bot (bị đánh giá thấp): {msg.content}\n\n'
                        f'[Yêu cầu admin bổ sung câu trả lời chính xác cho câu hỏi trên]'
                    ),
                    'keywords': question_text[:200],
                    'active': False,   # Draft — cần admin kích hoạt
                    'priority': 'high',
                })
                _logger.info(f"[F9] Tạo KB draft từ câu trả lời xấu #{msg.id}")

            # Đánh dấu đã xử lý
            msg.flagged_for_review = True

        _logger.info(f"[F9-CRON] Hoàn thành. Đã xử lý {len(bad_msgs)} câu trả lời.")


class ChatbotMessage(models.Model):
    _name = 'chatbot.message'
    _description = 'Tin nhắn Chatbot'
    _order = 'create_date asc'

    conversation_id = fields.Many2one('chatbot.conversation', string='Cuộc hội thoại', required=True, ondelete='cascade')

    message_type = fields.Selection([
        ('user', 'Từ người dùng'),
        ('bot', 'Từ chatbot'),
        ('system', 'Hệ thống')
    ], string='Loại tin nhắn', required=True)

    content = fields.Text(string='Nội dung', required=True)

    # RAG metadata (for bot messages)
    retrieved_docs = fields.Text(string='Tài liệu truy xuất (JSON)', help='IDs của knowledge base documents được sử dụng')
    confidence_score = fields.Float(string='Độ tin cậy', help='Confidence score của câu trả lời')

    # Gemini API metadata
    model_used = fields.Char(string='Model sử dụng', default='gemini-1.5-flash')
    tokens_used = fields.Integer(string='Tokens sử dụng')
    response_time = fields.Float(string='Thời gian phản hồi (s)')

    # [F9] User feedback on specific bot message ────────────────────────
    is_helpful = fields.Boolean(string='Hữu ích?')
    rating = fields.Selection([
        ('up',   '👍 Tốt'),
        ('down', '👎 Không tốt'),
    ], string='Đánh giá tin nhắn', tracking=True,
       help='KH đánh giá câu trả lời này của bot')
    flagged_for_review = fields.Boolean(
        default=False,
        help='True nếu đã được [F9] xử lý → tạo KB draft'
    )

    create_date = fields.Datetime(string='Thời gian', default=fields.Datetime.now, required=True)
