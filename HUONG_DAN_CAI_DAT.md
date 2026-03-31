# Hướng Dẫn Cài Đặt & Chạy Dịch Vụ Livestream Radar (Từ A-Z)

Dịch vụ Livestream Radar & Customer Profiling bao gồm hai thành phần chính:
1. **Local Backend (FastAPI)**: Chạy trên máy tính của bạn, bao gồm Server kết nối WebSocket và cơ sở dữ liệu để đồng bộ thông tin khách hàng từ Pancake POS.
2. **Chrome Extension (Tiện ích mở rộng Chrome)** / **Userscript**: Cài vào trình duyệt Chrome/Cốc Cốc để quét bình luận từ Facebook Live và gửi về Backend.

Dưới đây là hướng dẫn chi tiết để thiết lập hệ thống từ đầu.

---

## Phần 1: Cài đặt và Chạy Backend (FastAPI)

### Yêu cầu tiên quyết
- Máy tính đã cài đặt **Python** (phiên bản 3.10 trở lên). Nếu chưa có, bạn tải và cài đặt từ trang chủ [Python.org](https://www.python.org/downloads/). Nhớ tích chọn **"Add Python to PATH"** khi cài đặt.

### Các bước thực hiện
1. **Mở thư mục mã nguồn**: 
   - Mở Terminal (Command Prompt hoặc PowerShell trên Windows).
   - Di chuyển đến thư mục chứa mã nguồn:
     ```bash
     cd f:\code\Livestream
     ```

2. **Cài đặt thư viện cần thiết**: 
   - Chạy lệnh sau để cài đặt các thư viện Python cho hệ thống Backend:
     ```bash
     pip install -r requirements.txt
     ```
   *(Lời khuyên: Bạn có thể tạo môi trường ảo "venv" để cài đặt thư viện cho dự án bằng lệnh `python -m venv .venv` và kích hoạt bằng `.venv\Scripts\activate` trước khi chạy lệnh pip install).*

3. **Khởi chạy Hệ thống Backend**:
   - Sử dụng `uvicorn` để khởi động máy chủ API và WebSockets:
     ```bash
     uvicorn main:app --host 0.0.0.0 --port 8000 --reload
     ```
   - *Lưu ý: Màn hình Terminal sẽ báo "Application startup complete". Hãy giữ nguyên cửa sổ này, không được tắt trong suốt phiên Live.*

4. **Truy cập Dashboard**:
   - Mở trình duyệt và truy cập: [http://localhost:8000/](http://localhost:8000/)
   - Đây là Giao diện (Command Center) giúp bạn xem bình luận, chốt đơn mua hàng và quản lý tệp khách hàng.
   - Nhấn vào biểu tượng ⚙️ (Cài đặt) trên giao diện Dashboard để nhập **Shop ID** và **API Key** của Pancake POS.

---

## Phần 2: Cài Đặt Công Cụ Quét Bình Luận Trên Facebook

Hệ thống cung cấp hai lựa chọn để quét bình luận. Bạn chỉ cần chọn 1 trong 2 cách dưới đây.

### Cách A: Cài đặt qua Chrome Extension (Khuyên dùng)
1. Mở trình duyệt Chrome (hoặc Cốc Cốc, Edge).
2. Nhập `chrome://extensions/` vào thanh địa chỉ và nhấn Enter.
3. Ở góc trên cùng bên phải, bật chế độ **"Developer mode"** (Chế độ dành cho nhà phát triển).
4. Nhấn nút **"Load unpacked"** (Tải tiện ích đã giải nén) ở góc trái trên cùng.
5. Duyệt đến thư mục `f:\code\Livestream\chrome-extension` và nhấn **"Select Folder"** (Chọn thư mục).
6. Bạn sẽ thấy tiện ích **"Livestream Radar"** xuất hiện. Việc cài đặt Extension đã hoàn tất.

### Cách B: Cài đặt qua Tampermonkey Script (Đã có file .js)
1. Cài đặt tiện ích mở rộng [Tampermonkey](https://www.tampermonkey.net/) vào trình duyệt Chrome.
2. Từ biểu tượng Tampermonkey, chọn **"Create a new script"** (Tạo script mới) hoặc **"Dashboard"**.
3. Kéo thả file `f:\code\Livestream\extension.user.js` vào cửa sổ Tampermonkey và chọn **Install** (Cài đặt).
4. Khi truy cập vào trang quản lý Facebook Live (Producer/Live Dashboard), script sẽ tự động chạy bổ trợ giao diện.

---

## Phần 3: Chạy Thực Tế (Quy Trình Hoạt Động Của Hệ Thống)

Sau khi cài đặt 2 phần trên, làm theo các bước sau mỗi khi bạn tiến hành Livestream:

1. Mở Terminal và chạy Backend `uvicorn main:app --host 0.0.0.0 --port 8000` (ở thư mục `f:\code\Livestream`).
2. Mở trình duyệt Chrome, vào [Trang quản lý Livestream trên Facebook](https://www.facebook.com/live/producer/...). 
   *(Tiện ích mở rộng đã cài đặt sẽ tự chạy lên, bổ sung các nút và thẻ Tier khách hàng vào bên cạnh các bình luận).*
3. Mở thêm 1 Tab ở trình duyệt và truy cập [http://localhost:8000](http://localhost:8000). 
   *(Đây là màn hình Dashboard tổng quan. Tại đây bạn có thể thấy tình trạng khách VIP, KHÁCH MỚI, KHÁCH QUEN, hoặc BOM HÀNG nhảy vào theo gian thực).*
4. Trong lúc Livestream, Backend sẽ chạy ngầm luồng `sync_worker.py` để liên tục lưu trữ và phân loại các tệp khách hàng từ Pancake POS, đảm bảo báo cáo chốt đơn chính xác nhất với độ trễ gần bằng không!
