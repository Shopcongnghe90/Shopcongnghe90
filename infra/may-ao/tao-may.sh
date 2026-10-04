#!/usr/bin/env bash
# tao-may.sh — Tạo & cấu hình 7 máy ảo theo kế hoạch (chạy trên HOST, user trong nhóm incus-admin).
#
#   bash tao-may.sh [--chay-thu] <lệnh> [tham số]
#
#   thiet-lap                     kiểm tra pool/mạng; tạo-cập nhật 5 hồ sơ (may-ao-chung, agent-linux,
#                                 agent-linux-tu-mau, agent-windows, erp); nạp cloud-init; kiểm tra nhân E
#   tao <may>|linux|windows|erp|tat-ca
#                                 tạo & bật máy. Linux/ERP: chờ cloud-init rồi tự đẩy bí mật nếu có token.
#   bi-mat <may>|linux            đẩy token Claude + mật khẩu VNC + CLAUDE.md vai trò vào máy Linux; bật dịch vụ
#   erp-odoo-mau                  đẩy compose Odoo mẫu vào máy ERP và khởi chạy
#   trang-thai                    danh sách máy, IP, lệnh SSH tunnel (noVNC/RDP/ERP)
#   nhat-ky <may>                 nhật ký dịch vụ claude-remote-control (tìm URL phiên)
#   xoa <may>                     xoá máy (hỏi xác nhận)
#
#   Máy: zalo-1 zalo-2 (windows) | tim-hang facebook ban-hang he-thong (linux) | erp-thu-nghiem (erp)
#   Bí mật đọc từ bien-moi-truong.env: CLAUDE_CODE_OAUTH_TOKEN, VNC_MAT_KHAU, THU_MUC_ISO.
set -euo pipefail
# shellcheck source=thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/thu-vien.sh"

[[ "${1:-}" == "--chay-thu" ]] && { CHAY_THU=1; shift; }
LENH="${1:-}"; shift || true
[[ -n "$LENH" ]] || { sed -n '2,19p' "$0"; exit 1; }
nap_bien_moi_truong
incus_san_sang || [[ $CHAY_THU == 1 ]] || chet "Không nối được Incus (newgrp incus-admin? đã chạy cai-incus.sh?)."

ANH_UBUNTU="images:ubuntu/24.04/cloud"
ALIAS_MAU="agent-linux-mau"

# ---------------------------------------------------------------------------
giai_danh_sach() { # linux|windows|erp|tat-ca|<may> → danh sách tên
  local m
  case "$1" in
    tat-ca) printf '%s\n' "${THU_TU_MAY[@]}" ;;
    linux|windows|erp) for m in "${THU_TU_MAY[@]}"; do [[ "${LOAI_MAY[$m]}" == "$1" ]] && echo "$m"; done ;;
    *) [[ -n "${LOAI_MAY[$1]:-}" ]] || chet "Không có máy '$1' trong kế hoạch (xem đầu tệp)."; echo "$1" ;;
  esac
}
may_ton_tai() { [[ $CHAY_THU == 1 ]] && return 1; incus info "$1" >/dev/null 2>&1; }

