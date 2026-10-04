#!/usr/bin/env bash
# tao-may.sh — Tạo & cấu hình 8 instance ZeusVN (chạy trên HOST, user trong nhóm incus-admin).
#
#   bash tao-may.sh [--chay-thu] [--xac-nhan-rui-ro-erp-production] <lệnh> [tham số]
#
#   thiet-lap                     kiểm tra pool/5 bridge; tạo-cập nhật hồ sơ (may-ao-chung, zeus-worker,
#                                 zeus-worker-tu-mau, zeus-core, zeus-gpu, erp); nạp cloud-init; kiểm tra ghim nhân
#   tao <may>|core|worker|erp|tat-ca
#                                 tạo & bật instance. Thin worker: sau đó chạy dua-worker để cài zeus_worker.
#   dua-worker <may>|worker       đẩy mã nguồn zeus_worker + worker.toml + token RIÊNG vào VM thin worker, bật dịch vụ
#   erp-odoo-mau                  đẩy compose Odoo 19 staging vào erp-staging và khởi chạy
#   trang-thai                    danh sách instance, IP, lệnh SSH tunnel
#   nhat-ky <may>                 nhật ký dịch vụ zeus-worker
#   xoa <may>                     xoá instance (hỏi xác nhận)
#
#   Instance: zeus-core | zeus-gpu zeus-edge | w-code-1 w-code-2 w-browser w-ops | erp-staging
#   KHÔNG cài Claude Code / LLM / API key trên bất kỳ VM nào. Worker dùng token riêng: bi-mat/<may>.token (0600, gitignore).
#   Lệnh làm thay đổi host (thiet-lap, tao, dua-worker, erp-odoo-mau, xoa) đi qua cổng rủi ro ERP production:
#   cần --xac-nhan-rui-ro-erp-production + ZEUS_G1_DUYET=<mã duyệt>; --chay-thu luôn an toàn.
set -euo pipefail
# shellcheck source=thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/thu-vien.sh"

doc_co_chung "$@"
set -- "${CON_LAI[@]}"
LENH="${1:-}"; shift || true
[[ -n "$LENH" ]] || { sed -n '2,20p' "$0"; exit 1; }
nap_bien_moi_truong
case "$LENH" in trang-thai|nhat-ky) ;; *) bao_ve_host_that "lệnh tao-may.sh $LENH (incus: profile/instance/mạng)" ;; esac
incus_san_sang || [[ $CHAY_THU == 1 ]] || chet "Không nối được Incus (newgrp incus-admin? đã chạy cai-incus.sh?)."

ANH_UBUNTU="images:ubuntu/24.04/cloud"
ALIAS_MAU="zeus-worker-mau"
GOC_REPO="$(cd "$THU_MUC_GOC/../.." && pwd)"

# ---------------------------------------------------------------------------
giai_danh_sach() { # core|worker|erp|tat-ca|<may> → danh sách tên
  local m
  case "$1" in
    tat-ca) printf '%s\n' "${THU_TU_MAY[@]}" ;;
    core|worker|erp|gpu|edge) for m in "${THU_TU_MAY[@]}"; do [[ "${LOAI_MAY[$m]}" == "$1" ]] && echo "$m"; done ;;
    *) [[ -n "${LOAI_MAY[$1]:-}" ]] || chet "Không có instance '$1' trong kế hoạch (xem đầu tệp)."; echo "$1" ;;
  esac
}
may_ton_tai() { [[ $CHAY_THU == 1 ]] && return 1; incus info "$1" >/dev/null 2>&1; }

kiem_tra_nhan_e() { # cảnh báo nếu CPU ghim của VM thin worker/core/erp không phải nhân E trên host này
  local m="$1" c cpu_list; cpu_list="$(cpu_cua_may "$m")"
  case "${LOAI_MAY[$m]}" in gpu) thong_bao "$m: ghim $cpu_list (2 luồng P cho llama.cpp theo kiến trúc) — không kiểm tra nhân E."; return 0 ;; esac
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

