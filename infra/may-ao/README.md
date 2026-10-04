# Hệ máy ảo agent Claude — Incus + KVM trên Ubuntu (i9-13900, 64 GB)

Thư mục này mô tả **toàn bộ** cách dựng 7 máy ảo làm agent Claude điều khiển từ xa trên một server Ubuntu,
dưới dạng script + cấu hình có thể chạy lại. **Chưa có gì ở đây được chạy trên server thật** — mọi script đều
có chế độ `--chay-thu` (chỉ in lệnh) để bạn xem trước, và `kiem-tra.sh` hoàn toàn chỉ đọc.

> Lưu ý: kho không có `CLAUDE.md` tại thời điểm viết, nên bộ này dùng quy ước: tiếng Việt không dấu cho tên
> tệp/lệnh, tiếng Việt có dấu cho chú thích, không bí mật trong git (`.gitignore` đã chặn `bien-moi-truong.env`, `*.iso`).

## 1. Tổng quan

```
                 Internet
                    │ NAT (Incus)                      NAT (Incus)
   ┌────────────────┴──────────────────┐   ┌───────────────┴──────────────┐
   │ br-agent  10.90.10.0/24           │   │ br-erp  10.90.20.0/24        │
   │  zalo-1   .11  Win11  E 16-17 8G │   │  erp-thu-nghiem .11          │
   │  zalo-2   .12  Win11  E 18-19 8G │ ✕ │     Ubuntu+Docker (Odoo mẫu) │
   │  tim-hang .21  Linux  E 20-21 6G │   │     E 28-31  8G  60 GiB      │
   │  facebook .22  Linux  E 22-23 6G │   └──────────────────────────────┘
   │  ban-hang .23  Linux  E 24-25 6G │        ✕ = nftables chặn 2 chiều
   │  he-thong .24  Linux  E 26-27 6G │            (mang/cach-ly-erp.nft)
   └───────────────────────────────────┘
   Host: Ubuntu 24.04, Incus (Zabbly), pool ZFS "may-ao", snapshot đêm 02:00
   Nhân P (CPU 0-15) để trống cho host/ZFS/QEMU I/O; 16 nhân E (16-31) ghim 1:1 cho máy ảo.
```

| Máy | Loại | Việc | vCPU (nhân E) | RAM | Đĩa | IP |
|---|---|---|---|---|---|---|
| zalo-1 | Windows 11 | Zalo PC #1 | 16-17 | 8 GiB | 80 GiB | 10.90.10.11 |
| zalo-2 | Windows 11 | Zalo PC #2 | 18-19 | 8 GiB | 80 GiB | 10.90.10.12 |
| tim-hang | Ubuntu 24.04 | Tìm nguồn hàng | 20-21 | 6 GiB | 40 GiB | 10.90.10.21 |
| facebook | Ubuntu 24.04 | Facebook | 22-23 | 6 GiB | 40 GiB | 10.90.10.22 |
| ban-hang | Ubuntu 24.04 | Bán hàng | 24-25 | 6 GiB | 40 GiB | 10.90.10.23 |
| he-thong | Ubuntu 24.04 | Hệ thống | 26-27 | 6 GiB | 40 GiB | 10.90.10.24 |
| erp-thu-nghiem | Ubuntu 24.04 | ERP thử (Odoo mẫu) | 28-31 | 8 GiB | 60 GiB | 10.90.20.11 |
| **Tổng** | | | **16 nhân E** | **48 GiB** | ~380 GiB (thin) | |

Host còn 16 GiB (ZFS ARC giới hạn 6 GiB, phần còn lại cho Incus/QEMU). Đổi số liệu trong `may/*.yaml`.

**Mỗi máy Linux** chạy: XFCE tối giản trên Xvnc `:1` → noVNC cổng 6080; Google Chrome (cổng gỡ lỗi 9222,
điều khiển qua MCP `chrome-devtools`); **Claude Code `remote-control` là dịch vụ systemd**, bạn nối vào từ
claude.ai/code hoặc app điện thoại. **Máy Windows** chạy Zalo PC + Chrome + Claude Code remote-control qua tác vụ
theo lịch khi đăng nhập. **Máy ERP** không có agent, chỉ Docker; tường lửa host chặn mọi lưu thông agent ↔ ERP.

