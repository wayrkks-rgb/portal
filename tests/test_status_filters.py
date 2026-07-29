def test_confirmed_code_dictionary() -> None:
    from asset_sync.normalization.code_maps import ASSET_STATUS, SERVER_CATEGORY
    assert SERVER_CATEGORY["CMSVRCATCD020"] == "논리"
    assert {ASSET_STATUS["CMSTA010"], ASSET_STATUS["CMSTA050"]} == {"운영", "대기"}
