"""option_quote 单元测试（无网络）。"""

from services.option_quote import (
    ETF_OPTION_REGISTRY,
    _contract_matches_month,
    _normalize_end_month,
    _option_type_from_code,
    list_supported_underlyings,
)


def test_registry_includes_core_etfs():
    assert "588000" in ETF_OPTION_REGISTRY
    assert "159915" in ETF_OPTION_REGISTRY


def test_option_type_from_code():
    assert _option_type_from_code("588000P2606M02000") == "put"
    assert _option_type_from_code("588000C2606M01300") == "call"
    assert _option_type_from_code("159915P2606M002800") == "put"


def test_contract_matches_month():
    assert _contract_matches_month("588000P2606M02000", "2606")
    assert not _contract_matches_month("588000P2607M02000", "2606")


def test_normalize_end_month():
    assert _normalize_end_month("2606") == "2606"
    assert _normalize_end_month(None)  # noqa: S101 — 仅断言不抛错


def test_list_supported_underlyings():
    items = list_supported_underlyings()
    codes = {i["underlying"] for i in items}
    assert "588000" in codes