## 2. Cấu trúc thư mục

```
infra/may-ao/
├── README.md                      ← tệp này
├── kiem-tra.sh                    ← bước 1: kiểm tra server, CHỈ ĐỌC
├── cai-incus.sh                   ← bước 2: cài Incus/ZFS/nftables, preseed (--chay-thu)
├── tao-may.sh                     ← bước 3-5: hồ sơ, tạo máy, đẩy bí mật, trạng thái (--chay-thu)
├── snapshot.sh                    ← snapshot tay / khôi phục / xuất (--chay-thu)
├── thu-vien.sh                    ← hàm chung (nạp env, chạy thử, nhận diện nhân E)
├── bien-moi-truong.mau.env        ← mẫu biến/bí mật → chép thành bien-moi-truong.env (gitignore)
├── incus/                         ← preseed.yaml, sysctl, limits, giới hạn ZFS ARC
├── mang/                          ← cach-ly-erp.nft, may-ao-cach-ly.service, kiem-tra-cach-ly.sh
├── ho-so/                         ← 4 profile Incus: may-ao-chung, agent-linux, agent-windows, erp
├── may/                           ← 7 tệp cấu hình riêng từng máy (ghim nhân E, RAM, IP, đĩa)
├── vai-tro/                       ← CLAUDE.md theo vai trò cho từng agent
└── anh-mau/
    ├── linux/   cloud-init-user-data.yaml (ảnh mẫu), cloud-init-tu-mau.yaml, tao-anh-mau.sh
    ├── windows/ autounattend.xml, setup-agent.ps1, claude-remote-control.ps1, dat-token.ps1, chuan-bi-iso.sh
    └── erp/     cloud-init-user-data.yaml, docker-compose.odoo.yaml, odoo.conf
```

## 3. Chuẩn bị (máy admin)

1. Server đã cài Ubuntu Server 24.04 LTS (22.04 cũng được), có SSH, có internet. Bật **Intel VT-x** trong BIOS.
   Nên dành **một NVMe/SSD trống** cho pool ZFS (≥ 1 TB khuyến nghị; tối thiểu ~600 GB).
2. Chép thư mục này lên server:
   ```bash
   rsync -av infra/may-ao/ <user>@<server>:~/may-ao/
   ```
3. Trên máy admin, lấy token Claude cho các agent (cần gói Pro/Max/Team):
   ```bash
   claude setup-token        # dán kết quả vào CLAUDE_CODE_OAUTH_TOKEN
   ```
4. Trên server, tạo tệp bí mật:
   ```bash
   cd ~/may-ao && cp bien-moi-truong.mau.env bien-moi-truong.env && chmod 600 bien-moi-truong.env
   nano bien-moi-truong.env     # CLAUDE_CODE_OAUTH_TOKEN, VNC_MAT_KHAU, WIN_MAT_KHAU, O_DIA_ZFS, NGUOI_DUNG_INCUS
   ```

## 4. Cài từng bước trên server

### Bước 1 — Kiểm tra (chỉ đọc)
```bash
sudo bash ~/may-ao/kiem-tra.sh
```
Đọc phần **Tổng kết**. Quan trọng nhất: cờ `vmx`, `/dev/kvm`, RAM ≥ 60 GiB, và dòng **nhân E**.
Nếu danh sách nhân E **khác `16-31`**, sửa `limits.cpu` trong `may/*.yaml` cho khớp (giữ mỗi máy một khoảng riêng).

### Bước 2 — Cài Incus (xem trước, rồi cài thật)
```bash
sudo bash ~/may-ao/cai-incus.sh --chay-thu      # xem toàn bộ lệnh + preseed sẽ áp dụng
sudo bash ~/may-ao/cai-incus.sh                 # cài: kho Zabbly, incus, zfsutils, nftables, preseed, cách ly ERP
newgrp incus-admin && incus list                # user quản trị (NGUOI_DUNG_INCUS) dùng incus không cần sudo
```
Tuỳ chọn: `--nguon ubuntu` (gói incus 6.0 LTS của Ubuntu), `--ui` (giao diện web, chỉ nghe 127.0.0.1:8443).
Script tạo: pool ZFS `may-ao`, mạng `br-agent` + `br-erp`, profile `default` không mạng, nạp
`/etc/may-ao/cach-ly-erp.nft` qua dịch vụ `may-ao-cach-ly`, sysctl/limits/ARC. Reboot host một lần cho đủ hiệu lực.