cho_san_sang() { # chờ incus-agent + cloud-init của VM
  local m="$1"
  [[ $CHAY_THU == 1 ]] && { thong_bao "(chạy thử) chờ $m sẵn sàng"; return 0; }
  case "${LOAI_MAY[$m]}" in gpu|edge) return 0 ;; esac
  thong_bao "Chờ incus-agent của $m (tối đa 5 phút)..."
  for _ in $(seq 1 60); do incus exec "$m" -- true >/dev/null 2>&1 && break; sleep 5; done
  incus exec "$m" -- true >/dev/null 2>&1 || { canh_bao "$m: incus-agent chưa lên sau 5 phút (xem: incus console $m)"; return 1; }
  thong_bao "Chờ cloud-init của $m (lần đầu 5-15 phút)..."
  set +e; incus exec "$m" -- cloud-init status --wait >/dev/null 2>&1; local rc=$?; set -e
  case $rc in
    0) dat "$m: cloud-init hoàn tất." ;;
    2) canh_bao "$m: cloud-init 'degraded'. Xem: incus exec $m -- tail -50 /var/log/cloud-init-output.log" ;;
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
  local net so
  for net in br-core:30 br-agent:10 br-ops:40 br-erp-test:20 br-dmz:50; do
    so="${net#*:}"; net="${net%:*}"
    if [[ $CHAY_THU != 1 ]] && incus network show "$net" >/dev/null 2>&1; then dat "Mạng $net có."; continue; fi
    chay incus network create "$net" ipv4.address=10.90.$so.1/24 ipv4.nat=true ipv4.dhcp=true \
      ipv4.dhcp.ranges=10.90.$so.100-10.90.$so.199 ipv6.address=none
  done

  tieu_de "Hồ sơ (profiles)"
  local p
  for p in may-ao-chung zeus-worker zeus-core zeus-gpu erp; do
    if [[ $CHAY_THU == 1 ]] || ! incus profile show "$p" >/dev/null 2>&1; then chay incus profile create "$p"; fi
    chay_sh "incus profile edit $p < '$THU_MUC_GOC/ho-so/$p.yaml'"
  done
  # zeus-worker-tu-mau: cùng thiết bị với zeus-worker nhưng cloud-init tối giản (dùng với ảnh mẫu)
  if [[ $CHAY_THU == 1 ]] || ! incus profile show zeus-worker-tu-mau >/dev/null 2>&1; then chay incus profile create zeus-worker-tu-mau; fi
  chay_sh "sed -e 's/^name: zeus-worker$/name: zeus-worker-tu-mau/' -e 's/^description: .*/description: \"Thin worker tạo từ ảnh mẫu zeus-worker-mau\"/' '$THU_MUC_GOC/ho-so/zeus-worker.yaml' | incus profile edit zeus-worker-tu-mau"

  tieu_de "Nạp cloud-init vào hồ sơ"
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) incus profile set zeus-worker cloud-init.user-data <anh-mau/linux/cloud-init-worker.yaml>"
    thong_bao "(chạy thử) incus profile set zeus-worker-tu-mau cloud-init.user-data <anh-mau/linux/cloud-init-tu-mau.yaml>"
    thong_bao "(chạy thử) incus profile set erp cloud-init.user-data <anh-mau/erp/cloud-init-erp-staging.yaml>"
  else
    incus profile set zeus-worker cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/linux/cloud-init-worker.yaml")"
    incus profile set zeus-worker-tu-mau cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/linux/cloud-init-tu-mau.yaml")"
    incus profile set erp cloud-init.user-data "$(cat "$THU_MUC_GOC/anh-mau/erp/cloud-init-erp-staging.yaml")"
    dat "Đã nạp cloud-init."
  fi

  tieu_de "Kiểm tra ghim CPU của 8 instance"
  for m in "${THU_TU_MAY[@]}"; do kiem_tra_nhan_e "$m"; done
  dat "Thiết lập xong. Tiếp: bash tao-may.sh tao tat-ca   rồi   bash tao-may.sh dua-worker worker"
}

