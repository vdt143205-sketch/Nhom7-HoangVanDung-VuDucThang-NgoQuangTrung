# Những gì nhóm đã phát triển so với code base gốc

> **Code base gốc:** https://github.com/rynxu2/16-06-N01
> **Điểm mốc so sánh:** commit `32e9ee72` (29/01/2026) — commit cuối cùng trên repo gốc,
> đã đối chiếu trực tiếp với lịch sử commit trên GitHub của repo gốc để xác nhận.
> Mọi thứ **sau** mốc này là phần nhóm tự phát triển.
>
> Tài liệu liên quan: [`HIEU_HE_THONG.md`](HIEU_HE_THONG.md) (giải thích code chi tiết),
> [`KIEM_THU_HE_THONG.md`](KIEM_THU_HE_THONG.md) (checklist kiểm thử).

---

## 1. Cơ sở so sánh — cách xác định "của ai"

Lịch sử git của repo này (từ cũ → mới):

| Commit | Tác giả | Ngày | Thuộc về |
|---|---|---|---|
| `01ce5afd` → `e388a620` (6 commits) | Đỗ Bảo Long / rynxu2 | 05–28/01/2026 | **Bản gốc** — scaffold Odoo 15 + 3 module `nhan_su`, `customer_management`, `task_management` + web frontend |
| `ca335746` → `32e9ee72` (3 commits) | Vũ Minh Quốc (MinhQuoc04) | 29/01/2026 | **Bản gốc** — thêm module `chatbot_support` (Gemini + RAG keyword cơ bản, trang `/chatbot` standalone), web interface, README |
| `b6b9d573`, `7add6089` (2 commits) | **dunghv (nhóm)** | 06/07/2026 | **Nhóm phát triển** — đợt 1 |
| Working tree (chưa commit) | **dunghv (nhóm)** | 07–09/07/2026 | **Nhóm phát triển** — đợt 2 |

**Quy mô phần nhóm làm:** ~2.100 dòng thay đổi đã commit + ~650 dòng đang sửa
+ ~700 dòng code mới hoàn toàn (7 file chưa track) + 3 tài liệu md/pdf bắt buộc.

---

## 2. Bản gốc có gì (phần kế thừa — KHÔNG nhận là của nhóm)

- 4 module: `nhan_su` (nhân viên, chức vụ, đơn vị, lịch sử công tác, bằng cấp),
  `customer_management` (KH, đơn hàng, sản phẩm, feedback, care activity, lead),
  `task_management` (task + `order_inherit` tạo task từ đơn), `chatbot_support`
  (Gemini + RAG **keyword search**, trang chat standalone).
- Web frontend `customer-management-web/` (HTML/CSS/JS + proxy Node.js).
- **Kèm theo 3 bug:** `_sql_constrains` sai chính tả, `compute="so_nguoi_bang_tuoi"` sai tên hàm,
  `_update_related_tasks` dùng `write()` nên care activity không bao giờ được tạo.
- **Thiếu:** liên kết HRM ↔ khách hàng, auto-gán nhân viên, mọi loại External API,
  vector embedding, cron tự động hóa.

---

## 3. Đợt 1 — Đã commit ngày 06/07/2026 (`b6b9d573`, `7add6089`)

### 3.1. Sửa 3 bug của bản gốc

| Bug | File | Sửa thế nào |
|---|---|---|
| `_sql_constrains` (thiếu `t`) → Odoo bỏ qua ràng buộc, nhập trùng mã NV được | `nhan_su/models/nhan_vien.py` | Đổi thành `_sql_constraints` — DB thực sự chặn trùng `ma_dinh_danh` |
| `compute="so_nguoi_bang_tuoi"` sai tên hàm → field không bao giờ tính | `nhan_su/models/nhan_vien.py` | Đổi thành `"_compute_so_nguoi_bang_tuoi"` |
| Đơn hoàn thành → task `write({'state':'done'})` → bỏ qua business logic, care activity không tạo | `task_management/models/order_inherit.py` | Phân nhánh: `done` gọi `action_done()`, `cancel` gọi `action_cancel()`, còn lại mới `write()` |

### 3.2. Hoàn thiện Mức 1 (dữ liệu + liên kết HRM)

- Thêm field `trang_thai_lam_viec` (đang làm / tạm nghỉ / đã nghỉ) cho `nhan_vien` — nền tảng cho load balancing.
- Thêm `nhan_vien_phu_trach_id = Many2one('nhan_vien')` vào `khach_hang.customer` — nối customer_management với HRM, bản gốc hoàn toàn thiếu.
- Cập nhật view nhân viên, khách hàng tương ứng.

### 3.3. Mức 2 — Tự động hóa liên 3 module

