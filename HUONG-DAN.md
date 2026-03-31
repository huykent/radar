# 📡 Hướng Dẫn Cài Đặt & Sử Dụng — Livestream Radar

## Mục lục
1. [Yêu cầu hệ thống](#1-yêu-cầu-hệ-thống)
2. [Cài đặt Backend](#2-cài-đặt-backend)
3. [Chạy Server](#3-chạy-server)
4. [Cài Chrome Extension](#4-cài-chrome-extension)
5. [Cấu hình Pancake POS](#5-cấu-hình-pancake-pos)
6. [Sử dụng Live](#6-sử-dụng-khi-live)
7. [Xem lại Livestream](#7-xem-lại-livestream-replay)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Yêu cầu hệ thống

- **Python** 3.10 trở lên
- **Google Chrome** (hoặc Edge Chromium)
- **Pancake POS** account (để lấy Shop ID + API Key)

---

## 2. Cài đặt Backend

### Bước 1: Clone / tải project
```
Đặt project vào thư mục, ví dụ: F:\code\Livestream
```

### Bước 2: Tạo virtual environment
```powershell
cd F:\code\Livestream
python -m venv .venv
```

### Bước 3: Kích hoạt venv + cài dependencies
```powershell
# Windows
.venv\Scripts\activate

# Cài packages
pip install -r requirements.txt
```

> **Packages cần thiết:** `fastapi`, `uvicorn`, `aiosqlite`, `httpx`, `jinja2`

---

## 3. Chạy Server

```powershell
cd F:\code\Livestream
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Sau khi chạy thành công, bạn sẽ thấy:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Truy cập Dashboard
Mở trình duyệt → vào `http://localhost:8000`

> 💡 **Tip:** Để chạy ngầm (background), dùng:
> ```powershell
> Start-Process -NoNewWindow .venv\Scripts\python.exe -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000"
> ```

---

## 4. Cài Chrome Extension

### Bước 1: Mở Chrome Extensions
1. Mở Chrome → gõ `chrome://extensions/` vào thanh địa chỉ
2. Bật **Developer mode** (góc trên bên phải)

### Bước 2: Load extension
1. Nhấn **"Load unpacked"**
2. Chọn thư mục `F:\code\Livestream\chrome-extension`
3. Extension **📡 Livestream Radar** sẽ xuất hiện trong danh sách

### Bước 3: Xác nhận
- Mở bất kỳ trang Facebook nào
- Nhìn góc dưới bên phải → sẽ thấy thanh trạng thái `📡 Radar: connected`
- Góc dưới bên trái → nút `▶️ Tải hết` (cho chế độ xem lại)

> ⚠️ **Khi cập nhật Extension:** Sau khi sửa code, vào `chrome://extensions/` → nhấn nút 🔄 (reload) trên extension, rồi refresh trang Facebook.

---

## 5. Cấu hình Pancake POS

### Lấy API Key & Shop ID
1. Đăng nhập **Pancake POS** → `pos.pages.fm`
2. **Shop ID:** Xem trong URL khi đang ở trang quản lý shop
3. **API Key:** Vào **Cài đặt** → **API** → Copy key

### Nhập vào Dashboard
1. Mở `http://localhost:8000`
2. Nhấn **⚙️** (góc trên bên phải)
3. Điền:
   - **Shop ID** — ID của shop trên Pancake
   - **API Key** — Key vừa copy
   - **Sync Interval** — chọn thời gian đồng bộ (mặc định 60 phút)
4. Nhấn **💾 Lưu cài đặt**
5. Nhấn **🔄 Sync ngay** để đồng bộ lần đầu
6. (Tùy chọn) Nhấn **🔄 Tải tags từ Pancake** → chọn tags cần theo dõi

---

## 6. Sử dụng khi Live

### Dashboard (http://localhost:8000)
- **Stats Bar:** Hiển thị tổng Comments, VIP, Khách Quen, Bom Hàng
- **Live Feed:** Hiện comment real-time với badge phân loại:
  - 💎 **KHÁCH VIP** — khách chi >= 2 triệu hoặc >= 5 đơn OK
  - 🟢 **KHÁCH QUEN** — có đơn thành công, không bom
  - ⚪ **KHÁCH MỚI** — chưa có thông tin
  - 🟡 **CHỐT DẠO** — >= 3 đơn nhưng không đơn nào thành công
  - ☠️ **BOM HÀNG** — >= 2 đơn fail hoặc tỷ lệ hủy > 30%
  - ⚠️ **KHÔNG CỌC** — khách được tag "không cọc" trên Pancake

### Facebook Live (Chrome Extension)
- Extension tự động bắt comment → gửi cho backend phân tích
- Badge hiển thị ngay bên cạnh tên commenter trên Facebook
- Nút **⚡ Chốt** / **❌ Hết** giúp thao tác nhanh
- Khách **BOM HÀNG** bị mờ đi tự động

---

## 7. Xem lại Livestream (Replay)

Khi bạn mở lại một video live đã kết thúc trên Facebook:

1. Mở video live replay trên Facebook
2. Nhấn nút **▶️ Tải hết** (góc dưới bên trái)
3. Extension sẽ tự động:
   - Tìm và click **"Xem thêm bình luận"** / **"View more comments"**
   - Cuộn trang để tải thêm comment
   - Quét và gửi tất cả comment cho backend
4. Hiển thị tiến trình: `🔄 X clicks · Y comments`
5. Tự động dừng khi không còn nút tải thêm
6. Nhấn **⏹️ Dừng** nếu muốn dừng sớm

> 💡 **Tip:** Mở Dashboard song song để xem kết quả phân tích real-time

---

## 8. Troubleshooting

### Extension không kết nối được
```
📡 Radar: disconnected
```
- Kiểm tra server đang chạy: `http://localhost:8000`
- Refresh trang Facebook (F5)

### Không thấy badge trên Facebook
- Kiểm tra Console (F12): tìm log `[Radar]`
- Facebook thay đổi DOM thường xuyên → có thể cần cập nhật selector

### Sync Pancake lỗi
- Kiểm tra Shop ID và API Key đúng chưa
- Xem log server trong terminal
- Pancake có giới hạn API rate — đợi 30s rồi thử lại

### Khách hàng hiện "KHÁCH MỚI" dù đã mua
- Nhấn **🔄 Sync ngay** trong Settings
- Kiểm tra khách hàng có phone number trên Pancake không
- Nếu dùng FB UID matching: cần khách đã comment trên live ít nhất 1 lần

### Auto-load không tìm thấy nút
- Facebook có thể dùng ngôn ngữ khác → kiểm tra text của nút "load more"
- Cuộn lên đầu comment section trước khi bấm "Tải hết"
- Thử click thủ công 1 lần trước để Facebook load comment panel

---

## Cấu trúc Project

```
F:\code\Livestream\
├── main.py              # FastAPI backend + WebSocket
├── db.py                # SQLite database module  
├── sync_worker.py       # Pancake POS sync worker
├── tier.py              # Tier calculation engine
├── utils.py             # Phone normalization helpers
├── requirements.txt     # Python dependencies
├── radar.db             # SQLite database (auto-created)
├── templates/
│   └── index.html       # Dashboard UI
└── chrome-extension/
    ├── manifest.json    # Extension config
    ├── content.js       # FB comment capture + auto-load
    ├── content.css      # Injected styles
    ├── background.js    # Service worker
    └── icons/           # Extension icons
```
