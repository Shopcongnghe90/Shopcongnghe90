#!/usr/bin/env bash
# cai-incus.sh — Cài Incus + KVM + ZFS + nftables trên Ubuntu, khởi tạo pool/mạng theo preseed.
#
#   sudo bash cai-incus.sh --chay-thu            # CHỈ IN lệnh sẽ chạy + preflight, không đổi gì (chạy trước)
#   sudo ZEUS_G1_DUYET=<mã duyệt G1> bash cai-incus.sh --xac-nhan-rui-ro-erp-production   # cài thật
#   (ERP production chạy trên host này: không có cờ + mã duyệt + preflight đạt thì script TỪ CHỐI — ADR-012)
#   sudo bash cai-incus.sh --nguon ubuntu        # dùng gói incus của Ubuntu (6.0 LTS) thay kho Zabbly
#   sudo bash cai-incus.sh --ui                  # cài thêm giao diện web incus-ui-canonical (chỉ bind 127.0.0.1)
#
# Biến từ bien-moi-truong.env: O_DIA_ZFS, KICH_CO_POOL, ZFS_ARC_MAX, NGUOI_DUNG_INCUS, ZEUS_G1_DUYET.
# Idempotent ở mức hợp lý: chạy lại không phá pool/mạng đã có.
set -euo pipefail
# shellcheck source=thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/thu-vien.sh"

NGUON=zabbly; CAI_UI=0
doc_co_chung "$@"
set -- "${CON_LAI[@]}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --nguon) NGUON="$2"; shift ;;
    --ui) CAI_UI=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) chet "Tham số lạ: $1 (xem --help)" ;;
  esac; shift
done
[[ "$NGUON" =~ ^(zabbly|ubuntu)$ ]] || chet "--nguon phải là zabbly hoặc ubuntu"

tieu_de "cai-incus.sh — $([[ $CHAY_THU == 1 ]] && echo 'CHẾ ĐỘ CHẠY THỬ (không thay đổi gì)' || echo 'CÀI THẬT')"
nap_bien_moi_truong
if [[ $EUID -ne 0 && $CHAY_THU != 1 ]]; then chet "Cần chạy bằng sudo/root (hoặc thêm --chay-thu để xem lệnh)."; fi

# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu ]] || chet "Script dành cho Ubuntu (phát hiện: ${PRETTY_NAME:-?})"
case "${VERSION_ID:-}" in 22.04|24.04) ;; *) canh_bao "Ubuntu ${VERSION_ID} chưa kiểm chứng; tiếp tục với codename ${VERSION_CODENAME}";; esac
NGUOI_DUNG_INCUS="${NGUOI_DUNG_INCUS:-${SUDO_USER:-}}"

bao_ve_host_that "cài gói (incus, zfs, nftables), sysctl/limits/ZFS ARC, pool ZFS + 5 bridge, nftables đa bridge"

# ---------------------------------------------------------------------------
tieu_de "1/7 Gói nền: ZFS, nftables, công cụ"
export DEBIAN_FRONTEND=noninteractive
chay apt-get update -qq
chay apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg zfsutils-linux nftables bridge-utils

# ---------------------------------------------------------------------------
tieu_de "2/7 Cài Incus (nguồn: $NGUON)"
if [[ "$NGUON" == zabbly ]]; then
  if [[ ! -f /etc/apt/keyrings/zabbly.asc ]] || [[ $CHAY_THU == 1 ]]; then
    chay install -d -m 0755 /etc/apt/keyrings
    chay_sh "curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc"
  else thong_bao "Khoá Zabbly đã có."; fi
  ghi_tep /etc/apt/sources.list.d/zabbly-incus-stable.sources 0644 <<TEP
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: ${VERSION_CODENAME}
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/zabbly.asc
TEP
  chay apt-get update -qq
  GOI=(incus)   # gói Zabbly kèm QEMU, OVMF, swtpm, incus-agent cho máy ảo
  [[ $CAI_UI == 1 ]] && GOI+=(incus-ui-canonical)
  chay apt-get install -y -qq "${GOI[@]}"
else
  # Gói Ubuntu 24.04: incus 6.0 LTS; cần thêm QEMU/OVMF/swtpm cho máy ảo
  GOI=(incus incus-agent qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools)
  [[ $CAI_UI == 1 ]] && canh_bao "--ui: gói incus-ui-canonical chỉ có ở kho Zabbly; bỏ qua."
  chay apt-get install -y -qq --no-install-recommends "${GOI[@]}"
fi
chay systemctl enable --now incus.socket incus.service 2>/dev/null || chay systemctl enable --now incus

if [[ -n "$NGUOI_DUNG_INCUS" ]]; then
  chay usermod -aG incus-admin "$NGUOI_DUNG_INCUS"
  thong_bao "User $NGUOI_DUNG_INCUS cần đăng xuất/đăng nhập lại (hoặc newgrp incus-admin)."
else
  canh_bao "Không biết user quản trị (đặt NGUOI_DUNG_INCUS trong bien-moi-truong.env) — chỉ root dùng được incus."
fi

# ---------------------------------------------------------------------------
tieu_de "3/7 Tinh chỉnh host: sysctl, limits, ZFS ARC"
chay install -m 0644 "$THU_MUC_GOC/incus/sysctl-incus.conf" /etc/sysctl.d/99-may-ao-incus.conf
chay install -m 0644 "$THU_MUC_GOC/incus/limits-incus.conf" /etc/security/limits.d/99-may-ao-incus.conf
chay_sh "sed 's/__ZFS_ARC_MAX__/${ZFS_ARC_MAX}/' '$THU_MUC_GOC/incus/zfs-may-ao.conf' > /etc/modprobe.d/zfs-may-ao.conf"
chay sysctl --system -q
if [[ -w /sys/module/zfs/parameters/zfs_arc_max ]] || [[ $CHAY_THU == 1 ]]; then
  chay_sh "echo ${ZFS_ARC_MAX} > /sys/module/zfs/parameters/zfs_arc_max"
