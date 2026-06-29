"""
chart.py — image 2 panneaux (Weekly + H4) au style TradingView (thème gris)
avec le CRT tracé. Pour joindre aux alertes Telegram.
"""
import io
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Palette (couleurs exactes fournies par Djo)
BG = "#B8B8B8"          # fond
UP = "#0E2C80"          # bougie haussière
DOWN = "#DBDBDB"        # bougie baissière
BORDER = "#000000"      # bordures + mèches des bougies
LINE = "#000000"        # lignes CRT : noir
TAG_BG = "#000000"      # étiquette de prix : fond noir
TAG_TX = "#ffffff"      # étiquette de prix : texte blanc
AXTX = "#1c1c1c"        # texte des axes
WM = "#aeaeae"          # filigrane
GOLD = "#6b4e0a"        # petit repère TP (discret, lisible sur gris)
_JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def _candles(ax, bars):
    for i, (ts, o, h, l, c) in enumerate(bars):
        col = UP if c >= o else DOWN
        ax.plot([i, i], [l, h], color=BORDER, linewidth=0.9, zorder=2, solid_capstyle="butt")
        lo, hi = (o, c) if c >= o else (c, o)
        ax.add_patch(Rectangle((i - 0.3, lo), 0.6, max(hi - lo, 1e-9),
                               facecolor=col, edgecolor=BORDER, linewidth=0.7, zorder=3))
    ax.set_xlim(-1, len(bars) + 1)


def _axis(ax, bars, tz, title):
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(False)
    ax.yaxis.tick_right()
    ax.tick_params(colors=AXTX, labelsize=8, length=0)
    # dates en abscisse
    step = max(1, len(bars) // 7)
    ticks = list(range(0, len(bars), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([datetime.fromtimestamp(bars[i][0] / 1000, tz=tz).strftime("%d/%m")
                        for i in ticks], color=AXTX, fontsize=8)
    # filigrane central + titre haut-gauche (façon TradingView)
    ax.text(0.5, 0.5, title, transform=ax.transAxes, ha="center", va="center",
            color=WM, fontsize=26, fontweight="bold", alpha=0.45, zorder=0)
    ax.text(0.008, 0.97, title, transform=ax.transAxes, ha="left", va="top",
            color=AXTX, fontsize=9, fontweight="bold", zorder=6)


def _crt_line(ax, y, n_bars, label=None, accent=False):
    ax.axhline(y, color=LINE, linewidth=1.0, zorder=4)
    # étiquette de prix (boîte noire) sur le bord droit, façon TradingView
    ax.annotate(f"{y:.5g}".replace(".", ","), xy=(1.0, y), xycoords=("axes fraction", "data"),
                xytext=(2, 0), textcoords="offset points", ha="left", va="center",
                color=TAG_TX, fontsize=7.5, fontweight="bold", zorder=7, clip_on=False,
                bbox=dict(boxstyle="square,pad=0.25", fc=TAG_BG, ec="none"))
    if label:
        ax.text(0.008, y, label, transform=ax.get_yaxis_transform(), ha="left",
                va="bottom", color=(GOLD if accent else AXTX), fontsize=7.5,
                fontweight="bold", zorder=6)


def crt_chart(symbol, w_bars, h4_bars, weekly, target, h4_range, direction, tz) -> bytes:
    sym = symbol.split(":")[-1]
    dir_txt = "SHORT" if direction == "bear" else "LONG"

    fig, (axw, axh) = plt.subplots(2, 1, figsize=(8.2, 8.6), facecolor=BG,
                                   gridspec_kw={"hspace": 0.16})

    # Weekly : les 2 bords du range CRT (dont la cible weekly), + le niveau de manip
    _candles(axw, w_bars)
    _axis(axw, w_bars, tz, f"{sym} · 1W · {dir_txt}")
    _crt_line(axw, weekly.c1_high, len(w_bars),
              "TP cible" if direction == "bull" else None, accent=(direction == "bull"))
    _crt_line(axw, weekly.c1_low, len(w_bars),
              "TP cible" if direction == "bear" else None, accent=(direction == "bear"))
    manip = weekly.c2_high if direction == "bear" else weekly.c2_low
    _crt_line(axw, manip, len(w_bars), "manip (invalid. re-sweep)")

    # H4 : le range du CRT H4
    _candles(axh, h4_bars)
    _axis(axh, h4_bars, tz, f"{sym} · 4H")
    hl, hh = h4_range
    _crt_line(axh, hh, len(h4_bars), "CRT H4")
    _crt_line(axh, hl, len(h4_bars))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
