#!/usr/bin/env bash
# thu-vien.sh — hàm dùng chung cho các script trong infra/may-ao (thin worker, 8 instance).
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
  : "${ZFS_ARC_MAX:=4294967296}"   # 4 GiB (kiến trúc mục 4)
  : "${THU_MUC_BI_MAT:=$THU_MUC_GOC/bi-mat}"
  : "${ZEUS_SERVER_URL:=http://10.90.30.10:8080}"
  : "${RAM_TOI_THIEU_ERP_GIB:=20}"  # RAM còn trống tối thiểu để dám động vào host đang chạy ERP production
}

# ---------- Cổng rủi ro host (ADR-012 / gate G1) --------------------------------
# ERP production chạy trên CHÍNH host này. Mọi thay đổi host (gói, sysctl, nftables, Incus, VM, mạng)
# là HIGH PRODUCTION RISK. Quy tắc: --chay-thu luôn an toàn; chạy thật cần ĐỦ 3 thứ:
#   1. cờ  --xac-nhan-rui-ro-erp-production
#   2. biến ZEUS_G1_DUYET=<mã quyết định G1 của người duyệt>
#   3. preflight đạt (RAM còn trống, không quá tải) và (nếu có TTY) gõ lại 'dong-y-rui-ro'
: "${RAM_TOI_THIEU_ERP_GIB:=20}"
CO_RUI_RO="--xac-nhan-rui-ro-erp-production"
XAC_NHAN_RUI_RO=0
CON_LAI=()

# doc_co_chung "$@" : tiêu thụ các cờ đứng đầu (--chay-thu, --xac-nhan-rui-ro-erp-production); phần còn lại → CON_LAI.
doc_co_chung() {
  CON_LAI=()
  local dang_dau=1 a
  for a in "$@"; do
    if [[ $dang_dau == 1 ]]; then
      case "$a" in
        --chay-thu) CHAY_THU=1; continue ;;
        "$CO_RUI_RO") XAC_NHAN_RUI_RO=1; continue ;;
      esac
    fi
    dang_dau=0; CON_LAI+=("$a")
  done
}

# phat_hien_erp_production : CHỈ ĐỌC. Đặt ERP_PROD_PHAT_HIEN=1 nếu thấy dấu hiệu Odoo/PostgreSQL đang chạy trên host.
phat_hien_erp_production() {
  ERP_PROD_PHAT_HIEN=0; ERP_PROD_DAU_HIEU=()
  local u
  for u in odoo odoo-server postgresql; do
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$u" 2>/dev/null; then ERP_PROD_DAU_HIEU+=("dịch vụ $u đang chạy"); fi
  done
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -qE ':(8069|8072)[[:space:]]'; then ERP_PROD_DAU_HIEU+=("có tiến trình nghe cổng 8069/8072 (Odoo)"); fi
    if ss -ltn 2>/dev/null | grep -qE ':5432[[:space:]]'; then ERP_PROD_DAU_HIEU+=("có PostgreSQL nghe cổng 5432"); fi
  fi
  if (( ${#ERP_PROD_DAU_HIEU[@]} > 0 )); then ERP_PROD_PHAT_HIEN=1; fi
  return 0
}

ram_trong_gib() { awk '/^MemAvailable:/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0; }

# bao_ve_host_that "<mô tả thay đổi>" : gọi TRƯỚC mọi bước làm thay đổi host.
bao_ve_host_that() {
  local mo_ta="$1" ram tai
  tieu_de "Preflight host (ERP production chạy trên host này — ADR-012)"
  phat_hien_erp_production
  ram="$(ram_trong_gib)"; tai="$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo '?')"
  if (( ERP_PROD_PHAT_HIEN )); then canh_bao "Phát hiện ERP production trên host: $(printf '%s; ' "${ERP_PROD_DAU_HIEU[@]}")"
  else thong_bao "Không thấy dấu hiệu ERP production trên host (kiểm tra thủ công vẫn cần)."; fi
  thong_bao "RAM còn trống: ${ram} GiB (cần ≥ ${RAM_TOI_THIEU_ERP_GIB}) | tải 1 phút: ${tai} | thay đổi sắp làm: ${mo_ta}"
  if [[ "$CHAY_THU" == "1" ]]; then
    canh_bao "[CHẠY THỬ] Bước này THAY ĐỔI HOST. Khi chạy thật cần: $CO_RUI_RO + ZEUS_G1_DUYET=<mã duyệt G1>."
    return 0
  fi
  [[ "$XAC_NHAN_RUI_RO" == "1" ]] || chet "Từ chối: thiếu cờ $CO_RUI_RO (thay đổi host đang chạy ERP production). Xem --chay-thu trước."
  [[ -n "${ZEUS_G1_DUYET:-}" ]] || chet "Từ chối: thiếu ZEUS_G1_DUYET=<mã quyết định G1/G2/G3> (người duyệt) — ADR-012."
  (( ram >= RAM_TOI_THIEU_ERP_GIB )) || chet "Từ chối: RAM còn trống ${ram} GiB < ${RAM_TOI_THIEU_ERP_GIB} GiB — có thể ảnh hưởng ERP production."
  if [[ -t 0 ]]; then
    local xn; read -r -p "Gõ 'dong-y-rui-ro' để tiếp tục thay đổi host: " xn
    [[ "$xn" == "dong-y-rui-ro" ]] || chet "Huỷ."
  fi
  thong_bao "Đã xác nhận rủi ro (duyệt: ${ZEUS_G1_DUYET})."
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

# 8 instance mục tiêu (kiến trúc mục 4). Loại: core (VM zeus-core) | gpu (container) | edge (container) | worker (VM thin worker) | erp (VM staging).
# w-gui-win (Windows GUI fallback) TẮT mặc định, ngoài luồng tự động (gate G8) — xem luu-tru/windows-gui-g8.
# shellcheck disable=SC2034
declare -A LOAI_MAY=(
  [zeus-core]=core
  [zeus-gpu]=gpu
  [zeus-edge]=edge
  [w-code-1]=worker
  [w-code-2]=worker
  [w-browser]=worker
  [w-ops]=worker
  [erp-staging]=erp
)
# shellcheck disable=SC2034
THU_TU_MAY=(zeus-core zeus-gpu zeus-edge w-code-1 w-code-2 w-browser w-ops erp-staging)

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