else thong_bao "Module zfs chưa nạp — giới hạn ARC có hiệu lực sau reboot."; fi

# ---------------------------------------------------------------------------
tieu_de "4/7 Khởi tạo Incus (preseed: pool ZFS may-ao + br-core/agent/ops/erp-test/dmz)"
if [[ $CHAY_THU != 1 ]] && incus storage show may-ao >/dev/null 2>&1; then
  thong_bao "Pool may-ao đã tồn tại → bỏ qua preseed (dùng tao-may.sh thiet-lap để kiểm tra mạng/hồ sơ)."
else
  if [[ -n "${O_DIA_ZFS:-}" ]]; then
    [[ -b "$O_DIA_ZFS" ]] || [[ $CHAY_THU == 1 ]] || chet "O_DIA_ZFS=$O_DIA_ZFS không phải thiết bị khối."
    if [[ $CHAY_THU != 1 ]] && lsblk -nro FSTYPE,MOUNTPOINT "$O_DIA_ZFS" | grep -q '[^[:space:]]'; then
      chet "$O_DIA_ZFS đang có hệ tệp/được mount — KHÔNG ghi đè. Chọn đĩa trống khác."
    fi
    NGUON_POOL="source: $O_DIA_ZFS"
    canh_bao "Sẽ dùng TOÀN BỘ $O_DIA_ZFS cho pool ZFS (dữ liệu trên đó sẽ mất)."
  else
    NGUON_POOL="size: ${KICH_CO_POOL}"
    canh_bao "Không đặt O_DIA_ZFS → pool loop ${KICH_CO_POOL} tại /var/lib/incus/disks (chậm hơn đĩa riêng)."
  fi
  PRESEED="$(sed "s|__NGUON_POOL__|${NGUON_POOL}|" "$THU_MUC_GOC/incus/preseed.yaml")"
  printf '%s--- preseed ---%s\n%s\n%s--- hết ---%s\n' "$MAU_XAM" "$MAU_HET" "$PRESEED" "$MAU_XAM" "$MAU_HET"
  if [[ $CHAY_THU == 1 ]]; then thong_bao "(chạy thử) incus admin init --preseed <<< preseed"
  else printf '%s\n' "$PRESEED" | incus admin init --preseed; dat "Preseed xong."; fi
fi

# ---------------------------------------------------------------------------
tieu_de "5/7 Cách ly mạng đa bridge (nftables)"
chay install -d -m 0755 /etc/zeus
chay install -m 0644 "$THU_MUC_GOC/mang/cach-ly-mang.nft" /etc/zeus/cach-ly-mang.nft
chay install -m 0644 "$THU_MUC_GOC/mang/zeus-cach-ly.service" /etc/systemd/system/zeus-cach-ly.service
chay nft -c -f "$THU_MUC_GOC/mang/cach-ly-mang.nft"   # kiểm cú pháp TRƯỚC khi nạp
chay systemctl daemon-reload
chay systemctl enable --now zeus-cach-ly.service

# ---------------------------------------------------------------------------
tieu_de "6/7 Thư mục model GPU & xuất snapshot"
chay install -d -m 0755 /var/lib/zeus/models /var/lib/may-ao/xuat
if [[ $CAI_UI == 1 && "$NGUON" == zabbly ]]; then
  # UI chỉ nghe trên localhost; từ máy admin: ssh -L 8443:127.0.0.1:8443 <server> → https://localhost:8443
  chay incus config set core.https_address 127.0.0.1:8443
fi

# ---------------------------------------------------------------------------
tieu_de "7/7 Kiểm tra sau cài"
if [[ $CHAY_THU == 1 ]]; then
  thong_bao "(chạy thử) incus version; incus storage list; incus network list; nft list table inet zeus_cach_ly"
else
  incus version || true
  incus storage list || true
  incus network list || true
  nft list table inet zeus_cach_ly >/dev/null 2>&1 && dat "Bảng cách ly mạng đã nạp." || canh_bao "Bảng cách ly chưa nạp — systemctl status zeus-cach-ly"
fi

tieu_de "Xong"
cat <<HD
Bước tiếp theo:
  1. ${NGUOI_DUNG_INCUS:+Đăng nhập lại user $NGUOI_DUNG_INCUS (newgrp incus-admin) rồi }kiểm tra: incus list
  2. bash $THU_MUC_GOC/tao-may.sh --chay-thu thiet-lap     # xem hồ sơ sẽ tạo
  3. bash $THU_MUC_GOC/tao-may.sh thiet-lap
  4. (tuỳ chọn) bash $THU_MUC_GOC/anh-mau/linux/tao-anh-mau.sh   # dựng ảnh mẫu Linux một lần
  5. bash $THU_MUC_GOC/tao-may.sh tao tat-ca     # 8 instance; rồi: tao-may.sh dua-worker w-code-1 ...
  6. Windows GUI (w-gui-win): TẮT mặc định, gate G8 — xem luu-tru/windows-gui-g8/
Reboot host một lần để limits/ZFS ARC có hiệu lực đầy đủ (không bắt buộc ngay).
HD
