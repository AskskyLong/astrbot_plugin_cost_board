"""astrbot_plugin_cost_board —— 把 token 换算成钱。

为什么做这个：AstrBot 自带 provider_stats 只记 token，不记钱。站长能看到的
只有模型后台那张总额账单，看不出「哪个群、哪个会话在烧钱」。生态里讲「省
token」的插件很多，但没人算过省下来的到底是不是钱——缓存命中段的输入单价
只有未命中的 2%，省 token 不等于省钱。

规则：
- 只读：sqlite 用 mode=ro 打开，不写数据库、不改配置、不联网。
- 口径：provider_stats 一行 = 一次 agent run（可能含多轮工具循环），
  token_input_cached 是该 run 的累计命中量，不是单次请求值。
- 价格表可配；没配到的模型按 0 计并标注，不假装算得准。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_cost_board"
BJ = dt.timezone(dt.timedelta(hours=8))

# 每百万 tokens 的人民币价格：(缓存命中输入, 未命中输入, 输出)
DEFAULT_PRICES = [
    {"match": "deepseek-chat", "cached": 0.04, "input": 2.0, "output": 8.0, "offpeak": 0.5},
    {"match": "deepseek-flash", "cached": 0.04, "input": 2.0, "output": 8.0, "offpeak": 0.5},
    {"match": "deepseek-reasoner", "cached": 0.30, "input": 9.0, "output": 27.0, "offpeak": 0.5},
    {"match": "deepseek-pro", "cached": 0.30, "input": 9.0, "output": 27.0, "offpeak": 0.5},
]
# DeepSeek 错峰时段（北京时间）：00:30-08:30，左闭右开。
# 注意：右端不能用 "hour >= 23.5" 来兜，那是把 23:30-24:00 也算成错峰——错的。
OFFPEAK_START, OFFPEAK_END = 0.5, 8.5

# 本地/免费模型：计 0 元且不报「未配价」（它们本来就不花钱，报缺价是噪音）
DEFAULT_FREE_MODELS = ["qwen", ":7b", ":14b", ":32b", ":72b", "embedding", "bge-", "rerank"]


def _db_path(cfg: dict) -> str:
    custom = str(cfg.get("db_path") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        cand = os.path.join(get_astrbot_data_path(), "data_v4.db")
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    return ""


def _to_bj(ts: str) -> dt.datetime | None:
    try:
        naive = dt.datetime.fromisoformat(str(ts))
    except Exception:
        return None
    return naive.replace(tzinfo=dt.timezone.utc).astimezone(BJ)


def _is_free_model(model: str, hints: tuple[str, ...]) -> bool:
    """本地跑的模型（ollama/llama.cpp 等）不花钱，不该被报成「未配价」。"""
    m = (model or "").lower()
    return any(h and h in m for h in hints)


def _price_of(model: str, when: dt.datetime, prices: list[dict]) -> tuple[float, float, float] | None:
    """返回 (命中, 未命中, 输出) 的每百万 token 单价；未配价返回 None。"""
    m = (model or "").lower()
    hit = None
    for p in prices:
        if str(p.get("match", "")).lower() and str(p["match"]).lower() in m:
            hit = p
            break
    if hit is None:
        return None
    c, i, o = float(hit.get("cached", 0)), float(hit.get("input", 0)), float(hit.get("output", 0))
    hour = when.hour + when.minute / 60
    factor = 1.0
    if float(hit.get("offpeak", 1.0)) < 1.0 and (OFFPEAK_START <= hour < OFFPEAK_END):
        factor = float(hit.get("offpeak", 1.0))
    return c * factor, i * factor, o * factor


def _fmt_money(v: float) -> str:
    # 真 0 元和「缺数据」必须长得不一样：缺数据由「无记录」表达，
    # 这里绝不输出 ¥0.0000 —— 那是最容易让人误以为「今天没花钱」的一种写法。
    if v == 0:
        return "¥0"
    if v < 0.0001:
        return "<¥0.0001"
    if v >= 1:
        return f"¥{v:.2f}"
    if v >= 0.01:
        return f"¥{v:.3f}"
    return f"¥{v:.4f}"


def _short(s: str, n: int = 26) -> str:
    s = str(s or "")
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _sess(umo: str) -> str:
    """把 umo 压成能认出来的短标签：qq:GroupMessage:123456 -> GroupMessage:123456"""
    parts = str(umo or "?").split(":")
    tail = parts[-1] if parts else "?"
    kind = parts[1] if len(parts) > 1 else ""
    s = f"{kind}:{tail}" if kind else tail
    if len(s) <= 26:
        return s
    return s[:13] + "…" + s[-10:]


class _Agg:
    __slots__ = ("cost", "runs", "inc", "inhit", "out", "models", "umos", "unpriced", "free")

    def __init__(self) -> None:
        self.cost = 0.0
        self.runs = 0
        self.inc = 0   # 未命中输入
        self.inhit = 0  # 命中输入
        self.out = 0
        self.models: dict[str, float] = {}
        self.umos: dict[str, float] = {}
        self.unpriced: set[str] = set()
        self.free: set[str] = set()

    def add(self, model: str, umo: str, miss: int, hit: int, out: int, cost: float) -> None:
        self.cost += cost
        self.runs += 1
        self.inc += miss
        self.inhit += hit
        self.out += out
        self.models[model] = self.models.get(model, 0.0) + cost
        self.umos[umo] = self.umos.get(umo, 0.0) + cost

    @property
    def hit_rate(self) -> float:
        tot = self.inc + self.inhit
        return (self.inhit / tot) if tot else 0.0


@register(
    PLUGIN_NAME,
    "ayue",
    "AI 费用看板：把 provider_stats 的 token 换算成人民币，按天/模型/会话拆账",
    "v0.1.4",
)
class CostBoard(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.cfg = dict(config or {})
        self.db = _db_path(self.cfg)
        self.prices = self.cfg.get("prices") or DEFAULT_PRICES
        self.only_admin = bool(self.cfg.get("only_admin", True))
        fm = self.cfg.get("free_model_hints")
        self.free_models = tuple(str(x).lower() for x in (fm if isinstance(fm, list) else DEFAULT_FREE_MODELS))
        if not self.db:
            logger.warning("[cost-board] 找不到 data_v4.db，费用看板不可用")

    # ---------------------------------------------------------------- 取数
    def _rows(self, since: dt.datetime) -> list[dict]:
        if not self.db:
            return []
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(
                "select created_at, umo, conversation_id, provider_model, "
                "token_input_other, token_input_cached, token_output "
                "from provider_stats where created_at >= ? order by created_at",
                (since.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            con.close()

    def _aggregate(self, rows: list[dict]) -> _Agg:
        agg = _Agg()
        for r in rows:
            when = _to_bj(r["created_at"])
            if when is None:
                continue
            miss = int(r["token_input_other"] or 0)
            hit = int(r["token_input_cached"] or 0)
            out = int(r["token_output"] or 0)
            model = r["provider_model"] or "unknown"
            p = _price_of(model, when, self.prices)
            if p is None:
                pc = pi = po = 0.0
                if _is_free_model(model, self.free_models):
                    agg.free.add(model)
                else:
                    agg.unpriced.add(model)
            else:
                pc, pi, po = p
            cost = (hit * pc + miss * pi + out * po) / 1_000_000
            umo = r["umo"] or r["conversation_id"] or "?"
            agg.add(model, umo, miss, hit, out, cost)
        return agg

    def _last_write(self) -> tuple[dt.datetime | None, float]:
        """返回 (最后一条统计的北京时间, 距今小时数)。

        为什么要有这个：provider_stats 会静默停更——库被锁、写入任务被 GC、
        进程假活，全都不报错。只看金额不看新鲜度，会拿着一张三周前的表算账。
        """
        if not self.db:
            return None, -1.0
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                row = con.execute("select max(created_at) from provider_stats").fetchone()
            finally:
                con.close()
        except Exception:
            return None, -1.0
        if not row or not row[0]:
            return None, -1.0
        when = _to_bj(row[0])
        if when is None:
            return None, -1.0
        age = (dt.datetime.now(BJ) - when).total_seconds() / 3600.0
        return when, age

    # ---------------------------------------------------------------- 渲染
    def _report(self) -> str:
        now = dt.datetime.now(BJ)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spans = {
            "今日": midnight,
            "7 天": midnight - dt.timedelta(days=6),
            "30 天": midnight - dt.timedelta(days=29),
        }
        aggs = {k: self._aggregate(self._rows(v)) for k, v in spans.items()}
        a30 = aggs["30 天"]
        L = ["AI 费用看板"]
        last, age = self._last_write()
        if last is None:
            L.append("⚠️ 库里有 0 条统计数据，看板为空")
        elif age > 24:
            L.append(f"⚠️ 统计最后更新 {age:.1f} 小时前（{last:%m-%d %H:%M}），可能已停更")
        else:
            L.append(f"统计最后更新 {max(age, 0):.1f} 小时前（{last:%m-%d %H:%M}）")
        for k in ("今日", "7 天", "30 天"):
            a = aggs[k]
            # 0 行不等于 0 元：provider_stats 会整段漏记（11-03/04/05 就是空的），
            # 把缺失渲染成 ¥0.0000 会让人以为「今天没花钱」。缺失就说缺失。
            if a.runs == 0:
                L.append(f"{k:<4}{'无记录':>10}   0 次")
            else:
                L.append(f"{k:<4}{_fmt_money(a.cost):>10}   {a.runs} 次")
        if a30.runs == 0:
            L.append("")
            L.append("这段时间没有调用记录。")
            return "\n".join(L)

        L.append("")
        L.append("按模型（30 天）")
        for model, cost in sorted(a30.models.items(), key=lambda x: -x[1])[:4]:
            # 本地模型是真 0 元，不是缺价；写 ¥0.0000 会跟「未配价」混淆。
            money = "免费(本地)" if (cost == 0 and model in a30.free) else _fmt_money(cost)
            L.append(f"  {_short(model, 22):<24}{money:>9}")

        L.append("")
        L.append("最烧钱的会话（30 天）")
        top = sorted(a30.umos.items(), key=lambda x: -x[1])[:3]
        for i, (umo, cost) in enumerate(top, 1):
            L.append(f"  {i}. {_fmt_money(cost):>8}  {_sess(umo)}")

        L.append("")
        L.append(
            f"缓存命中 {a30.hit_rate * 100:.1f}%"
            f"（输入 {a30.inhit + a30.inc:,} tokens，命中 {a30.inhit:,}）"
        )
        if a30.free:
            L.append("本地/免费模型（计 0 元）: " + ", ".join(sorted(a30.free)[:3]))
        unpriced = sorted(a30.unpriced)
        if unpriced:
            L.append("未配价（按 0 元计，请在配置里补）: " + ", ".join(unpriced[:3]))
        L.append("口径：一行 = 一次 run 的累计值，价格为每百万 tokens")
        L.append("注意：定时任务/直连 API 不进 provider_stats，绝对值请看模型后台，本表看占比和趋势")
        return "\n".join(L)

    def _daily(self, days: int = 7) -> str:
        now = dt.datetime.now(BJ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=days - 1)
        rows = self._rows(start)
        by_day: dict[str, list[dict]] = {}
        for r in rows:
            when = _to_bj(r["created_at"])
            if when:
                by_day.setdefault(when.strftime("%m-%d"), []).append(r)
        L = [f"近 {days} 天逐日"]
        if not by_day:
            L.append("无记录。")
            return "\n".join(L)
        daily = {day: self._aggregate(v) for day, v in by_day.items()}
        peak = max([a.cost for a in daily.values()] or [0.0]) or 0.001
        # 把整段日期都列出来，缺的那天写「无记录」，不写 ¥0.0000——
        # provider_stats 会整段漏记，0 行不等于 0 花费。
        for i in range(days):
            day = (start + dt.timedelta(days=i)).strftime("%m-%d")
            a = daily.get(day)
            if a is None:
                L.append(f"  {day}  无记录（未统计，不等于没花）")
                continue
            bar = "█" * min(18, int(round(a.cost / peak * 18)))
            L.append(f"  {day}  {_fmt_money(a.cost):>9}  {a.runs:>3} 次  {bar}")
        return "\n".join(L)

    # ---------------------------------------------------------------- 命令
    @filter.command("费用", alias={"cost", "花费"})
    async def cmd_cost(self, event: AstrMessageEvent, sub: str = ""):
        """AI 费用看板：/费用 | /费用 天"""
        try:
            if self.only_admin:
                try:
                    ok = bool(event.is_admin())
                except Exception:
                    ok = False
                if not ok:
                    yield event.plain_result("只有管理员能看账本。")
                    return
            if not self.db:
                yield event.plain_result("找不到 data_v4.db，看板不可用。")
                return
            sub = (sub or "").strip()
            if sub in {"天", "日", "day", "daily"}:
                yield event.plain_result(self._daily(7))
            else:
                yield event.plain_result(self._report())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cost-board] 生成失败: %s", exc, exc_info=True)
            yield event.plain_result(f"生成失败: {exc}")
