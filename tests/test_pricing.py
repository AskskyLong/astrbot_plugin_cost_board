"""纯函数级自测：stub 掉 astrbot，不需要装插件、不需要跑 AstrBot。

重点覆盖两个我踩过的坑：
1. 错峰窗口边界（曾经把 23:30-24:00 和 00:00-00:30 也打折了）
2. 本地模型被误报「未配价」
"""
import datetime as dt
import importlib.util
import os
import pathlib
import sys
import types

# ---------------- stub astrbot ----------------
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = types.SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
ev = types.ModuleType("astrbot.api.event")


class AstrMessageEvent:
    pass


class _Filter:
    def command(self, *a, **k):
        return lambda f: f

    def on_llm_request(self, *a, **k):
        return lambda f: f


ev.filter = _Filter()
ev.AstrMessageEvent = AstrMessageEvent
star = types.ModuleType("astrbot.api.star")


class Star:
    def __init__(self, context=None):
        pass


class Context:
    pass


def register(*a, **k):
    return lambda c: c


star.Star, star.Context, star.register = Star, Context, register
for name, mod in [("astrbot", astrbot), ("astrbot.api", api),
                  ("astrbot.api.event", ev), ("astrbot.api.star", star)]:
    sys.modules[name] = mod
astrbot.api = api
api.event = ev
api.star = star

HERE = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cb", HERE / "main.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

FAILS = []
N = [0]


def check(name, got, want):
    N[0] += 1
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


def bj(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=cb.BJ)


PRICES = cb.DEFAULT_PRICES
flash = [p for p in PRICES if p["match"] == "deepseek-flash"][0]

# ---------------- 1. 错峰窗口边界 ----------------
# 基准：峰时单价就是表里写的 0.04 / 2.0 / 8.0
for h, mi, offpeak in [(0, 0, False), (0, 29, False), (0, 30, True), (3, 0, True),
                       (8, 29, True), (8, 30, False), (12, 0, False),
                       (23, 29, False), (23, 45, False), (23, 59, False)]:
    c, i, o = cb._price_of("deepseek-flash", bj(2026, 11, 7, h, mi), PRICES)
    factor = c / flash["cached"]
    check(f"offpeak {h:02d}:{mi:02d}", round(factor, 3), 0.5 if offpeak else 1.0)

# ---------------- 2. 本地模型不算「未配价」 ----------------
check("qwen 本地", cb._is_free_model("qwen3:14b-ctx32k", cb.DEFAULT_FREE_MODELS), True)
check("deepseek 非本地", cb._is_free_model("deepseek-flash", cb.DEFAULT_FREE_MODELS), False)
check("未配价的云模型", cb._price_of("gpt-9-turbo", bj(2026, 11, 7, 12, 0), PRICES), None)

# ---------------- 3. UTC -> 北京时间 ----------------
w = cb._to_bj("2026-11-06 00:06:59.955912")
check("UTC+8", (w.year, w.month, w.day, w.hour), (2026, 11, 6, 8))

# ---------------- 4. 聚合：免费模型单列，不进未配价 ----------------
board = cb.CostBoard(None, {})
rows = [
    dict(created_at="2026-11-06 04:00:00", umo="u1", conversation_id="c1",
         provider_model="deepseek-flash", token_input_other=1000000,
         token_input_cached=0, token_output=0),
    dict(created_at="2026-11-06 04:00:00", umo="u2", conversation_id="c2",
         provider_model="qwen3:14b-ctx32k", token_input_other=5000,
         token_input_cached=0, token_output=0),
    dict(created_at="2026-11-06 04:00:00", umo="u3", conversation_id="c3",
         provider_model="mystery-model", token_input_other=1000,
         token_input_cached=0, token_output=0),
]
agg = board._aggregate(rows)
check("免费集合", sorted(agg.free), ["qwen3:14b-ctx32k"])
check("未配价集合", sorted(agg.unpriced), ["mystery-model"])
check("runs", agg.runs, 3)
# 12:00 北京 = 峰时，未命中输入 1M tokens 应为 ¥2.00
check("峰时 1M 未命中", round(agg.cost, 4), 2.0)

# ---------------- 5. 新鲜度：真库只读检查 ----------------
# 不依赖 astrbot 的路径模块，直接找本机常见位置（只读打开）
_cands = [r"F:\apps\astrbot\data\data_v4.db"]
db = next((c for c in _cands if os.path.isfile(c)), "")
if not db:
    print("[info] 没找到 data_v4.db，跳过新鲜度检查")
if db:
    board.db = db
    last, age = board._last_write()
    check("能读到最后写入时间", last is not None, True)
    check("age 是数字", isinstance(age, float), True)
    print(f"[info] 本机最后一条统计: {last}，距今 {age:.1f} 小时")

print("=" * 50)
if FAILS:
    print(f"FAIL {len(FAILS)}/{N[0]}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"PASS {N[0]}/{N[0]}")