# ---------------------------------------------------------------------------
tao_mot() {
  local m="$1" loai="${LOAI_MAY[$1]}" anh hs
  if may_ton_tai "$m"; then thong_bao "$m đã tồn tại — bỏ qua tạo."; return 0; fi
  kiem_tra_nhan_e "$m"
  case "$loai" in
    worker)
      anh="$ANH_UBUNTU"; hs="zeus-worker"
      if [[ $CHAY_THU != 1 ]] && incus image info "$ALIAS_MAU" >/dev/null 2>&1; then
        anh="$ALIAS_MAU"; hs="zeus-worker-tu-mau"; thong_bao "$m: dùng ảnh mẫu $ALIAS_MAU (nhanh)."
      else thong_bao "$m: dùng $anh + cloud-init đầy đủ (5-15 phút)."; fi
      chay_sh "incus init $anh $m --vm -p may-ao-chung -p $hs < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    core)
      chay_sh "incus init $ANH_UBUNTU $m --vm -p may-ao-chung -p zeus-core < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    erp)
      chay_sh "incus init $ANH_UBUNTU $m --vm -p may-ao-chung -p erp < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    gpu)
      # Container (không --vm) với nvidia.runtime; cần driver NVIDIA + nvidia-container-toolkit trên host.
      if [[ $CHAY_THU != 1 ]] && ! command -v nvidia-smi >/dev/null 2>&1; then
        canh_bao "$m: host chưa có nvidia-smi — bỏ qua zeus-gpu (hệ thống vẫn chạy bằng cloud/queue). Cài driver rồi chạy lại."
        return 0
      fi
      chay_sh "incus init images:ubuntu/24.04 $m -p may-ao-chung -p zeus-gpu < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
    edge)
      chay_sh "incus init images:ubuntu/24.04 $m -p may-ao-chung < '$THU_MUC_GOC/may/$m.yaml'"
      chay incus start "$m" ;;
  esac
}

tao() {
  [[ -n "${1:-}" ]] || chet "Thiếu tham số: <may>|core|worker|erp|tat-ca"
  local ds m; mapfile -t ds < <(giai_danh_sach "$1")
  tieu_de "Tạo ${#ds[@]} instance: ${ds[*]}"
  for m in "${ds[@]}"; do tao_mot "$m"; done
  for m in "${ds[@]}"; do
    case "${LOAI_MAY[$m]}" in
      worker) if cho_san_sang "$m"; then thong_bao "Tiếp: bash tao-may.sh dua-worker $m (cần bi-mat/$m.token)"; fi ;;
      core|erp) cho_san_sang "$m" || true ;;
    esac
  done
  trang_thai
}

# ---------------------------------------------------------------------------
dua_worker_mot() {
  local m="$1" tep_token="$THU_MUC_BI_MAT/$1.token" cfg="$THU_MUC_GOC/cau-hinh-worker/$1.toml" tgz
  [[ "${LOAI_MAY[$m]:-}" == worker ]] || chet "dua-worker chỉ dùng cho VM thin worker (w-code-1/2, w-browser, w-ops)."
  [[ -f "$cfg" ]] || chet "Thiếu $cfg"
  tieu_de "Đưa zeus_worker vào $m"
  if [[ $CHAY_THU != 1 ]]; then
    [[ -f "$tep_token" ]] || chet "Thiếu token riêng $tep_token (admin phát hành trên zeus-core: PgWorkerRegistry.issue_token('$m')). Không dùng chung token giữa các worker."
    [[ "$(stat -c %a "$tep_token")" == 600 ]] || chet "$tep_token phải có quyền 0600."
  fi
  tgz="$(mktemp)"
  chay_sh "tar -C '$GOC_REPO' --exclude=__pycache__ -czf '$tgz' zeus/__init__.py zeus/contracts zeus_worker"
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) incus file push <mã nguồn> $m/tmp/zeus-src.tgz; giải nén vào /opt/zeus/src"
    thong_bao "(chạy thử) incus file push <worker.toml đã thay ZEUS_SERVER_URL=$ZEUS_SERVER_URL> $m/etc/zeus-worker/worker.toml (0640 root:zeus-worker)"
    thong_bao "(chạy thử) incus file push <token> $m/etc/zeus-worker/token (0640 root:zeus-worker; giá trị không in ra)"
  else
    incus file push --mode 0644 --uid 0 --gid 0 "$tgz" "$m/tmp/zeus-src.tgz"
    incus exec "$m" -- bash -c 'rm -rf /opt/zeus/src/* && tar -xzf /tmp/zeus-src.tgz -C /opt/zeus/src && rm -f /tmp/zeus-src.tgz'
    local cfg_tmp; cfg_tmp="$(mktemp)"
    sed "s|__ZEUS_SERVER_URL__|${ZEUS_SERVER_URL}|" "$cfg" > "$cfg_tmp"
    incus file push --mode 0640 --uid 0 --gid 0 "$cfg_tmp" "$m/etc/zeus-worker/worker.toml"
    incus file push --mode 0640 --uid 0 --gid 0 "$tep_token" "$m/etc/zeus-worker/token"
    incus exec "$m" -- chgrp zeus-worker /etc/zeus-worker/worker.toml /etc/zeus-worker/token
    rm -f "$cfg_tmp"
  fi
  rm -f "$tgz"
  chay incus exec "$m" -- systemctl daemon-reload
  chay incus exec "$m" -- systemctl enable --now zeus-worker.service
  dat "$m: zeus-worker đã bật. Kiểm tra trên zeus-core: worker '$m' ONLINE sau ~15 giây. Nhật ký: bash tao-may.sh nhat-ky $m"
}
dua_worker() {
  [[ -n "${1:-}" ]] || chet "Thiếu tham số: <may>|worker"
  local m; for m in $(giai_danh_sach "$1"); do dua_worker_mot "$m"; done
}

