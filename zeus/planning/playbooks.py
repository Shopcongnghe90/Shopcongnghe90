"""Playbook deterministic theo TaskFamily: luôn sinh TaskGraph hợp lệ kể cả khi không có model.

Mỗi bước có thể gắn tên action (typed). Planner chỉ gắn action nếu ActionSpec tồn tại trong registry;
nếu không, bước thành bước không-action (agent/người thực hiện) và Critic/Judge sẽ phản ánh thiếu bằng chứng.
"""

from __future__ import annotations

from dataclasses import dataclass

from zeus.contracts.models import RiskLevel, TaskFamily

PLAYBOOK_VERSION = "playbook-2"  # 2: bước optional (http.check/file.checksum) chỉ có khi entity của Intent qua kiểm allowlist


@dataclass(frozen=True)
class Step:
    node_id: str
    title: str
    depends_on: tuple[str, ...] = ()
    action: str | None = None
    risk: RiskLevel = RiskLevel.R0
    capabilities: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    optional: bool = False  # chỉ đưa vào kế hoạch nếu sinh được tham số typed hợp lệ từ entity của Intent (url/path...)


def _code(family_label: str) -> list[Step]:
    return [
        Step("analyze", f"Phân tích yêu cầu ({family_label})", acceptance=("Hiểu rõ phạm vi và tiêu chí hoàn thành",)),
        Step("implement", "Thực hiện thay đổi", ("analyze",), action="code.apply_patch", risk=RiskLevel.R1, capabilities=("git", "python"), acceptance=("Diff áp dụng sạch",)),
        Step("test", "Chạy kiểm thử", ("implement",), action="test.run", capabilities=("python",), acceptance=("Toàn bộ test liên quan pass",)),
        Step("check", "Kiểm tra trang/API sau thay đổi", ("test",), action="http.check", optional=True, acceptance=("HTTP 200 trên địa chỉ đã nêu",)),
        Step("report", "Tổng kết và bàn giao", ("test", "check"), acceptance=("Có bằng chứng test đính kèm",)),
    ]


_GENERIC = [
    Step("analyze", "Phân tích yêu cầu", acceptance=("Hiểu rõ phạm vi",)),
    Step("execute", "Thực hiện", ("analyze",), risk=RiskLevel.R1, acceptance=("Kết quả đúng yêu cầu",)),
    Step("verify", "Kiểm chứng", ("execute",), action="test.run", acceptance=("Có bằng chứng kiểm chứng",)),
]

PLAYBOOKS: dict[TaskFamily, list[Step]] = {
    TaskFamily.ERP_BUG: [
        Step("reproduce", "Tái hiện lỗi trên bản sao", action="erp.read", capabilities=("odoo_client",), acceptance=("Có log/ảnh tái hiện",)),
        Step("fix", "Sửa lỗi trên staging", ("reproduce",), action="code.apply_patch", risk=RiskLevel.R1, capabilities=("git", "python"), acceptance=("Diff áp dụng sạch",)),
        Step("test", "Kiểm thử hồi quy", ("fix",), action="test.run", capabilities=("python",), acceptance=("Test hồi quy pass",)),
        Step("report", "Báo cáo và đề xuất phát hành", ("test",), acceptance=("Có bằng chứng test",)),
    ],
    TaskFamily.ERP_FEATURE: _code("tính năng ERP"),
    TaskFamily.WEBSITE_EDIT: _code("sửa website"),
    TaskFamily.WEBSITE_BUILD: _code("xây website"),
    TaskFamily.FRONTEND: _code("frontend"),
    TaskFamily.BACKEND: _code("backend"),
    TaskFamily.DATABASE: [
        Step("analyze", "Phân tích schema và tác động", action="db.read", acceptance=("Liệt kê bảng/khoá bị ảnh hưởng",)),
        Step("backup", "Sao lưu trước khi đổi", ("analyze",), action="db.backup", risk=RiskLevel.R1, acceptance=("Có bản backup kiểm chứng",)),
        Step("migrate", "Áp dụng migration", ("backup",), action="db.migrate", risk=RiskLevel.R2, acceptance=("Migration chạy xong, schema đúng",)),
        Step("verify", "So khớp dữ liệu sau migration", ("migrate",), action="db.check", acceptance=("Số liệu trước/sau khớp",)),
    ],
    TaskFamily.CUSTOMER_SUPPORT: [
        Step("lookup", "Tra cứu đơn hàng/sản phẩm", action="erp.read", capabilities=("odoo_client",), acceptance=("Có dữ liệu đối chiếu",)),
        Step("draft", "Soạn phản hồi (chưa gửi)", ("lookup",), acceptance=("Bản nháp tiếng Việt đúng dữ liệu",)),
        Step("send", "Gửi phản hồi cho khách", ("draft",), action="channel.send_message", risk=RiskLevel.R2, acceptance=("Khách nhận được tin",)),
    ],
    TaskFamily.DOMAIN_PROVISIONING: [
        Step("check", "Kiểm tra tên miền/DNS hiện tại", action="dns.read", acceptance=("Biết bản ghi hiện có",)),
        Step("apply", "Cấu hình DNS", ("check",), action="dns.apply", risk=RiskLevel.R2, acceptance=("Bản ghi được tạo",)),
        Step("verify", "Kiểm tra phân giải và SSL", ("apply",), action="http.check", acceptance=("HTTP 200 trên tên miền mới",)),
    ],
    TaskFamily.DEPLOYMENT: [
        Step("prepare", "Chuẩn bị bản phát hành", action="test.run", capabilities=("python",), acceptance=("Test pass trước khi deploy",)),
        Step("deploy", "Triển khai", ("prepare",), action="deploy.apply", risk=RiskLevel.R2, acceptance=("Triển khai hoàn tất",)),
        Step("checksum", "Đối chiếu checksum tệp phát hành", ("deploy",), action="file.checksum", optional=True, acceptance=("sha256 của tệp phát hành khớp",)),
        Step("verify", "Kiểm tra sau triển khai", ("deploy", "checksum"), action="http.check", acceptance=("Health check pass",)),
    ],
    TaskFamily.SECURITY: [
        Step("scan", "Rà soát bảo mật", action="security.scan", acceptance=("Có báo cáo rà soát",)),
        Step("fix", "Khắc phục", ("scan",), action="code.apply_patch", risk=RiskLevel.R2, acceptance=("Diff áp dụng sạch",)),
        Step("verify", "Quét lại", ("fix",), action="security.scan", acceptance=("Không còn lỗ hổng đã biết",)),
    ],
    TaskFamily.VISUAL_QA: [
        Step("capture", "Chụp màn hình", action="browser.screenshot", capabilities=("browser",), acceptance=("Có ảnh chụp",)),
        Step("compare", "So sánh với thiết kế", ("capture",), acceptance=("Danh sách sai khác",)),
    ],
}


def playbook_for(family: TaskFamily) -> list[Step]:
    return PLAYBOOKS.get(family, _GENERIC)