kiem_tra_nhan_e() { # cảnh báo nếu CPU ghim của máy không phải nhân E trên host này
  local m="$1" c cpu_list; cpu_list="$(cpu_cua_may "$m")"
  nhan_dien_nhan
  (( ${#NHAN_E[@]} >= 16 )) || { canh_bao "Host nhận diện ${#NHAN_E[@]} nhân E (<16) — bỏ qua kiểm tra ghim cho $m."; return 0; }
  for c in $(mo_khoang "$cpu_list"); do
    if [[ ! " ${NHAN_E[*]} " == *" $c "* ]]; then
      canh_bao "$m: CPU $c trong limits.cpu=$cpu_list KHÔNG phải nhân E của host (E = $(gop_khoang "${NHAN_E[@]}")). Sửa may/$m.yaml."
      return 0
    fi
  done
  thong_bao "$m: ghim $cpu_list — toàn nhân E ✔"
}

cho_san_sang() { # chờ incus-agent + cloud-init của máy Linux/ERP
  local m="$1" i
  [[ $CHAY_THU == 1 ]] && { thong_bao "(chạy thử) chờ $m sẵn sàng"; return 0; }
  thong_bao "Chờ incus-agent của $m (tối đa 5 phút)..."
  for i in $(seq 1 60); do incus exec "$m" -- true >/dev/null 2>&1 && break; sleep 5; done
  incus exec "$m" -- true >/dev/null 2>&1 || { canh_bao "$m: incus-agent chưa lên sau 5 phút (máy đang cài? xem: incus console $m)"; return 1; }
  thong_bao "Chờ cloud-init của $m (lần đầu có thể 10-20 phút: apt upgrade, Chrome, Node, Claude)..."
  set +e; incus exec "$m" -- cloud-init status --wait >/dev/null 2>&1; local rc=$?; set -e
  case $rc in
    0) dat "$m: cloud-init hoàn tất." ;;
    2) canh_bao "$m: cloud-init 'degraded' — một số bước lỗi. Xem: incus exec $m -- tail -50 /var/log/cloud-init-output.log" ;;
    *) canh_bao "$m: cloud-init trả mã $rc." ;;
  esac
  return 0
}

# ---------------------------------------------------------------------------
thiet_lap() {
  tieu_de "Pool & mạng"
  if [[ $CHAY_THU != 1 ]]; then
    incus storage show may-ao >/dev/null 2>&1 || chet "Chưa có pool may-ao — chạy cai-incus.sh trước."
  fi
  for net in br-agent br-erp; do
    if [[ $CHAY_THU != 1 ]] && incus network show "$net" >/dev/null 2>&1; then dat "Mạng $net có."; continue; fi
    local so; so=$([[ $net == br-agent ]] && echo 10 || echo 20)
    chay incus network create "$net" ipv4.address=10.90.$so.1/24 ipv4.nat=true ipv4.dhcp=true \
      ipv4.dhcp.ranges=10.90.$so.100-10.90.$so.199 ipv6.address=none
  done

  tieu_de "Hồ sơ (profiles)"
  local p
  for p in may-ao-chung agent-linux agent-windows erp; do
    if [[ $CHAY_THU == 1 ]] || ! incus profile show "$p" >/dev/null 2>&1; then chay incus profile create "$p"; fi
    chay_sh "incus profile edit $p < '$THU_MUC_GOC/ho-so/$p.yaml'"
  done
  # agent-linux-tu-mau: cùng thiết bị với agent-linux nhưng cloud-init tối giản (dùng với ảnh mẫu)
  if [[ $CHAY_THU == 1 ]] || ! incus profile show agent-linux-tu-mau >/dev/null 2>&1; then chay incus profile create agent-linux-tu-mau; fi
  chay_sh "sed -e 's/^name: agent-linux$/name: agent-linux-tu-mau/' -e 's/^description: .*/description: \"Agent Linux tạo từ ảnh mẫu agent-linux-mau (đã cài sẵn)\"/' '$THU_MUC_GOC/ho-so/agent-linux.yaml' | incus profile edit agent-linux-tu-mau"

  tieu_de "Nạp cloud-init vào hồ sơ"
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) incus profile set agent-linux cloud-init.user-data <anh-mau/linux/cloud-init-user-data.yaml>"
    thong_bao "(chạy thử) incus profile set agent-linux-tu-mau cloud-init.user-data <anh-mau/linux/cloud-init-tu-mau.yaml>"
    thong_bao "(chạy thử) incus profile set erp cloud-init.user-data <anh-mau/erp/cloud-init-user-data.yaml>"
  else
    incus profile set agent-linux cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/linux/cloud-init-user-data.yaml")"
    incus profile set agent-linux-tu-mau cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/linux/cloud-init-tu-mau.yaml")"
    incus profile set erp cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/erp/cloud-init-user-data.yaml")"
    dat "Đã nạp cloud-init."
  fi

  tieu_de "Kiểm tra ghim nhân E của 7 máy"
  for m in "${THU_TU_MAY[@]}"; do kiem_tra_nhan_e "$m"; done
  dat "Thiết lập xong. Tiếp: bash tao-may.sh tao linux   (hoặc anh-mau/linux/tao-anh-mau.sh trước)"
}

