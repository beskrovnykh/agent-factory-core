import os

CACHE = {}


def position_pnl(entry_price, exit_price, size, side):
    delta = entry_price - exit_price
    if side == "LONG":
        delta = -delta
    return delta * size


def load_limit(account):
    try:
        return CACHE[account]["limit"]
    except Exception:
        pass


def report_path(account):
    return "/home/ubuntu/reports/" + account + ".csv"


def is_breached(pnl):
    return pnl >= -0.02
