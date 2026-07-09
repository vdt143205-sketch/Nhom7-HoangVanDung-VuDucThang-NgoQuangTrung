# -*- coding: utf-8 -*-
import logging
import requests
from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ChatbotConfig(models.Model):
    _name = 'chatbot.config'
    _description = 'Cấu hình Chatbot'
    _rec_name = 'name'

    name = fields.Char(string='Tên cấu hình', default='Chatbot Configuration', required=True)
    
    # Gemini API Settings
    gemini_api_key = fields.Char(string='Gemini API Key', required=True, 
                                   help='Lấy tại https://aistudio.google.com/app/apikey')
    gemini_model = fields.Selection([
        ('gemini-2.5-flash', 'Gemini 2.5 Flash (Khuyến nghị)'),
        ('gemini-2.0-flash', 'Gemini 2.0 Flash'),
        ('gemini-2.5-pro', 'Gemini 2.5 Pro (Chất lượng cao)'),
    ], string='Model LLM', default='gemini-2.5-flash', required=True)
    
    embedding_model = fields.Char(string='Embedding Model', default='models/gemini-embedding-001', required=True)
    
    # RAG Settings
    top_k_results = fields.Integer(string='Top K Documents', default=3, 
                                     help='Số lượng documents truy xuất từ vector DB')
    similarity_threshold = fields.Float(string='Ngưỡng tương đồng', default=0.5,
                                         help='Chỉ lấy documents có similarity > threshold')
    
    # Chatbot Behavior
    system_prompt = fields.Text(string='System Prompt', default="""Bạn là trợ lý AI thông minh của công ty, chuyên tư vấn khách hàng.

Nhiệm vụ của bạn:
- Trả lời câu hỏi của khách hàng một cách chính xác, lịch sự và chuyên nghiệp
- Sử dụng thông tin từ knowledge base được cung cấp để trả lời
- Nếu không có thông tin trong knowledge base, hãy thừa nhận và đề xuất liên hệ nhân viên
- Luôn giữ thái độ thân thiện và hỗ trợ tối đa

Quy tắc:
- Trả lời bằng tiếng Việt
- Ngắn gọn, súc tích nhưng đầy đủ thông tin
- Không bịa đặt thông tin không có trong knowledge base
- Nếu khách hàng hỏi về giá cả, chính sách, hãy dựa vào dữ liệu chính xác""")
    
    welcome_message = fields.Text(string='Tin nhắn chào mừng', 
                                    default='Xin chào! Tôi là trợ lý AI. Tôi có thể giúp gì cho bạn? 😊')
    
    fallback_message = fields.Text(string='Tin nhắn dự phòng',
                                     default='Xin lỗi, tôi chưa có đủ thông tin để trả lời câu hỏi này. Bạn có thể liên hệ nhân viên hỗ trợ để được tư vấn chi tiết hơn.')
    
    # Performance Settings
    max_tokens = fields.Integer(string='Max Tokens', default=1000)
    temperature = fields.Float(string='Temperature', default=0.7, 
                                help='0.0 = deterministic, 1.0 = creative')
    
    # Features
    enable_conversation_history = fields.Boolean(string='Lưu lịch sử hội thoại', default=True)
    enable_rating = fields.Boolean(string='Cho phép đánh giá', default=True)
    enable_human_handoff = fields.Boolean(string='Chuyển cho nhân viên', default=True)
    
    # Active config
    active = fields.Boolean(string='Kích hoạt', default=True)
    
    @api.constrains('top_k_results')
    def _check_top_k(self):
        for record in self:
            if record.top_k_results < 1 or record.top_k_results > 10:
                raise ValidationError("Top K phải từ 1 đến 10")
    
    @api.constrains('similarity_threshold')
    def _check_threshold(self):
        for record in self:
            if record.similarity_threshold < 0 or record.similarity_threshold > 1:
                raise ValidationError("Ngưỡng tương đồng phải từ 0.0 đến 1.0")
    
    @api.constrains('temperature')
    def _check_temperature(self):
        for record in self:
            if record.temperature < 0 or record.temperature > 2:
                raise ValidationError("Temperature phải từ 0.0 đến 2.0")
    
    @api.model
    def get_active_config(self):
        """Get the active chatbot configuration"""
        config = self.search([('active', '=', True)], limit=1)
        if not config:
            raise ValidationError("Chưa có cấu hình chatbot nào được kích hoạt!")
        return config

    def generate_embedding(self, text):
        """Gọi Gemini Embedding API để sinh vector embedding cho một đoạn text.

        Trả về list[float] hoặc None nếu lỗi (không raise để không làm gãy luồng
        chat/lưu knowledge base khi thiếu API key hoặc mất mạng).
        """
        self.ensure_one()
        if not text or not text.strip():
            return None

        model = self.embedding_model or 'models/gemini-embedding-001'
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:embedContent?key={self.gemini_api_key}"
        payload = {
            "model": model,
            "content": {"parts": [{"text": text[:8000]}]},
        }
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            result = response.json()
            values = result.get('embedding', {}).get('values')
            if not values:
                _logger.warning(f"Embedding API trả về không có values: {result}")
                return None
            return values
        except Exception as e:
            _logger.error(f"Lỗi khi gọi Gemini Embedding API: {str(e)}")
            return None
