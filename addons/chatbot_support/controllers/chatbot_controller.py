# -*- coding: utf-8 -*-
import json
import logging
import math
import requests
from datetime import datetime
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _cosine_similarity(vec_a, vec_b):
    """Độ tương đồng cosin giữa 2 vector embedding (thuần Python, không cần numpy)."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class ChatbotController(http.Controller):

    @http.route('/chatbot', type='http', auth='public', website=False, csrf=False)
    def chatbot_page(self, **kwargs):
        """Trang giao diện chat cho khách hàng"""
        import os
        chat_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'src', 'chat.html')
        with open(chat_path, 'r', encoding='utf-8') as f:
            return f.read()

    @http.route('/chatbot/api/chat', type='json', auth='public', methods=['POST'], csrf=False, cors='*')
    def chat(self, message, session_id=None, partner_id=None, **kwargs):
        """
        Main chat endpoint
        
        Args:
            message (str): User's message
            session_id (str): Session ID for conversation tracking
            partner_id (int): Optional customer ID
        
        Returns:
            dict: {
                'response': str,
                'conversation_id': int,
                'message_id': int,
                'success': bool
            }
        """
        try:
            # Get or create conversation
            conversation = self._get_or_create_conversation(session_id, partner_id, kwargs)
            
            # Save user message
            user_message = request.env['chatbot.message'].sudo().create({
                'conversation_id': conversation.id,
                'message_type': 'user',
                'content': message,
            })
            
            # Get chatbot response using RAG
            bot_response, metadata = self._get_bot_response(message, conversation)
            
            # Save bot message
            bot_message = request.env['chatbot.message'].sudo().create({
                'conversation_id': conversation.id,
                'message_type': 'bot',
                'content': bot_response,
                'retrieved_docs': json.dumps(metadata.get('retrieved_docs', [])),
                'confidence_score': metadata.get('confidence_score', 0.0),
                'model_used': metadata.get('model_used', 'gemini-1.5-flash'),
                'response_time': metadata.get('response_time', 0.0),
            })
            
            return {
                'success': True,
                'response': bot_response,
                'conversation_id': conversation.id,
                'message_id': bot_message.id,
                'metadata': metadata
            }
            
        except Exception as e:
            _logger.error(f"Chatbot error: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'response': 'Xin lỗi, đã có lỗi xảy ra. Vui lòng thử lại sau.'
            }
    
    @http.route('/chatbot/api/welcome', type='json', auth='public', methods=['GET'], csrf=False, cors='*')
    def get_welcome_message(self, **kwargs):
        """Get welcome message from config"""
        try:
            config = request.env['chatbot.config'].sudo().get_active_config()
            return {
                'success': True,
                'message': config.welcome_message
            }
        except Exception as e:
            return {
                'success': False,
                'message': 'Xin chào! Tôi có thể giúp gì cho bạn?'
            }
    
    @http.route('/chatbot/api/rate', type='json', auth='public', methods=['POST'], csrf=False, cors='*')
    def rate_conversation(self, conversation_id, rating, feedback=None, **kwargs):
        """Rate a conversation"""
        try:
            conversation = request.env['chatbot.conversation'].sudo().browse(conversation_id)
            if conversation.exists():
                conversation.write({
                    'rating': str(rating),
                    'feedback': feedback
                })
                return {'success': True}
            return {'success': False, 'error': 'Conversation not found'}
        except Exception as e:
            _logger.error(f"Rating error: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _get_or_create_conversation(self, session_id, partner_id, request_data):
        """Get existing conversation or create new one"""
        Conversation = request.env['chatbot.conversation'].sudo()
        
        if not session_id:
            session_id = f"session_{datetime.now().timestamp()}"
        
        # Try to find existing active conversation
        conversation = Conversation.search([
            ('session_id', '=', session_id),
            ('state', '=', 'active')
        ], limit=1)
        
        if not conversation:
            # Create new conversation
            vals = {
                'session_id': session_id,
                'user_ip': request_data.get('user_ip'),
                'user_agent': request_data.get('user_agent'),
            }
            if partner_id:
                vals['partner_id'] = partner_id
            
            conversation = Conversation.create(vals)
        
        return conversation
    
    def _get_bot_response(self, user_message, conversation):
        """
        Get bot response using RAG pipeline
        
        This method will:
        1. Call RAG service to retrieve relevant documents
        2. Call Gemini API to generate response
        3. Return response and metadata
        """
        import time
        start_time = time.time()
        
        try:
            # Get config
            config = request.env['chatbot.config'].sudo().get_active_config()
            
            # Step 1: Retrieve relevant documents from knowledge base
            retrieved_docs, confidence_score = self._retrieve_documents(user_message, config)

            # Step 2: Build context from retrieved documents + live data
            kb_context = self._build_context(retrieved_docs)
            live_context = self._get_live_data(user_message, conversation)
            context = kb_context + "\n\n" + live_context

            # Step 3: Generate response using Gemini
            response = self._generate_response(user_message, context, config, conversation)

            # Calculate response time
            response_time = time.time() - start_time

            # Update usage count for retrieved docs
            for doc in retrieved_docs:
                doc.increment_usage()

            metadata = {
                'retrieved_docs': [doc.id for doc in retrieved_docs],
                'confidence_score': confidence_score,
                'model_used': config.gemini_model,
                'response_time': response_time,
            }
            
            return response, metadata
            
        except Exception as e:
            _logger.error(f"RAG error: {str(e)}", exc_info=True)
            # Return fallback message
            config = request.env['chatbot.config'].sudo().get_active_config()
            return config.fallback_message, {'error': str(e)}
    
    def _get_live_data(self, user_message, conversation):
        """Truy vấn dữ liệu thực từ hệ thống dựa trên câu hỏi của khách hàng"""
        live_parts = []
        msg_lower = user_message.lower()

        # Nếu khách hỏi về đơn hàng
        if any(kw in msg_lower for kw in ['đơn hàng', 'đơn', 'order', 'mua', 'đặt']):
            Order = request.env['khach_hang.order'].sudo()
            orders = Order.search([], limit=10, order='create_date desc')
            if orders:
                total = Order.search_count([])
                lines = [f"=== DỮ LIỆU ĐƠN HÀNG THỰC TẾ (tổng cộng {total} đơn hàng, dưới đây là {len(orders)} đơn mới nhất) ==="]
                for o in orders:
                    lines.append(
                        f"- {o.name}: KH={o.customer_id.name or 'N/A'}, "
                        f"Tổng={o.total_amount:,.0f} VNĐ, Trạng thái={o.state or 'draft'}"
                    )
                live_parts.append("\n".join(lines))

        # Nếu khách hỏi về khách hàng
        if any(kw in msg_lower for kw in ['khách hàng', 'khách', 'customer']):
            Customer = request.env['khach_hang.customer'].sudo()
            customers = Customer.search([], limit=10)
            if customers:
                total = Customer.search_count([])
                lines = [f"=== DỮ LIỆU KHÁCH HÀNG THỰC TẾ (tổng cộng {total} khách hàng, dưới đây là {len(customers)} bản ghi) ==="]
                for c in customers:
                    lines.append(
                        f"- {c.name}: Email={c.email or 'N/A'}, SĐT={c.phone or 'N/A'}, "
                        f"Số đơn={c.order_count}"
                    )
                live_parts.append("\n".join(lines))

        # Nếu khách hỏi về công việc / task
        if any(kw in msg_lower for kw in ['công việc', 'task', 'tiến độ', 'trạng thái']):
            Task = request.env['task.management.task'].sudo()
            tasks = Task.search([], limit=10, order='create_date desc')
            if tasks:
                total = Task.search_count([])
                lines = [f"=== DỮ LIỆU CÔNG VIỆC THỰC TẾ (tổng cộng {total} công việc, dưới đây là {len(tasks)} công việc mới nhất) ==="]
                for t in tasks:
                    lines.append(
                        f"- {t.name}: NV={t.nhan_vien_id.ho_va_ten or 'Chưa gán'}, "
                        f"Trạng thái={t.state}, Tiến độ={t.progress}%"
                    )
                live_parts.append("\n".join(lines))

        # Nếu khách hỏi về nhân viên
        if any(kw in msg_lower for kw in ['nhân viên', 'nhan vien', 'staff', 'người phụ trách']):
            Employee = request.env['nhan_vien'].sudo()
            employees = Employee.search([], limit=10)
            if employees:
                total = Employee.search_count([])
                lines = [f"=== DỮ LIỆU NHÂN VIÊN THỰC TẾ (tổng cộng {total} nhân viên, dưới đây là {len(employees)} bản ghi) ==="]
                for nv in employees:
                    lines.append(
                        f"- {nv.ho_va_ten}: MĐD={nv.ma_dinh_danh}, "
                        f"Trạng thái={nv.trang_thai_lam_viec or 'N/A'}"
                    )
                live_parts.append("\n".join(lines))

        # Nếu khách hỏi về sản phẩm
        if any(kw in msg_lower for kw in ['sản phẩm', 'product', 'hàng hóa', 'giá']):
            Product = request.env['khach_hang.product'].sudo()
            products = Product.search([], limit=10)
            if products:
                total = Product.search_count([])
                lines = [f"=== DỮ LIỆU SẢN PHẨM THỰC TẾ (tổng cộng {total} sản phẩm, dưới đây là {len(products)} bản ghi) ==="]
                for p in products:
                    price = getattr(p, 'price', 0) or getattr(p, 'list_price', 0) or 0
                    lines.append(f"- {p.name}: Giá={price:,.0f} VNĐ")
                live_parts.append("\n".join(lines))

        if not live_parts:
            return ""
        return "\n\n".join(live_parts)

    def _retrieve_documents(self, query, config):
        """
        Retrieve relevant documents from knowledge base.

        Ưu tiên semantic search bằng vector embedding (Gemini). Nếu không thể
        (thiếu API key, chưa có doc nào có embedding, hoặc lỗi mạng) thì rơi về
        keyword search như cũ. Trả về (docs, confidence_score) để metadata phản
        ánh đúng chất lượng của kết quả tìm được thay vì giá trị hardcode.
        """
        semantic_docs, semantic_score = self._retrieve_documents_semantic(query, config)
        if semantic_docs:
            _logger.info(f"RAG Search (semantic): {len(semantic_docs)} docs, top_score={semantic_score:.3f}")
            return semantic_docs, semantic_score

        docs, confidence = self._retrieve_documents_keyword(query, config)
        return docs, confidence

    def _retrieve_documents_semantic(self, query, config):
        """Semantic search dựa trên cosine similarity giữa embedding của câu hỏi
        và embedding đã lưu sẵn của từng tài liệu trong knowledge base."""
        KnowledgeBase = request.env['chatbot.knowledge.base'].sudo()

        docs_with_embedding = KnowledgeBase.search([
            ('active', '=', True),
            ('embedding_vector', '!=', False),
        ])
        if not docs_with_embedding:
            return KnowledgeBase, 0.0

        query_vector = config.generate_embedding(query)
        if not query_vector:
            return KnowledgeBase, 0.0

        scored = []
        for doc in docs_with_embedding:
            try:
                doc_vector = json.loads(doc.embedding_vector)
            except (ValueError, TypeError):
                continue
            score = _cosine_similarity(query_vector, doc_vector)
            if score >= config.similarity_threshold:
                scored.append((score, doc))

        if not scored:
            return KnowledgeBase, 0.0

        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:config.top_k_results]
        top_score = top[0][0]
        doc_ids = [doc.id for _, doc in top]
        docs = KnowledgeBase.browse(doc_ids)
        return docs, top_score

    def _retrieve_documents_keyword(self, query, config):
        """Keyword search (fallback) - searches each word separately then combines results."""
        KnowledgeBase = request.env['chatbot.knowledge.base'].sudo()

        _logger.info(f"RAG Search (keyword fallback): query='{query}'")

        # Strategy 1: Try full query first
        docs = KnowledgeBase.search([
            ('active', '=', True),
            '|', '|',
            ('name', 'ilike', query),
            ('content_plain', 'ilike', query),
            ('keywords', 'ilike', query)
        ], limit=config.top_k_results, order='priority desc, usage_count desc')

        if docs:
            _logger.info(f"RAG Search: Full query found {len(docs)} docs")
            return docs, 0.6

        # Strategy 2: If no results, try individual words
        query_words = [w for w in query.lower().split() if len(w) >= 2]
        _logger.info(f"RAG Search: Trying words: {query_words}")

        doc_ids = set()
        for word in query_words:
            word_docs = KnowledgeBase.search([
                ('active', '=', True),
                '|', '|',
                ('name', 'ilike', word),
                ('content_plain', 'ilike', word),
                ('keywords', 'ilike', word)
            ])
            doc_ids.update(word_docs.ids)
            _logger.info(f"RAG Search: Word '{word}' found {len(word_docs)} docs")

        if not doc_ids:
            return KnowledgeBase, 0.0

        docs = KnowledgeBase.browse(list(doc_ids))
        docs = docs.sorted(key=lambda d: (d.priority, d.usage_count), reverse=True)
        docs = docs[:config.top_k_results]

        # Confidence tỉ lệ với số từ khóa khớp được trên tổng số từ trong câu hỏi
        matched_ratio = min(len(doc_ids), len(query_words)) / max(len(query_words), 1)
        confidence = round(0.3 + 0.3 * matched_ratio, 2)
        return docs, confidence
    
    def _build_context(self, documents):
        """Build context string from retrieved documents"""
        if not documents:
            return "Không có thông tin liên quan trong knowledge base."
        
        context_parts = []
        total_chars = 0
        max_context_chars = 8000  # Limit context to ~2000 tokens
        
        for i, doc in enumerate(documents, 1):
            # Get content, truncate if too long
            content = doc.content_plain or ""
            
            # Add document with clear formatting
            doc_text = f"""
