#!/usr/bin/env bash
# tao-anh-mau.sh — Dựng ẢNH MẪU thin worker một lần, để 4 VM worker tạo ra nhanh hơn (~1 phút thay vì 5-15 phút).
#
#   bash anh-mau/linux/tao-anh-mau.sh --chay-thu                                   # chỉ in lệnh + preflight
#   ZEUS_G1_DUYET=<mã> bash anh-mau/linux/tao-anh-mau.sh --xac-nhan-rui-ro-erp-production   # dựng thật
#
# Quy trình: launch VM tạm mau-zeus-worker (hồ sơ zeus-worker, cloud-init đầy đủ) → chờ cloud-init
#   → dọn định danh/cache (machine-id, khoá SSH, cấu hình/token worker nếu có) → cloud-init clean
#   → tắt → incus publish --alias zeus-worker-mau → xoá VM tạm.
# Ảnh KHÔNG chứa token/worker.toml: những thứ đó do tao-may.sh dua-worker đẩy vào từng VM.
set -euo pipefail
# shellcheck source=../../thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../thu-vien.sh"
doc_co_chung "$@"

TAM=mau-zeus-worker; ALIAS=zeus-worker-mau
tieu_de "tao-anh-mau.sh — $([[ $CHAY_THU == 1 ]] && echo 'CHẠY THỬ' || echo 'DỰNG THẬT')"
nap_bien_moi_truong
bao_ve_host_that "tạo VM tạm + publish ảnh mẫu trên pool Incus (tải CPU/IO ~10 phút)"
incus_san_sang || [[ $CHAY_THU == 1 ]] || chet "Không nối được Incus."
if [[ $CHAY_THU != 1 ]]; then
  incus profile show zeus-worker >/dev/null 2>&1 || chet "Chưa có hồ sơ zeus-worker — chạy: bash tao-may.sh thiet-lap"
  incus info "$TAM" >/dev/null 2>&1 && chet "VM tạm $TAM đang tồn tại — xoá (incus delete $TAM --force) rồi chạy lại."
fi

tieu_de "1/5 Khởi tạo VM tạm (4 vCPU bất kỳ, 6 GiB, không snapshot)"
chay incus launch images:ubuntu/24.04/cloud "$TAM" --vm -p may-ao-chung -p zeus-worker \
  -c limits.cpu=4 -c limits.memory=6GiB -c snapshots.schedule= -c boot.autostart=false

tieu_de "2/5 Chờ cloud-init"
if [[ $CHAY_THU != 1 ]]; then
  for _ in $(seq 1 60); do incus exec "$TAM" -- true >/dev/null 2>&1 && break; sleep 5; done
  set +e; incus exec "$TAM" -- cloud-init status --wait; rc=$?; set -e
  if [[ $rc -ne 0 ]]; then
    canh_bao "cloud-init trả mã $rc — 50 dòng cuối nhật ký:"; incus exec "$TAM" -- tail -50 /var/log/cloud-init-output.log || true
    [[ $rc -eq 2 ]] || chet "Dừng: cloud-init thất bại. Sửa rồi: incus delete $TAM --force; chạy lại."
    read -r -p "cloud-init 'degraded'. Vẫn publish ảnh mẫu? (dong-y/khong) " XN; [[ "$XN" == dong-y ]] || chet "Huỷ."
  fi
else thong_bao "(chạy thử) incus exec $TAM -- cloud-init status --wait"; fi

tieu_de "3/5 Dọn định danh & cache để nhân bản an toàn"
chay incus exec "$TAM" -- bash -c '
set -e
systemctl stop zeus-worker.service 2>/dev/null || true
rm -f /etc/zeus-worker/token /etc/zeus-worker/worker.toml
rm -rf /opt/zeus/src/* /var/lib/zeus-worker/work/* /root/.cache /home/admin/.cache
rm -f /root/.bash_history /home/admin/.bash_history
rm -f /etc/ssh/ssh_host_*
truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id
apt-get clean; rm -rf /var/lib/apt/lists/*
cloud-init clean --logs --seed
journalctl --rotate 2>/dev/null || true; journalctl --vacuum-time=1s 2>/dev/null || true
printf "anh-mau-zeus-worker publish %s\n" "$(date -u +%FT%TZ)" >> /etc/may-ao-anh-mau
sync'

tieu_de "4/5 Tắt & publish"
chay incus stop "$TAM" --timeout 120
chay incus publish "$TAM" --alias "$ALIAS" --reuse "description=Ảnh mẫu thin worker (Python venv + zeus-worker.service) $(date +%F)"

tieu_de "5/5 Xoá VM tạm"
chay incus delete "$TAM"
[[ $CHAY_THU == 1 ]] || incus image list "$ALIAS"
dat "Xong. Tiếp: bash $THU_MUC_GOC/tao-may.sh tao worker   (sẽ dùng ảnh mẫu $ALIAS)"