### Bước 3 — Hồ sơ máy & cloud-init
```bash
bash ~/may-ao/tao-may.sh --chay-thu thiet-lap
bash ~/may-ao/tao-may.sh thiet-lap
```
Tạo/cập nhật 5 profile (`may-ao-chung`, `agent-linux`, `agent-linux-tu-mau`, `agent-windows`, `erp`), nạp cloud-init
từ `anh-mau/`, và kiểm tra các CPU ghim có đúng là nhân E của host.

### Bước 4 — (Tuỳ chọn, khuyến nghị) Ảnh mẫu Linux
```bash
bash ~/may-ao/anh-mau/linux/tao-anh-mau.sh --chay-thu
bash ~/may-ao/anh-mau/linux/tao-anh-mau.sh          # ~15-25 phút, một lần
```
Dựng máy tạm bằng cloud-init đầy đủ, dọn định danh/bí mật, publish thành ảnh `agent-linux-mau`.
Sau đó 4 máy agent tạo trong ~1 phút. Bỏ qua bước này thì mỗi máy tự cài 10-20 phút (cùng kết quả).

### Bước 5 — Tạo 4 máy Linux + ERP
```bash
bash ~/may-ao/tao-may.sh tao linux          # tim-hang facebook ban-hang he-thong (chờ cloud-init, tự đẩy bí mật)
bash ~/may-ao/tao-may.sh tao erp            # erp-thu-nghiem trên br-erp
bash ~/may-ao/tao-may.sh erp-odoo-mau       # (tuỳ chọn) Odoo 18 + PostgreSQL 16 mẫu
bash ~/may-ao/tao-may.sh trang-thai         # IP + lệnh SSH tunnel
sudo bash ~/may-ao/mang/kiem-tra-cach-ly.sh # xác minh agent ↔ ERP bị chặn, internet vẫn thông
```
Nếu lúc tạo chưa có token: `bash tao-may.sh bi-mat linux` sau. Lệnh `bi-mat` đẩy `/etc/claude-agent/env`
(token), mật khẩu VNC, và `CLAUDE.md` theo vai trò (`vai-tro/<may>.md`) vào máy rồi bật dịch vụ.

**Nối vào agent:**
```bash
# máy admin — noVNC xem màn hình agent
ssh -L 6083:10.90.10.21:6080 <user>@<server>     # → http://localhost:6083/vnc.html  (mật khẩu VNC_MAT_KHAU)
# URL phiên Claude Code remote-control của máy
bash ~/may-ao/tao-may.sh nhat-ky tim-hang
```
Mở claude.ai/code (hoặc app Claude trên điện thoại) → chọn phiên remote-control theo tên máy.

### Bước 6 — Hai máy Windows Zalo
1. Tải ISO Windows 11 từ Microsoft, đặt tại `WIN_ISO` (mặc định `/var/lib/may-ao/iso/Win11.iso`). Cần giấy phép hợp lệ.
2. Chuẩn bị ISO (nhúng driver virtio bằng `distrobuilder repack-windows`, tạo `autounattend-zalo-{1,2}.iso`):
   ```bash
   sudo bash ~/may-ao/anh-mau/windows/chuan-bi-iso.sh --chay-thu
   sudo bash ~/may-ao/anh-mau/windows/chuan-bi-iso.sh
   ```
3. Tạo máy, Windows tự cài không tương tác (~15-25 phút), tự đăng nhập user `agent`, bật RDP, chạy
   `setup-agent.ps1` (Git, Chrome, Zalo PC, Claude Code, tác vụ `ClaudeRemoteControl`):
   ```bash
   bash ~/may-ao/tao-may.sh tao windows
   ```
4. Khi cài xong, gỡ ISO và vào bằng RDP qua SSH tunnel:
   ```bash
   incus config device remove zalo-1 install unattend; incus config device remove zalo-2 install unattend
   ssh -L 3391:10.90.10.11:3389 -L 3392:10.90.10.12:3389 <user>@<server>   # mstsc /v:localhost:3391
   ```
