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

# ---------------- 4b. 逐日渲染：缺的日子必须写「无记录」 ----------------
class _FakeBoard(cb.CostBoard):
    """不查库，直接给固定行，专测渲染。"""
    def __init__(self, rows):
        super().__init__(None, {})
        self.db = ""
        self._rows_fixed = rows

    def _rows(self, since):
        # 必须按 since 过滤，否则「今日」那行会被历史数据污染，测出来的结论是假的。
        return [r for r in self._rows_fixed
                if (cb._to_bj(r["created_at"]) or dt.datetime.min.replace(tzinfo=cb.BJ)) >= since]


today = dt.datetime.now(cb.BJ)
yday = today - dt.timedelta(days=1)
fmt = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
fb = _FakeBoard([dict(created_at=fmt(today), umo="u1", conversation_id="c1",
                      provider_model="deepseek-flash", token_input_other=1000,
                      token_input_cached=0, token_output=0)])
txt = fb._daily(3)
check("缺的日子写无记录", txt.count("无记录（未统计，不等于没花）") >= 1, True)
check("不出现 ¥0.0000", "¥0.0000" not in txt, True)

# ---------------- 4c. 总览：0 行也必须写「无记录」，不能写 ¥0.0000 ----------------
# 这是 11-07 午间做渲染 A/B 时抓到的：逐日表改了，总览还在渲染 ¥0.0000。
empty = _FakeBoard([])
empty._last_write = lambda: (None, -1.0)
rep = empty._report()
check("总览 0 行不写 ¥0.0000", "¥0.0000" not in rep, True)
check("总览 0 行写无记录", rep.count("无记录") >= 3, True)

# 有历史数据但今天没有：今日那行也得是「无记录」，不能是 ¥0.0000
only_yday = _FakeBoard([dict(created_at=fmt(yday), umo="u1", conversation_id="c1",
                             provider_model="deepseek-flash", token_input_other=1000,
                             token_input_cached=0, token_output=0)])
only_yday._last_write = lambda: (yday, 24.0)
rep2 = only_yday._report()
tline = [x for x in rep2.splitlines() if x.startswith("今日")]
check("今日无行 -> 无记录", bool(tline) and "无记录" in tline[0], True)

# ---------------- 4d. 免费模型在按模型表里不显示 ¥0.0000 ----------------
free_only = _FakeBoard([dict(created_at=fmt(today), umo="u1", conversation_id="c1",
                             provider_model="qwen3:14b-ctx32k", token_input_other=5000,
                             token_input_cached=0, token_output=0)])
free_only._last_write = lambda: (today, 1.0)
rep3 = free_only._report()
check("免费模型标注", "免费(本地)" in rep3, True)
check("免费模型不渲染 0.0000", "¥0.0000" not in rep3, True)

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
