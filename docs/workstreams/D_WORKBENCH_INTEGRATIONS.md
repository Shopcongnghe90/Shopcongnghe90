# Workstream D — WORKBENCH & INTEGRATIONS

## OBJECTIVE
Xây Workbench tiếng Việt (FastAPI + Jinja2 + vanilla JS/CSS vendored, không build step, không CDN) và các tích hợp ngoài:
kênh (Zalo Bot, Zalo OA, Messenger, Shopee: verify chữ ký + normalize thành `Event`, outbound chỉ qua typed action có approval),
ERP Odoo 19 JSON-2, SaaS/site và tên miền dưới dạng `ToolProvider`.

## OWNED PATHS
- `zeus/workbench/**` (router `/wb`, `templates/`, `static/` vendored)
- `zeus/channels/**` (adapter từng kênh + router webhook `/hooks/*`)
- `zeus/integrations/**` (`odoo/`, `saas/`, `domain/` — ToolProvider)
- `config/channels.yaml`
- `migrations/4xx_*.sql` (bắt đầu 401)
- `tests/workbench_channels/**` (có `__init__.py`), `docs/reports/D/**`

## DO NOT TOUCH
Shared paths và owned paths A/B/C. Đổi contract ⇒ CONTRACT_CHANGE_REQUEST. Không gửi tin thật / gọi ERP thật trong test (chỉ marker `live`).

## INTERFACES
- **Triển khai:** `ChannelAdapter` (ZaloBotAdapter: so chuỗi `X-Bot-Api-Secret-Token` hằng thời gian; ZaloOAAdapter:
  `X-ZEvent-Signature` = sha256(appId + data + timestamp + OA secret); MessengerAdapter: `hub.challenge` + `X-Hub-Signature-256`
  HMAC-SHA256; ShopeeAdapter: push HMAC-SHA256), `ToolProvider` (kênh outbound `channel.<kênh>.send_message`, Odoo `erp.*`,
  `site.*`, `domain.*`), router Workbench dùng `WorkbenchDataSource` + Control API (`Paths.APPROVALS`, `APPROVAL_DECISION`, `TASKS`…).
- **Kiểu:** `Event`, `ChannelIdentity`, `Attachment`, `ActionSpec`, `TypedAction`, `ActionResult`, `ApprovalRequest/Decision`,
  `Task`, `EvidenceRecord`, `WorkerInfo`.
- Router xuất ra: `zeus.workbench.router`, `zeus.channels.router` (`fastapi.APIRouter`). Webhook router gọi Event Gateway của A
  qua hàm ingest được inject (Protocol-level: nhận `Event` trả `EventIngestResponse`), test bằng fake.

## DEPENDENCIES
Shared kernel; A cung cấp Control API/Event Gateway (Phase 1 test bằng fake ingest + `FakeWorkbenchDataSource`).
`jinja2`, `python-multipart`. Không Node build.

## ACCEPTANCE
1. Mỗi adapter: verify đúng/sai với vector test, chống replay (timestamp/dedupe), normalize ra `Event(untrusted=True, signature_verified=True)`.
2. Zalo Bot: tách tin > 2000 ký tự an toàn; group chỉ @mention/reply; không dựa vào tính năng group cho luồng chính.
3. Messenger: chặn gửi tự động sau 24h (trả `PolicyDenied`/đẩy người với HUMAN_AGENT).
4. Không đường gửi tin/ghi ERP nào bỏ qua Tool Gateway (test grep + test hành vi).
5. Odoo: client JSON-2 (`POST /json/2/<model>/<method>`, bearer key, `X-Odoo-Database`), chỉ phương thức nghiệp vụ nguyên khối; ghi = R2.
6. Domain: `domain.register` R3, yêu cầu evidence đã thu tiền + khách tự eKYC; site deploy R2 tới VPS riêng.
7. Workbench: trang Hàng đợi duyệt / Task / Evidence / Worker / Chi phí tiếng Việt, chạy offline, CSRF cho form duyệt, chỉ bind mạng nội bộ.

## TESTS
`tests/workbench_channels/`: TestClient cho UI và webhook, `httpx.MockTransport` cho Odoo/Zalo/Messenger/Shopee, marker `live`
cho gọi thật (mặc định skip).

## EVIDENCE
Dòng tổng kết pytest thật; ảnh chụp/HTML render trang Workbench (artifact); bảng vector chữ ký đã kiểm.

## MERGE CONTRACT
Chỉ owned paths; test PASS; cloud_exit_check không FAIL; không CDN/không build step (test quét template không có `http(s)://` script/link ngoài);
`docs/reports/D/PHASE1.md` đủ mục.