5. Trong Windows (một lần, bằng tay): đăng nhập Zalo PC bằng QR; chạy `C:\agent\dat-token.ps1` (dán token);
   kích hoạt Windows; kiểm tra `C:\agent\logs\remote-control-*.log`. Rồi chụp snapshot mốc:
   ```bash
   bash ~/may-ao/snapshot.sh tao zalo-1 da-dang-nhap-zalo
   ```

## 5. Snapshot đêm, khôi phục, sao lưu

- **Tự động**: profile `may-ao-chung` đặt `snapshots.schedule="0 2 * * *"`, tên `dem-YYYY-MM-DD`, hết hạn 7 ngày
  (ERP 14 ngày, chụp cả khi tắt). Incus tự chạy, không cần cron. Xem: `bash snapshot.sh lich`.
- **Tay**: `bash snapshot.sh tao <may|tat-ca> [nhan]` trước khi làm việc rủi ro; `bash snapshot.sh danh-sach`.
- **Khôi phục**: `bash snapshot.sh khoi-phuc <may> <snap>` (tự chụp bản an toàn, tắt → khôi phục → bật).
- **Sao lưu ngoài host**: `bash snapshot.sh xuat tat-ca` → `/var/lib/may-ao/xuat/*.tar.gz`, rsync/rclone đi nơi khác.
  ERP còn có `cron.daily` dump PostgreSQL vào `/opt/erp/sao-luu/` trong máy.
- Snapshot máy đang chạy là *crash-consistent*; với ERP nên dump DB trước khi khôi phục/xuất quan trọng.

## 6. Cách ly mạng khỏi ERP

- Hai bridge riêng (`br-agent`, `br-erp`), đều NAT ra internet.
- `mang/cach-ly-erp.nft`: bảng `inet may_ao_cach_ly`, chain forward/input ở `priority filter - 10`
  (xét **trước** bảng của Incus) → DROP agent→ERP và ERP→agent; ERP chỉ được dùng DHCP/DNS/ping của host.
  Host vẫn chủ động vào ERP (để admin dùng SSH tunnel tới 8069).
- Mở ngoại lệ (ví dụ `he-thong` → ERP:8069): bỏ `#` dòng tương ứng trong tệp, `sudo systemctl reload may-ao-cach-ly`.
- Xác minh: `sudo bash mang/kiem-tra-cach-ly.sh`. Xem bộ đếm: `sudo nft list table inet may_ao_cach_ly`.
- Profile `default` của Incus cố ý **không có mạng** → máy tạo nhầm sẽ không ra ngoài.

## 7. Vận hành hằng ngày

```bash
incus list                                   # trạng thái, IP, RAM
bash tao-may.sh trang-thai                   # kèm lệnh tunnel
incus exec tim-hang -- systemctl status claude-remote-control xvnc@1 xfce@1 novnc
bash tao-may.sh nhat-ky facebook             # URL phiên remote-control
incus exec tim-hang -- sudo -u agent -H bash -lc 'claude update'     # cập nhật Claude Code một máy
bash anh-mau/linux/tao-anh-mau.sh            # dựng lại ảnh mẫu khi muốn Chrome/Claude mới cho máy tạo sau
incus restart ban-hang                       # khởi động lại máy
bash snapshot.sh don tat-ca 30               # xoá snapshot tay cũ hơn 30 ngày
```

## 8. Bảo mật & lưu ý

- **Token Claude** nằm trong `/etc/claude-agent/env` (root, 0600) từng máy Linux; Windows: biến môi trường user `agent`.
  Thu hồi/đổi: sửa `bien-moi-truong.env` → `bash tao-may.sh bi-mat linux`; Windows: `dat-token.ps1`.
- noVNC/VNC **không mã hoá**, chỉ nghe trong mạng `br-agent`; luôn vào qua SSH tunnel tới host, không mở ra LAN/Internet.
- Agent có `sudo` không mật khẩu **trong máy ảo** — đó là hộp cát. Không cấp cho máy ảo quyền vào socket Incus
  hay SSH root host. Agent `he-thong` chỉ nên có khoá SSH tới đúng đích cần.
