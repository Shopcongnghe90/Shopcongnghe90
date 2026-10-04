#!/usr/bin/env bash
# chuan-bi-iso.sh — Chuẩn bị ISO cho 2 máy Windows Zalo (chạy trên HOST, cần sudo):
#   1. Cài distrobuilder + công cụ; tải virtio-win.iso (driver đĩa/mạng cho KVM)
#   2. Đóng gói lại ISO Windows 11 có nhúng driver virtio  →  $THU_MUC_ISO/Win11.incus.iso
#   3. Tạo ISO autounattend riêng cho từng máy (zalo-1, zalo-2) kèm setup-agent.ps1, claude-remote-control.ps1,
#      dat-token.ps1, CLAUDE.md                                 →  $THU_MUC_ISO/autounattend-<may>.iso
#
#   sudo bash chuan-bi-iso.sh --chay-thu          # chỉ in lệnh
#   sudo bash chuan-bi-iso.sh                      # làm thật (ISO Windows: WIN_ISO trong bien-moi-truong.env)
#   sudo bash chuan-bi-iso.sh --chi-autounattend   # bỏ qua bước 1-2, chỉ tạo lại ISO autounattend
#
# ISO Windows 11 phải tự tải từ Microsoft (https://www.microsoft.com/software-download/windows11) và có giấy phép.
set -euo pipefail
# shellcheck source=../../thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../thu-vien.sh"
TM_WIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHI_AU=0
while [[ $# -gt 0 ]]; do
  case "$1" in --chay-thu) CHAY_THU=1;; --chi-autounattend) CHI_AU=1;; -h|--help) sed -n '2,13p' "$0"; exit 0;; *) chet "Tham số lạ: $1";; esac; shift
done
tieu_de "chuan-bi-iso.sh — $([[ $CHAY_THU == 1 ]] && echo 'CHẠY THỬ' || echo 'LÀM THẬT')"
nap_bien_moi_truong
[[ $EUID -eq 0 || $CHAY_THU == 1 ]] || chet "Cần sudo (mount ISO, snap install)."
: "${WIN_MAT_KHAU:?Đặt WIN_MAT_KHAU trong bien-moi-truong.env (mật khẩu user agent trên Windows)}"
[[ "$WIN_MAT_KHAU" =~ [\&\<\>\"\'] ]] && chet "WIN_MAT_KHAU không được chứa & < > \" ' (nằm trong XML)."

VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
VIRTIO="$THU_MUC_ISO/virtio-win.iso"
RA="$THU_MUC_ISO/Win11.incus.iso"

if [[ $CHI_AU == 0 ]]; then
  tieu_de "1/3 Công cụ: distrobuilder, wimtools, genisoimage"
  chay apt-get install -y -qq --no-install-recommends libguestfs-tools wimtools rsync genisoimage
  if ! command -v distrobuilder >/dev/null 2>&1; then chay snap install distrobuilder --classic; else thong_bao "distrobuilder đã có."; fi
  if [[ ! -f "$VIRTIO" ]]; then chay_sh "curl -fL --retry 3 -o '$VIRTIO' '$VIRTIO_URL'"; else thong_bao "virtio-win.iso đã có."; fi

  tieu_de "2/3 Đóng gói lại ISO Windows với driver virtio"
  [[ -f "$WIN_ISO" || $CHAY_THU == 1 ]] || chet "Không thấy $WIN_ISO — tải ISO Windows 11 và đặt WIN_ISO."
  if [[ -f "$RA" ]]; then thong_bao "$RA đã có — xoá nếu muốn làm lại."; else
    chay distrobuilder repack-windows "$WIN_ISO" "$RA" --windows-version=w11 --drivers="$VIRTIO"
  fi
fi

tieu_de "3/3 ISO autounattend cho từng máy"
command -v genisoimage >/dev/null 2>&1 || [[ $CHAY_THU == 1 ]] || chet "Thiếu genisoimage."
for may in zalo-1 zalo-2; do
  TMP="$(mktemp -d)"
  # Windows giới hạn tên máy 15 ký tự, không dấu; dùng dạng ZALO-1
  TEN_WIN="$(tr '[:lower:]' '[:upper:]' <<<"$may")"
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) tạo $THU_MUC_ISO/autounattend-$may.iso với ComputerName=$TEN_WIN"
  else
    sed -e "s|__TEN_MAY__|$TEN_WIN|g" -e "s|__MAT_KHAU__|$WIN_MAT_KHAU|g" "$TM_WIN/autounattend.xml" > "$TMP/autounattend.xml"
    cp "$TM_WIN/setup-agent.ps1" "$TM_WIN/claude-remote-control.ps1" "$TM_WIN/dat-token.ps1" "$TMP/"
    cp "$THU_MUC_GOC/vai-tro/zalo.md" "$TMP/CLAUDE.md"
    genisoimage -quiet -o "$THU_MUC_ISO/autounattend-$may.iso" -V OEMDRV -J -r "$TMP"
    chmod 0600 "$THU_MUC_ISO/autounattend-$may.iso"   # chứa mật khẩu
    dat "Đã tạo $THU_MUC_ISO/autounattend-$may.iso"
  fi
  rm -rf "$TMP"
done

tieu_de "Xong"
cat <<HD
Tiếp theo trên host:
  bash $THU_MUC_GOC/tao-may.sh tao windows        # tạo zalo-1, zalo-2, gắn ISO, vTPM, bật máy
Windows tự cài ~15-25 phút. Sau đó RDP từ máy admin:
  ssh -L 3391:10.90.10.11:3389 -L 3392:10.90.10.12:3389 <server>   → mstsc localhost:3391 / :3392 (user agent)
Trong máy: đăng nhập Zalo bằng QR, chạy C:\agent\dat-token.ps1, kích hoạt Windows, rồi chụp snapshot:
  bash $THU_MUC_GOC/snapshot.sh tao zalo-1 da-dang-nhap-zalo
HD
