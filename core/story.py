#!/usr/bin/env python3
"""Карточки для сторис: как идёт работа над ракетой. 1080×1920.

Серию держит не крупная надпись, а повторяющаяся мелочь: одно и то же место
счётчика дня, один кегль подписи, одна палитра. Поэтому типографика тихая, а
громким остаётся ровно одно число на кадре.

Палитра ночная не для настроения: работа идёт ночью, и это единственное, что
про неё видно снаружи без объяснений. Холодный — машина сделала, тёплый —
человеку разбирать. Это не украшение, а тот же смысл, что на доске.

    python3 core/story.py --day 2026-09-15 --out journal/stories
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
PAD = 96

GROUND = (13, 17, 23)        # не чистый чёрный: он читается как дешёвый
BONE = (232, 234, 237)       # тёплая кость вместо белого
DIM = (124, 133, 145)
COLD = (109, 178, 214)       # машина сделала
WARM = (216, 150, 92)        # человеку разбирать
RULE = (32, 38, 46)

SANS = "/System/Library/Fonts/Avenir Next.ttc"
MONO = "/System/Library/Fonts/Menlo.ttc"


def font(path: str, size: int, index: int = 0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except OSError:
        return ImageFont.load_default()


def tracked(draw, xy, text, fnt, fill, tracking: float = 0.0):
    """Текст с разрядкой: PIL её не умеет, рисуем посимвольно."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking


def wrap(draw, text: str, fnt, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        probe = f"{cur} {w}".strip()
        if draw.textlength(probe, font=fnt) <= width:
            cur = probe
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def base(day: str, n: int, total: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Общий каркас: счётчик дня внизу слева, дата справа, тонкая линейка.

    Одинаков на всех кадрах — по нему серия и узнаётся."""
    img = Image.new("RGB", (W, H), GROUND)
    d = ImageDraw.Draw(img)
    f_small = font(SANS, 26, index=0)
    f_mono = font(MONO, 24)

    d.line([(PAD, H - 150), (W - PAD, H - 150)], fill=RULE, width=2)
    tracked(d, (PAD, H - 118), "РАКЕТА", font(SANS, 26, index=1), DIM, 4.0)
    right = day
    d.text((W - PAD - d.textlength(right, font=f_mono), H - 116), right,
           font=f_mono, fill=DIM)
    if total > 1:
        mid = f"{n}/{total}"
        d.text(((W - d.textlength(mid, font=f_small)) / 2, H - 118), mid,
               font=f_small, fill=RULE if False else DIM)
    return img, d


def card_cover(day: str, dayno: int, usd: float, merges: int, n, total):
    img, d = base(day, n, total)
    tracked(d, (PAD, 300), f"ДЕНЬ {dayno}", font(SANS, 44, index=1), DIM, 6.0)

    big = font(SANS, 300, index=2)
    d.text((PAD - 14, 400), str(merges), font=big, fill=BONE)
    d.text((PAD, 760), "правок приехало", font=font(SANS, 54, index=0), fill=BONE)
    d.text((PAD, 830), "в основную ветку", font=font(SANS, 54, index=0), fill=BONE)

    d.line([(PAD, 980), (PAD + 120, 980)], fill=COLD, width=3)
    d.text((PAD, 1030), "ночью, пока никто", font=font(SANS, 40, index=0), fill=DIM)
    d.text((PAD, 1086), "не смотрел", font=font(SANS, 40, index=0), fill=DIM)

    f_mono = font(MONO, 30)
    d.text((PAD, 1320), f"${usd:,.2f}".replace(",", " "), font=font(MONO, 44), fill=WARM)
    d.text((PAD, 1382), "столько стоило", font=font(SANS, 30, index=0), fill=DIM)
    return img


def card_lines(day: str, dayno: int, title: str, rows: list[tuple[str, str]],
               n, total, accent=COLD):
    """Кадр-список: слева время или метка моноширинно, справа фраза."""
    img, d = base(day, n, total)
    tracked(d, (PAD, 300), f"ДЕНЬ {dayno}", font(SANS, 44, index=1), DIM, 6.0)
    f_t = font(SANS, 62, index=2)
    y = 400
    for line in wrap(d, title, f_t, W - 2 * PAD):
        d.text((PAD, y), line, font=f_t, fill=BONE)
        y += 76
    d.line([(PAD, y + 40), (PAD + 120, y + 40)], fill=accent, width=3)

    y += 130
    f_k = font(MONO, 30)
    f_v = font(SANS, 38, index=0)
    for k, v in rows:
        d.text((PAD, y + 4), k, font=f_k, fill=accent)
        vx = PAD + 190
        for line in wrap(d, v, f_v, W - PAD - vx):
            d.text((vx, y), line, font=f_v, fill=BONE)
            y += 50
        y += 34
    return img


def card_quote(day: str, dayno: int, text: str, sub: str, n, total):
    img, d = base(day, n, total)
    tracked(d, (PAD, 300), f"ДЕНЬ {dayno}", font(SANS, 44, index=1), DIM, 6.0)
    f = font(SANS, 68, index=2)
    y = 560
    for line in wrap(d, text, f, W - 2 * PAD):
        d.text((PAD, y), line, font=f, fill=BONE)
        y += 86
    d.line([(PAD, y + 50), (PAD + 120, y + 50)], fill=WARM, width=3)
    f_s = font(SANS, 36, index=0)
    y += 120
    for line in wrap(d, sub, f_s, W - 2 * PAD):
        d.text((PAD, y), line, font=f_s, fill=DIM)
        y += 50
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--dayno", type=int, default=1)
    ap.add_argument("--usd", type=float, default=0.0)
    ap.add_argument("--merges", type=int, default=0)
    ap.add_argument("--out", default="journal/stories")
    a = ap.parse_args()

    out = Path(a.out) / a.day
    out.mkdir(parents=True, exist_ok=True)

    cards = [
        card_cover(a.day, a.dayno, a.usd, a.merges, 1, 4),
        card_lines(a.day, a.dayno, "Три поломки, которые считали поведением модели", [
            ("logdiff", "датчик логов сообщал находку кодом возврата — его съедала труба. 34 вызова за три месяца, ни один не сработал"),
            ("handoff", "страховка ждала не тот статус и утаскивала к человеку правильно сданные задачи"),
            ("comments", "снимок задачи читал пять самых старых комментариев вместо свежих"),
        ], 2, 4),
        card_quote(a.day, a.dayno,
                   "Восемь раз за день в постановке было неверное число",
                   "И каждый раз это находил тот, кто делал работу, а не тот, кто её ставил", 3, 4),
        card_lines(a.day, a.dayno, "Осталось", [
            ("выкат", "до него ни одна правка не работает"),
            ("113%", "одна роль памяти читает в обрезке"),
            ("31/56", "повторных проверок. сдвинется или нет — видно за неделю"),
        ], 4, 4, accent=WARM),
    ]
    for i, img in enumerate(cards, 1):
        p = out / f"{i:02d}.png"
        img.save(p, "PNG")
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
