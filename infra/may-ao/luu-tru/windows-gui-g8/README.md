# Windows GUI fallback (w-gui-win) — LƯU TRỮ, TẮT MẶC ĐỊNH

Theo kiến trúc (mục 5), 2 VM Windows Zalo PC và máy Facebook cá nhân đã BỊ GỠ khỏi luồng tự động:

- Zalo: dùng Zalo Bot Platform + Zalo OA API (workstream D). Tự động hoá giao diện Zalo PC có rủi ro khoá tài khoản.
- Facebook cá nhân: không có API, điều khoản cấm. Fanpage đi qua Messenger Platform.
- `autounattend.xml` giữ lại để tham khảo. `setup-agent.ps1`, tác vụ điều khiển từ xa và `dat-token.ps1` đã gỡ (không còn token Claude trên bất kỳ máy nào).
- Chỉ bật lại khi gate **G8** được duyệt, dưới dạng `w-gui-win` chạy `zeus_worker` (Windows) với token riêng, mượn 6 GB từ phần dự phòng.
