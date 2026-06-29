"""
chart.py — génère une image 2 panneaux (Weekly + H4) avec le CRT tracé, pour
joindre aux alertes Telegram. Thème sombre, sans dépendance à TradingView.
"""
import io
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")                       # rendu sans écran (CI / serveur)
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

BG = "#0e1117"
PANEL = "#0e1117"
GRID = "#222a37"
TXT = "#8a93a6"
UP = "#3b82f6"          # bougie haussière (bleu, façon TradingView)
DOWN = "#cbd5e1"        # bougie baissière (gris clair)
BULL = "#2ebd85"
BEAR = "#e8585e"
GOLD = "#e3b15c"
_JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def _candles(ax, bars, tz):
    for i, (ts, o, h, l, c) in enumerate(bars):
        col = UP if c >= o else DOWN
        ax.plot([i, i], [l, h], color=col, linewidth=0.9, zorder=2)
        lo, hi = (o, c) if c >= o else (c, o)
        ax.add_patch(Rectangle((i - 0.32, lo), 0.64, max(hi - lo, 1e-9),
                               facecolor=col, edgecolor=col, linewidth=0.5, zorder=3))
    ax.set_xlim(-1, len(bars))
    # quelques dates en abscisse
    step = max(1, len(bars) // 6)
    ticks = list(range(0, len(bars), step))
    ax.set_xticks(ticks)
    labels = []
    for i in ticks:
        d = datetime.fromtimestamp(bars[i][0] / 1000, tz=tz)
        labels.append(f"{d:%d/%m}")
    ax.set_xticklabels(labels, color=TXT, fontsize=8)


def _style(ax, title, dir_color):
    ax.set_facecolor(PANEL)
    ax.set_title(title, color=dir_color, fontsize=11, fontweight="bold", loc="left", pad=8)
    ax.grid(color=GRID, linewidth=0.5, alpha=0.6)
    ax.tick_params(colors=TXT, labelsize=8)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.yaxis.tick_right()


def _hline(ax, y, color, label, ls="-", va="bottom"):
    ax.axhline(y, color=color, linewidth=1.1, linestyle=ls, zorder=4, alpha=0.9)
    ax.text(0.008, y, label, transform=ax.get_yaxis_transform(),
            ha="left", va=va, color=color, fontsize=7.5, fontweight="bold", zorder=5)


def crt_chart(symbol, w_bars, h4_bars, weekly, target, h4_range, direction, tz) -> bytes:
    """weekly = CRT weekly (objet avec c1_low/c1_high/c2_high/c2_low) ; h4_range = (low, high)."""
    dir_txt = "SHORT" if direction == "bear" else "LONG"
    dcol = BEAR if direction == "bear" else BULL
    sym = symbol.split(":")[-1]

    fig, (axw, axh) = plt.subplots(2, 1, figsize=(8, 8.5), facecolor=BG,
                                   gridspec_kw={"hspace": 0.22})

    # --- Weekly ---
    _candles(axw, w_bars, tz)
    _style(axw, f"{sym} · Weekly · {dir_txt}", dcol)
    axw.axhspan(weekly.c1_low, weekly.c1_high, color=dcol, alpha=0.06, zorder=1)
    _hline(axw, weekly.c1_high, "#5b6677", "CRT range high", "--", va="bottom")
    _hline(axw, weekly.c1_low, "#5b6677", "CRT range low", "--", va="top")
    manip = weekly.c2_high if direction == "bear" else weekly.c2_low
    _hline(axw, manip, dcol, "manip (invalidation re-sweep)", va="top" if direction == "bull" else "bottom")
    _hline(axw, target, GOLD, "TP · cible weekly", va="bottom")

    # --- H4 ---
    _candles(axh, h4_bars, tz)
    _style(axh, f"{sym} · H4 · CRT dans le biais", dcol)
    hl, hh = h4_range
    axh.axhspan(hl, hh, color=dcol, alpha=0.06, zorder=1)
    _hline(axh, hh, "#5b6677", "CRT H4 high", "--", va="bottom")
    _hline(axh, hl, "#5b6677", "CRT H4 low", "--", va="top")

    stamp = datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M %Z")
    fig.text(0.012, 0.012, f"CRT Scanner · {stamp}", color=TXT, fontsize=7.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
