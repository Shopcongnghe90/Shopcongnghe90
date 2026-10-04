# Hạ tầng ZeusVN Brain — 8 instance Incus (thin worker) trên host i9-13900 ES2, 80 GB

Script + cấu hình dựng 8 instance theo `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md` mục 4–5.
**Chưa có gì ở đây được chạy trên host thật.** ERP production chạy trên CHÍNH host này, nên mọi thay đổi host là
**HIGH PRODUCTION RISK** (ADR-012, gate G1/G2/G3).

## 1. Thay đổi so với thiết kế cũ (7 VM "Claude per VM")

| Cũ | Mới |
|---|---|
| 7 VM, mỗi VM một Claude Code + dịch vụ điều khiển từ xa + token OAuth | **Thin worker** `zeus_worker` (systemd) + token RIÊNG mỗi worker; không LLM, không API key trên VM |
| zalo-1, zalo-2 (Windows Zalo PC), facebook | **Bỏ khỏi luồng tự động** (Zalo Bot/OA API, Messenger Platform ở workstream D). Windows GUI chỉ là fallback `w-gui-win` TẮT, gate G8 (`luu-tru/windows-gui-g8/`) |
| tim-hang, ban-hang, he-thong (vai trò = CLAUDE.md) | `w-code-1`, `w-code-2`, `w-browser`, `w-ops` (vai trò = capability trong `cau-hinh-worker/*.toml`) |
| 2 bridge, nft cách ly agent↔ERP | 5 bridge (`br-core/agent/ops/erp-test/dmz`) + nft đa bridge `mang/cach-ly-mang.nft` |
| 64 GB, ARC 6 GiB | 80 GB, ARC 4 GiB, dòng dự phòng 16 GiB cho ERP production |
| `erp-thu-nghiem` Odoo 18 | `erp-staging` Odoo 19 (xác nhận cùng major với production) |
| Token Claude đẩy vào VM | **Không có.** Máy `forge` của người dùng đăng nhập tay: xem `forge/README.md` |

## 2. Bảng instance

| # | Instance | Loại | Vai trò | vCPU ghim | RAM | Đĩa | Mạng |
|---|---|---|---|---|---|---|---|
| 1 | zeus-core | VM | Control API, Temporal, Postgres16+pgvector, Workbench, artifact | 16–19 (E) | 12 GB | 150 GB | br-core 10.90.30.10 |
| 2 | zeus-gpu | Container `nvidia.runtime` | llama.cpp Qwen3.5-9B Q4_K_M + embedding + OCR | 14–15 (P) | 6 GB | 60 GB | br-core 10.90.30.20 |
| 3 | zeus-edge | Container | Ingress chỉ `/hooks/*` (TLS) | 28–29 (25%) | 1 GB | 10 GB | br-dmz 10.90.50.10 + br-core |
| 4 | w-code-1 | VM thin worker | git, build, test, Odoo addon dev | 20–23 | 8 GB | 80 GB | br-agent 10.90.10.21 |
| 5 | w-code-2 | VM thin worker | worker thứ hai | 24–25 | 6 GB | 60 GB | br-agent 10.90.10.22 |
| 6 | w-browser | VM thin worker | headless Chromium/Playwright | 26–27 | 6 GB | 40 GB | br-agent 10.90.10.23 |
| 7 | w-ops | VM thin worker | inspect, backup verify, monitoring | 28–29 | 4 GB | 40 GB | br-ops 10.90.40.10 |
| 8 | erp-staging | VM | Odoo 19 staging (dữ liệu đã làm sạch PII) | 30–31 | 8 GB | 80 GB | br-erp-test 10.90.20.11 |

Tổng RAM instance 51 GB + dự phòng ERP production 16 GB + host/Incus 4 GB + ARC 4 GB + an toàn 5 GB = 80 GB.
**Con số 16 GB là GIẢ ĐỊNH** — đo đỉnh ERP 7 ngày (gate G2) trước khi duyệt; `kiem-tra.sh` in dấu hiệu ERP production.

## 3. Cổng rủi ro host (BẮT BUỘC đọc)

Mọi script làm thay đổi host (`cai-incus.sh`, `tao-may.sh` trừ `trang-thai`/`nhat-ky`, `snapshot.sh` lệnh ghi,
`anh-mau/linux/tao-anh-mau.sh`) chạy `bao_ve_host_that` trước:

