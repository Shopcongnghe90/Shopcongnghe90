#!/usr/bin/env bash
# kiem-tra.sh — Kiểm tra server TRƯỚC khi cài Incus/KVM. CHỈ ĐỌC, không thay đổi gì.
#
# Cách dùng:   bash infra/may-ao/kiem-tra.sh            (chạy bằng user thường được; sudo cho đầy đủ hơn)
# Thoát mã 0 nếu không có mục LỖI, 1 nếu có.
#
# Mục tiêu phần cứng: Ubuntu 22.04/24.04, Intel i9-13900 (8P+16E = 32 luồng), 64 GB RAM,
# ZFS cho pool Incus, 7 máy ảo ghim nhân E (16 nhân E → 2+2+2+2+2+2+4).
set -uo pipefail
# shellcheck source=thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/thu-vien.sh"

SO_DAT=0; SO_CANH_BAO=0; SO_LOI=0
d()  { dat "$@";      SO_DAT=$((SO_DAT+1)); }
cb() { canh_bao "$@"; SO_CANH_BAO=$((SO_CANH_BAO+1)); }
l()  { loi "$@";      SO_LOI=$((SO_LOI+1)); }

LA_ROOT=0; [[ $EUID -eq 0 ]] && LA_ROOT=1

tieu_de "0. Thông tin chung"
thong_bao "Máy: $(hostname)  |  Ngày: $(date '+%Y-%m-%d %H:%M %Z')  |  Chạy với quyền: $([[ $LA_ROOT == 1 ]] && echo root || echo "user $USER")"
[[ $LA_ROOT == 0 ]] && thong_bao "Một số kiểm tra (nft, dmesg) cần root — chạy lại với sudo để đầy đủ."

# ------------------------------------------------------------------------------
tieu_de "1. Hệ điều hành"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  thong_bao "$PRETTY_NAME (codename: ${VERSION_CODENAME:-?})"
  case "${ID:-}-${VERSION_ID:-}" in
    ubuntu-24.04|ubuntu-22.04) d "Ubuntu ${VERSION_ID} được hỗ trợ bởi kho Zabbly (Incus) và có ZFS." ;;
    ubuntu-*) cb "Ubuntu ${VERSION_ID} chưa được kiểm chứng với bộ script này (khuyến nghị 24.04 LTS)." ;;
    *) l "Không phải Ubuntu — cai-incus.sh được viết cho Ubuntu." ;;
  esac
else
  l "Không đọc được /etc/os-release"
fi
thong_bao "Kernel: $(uname -r)"

# ------------------------------------------------------------------------------
tieu_de "2. CPU & ảo hoá"
MODEL="$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
thong_bao "CPU: ${MODEL:-?}  |  luồng logic: $(nproc --all)"
if grep -q '13900' <<<"$MODEL"; then d "Đúng dòng i9-13900 như thiết kế."; else cb "CPU không phải i9-13900 — xem lại việc ghim nhân trong may/*.yaml."; fi

if grep -qw vmx /proc/cpuinfo; then d "Cờ VT-x (vmx) có — CPU hỗ trợ ảo hoá."; else l "Không thấy cờ vmx: bật Intel VT-x trong BIOS/UEFI."; fi
if [[ -c /dev/kvm ]]; then
  d "/dev/kvm tồn tại."
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then d "User hiện tại đọc/ghi được /dev/kvm."; else thong_bao "User hiện tại chưa có quyền /dev/kvm (Incus chạy bằng root nên không sao)."; fi
else
  l "Không có /dev/kvm: kiểm tra VT-x trong BIOS và module kvm_intel (modprobe kvm_intel)."
fi
if module_da_nap kvm_intel; then d "Module kvm_intel đã nạp."; else cb "Module kvm_intel chưa nạp (sẽ tự nạp khi có /dev/kvm; nếu không: sudo modprobe kvm_intel)."; fi
[[ -r /sys/module/kvm_intel/parameters/nested ]] && thong_bao "Nested virt: $(cat /sys/module/kvm_intel/parameters/nested) (không cần cho thiết kế này)"

