"""Smoke tests for `whygraph.render.template.render`."""

from __future__ import annotations

from whygraph.render import template as template_module


def _fake_payload() -> dict:
    return {
        "meta": {
            "generated_at": "2026-05-07T00:00:00+00:00",
            "repo_root": "/x",
            "runtime": "static",
            "node_count": 0,
            "edge_count": 0,
            "rationale_coverage": {"covered": 0, "total": 0},
        },
        "nodes": [],
        "edges": [],
        "node_details": {},
        "dashboard": {
            "repo_overview": {},
            "top_contributors_90d": [],
            "hot_paths_90d": [],
            "activity_overall": {},
        },
        "authors": [],
    }


def test_render_inlines_data_and_libs() -> None:
    html = template_module.render(_fake_payload())
    # Data tag.
    assert '<script id="whygraph-data" type="application/json">' in html
    # Cytoscape inlined (header preserved).
    assert "Cytoscape Consortium" in html
    # App.js inlined.
    assert "WhyGraph viewer" in html
    # CSS inlined.
    assert ":root" in html
    # No stray placeholders left over.
    for token in ("{{DATA}}", "{{STYLE_CSS}}", "{{CYTOSCAPE_JS}}", "{{APP_JS}}"):
        assert token not in html, f"unsubstituted placeholder: {token}"


def test_render_passes_runtime_through() -> None:
    payload = _fake_payload()
    payload["meta"]["runtime"] = "serve"
    html = template_module.render(payload)
    assert '"runtime": "serve"' in html


def test_render_includes_level_slider_buttons() -> None:
    html = template_module.render(_fake_payload())
    # All four level buttons present, with the data-level attribute the
    # JS hooks into.
    for level in (1, 2, 3, 4):
        assert f'data-level="{level}"' in html
    # Default selected = 1 (Modules-only — fastest first paint).
    assert 'class="level-btn active" data-level="1"' in html


def test_render_handles_unicode_safely() -> None:
    payload = _fake_payload()
    payload["nodes"] = [
        {
            "id": "x",
            "qualified_name": "pkg.снежко",
            "kind": "function",
            "name": "снежко",
            "file_path": "src/x.py",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "signature": None,
            "docstring": None,
            "degree": 0,
            "primary_author": None,
            "has_rationale": False,
        }
    ]
    html = template_module.render(payload)
    assert "снежко" in html
