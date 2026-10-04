# Máy `forge` — bàn làm việc của NGƯỜI (ngoài runtime, cài tay)

`forge` là máy/VM **của người dùng** (laptop hoặc một VM riêng, KHÔNG nằm trong 8 instance và KHÔNG do script hạ tầng tạo).
Đây là nơi duy nhất người dùng có thể chạy Claude Code + Remote Control để điều khiển công việc dev bằng tay.

## Quy tắc (sửa lỗi của thiết kế cũ)

1. **Đăng nhập bằng tài khoản người dùng, tương tác**: `claude auth login` (mở trình duyệt/đăng nhập thiết bị).
   Remote Control **không** nhận token dài hạn tạo bằng lệnh `setup-token` và **không** nhận API key — thiết kế cũ
   (đẩy token vào mọi VM + dịch vụ systemd) vì thế vốn đã hỏng và đã bị gỡ.
2. **Chỉ chạy tay** trên `forge`: `claude remote-control` trong một phiên terminal của người dùng. Không systemd, không
   cloud-init, không script nào trong `infra/` được phép bật nó. Không có token/API key Claude trong git hay trong VM.
3. **Không phải dependency của runtime**: ZeusVN Brain (zeus-core + thin worker) chạy đầy đủ khi Claude Cloud/Remote
   Control không tồn tại (`CLAUDE_CLOUD_AVAILABLE=false`, kiểm bằng `scripts/cloud_exit_check.sh`).
4. `forge` gọi vào hệ thống như một người dùng bình thường: Workbench (`/wb`) hoặc Control API qua `ssh -L` tới zeus-core,
   qua approval/policy như mọi nguồn khác. Không SSH vào host ERP production.
5. Thin worker (`w-code-*`, `w-browser`, `w-ops`) **không bao giờ** cài Claude Code, LLM hay API key; chúng chỉ chạy
   `zeus_worker` với token riêng và executor có kiểu.
