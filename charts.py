"""Server-rendered SVG line charts -- no JS libraries, negligible memory.

Design follows the dataviz method: 2px lines, recessive grid, one axis,
text in ink tokens (never the series color -- a colored dot carries
identity), native <title> tooltips on >=8px hover targets, direct labels
at line ends. The three status-series colors (red/green/amber chart
steps) were validated together against the dark panel surface #171a21;
their CVD separation sits in the 6.5-8 band, which is why every series
also gets a distinct dash pattern and a direct label.
"""

from html import escape

PAD_LEFT = 10
PAD_RIGHT = 122  # room for direct labels at line ends
PAD_TOP = 14
PAD_BOTTOM = 22
VIEW_W = 480


def _scale(points_by_series, height, y_zero):
    values = [v for pts in points_by_series for _, v in pts if v is not None]
    if not values:
        return None
    lo = 0.0 if y_zero else min(values)
    hi = max(values)
    if hi == lo:
        hi = lo + 1
    if not y_zero:
        span = hi - lo
        lo -= span * 0.08
        hi += span * 0.08
    plot_w = VIEW_W - PAD_LEFT - PAD_RIGHT
    plot_h = height - PAD_TOP - PAD_BOTTOM
    n = max(len(pts) for pts in points_by_series)

    def x_at(i):
        if n == 1:
            return PAD_LEFT + plot_w / 2
        return PAD_LEFT + plot_w * i / (n - 1)

    def y_at(v):
        return PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    return x_at, y_at, lo, hi


def line_chart(series, height=170, y_zero=True, value_suffix="", area=False):
    """Render series -- [{"label", "color", "dash", "points": [(x_label, value)]}]
    -- as a responsive inline SVG. Returns "" when there is nothing to plot.
    """
    series = [s for s in series if s.get("points")]
    if not series:
        return ""
    scale = _scale([s["points"] for s in series], height, y_zero)
    if scale is None:
        return ""
    x_at, y_at, lo, hi = scale

    def fmt(v):
        if isinstance(v, float):
            text = f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        else:
            text = str(v)
        return f"{text}{value_suffix}"

    parts = [
        f'<svg viewBox="0 0 {VIEW_W} {height}" role="img" class="chart" '
        f'preserveAspectRatio="xMidYMid meet">'
    ]

    # Recessive grid: three horizontal lines + min/max labels in muted ink.
    for frac in (0.0, 0.5, 1.0):
        gy = PAD_TOP + (height - PAD_TOP - PAD_BOTTOM) * frac
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{gy:.1f}" x2="{VIEW_W - PAD_RIGHT}" '
            f'y2="{gy:.1f}" class="chart-grid"/>'
        )
    parts.append(
        f'<text x="{VIEW_W - PAD_RIGHT + 6}" y="{PAD_TOP + 4}" class="chart-tick">{fmt(hi)}</text>'
    )
    parts.append(
        f'<text x="{VIEW_W - PAD_RIGHT + 6}" y="{height - PAD_BOTTOM + 4}" '
        f'class="chart-tick">{fmt(lo)}</text>'
    )

    # First/last x labels.
    first_label = escape(str(series[0]["points"][0][0]))
    last_label = escape(str(series[0]["points"][-1][0]))
    parts.append(
        f'<text x="{PAD_LEFT}" y="{height - 6}" class="chart-tick">{first_label}</text>'
    )
    if len(series[0]["points"]) > 1:
        parts.append(
            f'<text x="{VIEW_W - PAD_RIGHT}" y="{height - 6}" text-anchor="end" '
            f'class="chart-tick">{last_label}</text>'
        )

    label_slots = []
    for s in series:
        color = s["color"]
        dash = s.get("dash", "")
        pts = [(i, v) for i, (_, v) in enumerate(s["points"]) if v is not None]
        if not pts:
            continue
        coords = [(x_at(i), y_at(v)) for i, v in pts]

        if area and len(coords) > 1:
            baseline = y_at(lo)
            path = (
                f'M{coords[0][0]:.1f},{baseline:.1f} '
                + " ".join(f"L{x:.1f},{y:.1f}" for x, y in coords)
                + f" L{coords[-1][0]:.1f},{baseline:.1f} Z"
            )
            parts.append(f'<path d="{path}" fill="{color}" opacity="0.12"/>')

        if len(coords) > 1:
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            # chart-line drives the CSS draw-in animation, which works by
            # animating stroke-dashoffset -- only solid lines get it, since
            # it would override a dashed series' own dash pattern.
            cls_attr = "" if dash else ' class="chart-line"'
            parts.append(
                f'<polyline points="{polyline}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"{dash_attr}{cls_attr}/>'
            )

        for (i, v), (x, y) in zip(pts, coords):
            x_label = escape(str(s["points"][i][0]))
            tooltip = f"{x_label} · {escape(s['label'])}: {fmt(v)}"
            parts.append(
                f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="transparent"/>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
                f"<title>{tooltip}</title></g>"
            )

        # Direct label at the line end: colored dot + text in ink tokens.
        end_x, end_y = coords[-1]
        label_slots.append((end_y, s["label"], fmt(pts[-1][1]), color, end_x))

    # Nudge overlapping end labels apart. Seed with the y-tick positions
    # so a label can't sit on top of the min/max tick text either.
    label_slots.sort(key=lambda t: t[0])
    placed = [PAD_TOP + 4, height - PAD_BOTTOM + 4]
    for end_y, label, value, color, end_x in label_slots:
        y = end_y
        while any(abs(y - p) < 14 for p in placed):
            y += 14
        y = min(max(y, PAD_TOP + 6), height - PAD_BOTTOM - 2)
        # The boundary clamp above can walk a label right back on top of
        # one already placed near that edge (common with a short chart
        # and several series landing at the same value, e.g. a flat
        # average line matching the current price) -- the nudge loop
        # ran against the unclamped y, so it never saw that collision.
        # Back off inward until clear, rather than silently overlapping.
        while any(abs(y - p) < 14 for p in placed) and y > PAD_TOP + 6:
            y -= 14
        y = max(y, PAD_TOP + 6)
        placed.append(y)
        # Single series: the chart title names it, so the end label only
        # needs the value. Multi-series: the label carries identity.
        text = value if len(series) == 1 else f"{escape(label)} · {value}"
        parts.append(
            f'<circle cx="{VIEW_W - PAD_RIGHT + 8}" cy="{y - 3:.1f}" r="3" fill="{color}"/>'
            f'<text x="{VIEW_W - PAD_RIGHT + 15}" y="{y:.1f}" class="chart-label">{text}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