# ---------------------------------------------------------------------------
tao_mot() {
  local m="$1" loai="${LOAI_MAY[$1]}" anh hs
  if may_ton_tai "$m"; then thong_bao "$m đã tồn tại — bỏ qua tạo."; return 0; fi
  kiem_tra_nhan_e "$m"
  case "$loai" in
    linux)
      anh="$ANH_UBUNTU"; hs="agent-linux"
      if [[ $CHAY_THU != 1 ]] && incus image info "$ALIAS_MAU" >/dev/null 2>&1; then
        anh="$ALIAS_MAU"; hs="agent-linux-tu-mau"; thong_bao "$m: dùng ảnh mẫu $ALIAS_MAU (nhanh)."
      else thong_bao "$m: dùng $anh + cloud-init đầy đủ (10-20 phút)."; fi
      chay_sh "incus init $anh $m --vm -p may-ao-chung -p $hs < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    erp)
      chay_sh "incus init $ANH_UBUNTU $m --vm -p may-ao-chung -p erp < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    windows)
      local iso_win="$THU_MUC_ISO/Win11.incus.iso" iso_au="$THU_MUC_ISO/autounattend-$m.iso"
      [[ -f "$iso_win" || $CHAY_THU == 1 ]] || chet "Thiếu $iso_win — chạy anh-mau/windows/chuan-bi-iso.sh."
      [[ -f "$iso_au"  || $CHAY_THU == 1 ]] || chet "Thiếu $iso_au — chạy anh-mau/windows/chuan-bi-iso.sh."
      chay_sh "incus init $m --empty --vm -p may-ao-chung -p agent-windows < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus config device add "$m" install disk source="$iso_win" boot.priority=10
      chay incus config device add "$m" unattend disk source="$iso_au"
      chay incus config device add "$m" vtpm tpm path=/dev/tpm0
      chay incus start "$m"
      thong_bao "$m: Windows tự cài ~15-25 phút. Theo dõi: incus console $m --type=vga (cần remote-viewer) hoặc RDP sau khi xong."
      thong_bao "$m: SAU KHI CÀI XONG, gỡ ISO: incus config device remove $m install unattend" ;;
  esac
}

tao() {
  [[ -n "${1:-}" ]] || chet "Thiếu tham số: <may>|linux|windows|erp|tat-ca"
  local ds m; mapfile -t ds < <(giai_danh_sach "$1")
  tieu_de "Tạo ${#ds[@]} máy: ${ds[*]}"
  for m in "${ds[@]}"; do tao_mot "$m"; done
  # Linux/ERP: chờ rồi đẩy bí mật
  for m in "${ds[@]}"; do
    case "${LOAI_MAY[$m]}" in
      linux) if cho_san_sang "$m"; then
               if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]]; then bi_mat_mot "$m"; else canh_bao "Chưa có token trong bien-moi-truong.env → sau này: bash tao-may.sh bi-mat $m"; fi
             fi ;;
      erp)   if cho_san_sang "$m"; then thong_bao "ERP sẵn sàng. Odoo mẫu: bash tao-may.sh erp-odoo-mau"; fi ;;
    esac
  done
  trang_thai
}

