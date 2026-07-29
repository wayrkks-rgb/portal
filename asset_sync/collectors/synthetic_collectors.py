from __future__ import annotations

from typing import Any

from ..config import AppConfig


class SyntheticITSMCollector:
    """Non-sensitive records for installation and demo verification."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_metadata: dict[str, Any] = {"mode": "DEMO", "synthetic": True}

    def collect(self) -> list[dict[str, Any]]:
        eos_field = str(self.config.itsm.get("os_eos_field", "OS_EOS_DATE")).upper()
        return [
            {
                "CM_ID": "CM-DEMO-001", "CM_NAME": "app-test-001", "CM_HOSTNAME": "app-test-001.example.invalid",
                "CM_IP": "192.0.2.10", "CM_SUB_IP": None, "CM_OS": "CMCIOSCD010", "CM_OS_VERSION": "8.8",
                "CM_CPU_CORE_CNT": 4, "CM_CPU_CNT": 1, "CM_MEMORY": 8, "CM_OWN_CAT_CD": "CMOWNCATCD0010",
                "CM_SVR_CAT_CD": "CMSVRCATCD020", "CM_CAT_CD": "HW0101", "CM_STA_CD": "CMSTA010",
                "CM_NET_CD": "CMNETCD020", "CM_WOR_MNG_EMP_ID": "operator001", "CM_OWN_DPT_ID": "DEPT_TEST",
                "CM_PLACE": "SITE_TEST", "CM_RACK_LOC": None, eos_field: "20281231",
            },
            {
                "CM_ID": "CM-DEMO-002", "CM_NAME": "db-test-001", "CM_HOSTNAME": "db-test-001.example.invalid",
                "CM_IP": "198.51.100.20", "CM_SUB_IP": None, "CM_OS": "CMCIOSCD020", "CM_OS_VERSION": "2022",
                "CM_CPU_CORE_CNT": 8, "CM_CPU_CNT": 2, "CM_MEMORY": 16, "CM_OWN_CAT_CD": "CMOWNCATCD0030",
                "CM_SVR_CAT_CD": "CMSVRCATCD020", "CM_CAT_CD": "HW0101", "CM_STA_CD": "CMSTA050",
                "CM_NET_CD": "CMNETCD020", "CM_WOR_MNG_EMP_ID": "operator001", "CM_OWN_DPT_ID": "DEPT_TEST",
                "CM_PLACE": "SITE_TEST", "CM_RACK_LOC": None, eos_field: "99991231",
            },
            {
                "CM_ID": "CM-DEMO-003", "CM_NAME": "legacy-test-001", "CM_HOSTNAME": "legacy-test-001.example.invalid",
                "CM_IP": "203.0.113.30", "CM_SUB_IP": None, "CM_OS": "CMCIOSCD070", "CM_OS_VERSION": "7.2",
                "CM_CPU_CORE_CNT": 2, "CM_CPU_CNT": 1, "CM_MEMORY": 4, "CM_OWN_CAT_CD": "CMOWNCATCD0040",
                "CM_SVR_CAT_CD": "CMSVRCATCD010", "CM_CAT_CD": "HW0102", "CM_STA_CD": "CMSTA020",
                "CM_NET_CD": "CMNETCD020", "CM_WOR_MNG_EMP_ID": "operator001", "CM_OWN_DPT_ID": "DEPT_TEST",
                "CM_PLACE": "SITE_TEST", "CM_RACK_LOC": "RACK-DEMO", eos_field: "20251231",
            },
        ]


class SyntheticRVToolsCollector:
    """Synthetic vInfo-shaped records for demo verification."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def collect(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        scope = "vc_demo"
        rows = [
            {
                "VM": "app-test-001", "Powerstate": "poweredOn", "Template": False, "SRM Placeholder": False,
                "DNS Name": "app-test-001.example.invalid", "Primary IP Address": "192.0.2.10", "CPUs": 4,
                "Memory": 8192, "OS according to the VMware Tools": "Red Hat Enterprise Linux 8.8",
                "Datacenter": "DC_DEMO", "Cluster": "CLUSTER_DEMO", "Host": "esxi-demo-001.example.invalid",
                "VM ID": "vm-101", "SMBIOS UUID": "demo-smbios-001", "VM UUID": "demo-vm-uuid-001",
                "VI SDK Server": scope, "_vcenter_scope": scope, "_source_file": "synthetic",
            },
            {
                "VM": "db-test-001", "Powerstate": "poweredOn", "Template": False, "SRM Placeholder": False,
                "DNS Name": "db-test-001.example.invalid", "Primary IP Address": "198.51.100.20", "CPUs": 8,
                "Memory": 16384, "OS according to the VMware Tools": "Microsoft Windows Server 2022",
                "Datacenter": "DC_DEMO", "Cluster": "CLUSTER_DEMO", "Host": "esxi-demo-002.example.invalid",
                "VM ID": "vm-102", "SMBIOS UUID": "demo-smbios-002", "VM UUID": "demo-vm-uuid-002",
                "VI SDK Server": scope, "_vcenter_scope": scope, "_source_file": "synthetic",
            },
            {
                "VM": "vm-demo-001", "Powerstate": "poweredOn", "Template": False, "SRM Placeholder": False,
                "DNS Name": "vm-demo-001.example.invalid", "Primary IP Address": "203.0.113.40", "CPUs": 2,
                "Memory": 4096, "OS according to the VMware Tools": "Ubuntu Linux 22.04",
                "Datacenter": "DC_DEMO", "Cluster": "CLUSTER_DEMO", "Host": "esxi-demo-002.example.invalid",
                "VM ID": "vm-103", "SMBIOS UUID": "demo-smbios-003", "VM UUID": "demo-vm-uuid-003",
                "VI SDK Server": scope, "_vcenter_scope": scope, "_source_file": "synthetic",
            },
        ]
        return rows, {
            "success_scopes": [scope], "failed_scopes": {},
            "files": [{"file": "synthetic", "rows": len(rows), "scope": scope}], "failed_files": [],
            "mode": "DEMO", "synthetic": True,
        }
