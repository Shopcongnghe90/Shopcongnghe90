#!/usr/bin/env bash
# thu-vien.sh — hàm dùng chung cho các script trong infra/may-ao.
# Được `source` bởi kiem-tra.sh, cai-incus.sh, tao-may.sh, snapshot.sh ...
# Không chạy trực tiếp.

# shellcheck disable=SC2034
THU_MUC_GOC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- Màu & in ấn --------------------------------------------------------
if [[ -t 1 ]]; then
  MAU_XANH=$'\e[32m'; MAU_VANG=$'\e[33m'; MAU_DO=$'\e[31m'; MAU_XAM=$'\e[90m'; MAU_DAM=$'\e[1m'; MAU_HET=$'\e[0m'
else
  MAU_XANH=""; MAU_VANG=""; MAU_DO=""; MAU_XAM=""; MAU_DAM=""; MAU_HET=""
fi

tieu_de() { printf '\n%s== %s ==%s\n' "$MAU_DAM" "$*" "$MAU_HET"; }
thong_bao() { printf '%s[.]%s %s\n' "$MAU_XAM" "$MAU_HET" "$*"; }
dat()       { printf '%s[ĐẠT]%s  %s\n' "$MAU_XANH" "$MAU_HET" "$*"; }
canh_bao()  { printf '%s[CẢNH BÁO]%s %s\n' "$MAU_VANG" "$MAU_HET" "$*"; }
loi()       { printf '%s[LỖI]%s  %s\n' "$MAU_DO" "$MAU_HET" "$*" >&2; }
chet()      { loi "$*"; exit 1; }

# ---------- Chế độ chạy thử -----------------------------------------------------
# CHAY_THU=1  => chỉ in lệnh, không thực thi. Bật bằng cờ --chay-thu.
CHAY_THU="${CHAY_THU:-0}"

# in_lenh <lệnh...> : in lệnh dạng dễ đọc (bọc nháy đơn khi cần, giữ tiếng Việt).
in_lenh() {
  local ra="" a
  for a in "$@"; do
    if [[ "$a" =~ ^[A-Za-z0-9_./:=,@%+-]+$ ]]; then ra+="$a "; else ra+="'${a//\'/\'\\\'\'}' "; fi
  done
  printf '%s$ %s%s\n' "$MAU_XAM" "${ra% }" "$MAU_HET"
}

# chay <lệnh...> : in lệnh rồi thực thi (trừ khi CHAY_THU=1).
chay() {
  in_lenh "$@"
  if [[ "$CHAY_THU" == "1" ]]; then return 0; fi
  "$@"
}

# chay_kin "<mô tả>" <lệnh...> : như chay nhưng KHÔNG in tham số (có bí mật), chỉ in mô tả.
chay_kin() {
  local mo_ta="$1"; shift
  printf '%s$ %s   (ẩn bí mật)%s\n' "$MAU_XAM" "$mo_ta" "$MAU_HET"
  if [[ "$CHAY_THU" == "1" ]]; then return 0; fi
  "$@"
}

# chay_sh "<chuỗi lệnh shell>" : như trên nhưng qua bash -c (cho ống/chuyển hướng).
chay_sh() {
  printf '%s$ %s%s\n' "$MAU_XAM" "$1" "$MAU_HET"
  if [[ "$CHAY_THU" == "1" ]]; then return 0; fi
  bash -c "$1"
}

# ghi_tep <đường dẫn> <quyền> : ghi stdin vào tệp (hoặc in ra khi chạy thử).
ghi_tep() {
  local duong_dan="$1" quyen="${2:-0644}" noi_dung
  noi_dung="$(cat)"
  if [[ "$CHAY_THU" == "1" ]]; then
    printf '%s--- sẽ ghi %s (quyền %s) ---%s\n%s\n%s--- hết ---%s\n' \
      "$MAU_XAM" "$duong_dan" "$quyen" "$MAU_HET" "$noi_dung" "$MAU_XAM" "$MAU_HET"
    return 0
  fi
  install -d -m 0755 "$(dirname "$duong_dan")"
  printf '%s\n' "$noi_dung" > "$duong_dan"
  chmod "$quyen" "$duong_dan"
  thong_bao "đã ghi $duong_dan"
}