# ---------------------------------------------------------------------------
erp_odoo_mau() {
  local m=erp-staging
  tieu_de "Odoo 19 staging vào $m (dữ liệu phải là bản đã làm sạch PII)"
  may_ton_tai "$m" || [[ $CHAY_THU == 1 ]] || chet "Chưa có $m (bash tao-may.sh tao erp)."
  cho_san_sang "$m" || true
  if [[ $CHAY_THU == 1 ]]; then
    thong_bao "(chạy thử) incus file push docker-compose.odoo.yaml $m/opt/erp/docker-compose.yaml; odoo.conf → /opt/erp/config/odoo.conf.mau"
  else
    incus file push --mode 0640 --uid 1000 --gid 1000 "$THU_MUC_GOC/anh-mau/erp/docker-compose.odoo.yaml" "$m/opt/erp/docker-compose.yaml"
    incus file push --mode 0640 --uid 1000 --gid 1000 "$THU_MUC_GOC/anh-mau/erp/odoo.conf" "$m/opt/erp/config/odoo.conf.mau"
  fi
  chay incus exec "$m" -- bash -c 'set -e; cd /opt/erp; set -a; . ./.env; set +a; sed -e "s|__DB_MAT_KHAU__|$DB_MAT_KHAU|" -e "s|__ODOO_ADMIN_MAT_KHAU__|$ODOO_ADMIN_MAT_KHAU|" config/odoo.conf.mau > config/odoo.conf; chmod 640 config/odoo.conf; docker compose pull -q; docker compose up -d'
  local ip; ip="$(ip_cua_may "$m")"
  dat "Odoo đang khởi động (1-2 phút). Từ máy admin: ssh -L 8069:$ip:8069 <server> → http://localhost:8069"
}

# ---------------------------------------------------------------------------
trang_thai() {
  tieu_de "Trạng thái"
  if [[ $CHAY_THU == 1 ]]; then thong_bao "(chạy thử) incus list"; else incus list -c ns4mt 2>/dev/null || incus list; fi
  echo
  printf '%s%-12s %-8s %-13s %-s%s\n' "$MAU_DAM" INSTANCE LOAI IP "TRUY CẬP TỪ MÁY ADMIN (thay <server>)" "$MAU_HET"
  local m ip
  for m in "${THU_TU_MAY[@]}"; do
    ip="$(ip_cua_may "$m")"
    case "${LOAI_MAY[$m]}" in
      core)   printf '%-12s %-8s %-13s ssh -L 8080:%s:8080 <server>   → Control API / Workbench\n' "$m" core "$ip" "$ip" ;;
      gpu)    printf '%-12s %-8s %-13s llama.cpp http://%s:8080/v1 (chỉ từ br-core)\n' "$m" gpu "$ip" "$ip" ;;
      edge)   printf '%-12s %-8s %-13s ingress /hooks/* (TLS) — chỉ tới zeus-core\n' "$m" edge "$ip" ;;
      worker) printf '%-12s %-8s %-13s worker nói HTTP với zeus-core:8080/worker/v1/*\n' "$m" worker "$ip" ;;
      erp)    printf '%-12s %-8s %-13s ssh -L 8069:%s:8069 <server>   → http://localhost:8069\n' "$m" erp "$ip" "$ip" ;;
    esac
  done
}

nhat_ky() {
  [[ -n "${1:-}" ]] || chet "Thiếu tên máy."
  chay incus exec "$1" -- journalctl -u zeus-worker -n 40 --no-pager
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
  dua-worker)   dua_worker "${1:-}" ;;
  erp-odoo-mau) erp_odoo_mau ;;
  trang-thai)   trang_thai ;;
  nhat-ky)      nhat_ky "${1:-}" ;;
  xoa)          xoa "${1:-}" ;;
  *) chet "Lệnh lạ: $LENH (xem đầu tệp)" ;;
esac
