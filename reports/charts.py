"""
Small SVG charts for the web pages, drawn on the server.

WHY NOT A CHART LIBRARY
-----------------------
The rest of the web app draws its one chart in CSS, loads nothing but
Bootstrap, and has to work on a slow connection in the yard. A candlestick
chart and a ring are a few dozen shapes each; drawing them here costs no
download, prints exactly as it shows, and can be tested like any other
Python. The phone draws the same charts itself from the API's numbers.

THE LOOK
--------
Thin marks, hairline grid, the data the only loud thing:

  * candles: the wick is the range, the body runs open -> close. A candle
    that ROSE is hollow, one that FELL is filled - the shape carries the
    direction, so it reads without colour. Colour says whether that was good
    (the cash chart: rising is good; the cost chart: rising is bad), and the
    legend says it in words;
  * rings: at most three parts, a 2px gap between them, the total in the
    middle, and every part named with its amount beside the ring.

Every mark carries a <title>, which the browser shows on hover, and every
chart has its numbers in a table on the same page - a chart that can only be
read by hovering is a chart some people cannot read.
"""
import math
from decimal import Decimal

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from core.utils import money

# Chart chrome and ink (light surface).
SURFACE = "#ffffff"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
MUTED = "#898781"
INK = "#0b0b0b"
INK_2 = "#52514e"

#: Direction colours - status colours, always paired with shape and words.
GOOD = "#0ca30c"
BAD = "#d03b3b"

#: The three categorical slots, in their validated order.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")

#: Reference lines on the cost chart.
LINE_YOURS = INK
LINE_PRICE = SERIES[0]
LINE_SUGGESTED = MUTED


def _num(value) -> float:
    return float(value) if value is not None else 0.0


