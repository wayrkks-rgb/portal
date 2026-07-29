from asset_sync.normalization.hostname import normalize_hostname
from asset_sync.normalization.ip import normalize_ip, split_ips
from asset_sync.normalization.numeric import memory_to_mb, normalize_int


def test_hostname_suffix_and_trailing_dot() -> None:
    assert normalize_hostname("WEB001.KOREALIFE.DOM.", [".korealife.dom"]) == "web001"


def test_invalid_and_multiple_ips() -> None:
    assert normalize_ip("0.0.0.0") is None
    assert split_ips(["10.0.0.2;10.0.0.1", "10.0.0.1/24"]) == ["10.0.0.1", "10.0.0.2"]


def test_numeric_equivalence_and_memory() -> None:
    assert normalize_int("8.0") == 8
    assert memory_to_mb("32 GB", "MB") == 32768
    assert memory_to_mb(32, "GB") == 32768