# Nhân P/E
nhan_dien_nhan
thong_bao "Theo SMT: nhân P (có luồng anh em) = ${#NHAN_P[@]} CPU logic [$(gop_khoang "${NHAN_P[@]}")]"
thong_bao "          nhân E (không SMT)       = ${#NHAN_E[@]} CPU logic [$(gop_khoang "${NHAN_E[@]}")]"
# Kiểm tra chéo bằng tần số tối đa (nhân E có max MHz thấp hơn)
if [[ -r /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq ]]; then
  declare -A TAN_SO=()
  for c in /sys/devices/system/cpu/cpu[0-9]*; do
    n=${c##*cpu}; [[ -r $c/cpufreq/cpuinfo_max_freq ]] && TAN_SO[$n]=$(<"$c/cpufreq/cpuinfo_max_freq")
  done
  MAX_P=0; for n in "${NHAN_P[@]}"; do (( ${TAN_SO[$n]:-0} > MAX_P )) && MAX_P=${TAN_SO[$n]}; done
  MAX_E=0; for n in "${NHAN_E[@]}"; do (( ${TAN_SO[$n]:-0} > MAX_E )) && MAX_E=${TAN_SO[$n]}; done
  thong_bao "Tần số tối đa: P ≈ $((MAX_P/1000)) MHz, E ≈ $((MAX_E/1000)) MHz"
  if (( MAX_E > 0 && MAX_P > MAX_E )); then d "Tần số xác nhận cách phân loại P/E ở trên."; else cb "Tần số không phân biệt được P/E — kiểm tra lại bằng: lscpu -e=CPU,CORE,MAXMHZ"; fi
fi
if (( ${#NHAN_E[@]} >= 16 )); then
  d "Có ≥16 nhân E — đủ cho kế hoạch ghim (xem may/*.yaml, mặc định giả định E = 16-31)."
  if [[ "$(gop_khoang "${NHAN_E[@]}")" != "16-31" ]]; then
    cb "Danh sách nhân E thực tế [$(gop_khoang "${NHAN_E[@]}")] KHÁC giả định 16-31 → sửa limits.cpu trong may/*.yaml trước khi tạo máy."
  fi
else
  l "Chỉ nhận diện ${#NHAN_E[@]} nhân E (<16). Nếu đây là máy ảo/kernel không phơi topology, bỏ qua; nếu là server thật, kiểm tra BIOS (E-cores bị tắt?)."
fi
thong_bao "Bảng topology (lscpu):"; lscpu -e=CPU,CORE,SOCKET,MAXMHZ 2>/dev/null | head -40 | sed 's/^/      /'

# ------------------------------------------------------------------------------
tieu_de "3. RAM"
RAM_KB=$(awk '/MemTotal/{print $2}' /proc/meminfo); RAM_GB=$((RAM_KB/1024/1024))
thong_bao "Tổng RAM: ${RAM_GB} GiB  |  Trống: $(awk '/MemAvailable/{printf "%d", $2/1024/1024}' /proc/meminfo) GiB"
if (( RAM_GB >= 60 )); then d "≥60 GiB: đủ cho 48 GiB cấp máy ảo + 6 GiB ARC + host."; else l "RAM < 60 GiB: giảm limits.memory trong may/*.yaml và ZFS_ARC_MAX."; fi
if [[ "$(cat /proc/sys/vm/swappiness 2>/dev/null)" ]]; then thong_bao "swappiness=$(cat /proc/sys/vm/swappiness); swap: $(free -h | awk '/Swap/{print $2}')"; fi

# ------------------------------------------------------------------------------
tieu_de "4. Đĩa & ZFS"
thong_bao "Thiết bị khối:"; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL 2>/dev/null | sed 's/^/      /'
# Đĩa trống (không FS, không mount, không con) — ứng viên cho pool ZFS
# bỏ qua zram/loop/ram và đĩa 0 byte
mapfile -t DIA_TRONG < <(lsblk -dnpbo NAME,TYPE,FSTYPE,MOUNTPOINT,SIZE 2>/dev/null \
  | awk '$2=="disk" && $3=="" && $4=="" && $5>0 && $1 !~ /(zram|loop|ram)[0-9]*$/ {print $1}')
CO_DIA_TRONG=0
for dd in "${DIA_TRONG[@]}"; do
  # không có phân vùng con
  if [[ -z "$(lsblk -nro NAME "$dd" 2>/dev/null | tail -n +2)" ]]; then
    d "Đĩa trống ứng viên cho pool ZFS: $dd ($(lsblk -dnro SIZE "$dd")) → đặt O_DIA_ZFS=$dd"; CO_DIA_TRONG=1
  fi
done
if (( CO_DIA_TRONG == 0 )); then
  cb "Không thấy đĩa trống riêng — cai-incus.sh sẽ dùng tệp loop (chậm hơn). Nên dành 1 NVMe cho pool."
fi
thong_bao "Dung lượng /var/lib (nơi Incus đặt dữ liệu): $(df -h /var/lib 2>/dev/null | awk 'NR==2{print $4" trống / "$2}')"
TRONG_GB=$(df -BG --output=avail /var/lib 2>/dev/null | awk 'NR==2{gsub("G","");print $1}')
if [[ -n "${TRONG_GB:-}" ]]; then (( TRONG_GB >= 700 )) && d "≥700 GB trống trên /var/lib (đủ cho pool loop 600GiB nếu cần)." || thong_bao "Chỉ ${TRONG_GB} GB trống trên /var/lib — nếu dùng loop, giảm KICH_CO_POOL hoặc dùng đĩa riêng."; fi
if modinfo zfs >/dev/null 2>&1; then d "Module ZFS có sẵn (modinfo zfs)."; else cb "Chưa có module ZFS — cai-incus.sh sẽ cài zfsutils-linux."; fi
command -v zpool >/dev/null && thong_bao "zpool hiện có: $(zpool list -H -o name 2>/dev/null | tr '\n' ' ' || true)"

# ------------------------------------------------------------------------------
tieu_de "5. Incus (nếu đã cài)"
if co_incus; then
  thong_bao "incus: $(incus version 2>/dev/null | tr '\n' ' ')"
  if incus_san_sang; then
    d "Kết nối được daemon Incus (user này trong nhóm incus-admin hoặc root)."
    thong_bao "Pool: $(incus storage list -f csv -c n 2>/dev/null | tr '\n' ' ')"
    thong_bao "Mạng: $(incus network list -f csv -c n,t 2>/dev/null | grep -E 'bridge' | cut -d, -f1 | tr '\n' ' ')"
    thong_bao "Máy:  $(incus list -f csv -c n,s,t 2>/dev/null | tr '\n' ' ')"
    for m in br-agent br-erp; do incus network show "$m" >/dev/null 2>&1 && d "Mạng $m đã có." || thong_bao "Mạng $m chưa có (tao-may.sh thiet-lap / cai-incus.sh sẽ tạo)."; done
    incus storage show may-ao >/dev/null 2>&1 && d "Pool may-ao đã có." || thong_bao "Pool may-ao chưa có."
  else
    cb "Có lệnh incus nhưng không nối được daemon (chưa init? user chưa trong incus-admin? → newgrp incus-admin)."
  fi
  for grp in incus-admin incus; do getent group "$grp" >/dev/null && thong_bao "Nhóm $grp: $(getent group "$grp" | cut -d: -f4)"; done
else
  thong_bao "Chưa cài Incus — đó là việc của cai-incus.sh."
fi
[[ -f /etc/apt/sources.list.d/zabbly-incus-stable.sources ]] && thong_bao "Kho Zabbly đã khai báo." || thong_bao "Kho Zabbly chưa khai báo."
command -v snap >/dev/null && { snap list distrobuilder >/dev/null 2>&1 && thong_bao "distrobuilder (snap) đã cài — cần cho Windows." || thong_bao "distrobuilder chưa cài (anh-mau/windows/chuan-bi-iso.sh sẽ cài)."; }
command -v swtpm >/dev/null && d "swtpm có — vTPM cho Windows 11." || thong_bao "swtpm chưa có (gói incus từ Zabbly kèm sẵn)."

# ------------------------------------------------------------------------------
tieu_de "6. Mạng & tường lửa"
thong_bao "Giao diện mặc định: $(ip -o route show default 2>/dev/null | awk '{print $5" qua "$3}' | head -1)"
thong_bao "Địa chỉ IP: $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $2"="$4}' | tr '\n' ' ')"
thong_bao "ip_forward=$(cat /proc/sys/net/ipv4/ip_forward) (Incus tự bật khi tạo bridge NAT)"
for br in br-agent br-erp incusbr0; do ip link show "$br" >/dev/null 2>&1 && thong_bao "Bridge $br đang tồn tại."; done
if command -v nft >/dev/null; then
  d "nftables có sẵn (Incus dùng nft; cach-ly-erp.nft cũng dùng nft)."
  if [[ $LA_ROOT == 1 ]]; then
    nft list table inet may_ao_cach_ly >/dev/null 2>&1 && d "Bảng nft may_ao_cach_ly (cách ly ERP) đang nạp." || thong_bao "Bảng nft may_ao_cach_ly chưa nạp."
    nft list table inet incus >/dev/null 2>&1 && thong_bao "Bảng nft của Incus đang nạp."
  fi
else
  cb "Chưa có nft — cai-incus.sh sẽ cài nftables."
fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
  cb "ufw đang BẬT: có thể chặn DHCP/DNS của bridge Incus. Xem README mục 'ufw'."
else
  thong_bao "ufw không bật (hoặc không cài)."
fi
if systemctl is-active --quiet docker 2>/dev/null; then cb "Docker đang chạy trên host: iptables của Docker có thể chặn forward từ bridge Incus (xem README)."; fi
for mang in 10.90.10.0/24 10.90.20.0/24; do
  if ip -4 route show 2>/dev/null | grep -q "^${mang%/*}" ; then
    thong_bao "Route cho $mang đã có."
  fi
done
thong_bao "Kiểm tra DNS ra ngoài: $(getent hosts pkgs.zabbly.com >/dev/null 2>&1 && echo 'pkgs.zabbly.com giải được' || echo 'KHÔNG giải được pkgs.zabbly.com (cần internet để cài)')"

# ------------------------------------------------------------------------------
tieu_de "7. Dịch vụ & linh tinh"
if systemctl is-active --quiet systemd-timesyncd 2>/dev/null || systemctl is-active --quiet chrony 2>/dev/null; then
  d "Đồng bộ giờ đang chạy."
else
  cb "Không thấy timesyncd/chrony — snapshot theo lịch cần giờ đúng."
fi
thong_bao "Múi giờ host: $(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null)"
thong_bao "Uptime: $(uptime -p 2>/dev/null)"
if [[ -r /sys/kernel/mm/transparent_hugepage/enabled ]]; then thong_bao "THP: $(cat /sys/kernel/mm/transparent_hugepage/enabled)"; fi
[[ $LA_ROOT == 1 ]] && { dmesg 2>/dev/null | grep -qi 'DMAR: IOMMU enabled' && thong_bao "IOMMU bật (không bắt buộc)."; }

# ------------------------------------------------------------------------------
tieu_de "Tổng kết"
printf '  %sĐẠT: %d%s   %sCẢNH BÁO: %d%s   %sLỖI: %d%s\n' "$MAU_XANH" "$SO_DAT" "$MAU_HET" "$MAU_VANG" "$SO_CANH_BAO" "$MAU_HET" "$MAU_DO" "$SO_LOI" "$MAU_HET"
if (( SO_LOI > 0 )); then
  echo "  → Khắc phục các mục LỖI trước khi chạy cai-incus.sh."; exit 1
else
  echo "  → Bước tiếp: sudo bash infra/may-ao/cai-incus.sh --chay-thu   (xem lệnh sẽ chạy)"; exit 0
fi
