"""Tests for charts.line_chart's SVG rendering, focused on the direct
end-label placement -- the one part of it with real layout logic (the
polyline/grid/tick emission is straight arithmetic with nothing to get
wrong the same way)."""

import re

from charts import line_chart

LABEL_Y_RE = re.compile(r'<text x="[^"]+" y="([\d.]+)" class="chart-label">')


def _label_ys(svg):
    return [float(y) for y in LABEL_Y_RE.findall(svg)]


def test_line_chart_returns_empty_string_for_no_series():
    assert line_chart([]) == ""
    assert line_chart([{"label": "Empty", "color": "red", "points": []}]) == ""


def test_line_chart_single_series_renders_one_label():
    svg = line_chart([{"label": "Τιμή", "color": "var(--chart-blue)", "points": [("Δ1", 1.0), ("Δ2", 2.0)]}])
    assert svg.count('class="chart-label"') == 1


def test_line_chart_end_labels_never_overlap_even_when_two_series_tie():
    # Regression: a flat "average price" reference line landing on the
    # exact same value as the current-price line (a real, unremarkable
    # case -- e.g. a stable-priced item, or exactly two history points)
    # used to collapse both end labels onto the identical pixel row.
    # The anti-overlap nudge ran BEFORE the boundary clamp, so the clamp
    # could walk a label right back on top of one already placed near
    # that edge, invisibly to the nudge loop.
    series = [
        {
            "label": "Τιμή",
            "color": "var(--chart-blue)",
            "dash": "",
            "points": [("14/08", 3.99), ("15/08", 3.99)],
        },
        {
            "label": "Αρχική",
            "color": "var(--chart-orange)",
            "dash": "6 3",
            "points": [("14/08", 7.99), ("15/08", 7.99)],
        },
        {
            "label": "Μέση τιμή",
            "color": "var(--chart-green)",
            "dash": "2 3",
            "points": [("14/08", 3.99), ("15/08", 3.99)],
        },
    ]
    svg = line_chart(series, height=150, y_zero=False, value_suffix="€", area=True)
    ys = _label_ys(svg)
    assert len(ys) == 3
    for i, a in enumerate(ys):
        for b in ys[i + 1 :]:
            assert abs(a - b) >= 14, f"labels at y={a} and y={b} are close enough to visually overlap"


def test_line_chart_many_series_tied_at_the_low_end_all_get_distinct_labels():
    # A harder version of the same scenario -- four series bunched at
    # the bottom of a short chart, one at the top. If the anti-overlap
    # fix only handled the single-collision case, this would still fail.
    series = [
        {"label": f"S{i}", "color": "var(--chart-blue)", "points": [("Δ1", 1.0), ("Δ2", 1.0)]}
        for i in range(4)
    ]
    series.append({"label": "High", "color": "var(--chart-orange)", "points": [("Δ1", 9.0), ("Δ2", 9.0)]})
    svg = line_chart(series, height=150, y_zero=False)
    ys = sorted(_label_ys(svg))
    assert len(ys) == 5
    for a, b in zip(ys, ys[1:]):
        assert b - a >= 14