# ---------------------------------------------------------------------------
bi_mat_mot() {
  local m="$1"
  [[ "${LOAI_MAY[$m]:-}" == linux ]] || chet "bi-mat chỉ dùng cho máy Linux (Windows: C:\\agent\\dat-token.ps1)."
  tieu_de "Đẩy bí mật vào $m"
  [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]] || chet "Thiếu CLAUDE_CODE_OAUTH_TOKEN (hoặc ANTHROPIC_API_KEY) trong bien-moi-truong.env."
  local tmp; tmp="$(mktemp)"; chmod 600 "$tmp"
  {
    echo "# sinh bởi tao-may.sh $(date -u +%FT%TZ)"
    [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] && echo "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}"
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
    echo "AGENT_TEN=$m"
  } > "$tmp"
  if [[ $CHAY_THU == 1 ]]; then thong_bao "(chạy thử) incus file push <env> $m/etc/claude-agent/env (0600 root)"
  else incus file push --mode 0600 --uid 0 --gid 0 "$tmp" "$m/etc/claude-agent/env"; fi
  rm -f "$tmp"

  if [[ -n "${VNC_MAT_KHAU:-}" ]]; then
    chay_kin "incus exec $m -- đặt mật khẩu VNC từ VNC_MAT_KHAU" \
      incus exec --env VNC_MK="${VNC_MAT_KHAU}" "$m" -- bash -c 'printf %s "$VNC_MK" | vncpasswd -f > /home/agent/.vnc/passwd && chown agent:agent /home/agent/.vnc/passwd && chmod 600 /home/agent/.vnc/passwd'
  else
    canh_bao "VNC_MAT_KHAU trống → giữ mật khẩu tạm: incus exec $m -- cat /etc/claude-agent/vnc-tam"
  fi

  # CLAUDE.md = phần chung + vai trò riêng
  tmp="$(mktemp)"; cat "$THU_MUC_GOC/vai-tro/_chung.md" <(echo) "$THU_MUC_GOC/vai-tro/$m.md" > "$tmp"
  if [[ $CHAY_THU == 1 ]]; then thong_bao "(chạy thử) incus file push <CLAUDE.md vai trò $m> $m/home/agent/work/CLAUDE.md"
  else incus file push --mode 0644 --uid 1000 --gid 1000 "$tmp" "$m/home/agent/work/CLAUDE.md"; fi
  rm -f "$tmp"

  chay incus exec "$m" -- systemctl daemon-reload
  chay incus exec "$m" -- systemctl restart xvnc@1.service xfce@1.service novnc.service
  chay incus exec "$m" -- systemctl enable --now claude-remote-control.service
  dat "$m: xong. URL phiên remote-control: bash tao-may.sh nhat-ky $m"
}
bi_mat() {
  [[ -n "${1:-}" ]] || chet "Thiếu tham số: <may>|linux"
  local ds; mapfile -t ds < <(giai_danh_sach "$1")
  for m in "${ds[@]}"; do [[ "${LOAI_MAY[$m]}" == linux ]] && bi_mat_mot "$m"; done
}

# ---------------------------------------------------------------------------
erp_odoo_mau() {
  local m=erp-thu-nghiem
  tieu_de "Odoo mẫu vào $m"
  may_ton_tai "$m" || [[ $CHAY_THU == 1 ]] || chet "Chưa có máy $m (bash tao-may.sh tao erp)."
  cho_san_sang "$m" || true
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) incus file push docker-compose.odoo.yaml $m/opt/erp/docker-compose.yaml; odoo.conf → /opt/erp/config/odoo.conf.mau"
  else
    incus file push --mode 0640 --uid 1000 --gid 1000 "$THU_MUC_GOC/anh-mau/erp/docker-compose.odoo.yaml" "$m/opt/erp/docker-compose.yaml"
    incus file push --mode 0640 --uid 1000 --gid 1000 "$THU_MUC_GOC/anh-mau/erp/odoo.conf" "$m/opt/erp/config/odoo.conf.mau"
  fi
  # thay mật khẩu từ /opt/erp/.env rồi khởi chạy
  chay incus exec "$m" -- bash -c 'set -e; cd /opt/erp; set -a; . ./.env; set +a; sed -e "s|__DB_MAT_KHAU__|$DB_MAT_KHAU|" -e "s|__ODOO_ADMIN_MAT_KHAU__|$ODOO_ADMIN_MAT_KHAU|" config/odoo.conf.mau > config/odoo.conf; chmod 640 config/odoo.conf; docker compose pull -q; docker compose up -d'
  local ip; ip="$(ip_cua_may "$m")"
  dat "Odoo đang khởi động (1-2 phút). Từ máy admin: ssh -L 8069:$ip:8069 <server> → http://localhost:8069"
  thong_bao "Mật khẩu quản trị DB (admin_passwd): incus exec $m -- grep ODOO_ADMIN /opt/erp/.env"
}