=== TÀI LIỆU {i}: {doc.name} ===
{content}
===================================
"""
            
            # Check if adding this doc would exceed limit
            if total_chars + len(doc_text) > max_context_chars:
                # Truncate this document
                remaining = max_context_chars - total_chars
                if remaining > 200:  # Only add if we have meaningful space
                    truncated = content[:remaining] + "...[đã cắt bớt]"
                    doc_text = f"""
=== TÀI LIỆU {i}: {doc.name} ===
{truncated}
===================================
"""
                    context_parts.append(doc_text)
                break
            
            context_parts.append(doc_text)
            total_chars += len(doc_text)
        
        _logger.info(f"Built context with {len(context_parts)} documents, {total_chars} chars")
        return "\n".join(context_parts)
    
    def _generate_response(self, user_message, context, config, conversation):
        """
        Generate response using Gemini API
        """
        try:
            history = self._get_conversation_history(conversation, limit=5)
            prompt = self._build_prompt(user_message, context, history, config)
            _logger.warning(f"=== CALLING GEMINI API === model={config.gemini_model}")
            response = self._call_gemini_api(prompt, config)
            _logger.warning(f"=== GEMINI OK === {response[:80]}")
            return response

        except Exception as e:
            import traceback
            _logger.error(f"=== GEMINI FAILED === {str(e)}")
            _logger.error(traceback.format_exc())
            return config.fallback_message
    
    def _get_conversation_history(self, conversation, limit=5):
        """Get recent conversation history"""
        messages = request.env['chatbot.message'].sudo().search([
            ('conversation_id', '=', conversation.id)
        ], order='create_date desc', limit=limit * 2)
        
        history = []
        for msg in reversed(messages):
            role = 'user' if msg.message_type == 'user' else 'model'
            history.append({
                'role': role,
                'parts': [msg.content]
            })
        
        return history
    
    def _build_prompt(self, user_message, context, history, config):
        """Build the full prompt for Gemini"""
        system_prompt = config.system_prompt
        
        prompt = f"""{system_prompt}

