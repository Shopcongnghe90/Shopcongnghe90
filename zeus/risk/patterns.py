"""Mẫu nguy hiểm dùng chung cho Risk Engine và Policy Engine (một nguồn sự thật). Khớp trên văn bản đã ``fold``."""

from __future__ import annotations

import re

# (rule_id, mô tả tiếng Việt, regex trên văn bản đã bỏ dấu/hạ chữ thường)
DANGEROUS_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("D-DROP-DB", "Xoá cơ sở dữ liệu/bảng (DROP)", re.compile(r"\bdrop\s+(database|table|schema)\b|\btruncate\s+table\b")),
    (
        "D-RM-RF",
        "Xoá đệ quy cưỡng bức (rm -rf)",
        re.compile(r"\brm\s+(-[a-z]+\s+)*-[a-z]*(rf|fr)\b|\brm\s+-r\s+-f\b|\brm\s+-f\s+-r\b|\bmkfs\b|\bdd\s+if="),
    ),
    (
        "D-CHMOD-ROOT",
        "Đổi quyền/chủ sở hữu hệ thống (chmod/chown root)",
        re.compile(r"\b(chmod|chown)\b[^;\n]{0,40}(root|777|-r\s+/|\s/\s*$|\s/etc|\s/var|\s/usr)"),
    ),
    ("D-SUDO", "Dùng sudo/leo thang root", re.compile(r"\bsudo\b|\bsu\s+root\b|\bsetuid\b|\bsudoers\b")),
    (
        "D-PAYMENT",
        "Thanh toán/chuyển tiền",
        re.compile(
            r"thanh toan|chuyen tien|chuyen khoan|\bpayment\b|\bpayout\b|transfer (the )?(money|funds)|wire transfer|hoan tien (het|toan bo)"
        ),
    ),
    (
        "D-DNS-DELETE",
        "Xoá bản ghi DNS",
        re.compile(r"(xoa|delete|remove|drop|go bo)\b[^.\n]{0,25}\bdns\b|\bdns\b[^.\n]{0,25}(xoa|delete|remove)"),
    ),
    (
        "D-CREDENTIAL-EXPORT",
        "Xuất credential/secret",
        re.compile(
            r"(xuat|export|dump|in ra|print|show|gui|send|leak|lay|cho toi|doc|cat|reveal|exfil\w*)\b[^.\n]{0,30}"
            r"(credential|secret|mat khau|password|api[ _-]?key|private key|access token|\.env\b|\btoken)"
        ),
    ),
    (
        "D-PRIV-ESC",
        "Nâng quyền",
        re.compile(
            r"nang quyen|privilege escalation|escalate privilege|grant all privileges|become root|cap quyen (admin|root)|make me (an )?admin"
        ),
    ),
    (
        "D-PROD-ERP-WRITE",
        "Sửa trực tiếp ERP production",
        re.compile(
            r"((sua|update|edit|modify|ghi|xoa|delete|alter)\b[^.\n]{0,30}(erp|odoo)[^.\n]{0,20}(production|prod\b|that\b|live))"
            r"|((sua|update|edit|modify|ghi|xoa|delete|alter)\b[^.\n]{0,30}(production|prod\b|live)[^.\n]{0,20}(erp|odoo))"
            r"|((production|prod\b)[^.\n]{0,15}(erp|odoo)[^.\n]{0,30}(sua|update|edit|modify|xoa|delete|alter))"
        ),
    ),
]

INJECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "I-IGNORE",
        re.compile(
            r"ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the )?(system|previous)|bo qua (moi|tat ca|cac) (huong dan|chi dan|lenh)"
        ),
    ),
    ("I-SYSPROMPT", re.compile(r"system prompt|you are now|ban bay gio la|developer mode|jailbreak")),
]


def find_dangerous(folded_text: str) -> list[tuple[str, str]]:
    return [(rid, desc) for rid, desc, rx in DANGEROUS_RULES if rx.search(folded_text)]


def find_injection(folded_text: str) -> list[str]:
    return [rid for rid, rx in INJECTION_RULES if rx.search(folded_text)]