# ---------------------------------------------------------------------------
trang_thai() {
  tieu_de "Trạng thái"
  if [[ $CHAY_THU == 1 ]]; then thong_bao "(chạy thử) incus list"; else incus list -c ns4mt 2>/dev/null || incus list; fi
  echo
  printf '%s%-16s %-8s %-13s %-s%s\n' "$MAU_DAM" MAY LOAI IP "TRUY CẬP TỪ MÁY ADMIN (thay <server>)" "$MAU_HET"
  local i=0 m ip
  for m in "${THU_TU_MAY[@]}"; do
    i=$((i+1)); ip="$(ip_cua_may "$m")"
    case "${LOAI_MAY[$m]}" in
      linux)   printf '%-16s %-8s %-13s ssh -L 608%d:%s:6080 <server>  → http://localhost:608%d/vnc.html\n' "$m" linux "$ip" "$i" "$ip" "$i" ;;
      windows) printf '%-16s %-8s %-13s ssh -L 339%d:%s:3389 <server>  → mstsc /v:localhost:339%d (user agent)\n' "$m" windows "$ip" "$i" "$ip" "$i" ;;
      erp)     printf '%-16s %-8s %-13s ssh -L 8069:%s:8069 <server>   → http://localhost:8069\n' "$m" erp "$ip" "$ip" ;;
    esac
  done
  echo; thong_bao "Phiên Claude remote-control: bash tao-may.sh nhat-ky <may-linux>  (Windows: C:\\agent\\logs\\)"
}

nhat_ky() {
  [[ -n "${1:-}" ]] || chet "Thiếu tên máy."
  chay incus exec "$1" -- journalctl -u claude-remote-control -n 40 --no-pager
  [[ $CHAY_THU == 1 ]] || { echo; thong_bao "URL/mã phiên (nếu có):"; incus exec "$1" -- journalctl -u claude-remote-control --no-pager 2>/dev/null | grep -Eo 'https://[^ ]+' | tail -3 || true; }
}

xoa() {
  [[ -n "${1:-}" && -n "${LOAI_MAY[$1]:-}" ]] || chet "Thiếu/sai tên máy."
  canh_bao "Xoá $1 và TOÀN BỘ snapshot của nó."
  if [[ $CHAY_THU != 1 ]]; then read -r -p "Gõ 'xoa $1' để xác nhận: " XN; [[ "$XN" == "xoa $1" ]] || chet "Huỷ."; fi
  chay incus delete "$1" --force
}

# ---------------------------------------------------------------------------
case "$LENH" in
  thiet-lap)    thiet_lap ;;
  tao)          tao "${1:-}" ;;
  bi-mat)       bi_mat "${1:-}" ;;
  erp-odoo-mau) erp_odoo_mau ;;
  trang-thai)   trang_thai ;;
  nhat-ky)      nhat_ky "${1:-}" ;;
  xoa)          xoa "${1:-}" ;;
  *) chet "Lệnh lạ: $LENH (xem đầu tệp)" ;;
esac
