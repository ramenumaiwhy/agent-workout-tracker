from __future__ import annotations

import html
from pathlib import Path
from typing import Any


def render_progress(progress: dict[str, Any], output_path: str | Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    exercise = str(progress["exercise"])
    points = list(progress["points"])
    chart = _chart(points)
    page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(exercise)}の重量推移</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#0d223a;color:#c8d6e5;font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 56px}}h1{{color:#dee4ed;font-size:1.5rem}}p{{color:#acbcc7}}.card{{overflow-x:auto;background:#0f2744;border:1px solid #2e445c;border-radius:14px;padding:16px}}svg{{display:block;min-width:560px;width:100%;height:auto}}.axis{{stroke:#52677d;stroke-width:1}}.line{{fill:none;stroke:#3ea8ff;stroke-width:4;stroke-linecap:round;stroke-linejoin:round}}.dot{{fill:#3ea8ff;stroke:#0f2744;stroke-width:3}}.label{{fill:#c8d6e5;font-size:13px;text-anchor:middle}}.sub{{fill:#acbcc7;font-size:12px;text-anchor:middle}}
</style>
</head>
<body><main><h1>{html.escape(exercise)}の重量推移</h1><p>各トレーニングの最高重量です。点の上に回数を表示します。</p><div class="card">{chart}</div></main></body>
</html>"""
    output.write_text(page, encoding="utf-8")
    return output


def _chart(points: list[dict[str, Any]]) -> str:
    if not points:
        return '<p role="status">記録がありません。</p>'
    width = max(560, 100 + len(points) * 90)
    height = 300
    left, right, top, bottom = 58, 30, 48, 62
    weights = [float(point["weight_kg"]) for point in points]
    low, high = min(weights), max(weights)
    if low == high:
        low -= 1
        high += 1
    padding = max((high - low) * 0.15, 1)
    low -= padding
    high += padding
    inner_width = width - left - right
    inner_height = height - top - bottom
    coordinates = []
    for index, point in enumerate(points):
        x = left + inner_width * index / max(1, len(points) - 1)
        y = top + inner_height - (float(point["weight_kg"]) - low) / (high - low) * inner_height
        coordinates.append((x, y, point))
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in coordinates)
    marks = []
    for x, y, point in coordinates:
        weight_value = float(point["weight_kg"])
        weight = "自重" if weight_value == 0 else f"{weight_value:g}kg"
        marks.append(
            f'<text class="label" x="{x:.1f}" y="{y - 20:.1f}">{html.escape(weight)}・{int(point["reps"])}回</text>'
            f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="7"/>'
            f'<text class="sub" x="{x:.1f}" y="{height - 24}">{html.escape(str(point["date"]))}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="重量推移">'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}"/>'
        f'<line class="axis" x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}"/>'
        f'<polyline class="line" points="{polyline}"/>{"".join(marks)}</svg>'
    )
