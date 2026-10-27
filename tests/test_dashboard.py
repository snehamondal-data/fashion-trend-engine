import json
import re

import pytest

from src import dashboard


def test_render_embeds_json_that_cannot_close_the_script_tag():
    # A label containing "</script>" would otherwise end the data block early
    # and the rest of the JSON would render as page text.
    payload = {"label": "</script><b>x</b>"}
    html = dashboard.render(payload, "<script>/*__DATA__*/</script>")
    inner = re.fullmatch(r"<script>(.*)</script>", html, re.S).group(1)
    assert "</" not in inner
    assert json.loads(inner) == payload


def test_render_requires_exactly_one_placeholder():
    with pytest.raises(ValueError, match="placeholder"):
        dashboard.render({}, "<script></script>")


def test_template_has_the_placeholder():
    assert dashboard.TEMPLATE_PATH.read_text().count("/*__DATA__*/") == 1


def test_index_maps_mean_to_100_and_keeps_gaps():
    import pandas as pd

    out = dashboard._index(pd.Series([1.0, 3.0, None]))
    assert out == [50.0, 150.0, None]
