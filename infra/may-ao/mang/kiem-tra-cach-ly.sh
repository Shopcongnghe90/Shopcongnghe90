#!/usr/bin/env bash
# kiem-tra-cach-ly.sh — Xác minh cách ly đa bridge. Chạy trên HOST SAU KHI các instance đã chạy (gate G1 đã duyệt).
# Chỉ đọc + ping/curl thử từ trong máy (không đổi cấu hình). Không chạy mặc định trong CI.
set -uo pipefail
# shellcheck source=../thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/../thu-vien.sh"

IP_CORE="$(ip_cua_may zeus-core)"; IP_ERP="$(ip_cua_may erp-staging)"; IP_OPS="$(ip_cua_may w-ops)"

tieu_de "Bảng nftables"
if nft list table inet zeus_cach_ly >/dev/null 2>&1; then dat "Bảng zeus_cach_ly đang nạp."; nft list table inet zeus_cach_ly | grep -E 'counter' | sed 's/^/   /'
else loi "Bảng zeus_cach_ly CHƯA nạp → systemctl start zeus-cach-ly"; fi

thu() { # thu <may-nguon> <đích> <cổng> <mong-doi: duoc|chan>
  local from="$1" ip="$2" port="$3" mong="$4" ket="chan"
  if incus exec "$from" -- timeout 4 bash -c "exec 3<>/dev/tcp/$ip/$port" >/dev/null 2>&1; then ket="duoc"; fi
  if [[ "$ket" == "$mong" ]]; then dat "$from -> $ip:$port: $ket (đúng)"; else loi "$from -> $ip:$port: $ket nhưng mong đợi $mong"; fi
}

tieu_de "Kiểm tra danh sách trắng"
thu w-code-1 "$IP_CORE" 8080 duoc
thu w-code-1 "$IP_ERP" 8069 duoc
thu w-browser "$IP_ERP" 8069 chan
thu w-code-1 "$IP_OPS" 22 chan
thu erp-staging "$(ip_cua_may w-code-1)" 22 chan
thu w-ops "$IP_CORE" 8080 duoc
thu w-ops "$IP_ERP" 8069 chan
thu zeus-edge "$IP_CORE" 8080 duoc
thu zeus-edge "$(ip_cua_may w-code-1)" 22 chan

tieu_de "Từ bridge -> host (ngoài DHCP/DNS phải bị chặn)"
for m in w-code-1 w-ops erp-staging zeus-edge; do thu "$m" 10.90.30.1 22 chan; done
thong_bao "ERP production: chỉ zeus-core -> host:8069 được mở (kiểm tra bằng zeus-core: curl http://<ip-host>:8069/web/health)."