1. **Preflight chỉ đọc**: phát hiện Odoo/PostgreSQL đang chạy trên host, RAM còn trống, tải CPU.
2. `--chay-thu`: chỉ in lệnh + preflight, luôn an toàn, không cần cờ.
3. Chạy thật cần **cả ba**: cờ `--xac-nhan-rui-ro-erp-production`, biến `ZEUS_G1_DUYET=<mã quyết định của người duyệt>`,
   RAM trống ≥ `RAM_TOI_THIEU_ERP_GIB` (mặc định 20 GiB); có TTY thì gõ lại `dong-y-rui-ro`. Thiếu một trong ba ⇒ script TỪ CHỐI.

```bash
bash kiem-tra.sh                                   # chỉ đọc
sudo bash cai-incus.sh --chay-thu                  # xem toàn bộ lệnh + preflight
bash tao-may.sh --chay-thu thiet-lap
bash tao-may.sh --chay-thu tao tat-ca
bash tao-may.sh --chay-thu dua-worker worker
# Sau khi người dùng duyệt G1/G2/G3:
sudo ZEUS_G1_DUYET=<mã> bash cai-incus.sh --xac-nhan-rui-ro-erp-production
ZEUS_G1_DUYET=<mã> bash tao-may.sh --xac-nhan-rui-ro-erp-production thiet-lap
ZEUS_G1_DUYET=<mã> bash tao-may.sh --xac-nhan-rui-ro-erp-production tao tat-ca
```

## 4. Thin worker

- `anh-mau/linux/cloud-init-worker.yaml`: Python venv (chỉ `httpx` + `pydantic`), git, build-essential, unit `zeus-worker.service`
  (hardening: `NoNewPrivileges`, `ProtectSystem=strict`, user riêng). **Không** Claude Code, LLM hay API key.
- `tao-may.sh dua-worker <may>`: đẩy mã nguồn `zeus_worker` + `cau-hinh-worker/<may>.toml` (thay `__ZEUS_SERVER_URL__`) +
  token RIÊNG `bi-mat/<may>.token` (0600, gitignore; admin phát hành trên zeus-core) rồi bật dịch vụ.
- Worker chỉ có executor có kiểu (`noop.echo`, `http.fetch` với allowlist domain, `file.checksum`, `repo.tests.run`,
  `shell.allowlisted` chỉ chạy lệnh có TÊN trong cấu hình). Không có shell tự do.

## 5. Mạng (`mang/cach-ly-mang.nft`)

`br-agent` → chỉ `zeus-core:8080` (+ internet); `br-ops` → `zeus-core`; `br-dmz` → `zeus-core:8080`; `erp-staging` ↔ `zeus-core`
và ← `w-code-1/2:8069`; mọi bridge → host chỉ DHCP/DNS/ping; ERP production chỉ mở cho `zeus-core → host:8069`.
`mang/kiem-tra-cach-ly.sh` kiểm tra sau khi chạy thật.

## 6. Local AI

Profile `ho-so/zeus-gpu.yaml` (`nvidia.runtime`, bản sao của `zeus/localai/deploy/incus-gpu-profile.yaml`) và unit
`llama-server.service` / `llama-embed.service` (Qwen3.5-9B GGUF Q4_K_M + embedding). Cần driver NVIDIA trên host; không có GPU thì
`tao-may.sh` bỏ qua `zeus-gpu` và hệ thống vẫn chạy (broker dùng cloud hoặc xếp hàng). Benchmark: `python -m zeus.localai.benchmark`.

## 7. Cấu trúc

```
infra/may-ao/
├── kiem-tra.sh  cai-incus.sh  tao-may.sh  snapshot.sh  thu-vien.sh
├── bien-moi-truong.mau.env        ← KHÔNG chứa token Claude/API key
├── incus/ (preseed 5 bridge, sysctl, limits, ZFS ARC 4 GiB)   mang/ (nft đa bridge, service, kiểm tra)
├── ho-so/ (may-ao-chung, zeus-worker, zeus-core, zeus-gpu, erp)   may/ (8 instance)   cau-hinh-worker/ (4 worker.toml)
├── anh-mau/linux (cloud-init-worker, tao-anh-mau.sh)  anh-mau/erp (Odoo 19 staging)
├── forge/ (máy của NGƯỜI, cài tay, ngoài runtime)   luu-tru/windows-gui-g8/ (lưu trữ, TẮT)
```

Snapshot đêm 02:00 (Incus tự chạy), giữ 7 ngày (ERP staging 14 ngày). Khôi phục/xuất: `snapshot.sh` (qua cổng rủi ro).