- Ảnh mẫu `agent-linux-mau` không chứa token/mật khẩu (đã dọn trong `tao-anh-mau.sh`).
- Windows: tự cung cấp ISO + giấy phép; khoá trong `autounattend.xml` là khoá generic chỉ để Setup chạy.
  `autounattend-*.iso` chứa mật khẩu `agent` → tệp 0600, nằm ngoài git.
- Zalo/Facebook có cơ chế chống tự động hoá; vận hành ở tốc độ người thật, một tài khoản một máy, snapshot sau khi đăng nhập.
- `ufw`/Docker **trên host** có thể chặn forward của bridge Incus. Nếu bắt buộc dùng ufw: cho phép
  `in on br-agent`, `in on br-erp`, `route on br-agent`, `route on br-erp` rồi để nft cách ly siết lại.

## 9. Khắc phục sự cố

| Triệu chứng | Kiểm tra / xử lý |
|---|---|
| `kiem-tra.sh` báo nhân E ≠ 16-31 | `lscpu -e=CPU,CORE,MAXMHZ`; sửa `limits.cpu` trong `may/*.yaml`, `tao-may.sh thiet-lap` cảnh báo nếu lệch |
| Máy Linux tạo xong nhưng không có IP | `incus network show br-agent`; `sudo nft list ruleset \| grep -i incus`; ufw/Docker trên host |
| cloud-init `degraded` | `incus exec <may> -- tail -80 /var/log/cloud-init-output.log` (thường do mạng khi tải Chrome/Node/Claude); sửa rồi `incus exec <may> -- cloud-init clean --logs && incus restart <may>` |
| noVNC không kết nối | `incus exec <may> -- systemctl status xvnc@1 novnc`; mật khẩu tạm: `cat /etc/claude-agent/vnc-tam` |
| Màn hình trống, không Chrome | `systemctl status xfce@1`; `sudo -u agent DISPLAY=:1 XAUTHORITY=/home/agent/.Xauthority /usr/local/bin/chrome-agent.sh` |
| Dịch vụ remote-control không chạy | `journalctl -u claude-remote-control -n 50`; thiếu `/etc/claude-agent/env`? token hết hạn → `claude setup-token` lại |
| `claude remote-control` báo tham số lạ | Phiên bản Claude Code khác: `claude remote-control --help`, chỉnh `CLAUDE_RC_THAM_SO` trong `/etc/claude-agent/env` |
| Windows không boot từ ISO | `incus config show zalo-1` phải có device `install` với `boot.priority=10`; `security.secureboot=false` |
| Windows Setup dừng chờ nhập | Mở console VGA: `incus console zalo-1 --type=vga` (cần `remote-viewer` trên máy có X; hoặc chuyển qua RDP khi đã cài xong) |
| Zalo không có trong winget | `winget search Zalo`; sửa id trong `setup-agent.ps1`, hoặc cài tay từ zalo.me |
| Agent ping được ERP | `systemctl status may-ao-cach-ly`; `nft list table inet may_ao_cach_ly`; dải IP trong `.nft` khớp preseed? |
| Host thiếu RAM | giảm `limits.memory` trong `may/*.yaml` rồi `incus config set <may> limits.memory 5GiB` (máy ảo cần khởi động lại) |

## 10. Việc KHÔNG tự động (cần người làm một lần)

1. Bật VT-x trong BIOS; cài Ubuntu; chọn đĩa cho ZFS (`O_DIA_ZFS`).
2. `claude setup-token` trên máy admin → điền `bien-moi-truong.env`.
3. Đăng nhập Chrome tài khoản công việc trên từng máy agent qua noVNC (Facebook, sàn TMĐT, Gmail…), rồi
   `snapshot.sh tao <may> da-dang-nhap`.
4. Windows: tải ISO, giấy phép, đăng nhập Zalo bằng QR, `dat-token.ps1`.
5. Viết kịch bản/chính sách cho agent vào `/home/agent/work/kich-ban/` (mẫu ý tưởng trong `vai-tro/`).
6. Chọn ERP thật thay Odoo mẫu nếu cần (thay `anh-mau/erp/docker-compose.odoo.yaml`).