def _f(value, places=1) -> str:
    """A coordinate as text. format_html escapes its arguments to strings
    before formatting, so a float format spec cannot be used inside it."""
    return f"{float(value):.{places}f}"


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round axis values that cover [low, high] - 0 / 500 / 1,000, never 437."""
    if high <= low:
        high = low + 1
    raw = (high - low) / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for step in (1, 2, 2.5, 5, 10):
        if step * magnitude >= raw:
            step *= magnitude
            break
    start = math.floor(low / step) * step
    ticks, value = [], start
    while value <= high + step * 0.5:
        ticks.append(round(value, 6))
        value += step
        if len(ticks) > 12:
            break
    return ticks


def compact(value) -> str:
    """1,284 / 12.9K / 4.2M - for axis labels, where space is short."""
    v = _num(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 10_000:
        return f"{sign}{v / 1_000:.0f}K"
    if v >= 1_000:
        return f"{sign}{v / 1_000:.1f}K".replace(".0K", "K")
    if v == int(v):
        return f"{sign}{int(v)}"
    return f"{sign}{v:.2f}"


def _label_for(candle, bucket):
    start = candle["start"]
    if bucket == "month":
        return start.strftime("%b %y")
    return start.strftime("%d %b")


def candle_svg(candles, *, bucket="day", rising_good=True, lines=None,
               zero_line=False, title="", width=720, height=260,
               max_labels=8, skip_idle=False):
    """
    A candlestick chart as an <svg> string.

    `candles` are dicts with start/open/high/low/close (open None = no
    candle in that bucket). `lines` are (label, value, colour) reference
    lines drawn across the plot, e.g. the owner's cost and the price.

    `skip_idle` leaves out a bucket in which no money moved at all, rather
    than drawing a flat dash for it: a month with three busy days is three
    candles, not thirty marks along the axis. The running total is unchanged,
    so the next candle still opens where the last one closed.

    Drawn twice by the page - wide, and narrow for a phone - because an SVG
    scales its text with it: one 720 units wide shrunk onto a 340px screen
    would print its axis in 5px type.
    """
    lines = [(lbl, _num(v), colour) for lbl, v, colour in (lines or []) if v is not None]
    drawn = [c for c in candles if c.get("open") is not None]
    if not drawn:
        return ""

    left = 58 if width >= 500 else 46
    right, top, bottom = 14, 12, 30
    plot_w, plot_h = width - left - right, height - top - bottom
    lows = [_num(c["low"]) for c in drawn] + [v for _, v, _ in lines]
    highs = [_num(c["high"]) for c in drawn] + [v for _, v, _ in lines]
    if zero_line:
        lows.append(0.0)
        highs.append(0.0)
    lo, hi = min(lows), max(highs)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    ticks = nice_ticks(lo, hi)
    lo, hi = min(ticks[0], lo), max(ticks[-1], hi)

    def y(v):
        return top + plot_h - (_num(v) - lo) / (hi - lo) * plot_h

    slot = plot_w / max(len(candles), 1)
    body = max(3.0, min(14.0, slot * 0.6))
    up_colour, down_colour = (GOOD, BAD) if rising_good else (BAD, GOOD)

    parts = [
        format_html(
            '<svg class="audit-chart" viewBox="0 0 {} {}" role="img" aria-label="{}" '
            'preserveAspectRatio="xMidYMid meet">',
            width, height, title,
        )
    ]
    # Grid and axis labels.
    for t in ticks:
        ty = y(t)
        parts.append(format_html(
            '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1"/>',
            left, width - right, _f(ty), _f(ty), GRID,
        ))
        parts.append(format_html(
            '<text x="{}" y="{}" text-anchor="end" class="audit-axis">{}</text>',
            left - 8, _f(ty + 4), compact(t),
        ))
    if zero_line and lo < 0 < hi:
        zy = y(0)
        parts.append(format_html(
            '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1"/>',
            left, width - right, _f(zy), _f(zy), BASELINE,
        ))

    # Reference lines.
    for label, value, colour in lines:
        ly = y(value)
        parts.append(format_html(
            '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1.5">'
            '<title>{}: {}</title></line>',
            left, width - right, _f(ly), _f(ly), colour, label, money(value),
        ))

    # Candles.
    label_every = max(1, math.ceil(len(candles) / max_labels))
    for i, c in enumerate(candles):
        cx = left + slot * (i + 0.5)
        if i % label_every == 0:
            parts.append(format_html(
                '<text x="{}" y="{}" text-anchor="middle" class="audit-axis">{}</text>',
                _f(cx), height - 10, _label_for(c, bucket),
            ))
        if c.get("open") is None:
            continue
        if skip_idle and not c.get("money_in") and not c.get("money_out"):
            continue
        o, h, l_, cl = (_num(c[k]) for k in ("open", "high", "low", "close"))
        rose = cl > o
        colour = up_colour if rose else (down_colour if cl < o else INK_2)
        tip = _tooltip(c, bucket)
        parts.append('<g class="audit-candle" tabindex="0">')
        parts.append(format_html('<title>{}</title>', tip))
        # A hit area wider than the mark, so the tooltip is easy to find.
        parts.append(format_html(
            '<rect x="{}" y="{}" width="{}" height="{}" fill="transparent"/>',
            _f(cx - slot / 2), top, _f(slot), plot_h,
        ))
        parts.append(format_html(
            '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1.2"/>',
            _f(cx), _f(cx), _f(y(h)), _f(y(l_)), colour,
        ))
        top_y, bottom_y = min(y(o), y(cl)), max(y(o), y(cl))
        bar_h = bottom_y - top_y
        if bar_h < 2.5:
            # Opened and closed at (almost) the same figure: a short level
            # bar, tall enough to see, centred on the price.
            top_y, bar_h = (top_y + bottom_y) / 2 - 1.25, 2.5
        if rose:
            parts.append(format_html(
                '<rect x="{}" y="{}" width="{}" height="{}" rx="1.5" '
                'fill="{}" stroke="{}" stroke-width="1.5"/>',
                _f(cx - body / 2), _f(top_y), _f(body), _f(bar_h), SURFACE, colour,
            ))
        else:
            parts.append(format_html(
                '<rect x="{}" y="{}" width="{}" height="{}" rx="1.5" fill="{}"/>',
                _f(cx - body / 2), _f(top_y), _f(body), _f(bar_h), colour,
            ))
        parts.append("</g>")
    parts.append("</svg>")
    return mark_safe("".join(str(p) for p in parts))


def _tooltip(c, bucket):
    when = c["start"].strftime("%b %Y") if bucket == "month" else c["start"].strftime("%d %b %Y")
    if bucket == "week":
        when = f"Week of {when}"
    text = (
        f"{when}: open {money(c['open'])}, high {money(c['high'])}, "
        f"low {money(c['low'])}, close {money(c['close'])}"
    )
    if "money_in" in c:
        text += f" (in {money(c['money_in'])}, out {money(c['money_out'])})"
    if c.get("count"):
        text += f" - {c['count']} batch{'es' if c['count'] != 1 else ''}"
    return text


def bars_svg(rows, *, labels=(), colours=SERIES, signed=False, bucket="day",
             title="", width=720, height=240, max_labels=8):
    """
    Bars per bucket, as an <svg> string - the Audit detail pages' chart.

    `rows` are the series rows of reports/audit_detail.py: start/end and
    `values`, one per key, in `labels`' order (which is also the colour
    order). Several keys stack, money out above the line and any money back
    (a return to a supplier) below it. `signed` draws one key as gain/loss
    bars instead: above the line in the good colour, below it in the bad
    one - status colours, and the legend says which is which.

    Every bucket carries a <title> with all its figures, on a hit area as
    tall as the plot, so the tooltip is easy to find; the page has the same
    figures in a table.
    """
    if not rows or not any(_num(v) for r in rows for v in r["values"]):
        return ""
    left = 58 if width >= 500 else 46
    right, top, bottom = 14, 12, 30
    plot_w, plot_h = width - left - right, height - top - bottom

    highs, lows = [0.0], [0.0]
    for r in rows:
        values = [_num(v) for v in r["values"]]
        highs.append(sum(v for v in values if v > 0))
        lows.append(sum(v for v in values if v < 0))
    lo, hi = min(lows), max(highs)
    if hi == lo:
        hi = lo + 1
    ticks = nice_ticks(lo, hi)
    lo, hi = min(ticks[0], lo), max(ticks[-1], hi)

    def y(v):
        return top + plot_h - (_num(v) - lo) / (hi - lo) * plot_h

    slot = plot_w / len(rows)
    bar = max(2.0, min(28.0, slot * 0.62))
    out = [format_html(
        '<svg class="audit-chart" viewBox="0 0 {} {}" role="img" aria-label="{}" '
        'preserveAspectRatio="xMidYMid meet">',
        width, height, title,
    )]
    for t in ticks:
        ty = y(t)
        out.append(format_html(
            '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1"/>',
            left, width - right, _f(ty), _f(ty), GRID,
        ))
        out.append(format_html(
            '<text x="{}" y="{}" text-anchor="end" class="audit-axis">{}</text>',
            left - 8, _f(ty + 4), compact(t),
        ))
    zero = y(0)
    out.append(format_html(
        '<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="{}" stroke-width="1"/>',
        left, width - right, _f(zero), _f(zero), BASELINE,
    ))

    every = max(1, math.ceil(len(rows) / max_labels))
    for i, r in enumerate(rows):
        cx = left + slot * (i + 0.5)
        if i % every == 0:
            out.append(format_html(
                '<text x="{}" y="{}" text-anchor="middle" class="audit-axis">{}</text>',
                _f(cx), height - 10, _label_for(r, bucket),
            ))
        values = [_num(v) for v in r["values"]]
        if not any(values):
            continue
        when = r["start"].strftime("%b %Y") if bucket == "month" else r["start"].strftime("%d %b %Y")
        if bucket == "week":
            when = f"Week of {when}"
        parts_text = ", ".join(
            f"{labels[k] if k < len(labels) else k}: {money(Decimal(str(v)))}"
            for k, v in enumerate(values) if v
        )
        out.append('<g class="audit-bargroup" tabindex="0">')
        out.append(format_html("<title>{}: {}</title>", when, parts_text))
        out.append(format_html(
            '<rect x="{}" y="{}" width="{}" height="{}" fill="transparent"/>',
            _f(cx - slot / 2), top, _f(slot), plot_h,
        ))
        up = down = 0.0
        for k, v in enumerate(values):
            if not v:
                continue
            if signed:
                colour = GOOD if v >= 0 else BAD
            else:
                colour = colours[k % len(colours)]
            if v > 0:
                y0, y1 = y(up + v), y(up)
                up += v
            else:
                y0, y1 = y(down), y(down + v)
                down += v
            h = max(y1 - y0, 1.5)
            out.append(format_html(
                '<rect x="{}" y="{}" width="{}" height="{}" rx="2" fill="{}" '
                'stroke="{}" stroke-width="1"/>',
                _f(cx - bar / 2), _f(y0), _f(bar), _f(h), colour, SURFACE,
            ))
        out.append("</g>")
    out.append("</svg>")
    return mark_safe("".join(str(p) for p in out))


def donut_svg(parts, *, centre_value="", centre_label="", title="", size=200):
    """
    A ring of up to three parts. `parts` are (label, amount) in drawing order;
    colours follow the order (SERIES). Parts of zero are left out of the
    ring but keep their colour, so a colour always means the same thing.
    """
    total = sum((_num(a) for _, a in parts), 0.0)
    radius, stroke = 74, 24
    cx = cy = size / 2
    out = [format_html(
        '<svg class="audit-donut" viewBox="0 0 {} {}" role="img" aria-label="{}">',
        size, size, title,
    )]
    if total <= 0:
        out.append(format_html(
            '<circle cx="{}" cy="{}" r="{}" fill="none" stroke="{}" stroke-width="{}"/>',
            cx, cy, radius, GRID, stroke,
        ))
    else:
        gap = 2 / radius  # 2px of surface between neighbours, in radians
        angle = -math.pi / 2
        live = [(i, lbl, _num(a)) for i, (lbl, a) in enumerate(parts) if _num(a) > 0]
        for i, label, amount in live:
            sweep = amount / total * 2 * math.pi
            a0 = angle + (gap / 2 if len(live) > 1 else 0)
            a1 = angle + sweep - (gap / 2 if len(live) > 1 else 0)
            angle += sweep
            colour = SERIES[i % len(SERIES)]
            share = amount / total * 100
            tip = f"{label}: {money(amount)} ({share:.0f}%)"
            if len(live) == 1:
                out.append(format_html(
                    '<circle cx="{}" cy="{}" r="{}" fill="none" stroke="{}" stroke-width="{}">'
                    '<title>{}</title></circle>',
                    cx, cy, radius, colour, stroke, tip,
                ))
                continue
            x0, y0 = cx + radius * math.cos(a0), cy + radius * math.sin(a0)
            x1, y1 = cx + radius * math.cos(a1), cy + radius * math.sin(a1)
            large = 1 if (a1 - a0) > math.pi else 0
            out.append(format_html(
                '<path d="M {} {} A {} {} 0 {} 1 {} {}" fill="none" '
                'stroke="{}" stroke-width="{}"><title>{}</title></path>',
                _f(x0, 2), _f(y0, 2), radius, radius, large, _f(x1, 2), _f(y1, 2),
                colour, stroke, tip,
            ))
    out.append(format_html(
        '<text x="{}" y="{}" text-anchor="middle" class="audit-donut-value">{}</text>',
        cx, cy + 2, centre_value,
    ))
    out.append(format_html(
        '<text x="{}" y="{}" text-anchor="middle" class="audit-donut-label">{}</text>',
        cx, cy + 22, centre_label,
    ))
    out.append("</svg>")
    return mark_safe("".join(str(p) for p in out))


def legend(parts):
    """(label, amount, share %, colour) rows for the list beside a ring."""
    total = sum((_num(a) for _, a in parts), 0.0)
    return [
        {
            "label": label,
            "amount": money(Decimal(str(amount or 0))),
            "share": round(_num(amount) / total * 100) if total else 0,
            "colour": SERIES[i % len(SERIES)],
        }
        for i, (label, amount) in enumerate(parts)
    ]


__all__ = ["bars_svg", "candle_svg", "donut_svg", "legend", "nice_ticks", "compact", "escape"]
