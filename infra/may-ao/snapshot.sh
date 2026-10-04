#!/usr/bin/env bash
# snapshot.sh — Quản lý snapshot cho 7 máy ảo.
#
# Snapshot ĐÊM đã được Incus tự chạy theo hồ sơ may-ao-chung:
#   snapshots.schedule "0 2 * * *", tên dem-YYYY-MM-DD, hết hạn 7 ngày (ERP: 14 ngày).
# Script này bổ sung thao tác tay:
#
#   bash snapshot.sh danh-sach [may]            # liệt kê snapshot (mọi máy hoặc 1 máy)
#   bash snapshot.sh tao <may|tat-ca> [nhan]    # chụp tay, tên tay-<nhan|HHMM>-YYYY-MM-DD (không hết hạn)
#   bash snapshot.sh khoi-phuc <may> <snap>     # tắt máy → khôi phục → bật lại (HỎI xác nhận)
#   bash snapshot.sh xoa <may> <snap>           # xoá 1 snapshot
#   bash snapshot.sh don <may|tat-ca> <ngay>    # xoá snapshot TAY cũ hơn N ngày (snapshot đêm tự hết hạn)
#   bash snapshot.sh xuat <may|tat-ca> [thu-muc] # xuất máy ra tarball (mặc định /var/lib/may-ao/xuat) — sao lưu ngoài host
#   bash snapshot.sh lich                       # xem lịch snapshot hiện áp dụng cho từng máy
#   Thêm --chay-thu trước lệnh để chỉ in ra.
set -euo pipefail
# shellcheck source=thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/thu-vien.sh"

[[ "${1:-}" == "--chay-thu" ]] && { CHAY_THU=1; shift; }
LENH="${1:-}"; shift || true
[[ -n "$LENH" ]] || { sed -n '2,17p' "$0"; exit 1; }
incus_san_sang || [[ $CHAY_THU == 1 ]] || chet "Không nối được Incus (chạy bằng user trong nhóm incus-admin)."

# danh sách máy đang tồn tại trong Incus thuộc kế hoạch
may_ton_tai() {
  local m; for m in "${THU_TU_MAY[@]}"; do
    if [[ $CHAY_THU == 1 ]] || incus info "$m" >/dev/null 2>&1; then echo "$m"; fi
  done
}
giai_may() { # "tat-ca" → mọi máy có; tên → tên
  if [[ "$1" == tat-ca ]]; then may_ton_tai; else echo "$1"; fi
}

case "$LENH" in
  danh-sach)
    if [[ -n "${1:-}" ]]; then incus snapshot list "$1"
    else
      for m in $(may_ton_tai); do tieu_de "$m"; incus snapshot list "$m" -f compact 2>/dev/null || true; done
    fi ;;

  tao)
    [[ -n "${1:-}" ]] || chet "Thiếu tên máy (hoặc tat-ca)."
    NHAN="${2:-$(date +%H%M)}"; TEN="tay-${NHAN}-$(date +%F)"
    for m in $(giai_may "$1"); do
      thong_bao "Chụp $m → $TEN (máy đang chạy: snapshot kiểu crash-consistent; ERP nên dump DB trước)"
      chay incus snapshot create "$m" "$TEN"
    done ;;

  khoi-phuc)
    [[ -n "${1:-}" && -n "${2:-}" ]] || chet "Cách dùng: khoi-phuc <may> <snap>"
    M="$1"; S="$2"
    incus snapshot show "$M" "$S" >/dev/null 2>&1 || [[ $CHAY_THU == 1 ]] || chet "Không có snapshot $S của $M."
    canh_bao "Khôi phục $M về '$S' sẽ MẤT mọi thay đổi sau thời điểm đó."
    if [[ $CHAY_THU != 1 ]]; then read -r -p "Gõ 'dong-y' để tiếp tục: " XN; [[ "$XN" == dong-y ]] || chet "Huỷ."; fi
    DANG_CHAY=0; [[ $CHAY_THU == 1 ]] || { [[ "$(incus list "$M" -c s -f csv)" == RUNNING ]] && DANG_CHAY=1; }
    thong_bao "Chụp thêm bản an toàn trước khi khôi phục"
    chay incus snapshot create "$M" "truoc-khoi-phuc-$(date +%F-%H%M)"
    chay incus stop "$M" --timeout 90 || chay incus stop "$M" --force
    chay incus snapshot restore "$M" "$S"
    [[ $DANG_CHAY == 1 || $CHAY_THU == 1 ]] && chay incus start "$M"
    dat "Đã khôi phục $M về $S." ;;

  xoa)
    [[ -n "${1:-}" && -n "${2:-}" ]] || chet "Cách dùng: xoa <may> <snap>"
    chay incus snapshot delete "$1" "$2" ;;

  don)
    [[ -n "${1:-}" && -n "${2:-}" ]] || chet "Cách dùng: don <may|tat-ca> <ngay>"
    NGAY="$2"; MOC=$(( $(date +%s) - NGAY*86400 ))
    for m in $(giai_may "$1"); do
      # chỉ đụng snapshot tay-*/truoc-khoi-phuc-* ; snapshot dem-* để Incus tự hết hạn
      while IFS=, read -r ten luc; do
        [[ "$ten" =~ ^(tay|truoc-khoi-phuc)- ]] || continue
        T=$(date -d "$luc" +%s 2>/dev/null || echo 0)
        if (( T > 0 && T < MOC )); then chay incus snapshot delete "$m" "$ten"; fi
      done < <(incus snapshot list "$m" -f csv -c nT 2>/dev/null || true)
    done ;;

  xuat)
    [[ -n "${1:-}" ]] || chet "Thiếu tên máy (hoặc tat-ca)."
    TM="${2:-/var/lib/may-ao/xuat}"; chay install -d -m 0750 "$TM"
    for m in $(giai_may "$1"); do
      TEP="$TM/$m-$(date +%F).tar.gz"
      thong_bao "Xuất $m → $TEP (chỉ máy, không kèm snapshot; dùng --optimized-storage cho ZFS)"
      chay incus export "$m" "$TEP" --instance-only --optimized-storage
    done
    thong_bao "Chép tarball ra NAS/đám mây bằng rsync/rclone. Nhập lại: incus import <tep>" ;;

  lich)
    printf '%-16s %-14s %-8s %-40s\n' MAY LICH HET_HAN MAU_TEN
    for m in $(may_ton_tai); do
      if [[ $CHAY_THU == 1 ]]; then echo "(chạy thử) incus config get $m snapshots.schedule --expanded"; continue; fi
      printf '%-16s %-14s %-8s %-40s\n' "$m" \
        "$(incus config get "$m" snapshots.schedule --expanded 2>/dev/null || echo '-')" \
        "$(incus config get "$m" snapshots.expiry --expanded 2>/dev/null || echo '-')" \
        "$(incus config get "$m" snapshots.pattern --expanded 2>/dev/null || echo '-')"
    done ;;

  *) chet "Lệnh lạ: $LENH (xem đầu tệp)";;
esac
