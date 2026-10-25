from tools.estimate_storage import estimate


def test_storage_estimate_is_explicitly_bounded():
    result = estimate(10000, 2000000, 4, 5, 960)
    assert result["sample_bytes_per_row"] == 200
    assert result["watchlist_row_upper_bound"] == 4 * 5 * 366 * 960
    assert result["estimated_bar_storage_upper_bound_bytes"] > 500_000_000
    assert estimate(0, 0, 4, 5, 960)["estimated_bar_storage_upper_bound_bytes"] is None
