"""
charting.py
-----------
Renders a lightweight inline SVG chart per symbol: closing price line,
horizontal dashed lines for support/resistance levels, and small markers
on the right edge showing top CE/PE OI strikes. No matplotlib dependency —
plain SVG string, served directly by Flask.
"""
from typing import List
from models import Candle, SRLevel, OIStrike

WIDTH, HEIGHT = 720, 300
PAD_L, PAD_R, PAD_T, PAD_B = 50, 90, 20, 30


def render_chart(symbol: str, candles: List[Candle], sr_levels: List[SRLevel],
                  ce_strikes: List[OIStrike], pe_strikes: List[OIStrike]) -> str:
    if not candles:
        return f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" xmlns="http://www.w3.org/2000/svg">' \
               f'<text x="20" y="30" fill="#6b7280" font-size="13">No data yet for {symbol}</text></svg>'

    closes = [c.close for c in candles]
    all_vals = closes + [l.price for l in sr_levels]
    lo, hi = min(all_vals), max(all_vals)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad

    def x_of(i):
        n = max(len(closes) - 1, 1)
        return PAD_L + (WIDTH - PAD_L - PAD_R) * (i / n)

    def y_of(price):
        return PAD_T + (HEIGHT - PAD_T - PAD_B) * (1 - (price - lo) / (hi - lo))

    # price line
    points = " ".join(f"{x_of(i):.1f},{y_of(c):.1f}" for i, c in enumerate(closes))

    svg_parts = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" xmlns="http://www.w3.org/2000/svg" '
        f'style="background:#0f1117;font-family:sans-serif">',
        f'<text x="{PAD_L}" y="16" fill="#e6e6e6" font-size="13" font-weight="600">{symbol}</text>',
    ]

    # SR level dashed lines
    for lvl in sr_levels[:6]:
        y = y_of(lvl.price)
        color = "#4fd1c5" if lvl.kind == "support" else "#ff6b6b"
        svg_parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{WIDTH-PAD_R}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>'
        )
        svg_parts.append(
            f'<text x="{WIDTH-PAD_R+4}" y="{y+3:.1f}" fill="{color}" font-size="10">{lvl.price:.0f}</text>'
        )

    # OI strike markers (right margin, small ticks colored by side)
    for s in ce_strikes[:3]:
        y = y_of(s.strike) if lo <= s.strike <= hi else None
        if y is not None:
            svg_parts.append(f'<circle cx="{WIDTH-PAD_R+2}" cy="{y:.1f}" r="2.5" fill="#ff6b6b"/>')

    for s in pe_strikes[:3]:
        y = y_of(s.strike) if lo <= s.strike <= hi else None
        if y is not None:
            svg_parts.append(f'<circle cx="{WIDTH-PAD_R+2}" cy="{y:.1f}" r="2.5" fill="#4fd1c5"/>')

    # price line + last price label
    svg_parts.append(f'<polyline points="{points}" fill="none" stroke="#4f7cff" stroke-width="1.8"/>')
    last_y = y_of(closes[-1])
    svg_parts.append(
        f'<circle cx="{x_of(len(closes)-1):.1f}" cy="{last_y:.1f}" r="3" fill="#4f7cff"/>'
    )
    svg_parts.append(
        f'<text x="{PAD_L}" y="{HEIGHT-8}" fill="#6b7280" font-size="10">'
        f'LTP {closes[-1]:.2f}</text>'
    )

    svg_parts.append('</svg>')
    return "".join(svg_parts)
