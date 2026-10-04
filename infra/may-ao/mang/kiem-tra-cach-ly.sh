#!/usr/bin/env bash
# kiem-tra-cach-ly.sh — Xác minh ERP đã bị cách ly. Chạy trên HOST sau khi máy chạy.
# Chỉ đọc + ping/curl thử từ trong máy (không đổi cấu hình).
set -uo pipefail
# shellcheck source=../thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/../thu-vien.sh"

IP_ERP="$(ip_cua_may erp-thu-nghiem)"; IP_AGENT="$(ip_cua_may tim-hang)"
tieu_de "Bảng nftables"
if nft list table inet may_ao_cach_ly >/dev/null 2>&1; then dat "Bảng may_ao_cach_ly đang nạp."; nft list table inet may_ao_cach_ly | grep -E 'counter' | sed 's/^/   /'
else loi "Bảng may_ao_cach_ly CHƯA nạp → systemctl start may-ao-cach-ly"; fi

tieu_de "Thử từ agent (tim-hang) → ERP ($IP_ERP)"
if incus exec tim-hang -- timeout 3 ping -c1 -W1 "$IP_ERP" >/dev/null 2>&1; then loi "Agent PING ĐƯỢC ERP — cách ly thất bại!"; else dat "Agent không ping được ERP (đúng)."; fi
if incus exec tim-hang -- timeout 3 bash -c "curl -s -m2 http://$IP_ERP:8069 >/dev/null" 2>/dev/null; then loi "Agent mở được cổng 8069 của ERP — cách ly thất bại!"; else dat "Agent không mở được ERP:8069 (đúng)."; fi
if incus exec tim-hang -- timeout 5 bash -c "curl -s -m4 -o /dev/null https://www.google.com" 2>/dev/null; then dat "Agent vẫn ra internet được."; else canh_bao "Agent không ra internet (DNS/NAT?)"; fi

tieu_de "Thử từ ERP → agent ($IP_AGENT) và host"
if incus exec erp-thu-nghiem -- timeout 3 ping -c1 -W1 "$IP_AGENT" >/dev/null 2>&1; then loi "ERP PING ĐƯỢC agent — cách ly thất bại!"; else dat "ERP không ping được agent (đúng)."; fi
if incus exec erp-thu-nghiem -- timeout 3 bash -c "curl -s -m2 http://10.90.20.1:8443 >/dev/null" 2>/dev/null; then canh_bao "ERP mở được cổng 8443 trên host?"; else dat "ERP không chạm được dịch vụ host ngoài DHCP/DNS (đúng)."; fi
if incus exec erp-thu-nghiem -- timeout 5 bash -c "curl -s -m4 -o /dev/null https://www.google.com" 2>/dev/null; then dat "ERP vẫn ra internet được."; else canh_bao "ERP không ra internet."; fi

tieu_de "Từ host → ERP web"
if curl -s -m3 -o /dev/null "http://$IP_ERP:8069"; then dat "Host mở được ERP:8069 (dùng SSH tunnel từ máy admin: ssh -L 8069:$IP_ERP:8069 <server>)"; else canh_bao "Host chưa mở được ERP:8069 (ERP chưa chạy?)"; fi