# ---------- Biến môi trường -----------------------------------------------------
# Nạp bien-moi-truong.env nếu có (bí mật, bị .gitignore). Không bắt buộc.
nap_bien_moi_truong() {
  local tep="$THU_MUC_GOC/bien-moi-truong.env"
  if [[ -f "$tep" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$tep"
    set +a
    thong_bao "đã nạp $tep"
  else
    thong_bao "không có $tep — dùng giá trị mặc định (xem bien-moi-truong.mau.env)"
  fi
  : "${KICH_CO_POOL:=600GiB}"
  : "${ZFS_ARC_MAX:=6442450944}"
  : "${THU_MUC_ISO:=/var/lib/may-ao/iso}"
  : "${WIN_ISO:=$THU_MUC_ISO/Win11.iso}"
  : "${MANG_AGENT:=10.90.10.0/24}"
  : "${MANG_ERP:=10.90.20.0/24}"
}

# ---------- Nhận diện nhân P/E ---------------------------------------------------
# Trên Intel lai (Alder/Raptor Lake): nhân P có 2 luồng SMT (danh sách anh em có 2 CPU),
# nhân E không có SMT (danh sách anh em chỉ có chính nó). Dựa vào đó để tách.
# Kết quả: mảng NHAN_P, NHAN_E (số CPU logic).
nhan_dien_nhan() {
  NHAN_P=(); NHAN_E=()
  local cpu so anh_em
  for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    so="${cpu##*cpu}"
    [[ -r "$cpu/topology/thread_siblings_list" ]] || continue
    anh_em="$(<"$cpu/topology/thread_siblings_list")"
    if [[ "$anh_em" == "$so" ]]; then NHAN_E+=("$so"); else NHAN_P+=("$so"); fi
  done
  # sắp xếp số (giữ mảng rỗng đúng là rỗng)
  if (( ${#NHAN_P[@]} > 0 )); then mapfile -t NHAN_P < <(printf '%s\n' "${NHAN_P[@]}" | sort -n); fi
  if (( ${#NHAN_E[@]} > 0 )); then mapfile -t NHAN_E < <(printf '%s\n' "${NHAN_E[@]}" | sort -n); fi
}

# module_da_nap <tên> : đọc /proc/modules (không cần lsmod)
module_da_nap() { grep -q "^$1 " /proc/modules 2>/dev/null; }

# gop_khoang 16 17 18 20 => "16-18,20"
gop_khoang() {
  local -a ds=("$@"); local ket="" dau="" truoc=""
  [[ ${#ds[@]} -eq 0 ]] && { printf ''; return; }
  for n in "${ds[@]}"; do
    if [[ -z "$dau" ]]; then dau=$n; truoc=$n; continue; fi
    if (( n == truoc + 1 )); then truoc=$n; continue; fi
    ket+="${ket:+,}$([[ $dau == "$truoc" ]] && echo "$dau" || echo "$dau-$truoc")"
    dau=$n; truoc=$n
  done
  ket+="${ket:+,}$([[ $dau == "$truoc" ]] && echo "$dau" || echo "$dau-$truoc")"
  printf '%s' "$ket"
}

# mo_khoang "16-17,20" => "16 17 20"
mo_khoang() {
  local phan a b
  IFS=',' read -ra phan <<<"$1"
  for p in "${phan[@]}"; do
    if [[ "$p" == *-* ]]; then a=${p%-*}; b=${p#*-}; seq "$a" "$b"; else echo "$p"; fi
  done | tr '\n' ' '
}

# ---------- Incus ---------------------------------------------------------------
co_incus() { command -v incus >/dev/null 2>&1; }

incus_san_sang() {
  co_incus || return 1
  incus info >/dev/null 2>&1
}

# Danh sách 7 máy & loại. Giữ đồng bộ với may/*.yaml và vai-tro/*.md
# shellcheck disable=SC2034
declare -A LOAI_MAY=(
  [zalo-1]=windows
  [zalo-2]=windows
  [tim-hang]=linux
  [facebook]=linux
  [ban-hang]=linux
  [he-thong]=linux
  [erp-thu-nghiem]=erp
)
# shellcheck disable=SC2034
THU_TU_MAY=(zalo-1 zalo-2 tim-hang facebook ban-hang he-thong erp-thu-nghiem)

# Đọc địa chỉ IP tĩnh khai báo trong may/<ten>.yaml (dòng ipv4.address)
ip_cua_may() {
  local tep="$THU_MUC_GOC/may/$1.yaml"
  [[ -f "$tep" ]] || return 1
  sed -n 's/^[[:space:]]*ipv4\.address:[[:space:]]*"\{0,1\}\([0-9.]*\)"\{0,1\}.*/\1/p' "$tep" | head -1
}

# Đọc limits.cpu khai báo trong may/<ten>.yaml
cpu_cua_may() {
  local tep="$THU_MUC_GOC/may/$1.yaml"
  [[ -f "$tep" ]] || return 1
  sed -n 's/^[[:space:]]*limits\.cpu:[[:space:]]*"\{0,1\}\([0-9,-]*\)"\{0,1\}.*/\1/p' "$tep" | head -1
}
