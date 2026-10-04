#!/usr/bin/env bash
# tao-anh-mau.sh — Dựng ẢNH MẪU agent Linux một lần, để 4 máy agent tạo ra trong ~1 phút thay vì 10-20 phút.
#
#   bash anh-mau/linux/tao-anh-mau.sh --chay-thu     # chỉ in lệnh
#   bash anh-mau/linux/tao-anh-mau.sh                # dựng thật (~15-25 phút, cần internet)
#
# Quy trình: launch máy tạm mau-agent-linux (hồ sơ agent-linux, cloud-init đầy đủ) → chờ cloud-init
#   → dọn bí mật/định danh (machine-id, khoá SSH, mật khẩu VNC tạm, cache) → cloud-init clean
#   → tắt → incus publish --alias agent-linux-mau → xoá máy tạm.
# Sau đó tao-may.sh tao linux tự nhận ra ảnh mẫu và dùng hồ sơ agent-linux-tu-mau.
# Dựng lại (cập nhật Chrome/Claude mới): chạy lại script này; alias cũ bị thay (--reuse).
set -euo pipefail
# shellcheck source=../../thu-vien.sh
source "$(dirname "${BASH_SOURCE[0]}")/../../thu-vien.sh"
[[ "${1:-}" == "--chay-thu" ]] && CHAY_THU=1

TAM=mau-agent-linux; ALIAS=agent-linux-mau
tieu_de "tao-anh-mau.sh — $([[ $CHAY_THU == 1 ]] && echo 'CHẠY THỬ' || echo 'DỰNG THẬT')"
nap_bien_moi_truong
incus_san_sang || [[ $CHAY_THU == 1 ]] || chet "Không nối được Incus."
if [[ $CHAY_THU != 1 ]]; then
  incus profile show agent-linux >/dev/null 2>&1 || chet "Chưa có hồ sơ agent-linux — chạy: bash tao-may.sh thiet-lap"
  incus info "$TAM" >/dev/null 2>&1 && chet "Máy tạm $TAM đang tồn tại — xoá (incus delete $TAM --force) rồi chạy lại."
fi

tieu_de "1/5 Khởi tạo máy tạm (4 vCPU bất kỳ, 6 GiB, không snapshot)"
chay incus launch images:ubuntu/24.04/cloud "$TAM" --vm -p may-ao-chung -p agent-linux \
  -c limits.cpu=4 -c limits.memory=6GiB -c snapshots.schedule= -c boot.autostart=false

tieu_de "2/5 Chờ cloud-init (10-20 phút)"
if [[ $CHAY_THU != 1 ]]; then
  for _ in $(seq 1 60); do incus exec "$TAM" -- true >/dev/null 2>&1 && break; sleep 5; done
  set +e; incus exec "$TAM" -- cloud-init status --wait; rc=$?; set -e
  if [[ $rc -ne 0 ]]; then
    canh_bao "cloud-init trả mã $rc — 50 dòng cuối nhật ký:"; incus exec "$TAM" -- tail -50 /var/log/cloud-init-output.log || true
    [[ $rc -eq 2 ]] || chet "Dừng: cloud-init thất bại. Sửa rồi: incus delete $TAM --force; chạy lại."
    read -r -p "cloud-init 'degraded'. Vẫn publish ảnh mẫu? (dong-y/khong) " XN; [[ "$XN" == dong-y ]] || chet "Huỷ."
  fi
  thong_bao "Phiên bản trong ảnh: $(incus exec "$TAM" -- bash -c 'google-chrome-stable --version 2>/dev/null; node --version; sudo -u agent -H /home/agent/.local/bin/claude --version 2>/dev/null' | tr '\n' ' ')"
else thong_bao "(chạy thử) incus exec $TAM -- cloud-init status --wait"; fi

tieu_de "3/5 Dọn bí mật & định danh để nhân bản an toàn"
chay incus exec "$TAM" -- bash -c '
set -e
systemctl stop claude-remote-control.service xfce@1.service xvnc@1.service novnc.service 2>/dev/null || true
rm -f /etc/claude-agent/env /etc/claude-agent/vnc-tam /home/agent/.vnc/passwd /home/agent/.Xauthority
rm -rf /home/agent/.config/chrome-agent /home/agent/.cache /home/agent/.npm/_logs /tmp/.X11-unix/* /tmp/.X*-lock
rm -f /home/agent/.bash_history /root/.bash_history
rm -f /etc/ssh/ssh_host_*
truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id
apt-get clean; rm -rf /var/lib/apt/lists/*
cloud-init clean --logs --seed
journalctl --rotate 2>/dev/null || true; journalctl --vacuum-time=1s 2>/dev/null || true
printf "anh-mau-agent-linux publish %s\n" "$(date -u +%FT%TZ)" >> /etc/may-ao-anh-mau
sync'

tieu_de "4/5 Tắt & publish"
chay incus stop "$TAM" --timeout 120
chay incus publish "$TAM" --alias "$ALIAS" --reuse "description=Ảnh mẫu agent Linux (XFCE+Chrome+noVNC+Claude Code) $(date +%F)"

tieu_de "5/5 Xoá máy tạm"
chay incus delete "$TAM"
[[ $CHAY_THU == 1 ]] || incus image list "$ALIAS"
dat "Xong. Tiếp: bash $THU_MUC_GOC/tao-may.sh tao linux   (sẽ dùng ảnh mẫu $ALIAS)"
