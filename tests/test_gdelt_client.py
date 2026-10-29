import requests

from src.config import GDELT_DOC_URL
from src.gdelt_client import GDELTClient, time_params
from src.query_builder import or_block


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


def test_accented_term_is_utf8_percent_encoded():
    # What actually goes on the wire for a quoted non-ASCII term: UTF-8
    # percent-encoding, quotes intact. Checked when every "lamé" fetch failed.
    query = or_block(["metallic", "lamé"])
    url = requests.Request("GET", GDELT_DOC_URL, params={"query": query}).prepare().url
    assert url.endswith("?query=%28metallic+OR+%22lam%C3%A9%22%29")