THÔNG TIN TỪ KNOWLEDGE BASE VÀ DỮ LIỆU HỆ THỐNG:
{context}

---

HƯỚNG DẪN TRẢ LỜI:
1. ĐỌC KỸ nội dung trong các tài liệu và dữ liệu hệ thống trên
2. TÌM KIẾM thông tin liên quan đến câu hỏi của khách hàng
3. TỔNG HỢP và trả lời dựa trên nội dung tìm được
4. TRÍCH DẪN thông tin cụ thể (số liệu, tên, trạng thái) từ dữ liệu thực tế
5. Nếu KHÔNG TÌM THẤY thông tin phù hợp, hãy thừa nhận và đề xuất liên hệ nhân viên

LƯU Ý:
- Trả lời bằng tiếng Việt, ngắn gọn nhưng đầy đủ
- Ưu tiên dữ liệu thực tế từ hệ thống (đơn hàng, khách hàng, nhân viên, công việc)
- Nếu có nhiều thông tin liên quan, hãy tổng hợp lại
- Không bịa đặt thông tin không có trong dữ liệu

Câu hỏi của khách hàng: {user_message}
"""
        return prompt
    
    def _call_gemini_api(self, prompt, config):
        """
        Call Gemini API to generate response
        """
        api_key = config.gemini_api_key
        model = config.gemini_model

        _logger.info(f"Calling Gemini API: model={model}, key={api_key[:10]}...")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": config.temperature,
                "maxOutputTokens": config.max_tokens,
            }
        }

        headers = {'Content-Type': 'application/json'}

        response = requests.post(url, json=payload, headers=headers, timeout=30)
        _logger.info(f"Gemini API response status: {response.status_code}")

        if response.status_code != 200:
            _logger.error(f"Gemini API error body: {response.text}")

        response.raise_for_status()

        result = response.json()

        if 'candidates' in result and len(result['candidates']) > 0:
            candidate = result['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                text = candidate['content']['parts'][0]['text']
                _logger.info(f"Gemini response OK: {text[:100]}...")
                return text

        raise Exception(f"Invalid response from Gemini API: {json.dumps(result)[:200]}")

    # ══════════════════════════════════════════════════════════════════════════
    # F7: Conversational Commerce — Intent Detection & Action Handlers
    # ══════════════════════════════════════════════════════════════════════════

    def _detect_intent(self, message):
        """[F7] Nhận diện ý định từ tin nhắn người dùng.

        Trả về string intent:
          'check_order'      — hỏi về trạng thái đơn hàng cụ thể
          'search_product'   — tìm kiếm sản phẩm theo yêu cầu
          'request_support'  — yêu cầu đổi trả / hỗ trợ
          'churn_risk_check' — nhân viên hỏi về KH rủi ro
          'general_question' — câu hỏi thông thường (không phân loại)
        """
        msg = message.lower()

        # Check order intent
        check_order_kw = ['đơn hàng của tôi', 'kiểm tra đơn', 'xem đơn',
                          'trạng thái đơn', 'đơn #', 'ord', 'mã đơn',
                          'đơn hàng số', 'giao hàng chưa', 'ship chưa']
        if any(kw in msg for kw in check_order_kw):
            return 'check_order'

        # Search product intent
        search_product_kw = ['tìm sản phẩm', 'xem sản phẩm', 'có sản phẩm',
                             'dưới', 'triệu', 'giá', 'rẻ', 'đắt', 'mua gì',
                             'tư vấn sản phẩm', 'sản phẩm nào', 'giá bao nhiêu']
        if any(kw in msg for kw in search_product_kw):
            return 'search_product'

        # Support request intent
        support_kw = ['đổi trả', 'hoàn tiền', 'khiếu nại', 'hỗ trợ',
                      'bị lỗi', 'hàng lỗi', 'không nhận được', 'tư vấn',
                      'cần giúp', 'liên hệ nhân viên']
        if any(kw in msg for kw in support_kw):
            return 'request_support'

        # Churn risk check (internal staff)
        churn_kw = ['khách hàng rủi ro', 'churn', 'rời bỏ', 'kh nguy cơ',
                    'kh cần chăm sóc', 'danh sách rủi ro']
        if any(kw in msg for kw in churn_kw):
            return 'churn_risk_check'

        return 'general_question'

    def _handle_commerce_intent(self, intent, message):
        """[F7] Xử lý intent và trả về context string có cấu trúc."""
        if intent == 'check_order':
            return self._handle_check_order(message)
        elif intent == 'search_product':
            return self._handle_search_product(message)
        elif intent == 'request_support':
            return self._handle_request_support(message)
        elif intent == 'churn_risk_check':
            return self._handle_churn_risk_check()
        return ""

    def _handle_check_order(self, message):
        """[F7] Tìm đơn hàng theo mã hoặc tên KH từ tin nhắn."""
        import re
        Order = request.env['khach_hang.order'].sudo()

        # Thử trích mã đơn (ORD... hoặc số)
        code_match = re.search(r'(ORD[-/]?\d+|\b\d{4,}\b)', message.upper())
        orders = None

        if code_match:
            code = code_match.group(1)
            orders = Order.search([('name', 'ilike', code)], limit=3)

        if not orders:
            orders = Order.search([], order='create_date desc', limit=5)

        if not orders:
            return "=== KIỂM TRA ĐƠN HÀNG ===\nKhông tìm thấy đơn hàng nào."

        state_label = {
            'draft': 'Nháp', 'confirmed': 'Đã xác nhận',
            'shipping': 'Đang giao', 'done': 'Hoàn thành', 'cancel': 'Đã hủy'
        }
        lines = ["=== THÔNG TIN ĐƠN HÀNG ==="]
        for o in orders:
            lines.append(
                f"• Mã: {o.name} | KH: {o.customer_id.name or 'N/A'} | "
                f"Trạng thái: {state_label.get(o.state, o.state)} | "
                f"Tổng: {(o.total_amount or 0):,.0f} VNĐ | "
                f"Ngày tạo: {str(o.create_date.date()) if o.create_date else 'N/A'}"
            )
        return "\n".join(lines)

    def _handle_search_product(self, message):
        """[F7] Tìm kiếm sản phẩm theo từ khóa hoặc ngưỡng giá."""
        import re
        Product = request.env['khach_hang.product'].sudo()

        # Tìm ngưỡng giá nếu có
        price_match = re.search(r'dưới\s*([\d.,]+)\s*(triệu|tr|nghìn|k)?', message.lower())
        max_price = None
        if price_match:
            val = float(price_match.group(1).replace(',', '.').replace('.', ''))
            unit = price_match.group(2) or ''
            if 'triệu' in unit or 'tr' in unit:
                max_price = val * 1_000_000
            elif 'nghìn' in unit or 'k' in unit:
                max_price = val * 1_000
            else:
                max_price = val

        domain = []
        if max_price:
            domain.append(('price', '<=', max_price))

        # Tìm từ khóa sản phẩm
        keywords = [w for w in message.lower().split()
                    if len(w) > 3 and w not in
                    ('tìm', 'sản', 'phẩm', 'nào', 'giá', 'bao', 'nhiêu',
                     'dưới', 'triệu', 'rẻ', 'đắt', 'mua', 'gì', 'cho')]
        if keywords:
            domain.append('|')
            for kw in keywords[:2]:
                domain.append(('name', 'ilike', kw))

        products = Product.search(domain if domain else [], limit=5,
                                  order='price asc')

        if not products:
            return "=== TÌM KIẾM SẢN PHẨM ===\nKhông tìm thấy sản phẩm phù hợp."

        lines = ["=== SẢN PHẨM GỢI Ý ==="]
        for p in products:
            price = getattr(p, 'price', 0) or 0
            lines.append(f"• {p.name}: {price:,.0f} VNĐ")
        if max_price:
            lines.append(f"(Lọc: giá ≤ {max_price:,.0f} VNĐ)")
        return "\n".join(lines)

    def _handle_request_support(self, message):
        """[F7] Hướng dẫn quy trình hỗ trợ / đổi trả."""
        return (
            "=== HỖ TRỢ KHÁCH HÀNG ===\n"
            "Để được hỗ trợ nhanh nhất, vui lòng:\n"
            "1. Cung cấp mã đơn hàng (ví dụ: ORD001)\n"
            "2. Mô tả vấn đề cụ thể\n"
            "3. Chúng tôi sẽ phân công nhân viên xử lý trong vòng 24h\n"
            "Hotline: 1800-xxx-xxx | Email: support@company.com"
        )

    def _handle_churn_risk_check(self):
        """[F7] Trả về danh sách KH có rủi ro cao cho nhân viên nội bộ."""
        Customer = request.env['khach_hang.customer'].sudo()
        high_risk = Customer.search([
            ('churn_risk_label', '=', 'high')
        ], limit=5, order='churn_risk_score desc')

        if not high_risk:
            return "=== CHURN RISK ===\nHiện không có khách hàng nào ở mức rủi ro cao. 🟢"

        lines = ["=== KHÁCH HÀNG RỦI RO CAO (cần chăm sóc ngay) ==="]
        for c in high_risk:
            lines.append(
                f"• {c.name}: Score={c.churn_risk_score:.1f}% | "
                f"Lý do: {c.churn_reason or 'N/A'} | "
                f"NV phụ trách: {c.nhan_vien_phu_trach_id.ho_va_ten if c.nhan_vien_phu_trach_id else 'Chưa gán'}"
            )
        return "\n".join(lines)

    # ── Override chat endpoint để tích hợp F7 intent ─────────────────────────
    @http.route('/chatbot/api/chat/v2', type='json', auth='public',
                methods=['POST'], csrf=False, cors='*')
    def chat_v2(self, message, session_id=None, partner_id=None, **kwargs):
        """[F7] Enhanced chat endpoint với intent detection.

        Gọi _detect_intent → nếu là commerce intent, thêm action context
        vào response metadata để frontend có thể render UI đặc biệt.
        """
        try:
            conversation = self._get_or_create_conversation(
                session_id, partner_id, kwargs)

            # [F7] Nhận diện intent
            intent = self._detect_intent(message)
            commerce_context = self._handle_commerce_intent(intent, message)

            # Lưu tin nhắn người dùng
            request.env['chatbot.message'].sudo().create({
                'conversation_id': conversation.id,
                'message_type': 'user',
                'content': message,
            })

            # Lấy response RAG bình thường, bổ sung commerce context
            bot_response, metadata = self._get_bot_response_v2(
                message, conversation, commerce_context)

            bot_message = request.env['chatbot.message'].sudo().create({
                'conversation_id': conversation.id,
                'message_type': 'bot',
                'content': bot_response,
                'retrieved_docs': json.dumps(metadata.get('retrieved_docs', [])),
                'confidence_score': metadata.get('confidence_score', 0.0),
                'model_used': metadata.get('model_used', 'gemini-2.0-flash'),
                'response_time': metadata.get('response_time', 0.0),
            })

            return {
                'success': True,
                'response': bot_response,
                'intent': intent,
                'conversation_id': conversation.id,
                'message_id': bot_message.id,
                'metadata': metadata,
            }

        except Exception as e:
            _logger.error(f"[F7] Chat v2 error: {e}", exc_info=True)
            return {
                'success': False,
                'response': 'Xin lỗi, đã có lỗi xảy ra. Vui lòng thử lại.',
                'error': str(e),
            }

    def _get_bot_response_v2(self, user_message, conversation, commerce_context=""):
        """Giống _get_bot_response nhưng ưu tiên inject commerce_context."""
        import time
        start_time = time.time()
        try:
            config = request.env['chatbot.config'].sudo().get_active_config()
            retrieved_docs, confidence_score = self._retrieve_documents(
                user_message, config)
            kb_context = self._build_context(retrieved_docs)
            live_context = self._get_live_data(user_message, conversation)

            # [F7] Ưu tiên commerce context lên đầu
            context_parts = []
            if commerce_context:
                context_parts.append(commerce_context)
            context_parts.append(kb_context)
            if live_context:
                context_parts.append(live_context)
            context = "\n\n".join(context_parts)

            response = self._generate_response(
                user_message, context, config, conversation)
            response_time = time.time() - start_time

            for doc in retrieved_docs:
                doc.increment_usage()

            return response, {
                'retrieved_docs': [doc.id for doc in retrieved_docs],
                'confidence_score': confidence_score,
                'model_used': config.gemini_model,
                'response_time': response_time,
            }
        except Exception as e:
            _logger.error(f"[F7] RAG v2 error: {e}", exc_info=True)
            config = request.env['chatbot.config'].sudo().get_active_config()
            return config.fallback_message, {'error': str(e)}

    # ── F10: Dashboard API endpoint ───────────────────────────────────────────
    @http.route('/chatbot/api/dashboard', type='json', auth='user',
                methods=['GET'], csrf=False)
    def get_dashboard_data(self, **kwargs):
        """[F10] API trả về KPI + AI summary mới nhất cho dashboard."""
        try:
            summary = request.env['chatbot.ai.summary'].sudo().get_latest_summary()
            return {'success': True, 'data': summary}
        except Exception as e:
            return {'success': False, 'error': str(e)}