- **`_find_available_nhan_vien()` (load balancing):** khi tạo đơn, đếm task chưa xong
  (`todo`/`in_progress`) của từng NV đang làm việc, gán task cho người ít việc nhất.
  Bản gốc tạo task nhưng `nhan_vien_id` luôn trống → luồng HRM→KH→Task bị đứt.
- `create()` của order gán `nhan_vien_id` tự động; không còn NV rảnh thì để trống, không crash.

### 3.4. Mức 3 — AI & External API

| Tính năng | File | Mô tả |
|---|---|---|
| **RAG vector embedding thật** | `chatbot_config.py: generate_embedding()`, `knowledge_base.py`, `chatbot_controller.py: _retrieve_documents_semantic()` | Gọi Gemini Embedding API sinh vector cho tài liệu KB (tự sinh khi create/write) và cho câu hỏi; so cosine similarity, lọc theo threshold, lấy top-k. Fallback 2 tầng về keyword search khi thiếu key/mất mạng. `confidence_score` tính thật thay vì hardcode 0.8 |
| **External API — Telegram Bot** | `order_inherit.py: _notify_telegram_new_task()`, `res_config_settings.py` + view | Task vừa auto-gán → gửi tin Telegram (tên đơn, KH, task, người phụ trách, deadline). Token/chat_id lưu qua `ir.config_parameter` tại Settings > Task Management, không hardcode. Lỗi mạng chỉ log, không phá luồng tạo đơn |
| **Floating chatbot widget** | `static/src/js/chatbot_widget.js` + xml + css (414 dòng mới) | OWL Component — nút 🤖 nổi trên mọi trang Odoo backend, kèm gợi ý nhanh; bản gốc chỉ có trang standalone |

### 3.5. Tài liệu bắt buộc của đề bài

- `docs/HIEU_HE_THONG.md` (987 dòng) — giải thích toàn hệ thống từ code thật.
- `docs/business-flow/Nhom07_BusinessFlow_QuanLyKhachHang_QuanLyCongViec.pdf` — sơ đồ Swimlane 12 bước, 4 actor.
- `docs/poster/Nhom07_Poster_HeThongERP.pdf` — poster giới thiệu hệ thống.

---

## 4. Đợt 2 — Đang phát triển, chưa commit (07–09/07/2026)

Bộ tính năng AI nâng cao đánh mã **F1–F10**, xuyên suốt cả 3 module nghiệp vụ:

### 4.1. `task_management`

| Mã | Tính năng | File | Mô tả |
|---|---|---|---|
| **F1** | AI Smart Priority | `order_inherit.py: _ai_suggest_priority()` | Khi tạo task từ đơn, gọi Gemini phân tích ngữ cảnh (giá trị đơn, deadline...) đề xuất mức ưu tiên 0–3; Gemini lỗi thì rơi về rule-based, không phá luồng |
| **F3** | Auto Escalation | `task_escalation.py` (182 dòng, mới) + `data/cron_data.xml` | Cron chạy hàng ngày 8h: task quá hạn ≥1 ngày → cảnh báo Telegram cho quản lý; quá hạn ≥5 ngày → **tự tái phân công** sang NV rảnh nhất (dùng lại thuật toán load balancing), ghi log vào chatter + báo Telegram |

### 4.2. `customer_management`

| Mã | Tính năng | File | Mô tả |
|---|---|---|---|
| **F4** | Sentiment Analysis | `feedback.py` (+108 dòng) | Mỗi feedback tạo/sửa → Gemini phân tích cảm xúc tiếng Việt, trả JSON `{label, score -1..1, reason}` → lưu vào 3 field mới (😊/😐/😠) |
| **F5** | Churn Detection | `churn_detection.py` (186 dòng, mới) + `data/cron_data.xml` | Cron hàng tuần tính điểm rủi ro rời bỏ 0–100% từ 3 tín hiệu: recency đơn cuối (40đ) + tỷ lệ feedback tiêu cực từ F4 (35đ) + tỷ lệ đơn hủy (25đ). Rủi ro ≥65% → **tự tạo care activity** nhắc chăm sóc (chống trùng trong 7 ngày). Có nút tính tay trên form KH |

### 4.3. `chatbot_support`

| Mã | Tính năng | File | Mô tả |
|---|---|---|---|
| **F7** | Conversational Commerce | `chatbot_controller.py` (+301 dòng), endpoint mới `/chatbot/api/chat/v2` | Nhận diện intent (kiểm tra đơn / tìm sản phẩm theo giá / yêu cầu hỗ trợ / tra KH rủi ro churn) → query DB đúng nghiệp vụ trước khi đưa vào Gemini |
| **F9** | Self-learning KB | `conversation.py` (+81 dòng, field `flagged_for_review` + cron) | Cron hàng ngày gom các câu trả lời bị người dùng đánh giá 👎 → tự tạo **bản nháp Knowledge Base** kèm câu hỏi gốc để admin duyệt — chatbot "học" từ thất bại |
| **F10** | AI Dashboard | `ai_dashboard.py` (219 dòng, mới) + view + menu + API `/chatbot/api/dashboard` | Model `chatbot.ai.summary`: cron hàng ngày gom KPI (đơn/doanh thu hôm nay, task quá hạn, KH churn cao, feedback tiêu cực) → Gemini viết bản tóm tắt điều hành tiếng Việt; có fallback khi Gemini lỗi |

