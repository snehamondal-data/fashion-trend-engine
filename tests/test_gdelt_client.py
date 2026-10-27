from src.gdelt_client import GDELTClient, time_params


def test_relative_timespan_passes_through():
    assert time_params("12m") == {"timespan": "12m"}


def test_pinned_window_becomes_start_and_end():
    assert time_params("20260713000000-20261025235959") == {
        "startdatetime": "20260713000000",
        "enddatetime": "20261025235959",
    }


def test_window_is_part_of_the_cache_key(tmp_path):
    # A different window must never be served from another window's cache.
    client = GDELTClient(cache_dir=tmp_path)
    a = client.cache_path("suede", "luxury", "q", "20260713000000-20261025235959")
    b = client.cache_path("suede", "luxury", "q", "20260720000000-20261025235959")
    assert a != b
