"""Shared matplotlib chrome for weather-skills figures.

Matplotlib places artists; callers set style and data only.
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_FONTSIZE = 16
DEFAULT_DPI = 150


def parse_figsize(value):
    """Argparse converter for ``W,H`` or ``WxH`` inches."""
    if value is None:
        return None
    raw = str(value).strip().lower().replace("×", "x")
    if not raw:
        raise argparse.ArgumentTypeError("--figsize must be W,H inches (e.g. 10,6 or 10x6)")
    sep = "x" if "x" in raw and "," not in raw else ","
    parts = [p.strip() for p in raw.split(sep)]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--figsize must be W,H inches (e.g. 10,6 or 10x6)")
    try:
        width, height = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--figsize must be W,H inches (e.g. 10,6 or 10x6)"
        ) from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("--figsize width and height must be positive")
    return (width, height)


def resolve_figsize(requested, default):
    """Honor an explicit ``--figsize``; otherwise use ``default``."""
    return tuple(requested) if requested is not None else tuple(default)


def apply_style(fontsize=DEFAULT_FONTSIZE):
    """Set rcParams from a single ``--fontsize``. Ticks use matplotlib's usual ratio."""
    import matplotlib as mpl

    fs = int(fontsize)
    tick = max(8, int(round(fs * 0.85)))
    legend = max(8, int(round(fs * 0.9)))
    mpl.rcParams.update(
        {
            "font.size": fs,
            "axes.titlesize": fs,
            "axes.labelsize": fs,
            "xtick.labelsize": tick,
            "ytick.labelsize": tick,
            "legend.fontsize": legend,
            "figure.titlesize": fs,
        }
    )


def add_shared_colorbar(fig, mappable, axes, label="", *, location=None, **kwargs):
    """Attach a colorbar in a matplotlib-reserved slot (not a figure-fraction box).

    One map gets a right-hand bar (class ticks fit vertically). Several maps
    share a bottom bar; dense CHIRPS-class ticks show every other bound.
    """
    import numpy as np

    if mappable is None:
        return None
    if hasattr(axes, "ravel"):
        axes = [ax for ax in np.ravel(axes) if getattr(ax, "get_visible", lambda: True)()]
    elif not isinstance(axes, (list, tuple)):
        axes = [axes]
    if not axes:
        return None
    if location is None:
        location = "right" if len(axes) == 1 else "bottom"
    ticks = kwargs.get("ticks")
    cbar = fig.colorbar(
        mappable,
        ax=axes,
        location=location,
        shrink=0.8,
        pad=0.08,
        **kwargs,
    )
    if label:
        cbar.set_label(label)
    n_ticks = len(list(ticks)) if ticks is not None else 0
    # Dense CHIRPS-class ticks: show every other bound instead of rotating
    # (rotation collides on narrow maps).
    if n_ticks >= 8 and location == "bottom":
        for i, tick in enumerate(cbar.ax.get_xticklabels()):
            if i % 2:
                tick.set_visible(False)
    return cbar


def save_figure(fig, path, *, pad_inches=None, tight=True):
    """Write a PNG. Default tight-crops chrome; ``tight=False`` keeps ``figsize``."""
    import matplotlib.pyplot as plt

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    kw = {"dpi": DEFAULT_DPI}
    if tight:
        kw["bbox_inches"] = "tight"
        if pad_inches is not None:
            kw["pad_inches"] = pad_inches
    fig.savefig(output, **kw)
    plt.close(fig)
    return output
