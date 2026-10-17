"""Chart pictures for family stats. Files are named by their content, so an unchanged chart is reused."""

import hashlib
import shutil
import tempfile
from pathlib import Path

import matplotlib
from matplotlib.figure import Figure

matplotlib.use("Agg")

CHART_DIR = Path(tempfile.gettempdir()) / "family-charts"
BACKGROUND, TEXT, GRID = "#17212b", "#e9eef2", "#2b3a48"
COLORS = ["#4fa3e0", "#5fd3a0", "#f2b84b", "#ee6f6f", "#b48ef0", "#4dd0e1", "#f48fb1", "#aed581"]


def reset_cache() -> None:
    if CHART_DIR.exists():
        shutil.rmtree(CHART_DIR)
    CHART_DIR.mkdir()


def _figure(title: str, height: float) -> tuple[Figure, object]:
    figure = Figure(figsize=(10, height), dpi=110, facecolor=BACKGROUND)
    axes = figure.add_subplot()
    axes.set_facecolor(BACKGROUND)
    axes.set_title(title, color=TEXT, fontsize=16, loc="left", pad=14, fontweight="bold")
    axes.tick_params(colors=TEXT, labelsize=11)
    for side in ("top", "right", "left", "bottom"):
        axes.spines[side].set_visible(False)
    return figure, axes


def _save(figure: Figure, key: str, directory: Path) -> str:
    path = directory / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.png"
    if not path.exists():
        figure.savefig(path, facecolor=BACKGROUND, bbox_inches="tight")
    return str(path)


def bars(title: str, items: list[tuple[str, float]], unit: str = "", *, directory: Path) -> str:
    """Horizontal bars, biggest on top."""
    items = items or [("—", 0)]
    figure, axes = _figure(title, max(3.0, 0.6 * len(items) + 1.5))
    labels = [label if len(label) <= 32 else label[:31] + "…" for label, _ in items][::-1]
    values = [value for _, value in items][::-1]
    shown = axes.barh(range(len(values)), values, color=[COLORS[i % len(COLORS)] for i in range(len(values))][::-1])
    axes.set_yticks(range(len(labels)), labels)
    axes.bar_label(
        shown,
        labels=[f"{v:,.0f}{unit}".replace(",", " ") if v >= 10 else f"{v:g}{unit}" for v in values],
        padding=6,
        color=TEXT,
        fontsize=11,
    )
    axes.xaxis.set_visible(False)
    axes.set_xlim(0, max(values or [1]) * 1.2 or 1)
    return _save(figure, f"bars|{title}|{items}|{unit}", directory)


def columns(title: str, items: list[tuple[str, float]], *, directory: Path) -> str:
    """Vertical columns over time, labels like 2025-03."""
    figure, axes = _figure(title, 5)
    labels = [label for label, _ in items]
    values = [value for _, value in items]
    axes.bar(range(len(values)), values, color=COLORS[0])
    axes.set_xticks(
        range(len(labels)), [label if i % 3 == 0 else "" for i, label in enumerate(labels)], rotation=45, ha="right"
    )
    axes.grid(axis="y", color=GRID)
    axes.set_axisbelow(True)
    return _save(figure, f"columns|{title}|{items}", directory)