### 4.4. Sửa lỗi phát hiện khi kiểm thử toàn hệ thống (09/07/2026)

| Lỗi | Nguyên nhân | Sửa |
|---|---|---|
| 7/7 tài liệu KB không có embedding → semantic search chưa từng chạy thật | Google **đã ngừng** model `text-embedding-004` (API trả 404) | Đổi sang `models/gemini-embedding-001` trong `chatbot_config.py`, demo data, config DB; sinh lại embedding 7/7 OK |
| Chatbot trả lời sai tổng số bản ghi (nói "10 đơn" khi DB có 15) | `_get_live_data` chỉ đưa danh sách `limit=10` vào context, Gemini tự suy tổng | Thêm `search_count()` tổng thật vào header context cho cả 5 nhánh (đơn/KH/task/NV/SP) |

Kèm theo: `docs/KIEM_THU_HE_THONG.md` (177 dòng) — checklist kiểm thử toàn hệ thống theo 3 mức.

---

## 5. Bảng tổng hợp file thay đổi so với bản gốc

### Đã commit (23 file, +2.098/−96 dòng)

```
nhan_su/models/nhan_vien.py            sửa 2 bug + trang_thai_lam_viec
nhan_su/views/nhan_vien.xml            view trạng thái làm việc
customer_management/models/customer.py nhan_vien_phu_trach_id (link HRM)
task_management/models/order_inherit.py load balancing + Telegram + sửa bug care activity
task_management/models/res_config_settings.py  cấu hình Telegram (mới)
task_management/views/res_config_settings_view.xml  (mới)
chatbot_support/controllers/chatbot_controller.py  RAG semantic + fallback
chatbot_support/models/chatbot_config.py  generate_embedding()
chatbot_support/models/knowledge_base.py  tự sinh embedding khi create/write
chatbot_support/static/src/{js,xml,css}/chatbot_widget.*  (mới, 414 dòng)
docs/HIEU_HE_THONG.md + business-flow/ + poster/  (mới)
```

### Chưa commit (17 file sửa +644 dòng, 7 file mới ~886 dòng)

```
MỚI  task_management/models/task_escalation.py        F3 (182 dòng)
MỚI  task_management/data/cron_data.xml               cron F3
MỚI  customer_management/models/churn_detection.py    F5 (186 dòng)
MỚI  customer_management/data/cron_data.xml           cron F5
MỚI  chatbot_support/models/ai_dashboard.py           F10 (219 dòng)
MỚI  chatbot_support/views/ai_dashboard_view.xml      F10 view
MỚI  docs/KIEM_THU_HE_THONG.md                        checklist kiểm thử
SỬA  customer_management/models/feedback.py           F4 sentiment (+108)
SỬA  chatbot_support/controllers/chatbot_controller.py F7 + fix số liệu (+301)
SỬA  chatbot_support/models/conversation.py           F9 (+81)
SỬA  chatbot_support/models/chatbot_config.py         fix embedding model
SỬA  task_management/models/order_inherit.py          F1 AI priority (+106)
SỬA  các __manifest__.py, __init__.py, view, security tương ứng
```

---

## 6. Tóm tắt một câu cho từng mức điểm

- **Mức 1:** kế thừa 3 module gốc, **sửa 3 bug**, bổ sung `trang_thai_lam_viec` và
  liên kết HRM (`nhan_vien_phu_trach_id`) mà bản gốc thiếu.
- **Mức 2:** tự viết **load balancing auto-gán nhân viên** khi tạo đơn — luồng
  Nhân sự → Khách hàng → Đơn → Task liên thông thật, kèm đồng bộ trạng thái và care activity tự động.
- **Mức 3:** nâng RAG từ keyword lên **vector embedding thật** (fallback 2 tầng),
  thêm **External API Telegram**, floating widget, và đang mở rộng thành hệ sinh thái AI
  (smart priority, escalation, sentiment, churn, commerce intent, self-learning KB, AI dashboard).

**Lưu ý khi bảo vệ:** phần scaffold + chatbot cơ bản là của repo gốc (rynxu2 / Vũ Minh Quốc) —
chỉ nhận phần từ mục 3 trở đi, đối chiếu được bằng `git log --author=dunghv` và `git status`.

---

*Cập nhật: 09/07/2026 — số liệu dòng code lấy từ `git diff --stat` tại thời điểm viết.*
