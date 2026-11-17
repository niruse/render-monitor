"""English is the only language shipped, and it covers every error/abort the flows use."""
from __future__ import annotations

import json
import re
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "custom_components" / "render_monitor"


def load(lang):
    return json.loads((PACKAGE / "translations" / f"{lang}.json").read_text(encoding="utf-8"))


def flatten(doc, prefix=""):
    out = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def test_only_english_is_shipped():
    assert sorted(p.name for p in (PACKAGE / "translations").iterdir()) == ["en.json"]


def test_english_texts_are_plain_and_complete():
    for key, text in flatten(load("en")).items():
        assert text.strip() and text.isascii(), key
        parent = key.rsplit(".", 2)[-2]
        if key.endswith("description") or parent in ("error", "abort") or parent.endswith("_description"):
            assert text.rstrip()[-1] in ".}", key  # full sentences (a description may end on a link)


def test_every_error_and_abort_in_the_flow_is_translated():
    source = (PACKAGE / "config_flow.py").read_text(encoding="utf-8")
    used_errors = set(re.findall(r'errors\["base"\] = "(\w+)"', source))
    used_aborts = set(re.findall(r'async_abort\(reason="(\w+)"\)', source))
    mapped = set(re.findall(r'return "(\w+)"', source))  # _error_key results: errors and aborts
    en = load("en")
    assert used_errors | mapped <= set(en["config"]["error"])
    assert used_aborts | mapped | {"already_configured", "reauth_successful"} <= set(en["config"]["abort"])
    assert mapped <= set(en["options"]["abort"])
    assert {"no_services_selected"} <= set(en["options"]["error"])
