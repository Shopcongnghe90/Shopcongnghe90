"""Test tĩnh + dry-run cho infra/may-ao (không chạy gì lên host thật)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from zeus_worker.config import WorkerConfig

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "may-ao"
TEXT_SUFFIX = {".sh", ".yaml", ".yml", ".md", ".env", ".nft", ".service", ".toml", ".xml", ".conf", ".ps1"}

# Bảng 8 instance trong kiến trúc mục 4: (cpu, ram, đĩa, bridge, ip)
EXPECTED = {
    "zeus-core": ("16-19", "12GiB", "150GiB", "br-core", "10.90.30.10"),
    "zeus-gpu": ("14-15", "6GiB", "60GiB", "br-core", "10.90.30.20"),
    "zeus-edge": ("28-29", "1GiB", "10GiB", "br-dmz", "10.90.50.10"),
    "w-code-1": ("20-23", "8GiB", "80GiB", "br-agent", "10.90.10.21"),
    "w-code-2": ("24-25", "6GiB", "60GiB", "br-agent", "10.90.10.22"),
    "w-browser": ("26-27", "6GiB", "40GiB", "br-agent", "10.90.10.23"),
    "w-ops": ("28-29", "4GiB", "40GiB", "br-ops", "10.90.40.10"),
    "erp-staging": ("30-31", "8GiB", "80GiB", "br-erp-test", "10.90.20.11"),
}
FORBIDDEN = re.compile(r"setup-token|remote-control|CLAUDE_CODE_OAUTH_TOKEN|claude-remote", re.I)


def infra_files(exclude_forge=True):
    for p in INFRA.rglob("*"):
        if p.is_file() and (p.suffix in TEXT_SUFFIX or p.name == ".gitignore"):
            if exclude_forge and "forge" in p.relative_to(INFRA).parts:
                continue
            yield p


def run(*args, env_extra=None, cwd=INFRA):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZEUS_G1")}
    env.update(env_extra or {})
    return subprocess.run(["bash", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)


def test_no_remote_control_or_setup_token_in_default_path():
    hits = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in infra_files() for m in FORBIDDEN.finditer(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []
    forge = (INFRA / "forge" / "README.md").read_text()
    assert "claude auth login" in forge and "setup-token" in forge  # chỉ nằm trong tài liệu máy forge của người dùng


def test_eight_instances_match_architecture_table():
    names = {p.stem for p in (INFRA / "may").glob("*.yaml")}
    assert names == set(EXPECTED)  # không còn zalo-1/2, facebook, tim-hang...
    for n, (cpu, ram, disk, bridge, ip) in EXPECTED.items():
        y = yaml.safe_load((INFRA / "may" / f"{n}.yaml").read_text())
        assert y["config"]["limits.cpu"] == cpu and y["config"]["limits.memory"] == ram, n
        assert y["devices"]["root"]["size"] == disk and y["devices"]["eth0"]["network"] == bridge and y["devices"]["eth0"]["ipv4.address"] == ip, n
    lib = (INFRA / "thu-vien.sh").read_text()
    for n in EXPECTED:
        assert re.search(rf"\[{re.escape(n)}\]=", lib), n
    assert not re.search(r"zalo|facebook|tim-hang|ban-hang|he-thong", lib)


def test_worker_vm_cloud_init_installs_zeus_worker_without_llm():
    ci = (INFRA / "anh-mau/linux/cloud-init-worker.yaml").read_text()
    doc = yaml.safe_load(ci)
    assert "zeus-worker.service" in ci and "-m zeus_worker" in ci and "ProtectSystem=strict" in ci
    assert set(doc["packages"]).isdisjoint({"nodejs", "npm", "chromium", "google-chrome-stable"})
    low = ci.lower()
    assert not re.search(r"anthropic_api_key|openai_api_key|oauth|npm install|llama", low)
    assert (INFRA / "ho-so/zeus-worker.yaml").exists() and not (INFRA / "ho-so/agent-linux.yaml").exists()
    assert not (INFRA / "anh-mau/windows").exists()


@pytest.mark.parametrize("name", ["w-code-1", "w-code-2", "w-browser", "w-ops"])
def test_worker_toml_valid_and_secret_free(name):
    raw = (INFRA / "cau-hinh-worker" / f"{name}.toml").read_text()
    cfg = WorkerConfig.from_dict(tomllib.loads(raw.replace("__ZEUS_SERVER_URL__", "http://10.90.30.10:8080")))
    assert cfg.worker_id == name and cfg.token_file == Path("/etc/zeus-worker/token")
    assert "token =" not in raw and not re.search(r"zwt_|secret|password", raw, re.I)
    assert cfg.network_zone == ("ops" if name == "w-ops" else "agent")


def test_nft_policy_whitelists_only_expected_flows():
    nft = (INFRA / "mang/cach-ly-mang.nft").read_text()
    for br in ("br-core", "br-agent", "br-ops", "br-erp-test", "br-dmz"):
        assert f'"{br}"' in nft
    for line in nft.splitlines():
        if line.strip().startswith("#") or " accept" not in line or 'iifname "br-' not in line:
            continue
        if "chain input" in line or "dport { 53, 67 }" in line or "dport 53" in line or "icmp" in line:
            continue
        assert re.search(r"10\.90\.30\.10|10\.90\.20\.11", line), line  # mọi đường mở giữa bridge đều trỏ vào zeus-core hoặc erp-staging
    assert "bridge -> bridge ngoài danh sách trắng" in nft and "bridge -> host: chặn" in nft
    # ERP production (host): chỉ zeus-core
    prod = [l for l in nft.splitlines() if "ERP production" in l and "accept" in l]
    assert len(prod) == 1 and "10.90.30.10" in prod[0] and "8069" in prod[0]


def test_gpu_profile_nvidia_runtime_and_copy_in_sync():
    a = (INFRA / "ho-so/zeus-gpu.yaml").read_text()
    assert a == (ROOT / "zeus/localai/deploy/incus-gpu-profile.yaml").read_text()
    y = yaml.safe_load(a)
    assert y["config"]["nvidia.runtime"] == "true" and y["devices"]["gpu"]["type"] == "gpu"


def test_preseed_has_five_bridges_and_no_default_nic():
    y = yaml.safe_load((INFRA / "incus/preseed.yaml").read_text().replace("__NGUON_POOL__", "size: 1GiB"))
    assert {n["name"] for n in y["networks"]} == {"br-core", "br-agent", "br-ops", "br-erp-test", "br-dmz"}
    default = next(p for p in y["profiles"] if p["name"] == "default")
    assert "eth0" not in default["devices"]


def test_all_scripts_pass_bash_syntax_check():
    scripts = sorted(INFRA.rglob("*.sh"))
    assert len(scripts) >= 8
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, f"{s}: {r.stderr}"


def test_shellcheck_clean():
    exe = os.environ.get("ZEUS_SHELLCHECK") or shutil.which("shellcheck")
    if not exe:
        pytest.skip("shellcheck chưa cài (pip install shellcheck-py; đặt ZEUS_SHELLCHECK)")
    scripts = [str(p) for p in sorted(INFRA.rglob("*.sh"))]
    r = subprocess.run([exe, "-x", "-S", "warning", *scripts], capture_output=True, text=True, cwd=INFRA)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize(
    "args",
    [["tao-may.sh", "thiet-lap"], ["tao-may.sh", "tao", "tat-ca"], ["tao-may.sh", "dua-worker", "w-code-1"],
     ["snapshot.sh", "tao", "tat-ca"], ["snapshot.sh", "khoi-phuc", "w-ops", "x"], ["anh-mau/linux/tao-anh-mau.sh"]],
)
def test_host_changes_refused_without_risk_flag_and_approval(args):
    r = run(*args)
    assert r.returncode != 0 and "thiếu cờ --xac-nhan-rui-ro-erp-production" in r.stdout + r.stderr
    r = run(args[0], "--xac-nhan-rui-ro-erp-production", *args[1:])  # có cờ nhưng không có mã duyệt G1
    assert r.returncode != 0 and "ZEUS_G1_DUYET" in r.stdout + r.stderr


@pytest.mark.parametrize(
    "args",
    [["tao-may.sh", "--chay-thu", "thiet-lap"], ["tao-may.sh", "--chay-thu", "tao", "tat-ca"],
     ["tao-may.sh", "--chay-thu", "dua-worker", "worker"], ["tao-may.sh", "--chay-thu", "erp-odoo-mau"],
     ["snapshot.sh", "--chay-thu", "tao", "w-ops"], ["anh-mau/linux/tao-anh-mau.sh", "--chay-thu"]],
)
def test_chay_thu_prints_plan_and_exits_zero(args):
    r = run(*args)
    out = (r.stdout + r.stderr).replace(str(ROOT), "<repo>")  # đường dẫn worktree có thể chứa '.claude'
    assert r.returncode == 0, out
    assert "Preflight host" in out and "CHẠY THỬ" in out
    assert not re.search(r"claude|oauth|remote-control", out, re.I)


def test_chay_thu_tao_all_covers_8_instances_and_skips_windows():
    out = run("tao-may.sh", "--chay-thu", "tao", "tat-ca").stdout.replace(str(ROOT), "<repo>")
    for n in EXPECTED:
        assert re.search(rf"incus init \S+ {re.escape(n)}\b", out), n
    assert "--vm" in out and not re.search(r"windows|zalo|facebook", out, re.I)
    gpu_line = next(l for l in out.splitlines() if "incus init" in l and "zeus-gpu" in l)
    assert "--vm" not in gpu_line and "-p zeus-gpu" in gpu_line  # container nvidia.runtime, không phải VM


def test_dua_worker_refuses_non_worker_and_missing_token(tmp_path):
    r = run("tao-may.sh", "--chay-thu", "dua-worker", "erp-staging")
    assert r.returncode != 0 and "chỉ dùng cho VM thin worker" in r.stdout + r.stderr


def test_preflight_detects_missing_ram_budget():
    r = run("tao-may.sh", "--xac-nhan-rui-ro-erp-production", "thiet-lap", env_extra={"ZEUS_G1_DUYET": "G1-test", "RAM_TOI_THIEU_ERP_GIB": "99999"})
    assert r.returncode != 0 and "RAM còn trống" in r.stdout + r.stderr and "Từ chối" in r.stdout + r.stderr
