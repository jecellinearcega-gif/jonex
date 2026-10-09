#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`cron_expr` 的解析器与「下一次触发时刻」的计算（§3.3.2）。

`cron_expr` 的解析与 `next_fire_at` 的计算是 cron 里**唯一算得上「算法」的部分**，
所以规格在文档里写死了，这里照它实现。**四处都调同一个函数**：

| 调用点 | 干什么 |
|--------|--------|
| `trig.create` | 建触发器时算出**首个** `next_fire_at` |
| `trig.update` | 改了 `cron_expr` / `misfire_policy` 时**重算** |
| `trig.resume` | 从 `paused` 恢复时**按当前时间重算**（不用暂停时的旧值） |
| §7.5 ⓪ 触发抢占 | 抢占成功后**推下一次** |

## 支持的写法（只有这 5 种，其余一律非法）

| 写法 | 例子 | 含义 |
|------|------|------|
| `*` | `*` | 任意值 |
| 单值 | `3` | 就是 3 |
| 区间 | `1-5` | 1 到 5（**含两端**），要求左 ≤ 右 |
| 列表 | `1,3,5` / `1-3,7` | 列出的这些值（元素可以是单值或区间） |
| 步长 | `*/15` / `1-30/10` | 从**起点**每 `n` 个取一个，`n` 必须是 ≥ 1 的正整数 |

**「起点」怎么定 —— 两种步长写法别混**：

- `*/n` → 起点 = **该段取值范围的下界**（`0 0 */5 * *` 在日段上是 `1,6,11,16,21,26,31`
  —— 下界是 `1` 不是 `0`）；
- `a-b/n` → 起点 = **区间左端 `a`**（`0 0 10-30/5 * *` 是 `10,15,20,25,30`，
  **不是**「先按段下界算、再跟区间取交集」得到的 `11,16,21,26`）。

两种都跟 Vixie cron 一致。**日 / 月两段最容易踩**，写错就是「到点不触发」或「触发日期不对」。

**明确不支持**（写进去就是非法）：`?`、`L`、`W`、`#`、`@daily` / `@hourly` 这类别名、
月份 / 星期的英文名（`JAN` / `MON`）、超过 5 段的表达式（**没有「秒」段**）、带年份段的扩展 Crontab。

## 日 / 周两段怎么合起来算（**别照「跟标准 cron 一样」记，只对一半**）

| 日段 / 周段 | 怎么算 |
| --- | --- |
| **两个都以 `*` 开头** | **两段的集合都参与**（`dom_ok and dow_ok`）：真·`* *` 时两个集合都是全集、恒真（= 每天都触发）；**带步长时按步长的集合** —— `0 0 */5 * *` = 每月 1/6/11…号，`0 0 * * */2` = 周日/二/四/六 |
| 只有一个被限定 | **只看被限定的那一段**：`0 3 */5 * 1` → 每周一（日段那个 `*/5` 的集合**不参与**） |
| **两个都被限定**（`0 3 1 * 1`） | **满足任一即触发** —— 「每月 1 号 **或** 每周一」，**不是「且」** |

⚠️ **第一行「两段都参与」必须成立，别把它简化成「每天都触发」**：那样会把
`0 0 */5 * *` 里 `*/5` 算出来的集合整个丢掉、变成每天都跑（**2026-09-30 修掉的就是这个**）。
⚠️ **第二行则是本项目刻意的简化**（文档 §3.3.2 ④ 明写）：Vixie 的判据是
`(DOM_STAR || DOW_STAR) ? (dom && dow) : (dom || dow)` —— 按它，`0 3 */5 * 1` 也得是
「周一 ∩ 1/6/11…号」；本项目只按被限定那一段判，写触发器的人按文档记即可。

想表达「1 号、而且是周一」5 段式表达不了 —— 要么建两条触发器，要么把判断挪到步骤内部。

**「被限定」的判据 = 该段的第一个字符是不是 `*`**（实现里就是
`parts[2].startswith("*")` / `parts[4].startswith("*")`）—— 这一半与 Vixie cron 的
`DOM_STAR` / `DOW_STAR` **一致**（它同样只看首字符）：

- 以 `*` 开头的一律算**没被限定**：`*` 本身，以及 `*/1` / `*/5` 这类步长写法
  （`*/1` 与 `*/5` 在「算不算被限定」上同类，但**两支都以 `*` 开头**时它们的集合照样参与比对）；
- 首字符不是 `*` 的算**被限定**：`1-31` 这种「数值上覆盖全范围」的写法**也算**
  （跟 Vixie 一样 —— 它只看首字符，不看集合等不等于全集）。

⚠️ **别再拿「集合等不等于全集」去判这件事**：那是另一套口径，会在 `*/1` 上与标准实现
分道。详见 `CronSchedule.dom_star` 的说明。

## 时区与误差

**按服务 / 数据库所在时区**解释（不做「每个触发器单独时区」，§3.3.2 ⑥），
所以传进来的 `after` 就是裸 `datetime`、跟 `datetime.now()` 一个口径。

## 本模块的返回契约（调用方按它分流）

- **语法 / 取值范围不合法** → 抛 `CronExprError`（M9b，`trig.create` / `trig.update` 直接拒绝）；
- **语法对、但算不出「下一次」** → **返回 `None`**（M9c，如 `0 0 30 2 *` = 2 月 30 日）。
  **不用异常表达这一支**：它在「抢占后推下一次」那条路径上是**预期内的兜底分支**
  （处置是告警 + 置 `paused`），在那条路径上用异常当控制流反而容易漏接。

`trig.fire` **不调本模块**：它只是「按预设入口跑一次」，跟 `cron_expr` 无关（§3.3.2 ⑦）。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import FrozenSet, Optional, Tuple

from jonex_core.common.exceptions import InvalidParameterError

# 段名（按顺序固定），报错文案与解析都按它走
_FIELD_NAMES = ("分钟", "小时", "日", "月", "星期")
# 各段的取值范围（闭区间）；「日」的下界是 1、不是 0，`*/n` 的起点就取这个下界
_FIELD_BOUNDS: Tuple[Tuple[int, int], ...] = (
    (0, 59),  # 分钟
    (0, 23),  # 小时
    (1, 31),  # 日
    (1, 12),  # 月
    (0, 7),   # 星期（0 和 7 都表示周日，跟标准 cron 一致）
)

# 搜索窗口：从「现在」往后最多找 9 年（§3.3.2 ⑤）
# **为什么是 9 年**：公历里两个 `2/29` 的**最大间隔是 8 年** —— 比如 `2096-02-29` 的
# 下一次是 `2104-02-29`（中间的 `2100` 不是闰年），4 年那种窗口会把 `0 0 29 2 *`
# 这个**完全合法**的表达式误判成「算不出」；留到 9 年是给足余量。
MAX_SEARCH_YEARS = 9

# 明确不支持的写法 —— 单独拦一下，好给出「你怎么写错了」的具体文案，
# 而不是让它掉进「段里有个看不懂的字符」这种泛泛的报错里。
_UNSUPPORTED_CHARS = "?LW#"
_ALIAS_RE = re.compile(r"^@")
_LETTER_RE = re.compile(r"[A-Za-z]")

_STEP_RE = re.compile(r"^(\*|\d{1,2}-\d{1,2})/(\d+)$")
_RANGE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_VALUE_RE = re.compile(r"^\d{1,2}$")


class CronExprError(InvalidParameterError):
    """`cron_expr` 非法（段数不对 / 用了不支持的写法 / 取值超范围）。

    对应 M9b：`trig.create` / `trig.update` 直接拒绝（`success=false` + 非 0 `code`）。
    **「语法对但算不出下一次」不走这个异常**，那个分支返回 `None`，见模块 docstring。
    """

    default_message = "cron 表达式非法"


@dataclass(frozen=True)
class CronSchedule:
    """解析好的 5 段表达式；`next_fire_at` 的搜索都基于它。"""

    expr: str
    minutes: Tuple[int, ...]
    hours: Tuple[int, ...]
    days: Tuple[int, ...]
    months: Tuple[int, ...]
    # 星期已归一成 `date.isoweekday()` 的口径（周一=1 … 周日=7）：
    # cron 里 `0` 和 `7` 都是周日，这里都落成 `7`，比对时直接用 isoweekday。
    weekdays: Tuple[int, ...]
    # 该段是不是「**没被限定**」—— 判据 = **该段首字符是不是 `*`**
    # （`parts[2].startswith("*")` / `parts[4].startswith("*")`），与 Vixie cron 的
    # `DOM_STAR` / `DOW_STAR` **口径完全一致**（Vixie 源码同样只看 `*ch == '*'`）。
    #
    # 所以：以 `*` 开头的都算「没被限定」—— `*` 本身、以及 `*/1` / `*/5` 这类步长写法；
    # 首字符不是 `*` 的算「被限定」，`1-31` 这种「数值上覆盖全范围」的也算（跟 Vixie 一样）。
    #
    # ⚠️ **别再拿「集合等不等于全集」去判这件事**：那是另一套口径，与标准实现会在
    # `*/1` 上分道 —— 同一个 `0 3 */1 * 1`，按首字符判是「只看周段 = 每周一」，按集合判
    # 是「日段是全集 1..31 → 或 → 每天都触发」。这里取**首字符**（= 标准）。
    dom_star: bool
    dow_star: bool

    def day_matches(self, day: date) -> bool:
        """这一天是否命中「日 / 星期」的限定（§3.3.2 ④）。

        ⚠️ **「两个都没被限定」那一支不能直接 `return True`**（2026-09-30 修）：
        「没被限定」的判据是**首字符是不是 `*`**，于是 `*/5` 这种**带步长**的写法也落进
        这一支 —— 直接放行等于把 `days` / `weekdays` 里**刚解析出来的集合整个丢掉**，
        `0 0 */5 * *`（本意每月 1、6、11…号）会变成**每天**触发，`0 0 */2 * *` 同理。
        频率被放大 2～28 倍，而 `trig.list` 上显示的仍是用户写的那个表达式，看不出异常。

        正确做法就是让两个集合**都参与判定**：真·两个 `*` 时两个集合都是全集，
        `dom_ok and dow_ok` 恒为真（与原来的「每天都触发」一致）；而带步长时筛的就是
        用户写的那几个日子 —— 这也是 Vixie cron 的真实行为（它的 `DOM_STAR` /
        `DOW_STAR` 同样只看首字符，但匹配那一句是 `bit_test(dom) && bit_test(dow)`）。

        ⚠️ **只改这一支，别顺手把下面那两支也改成「且」**：`0 3 */5 * 1`（日段带步长、
        周段限定）按 §3.3.2 ④ 的明确规定是「**只看被限定的周段** → 每周一」，文档专门
        举过这个例子、单测也钉着它。
        """
        dom_ok = day.day in self.days
        dow_ok = day.isoweekday() in self.weekdays
        if self.dom_star and self.dow_star:
            # 两个都没被限定 —— 各自判一次（真·`* *` 时两边都恒真 = 每天都触发）
            return dom_ok and dow_ok
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        # 两个都被限定 —— **满足任一即触发**（「每月 1 号 或 每周一」）
        return dom_ok or dow_ok


def _field_error(index: int, detail: str) -> CronExprError:
    return CronExprError(f"cron 表达式的「{_FIELD_NAMES[index]}」段不合法：{detail}")


def _parse_field(index: int, raw: str) -> FrozenSet[int]:
    """解析一段，返回它允许的取值集合（语法非法直接抛 `CronExprError`）。"""
    low, high = _FIELD_BOUNDS[index]
    star = raw == "*"

    def check(v: int) -> int:
        if v < low or v > high:
            raise _field_error(index, f"取值 {v} 超出 {low}-{high}")
        return v

    values: set = set()
    for token in raw.split(","):
        if token == "*":
            # `1,*` 这种写法也收下（等价于 `*`），不额外找麻烦
            values.update(range(low, high + 1))
            continue

        step_match = _STEP_RE.match(token)
        if step_match:
            base_text, step_text = step_match.groups()
            step = int(step_text)
            if step < 1:
                raise _field_error(index, f"步长 {step} 必须是 ≥ 1 的正整数")
            if base_text == "*":
                # `*/n` → 起点 = **该段取值范围的下界**（日段是 1、不是 0）
                start, end = low, high
            else:
                # `a-b/n` → 起点 = **区间左端 `a`**（到 `b` 为止、含两端）
                left, right = (int(x) for x in base_text.split("-"))
                start, end = check(left), check(right)
                if start > end:
                    raise _field_error(index, f"区间 {token} 的左端必须不大于右端")
            values.update(range(start, end + 1, step))
            continue

        range_match = _RANGE_RE.match(token)
        if range_match:
            left, right = (int(x) for x in range_match.groups())
            left, right = check(left), check(right)
            if left > right:
                raise _field_error(index, f"区间 {token} 的左端必须不大于右端")
            values.update(range(left, right + 1))
            continue

        if _VALUE_RE.match(token):
            values.add(check(int(token)))
            continue

        # 走到这里说明这段里有不认识的写法。分两种给文案：
        # ① 是文档点名的「不支持写法」→ 直接说清哪一类；
        # ② 其余（含 `5/10` 这种「单值 + 步长」、空段、双横线等）→ 报「只支持这 5 种」。
        # 注意：`a-b/n` 的步长写法在上面已经处理掉了，能落到这里的 `5/10` 确实非法
        #   —— 文档只认 `*/n` 与 `a-b/n` 两种步长写法。
        if token.startswith("@"):
            raise _field_error(index, "不支持 @daily / @hourly 这类别名写法")
        if any(ch in token for ch in _UNSUPPORTED_CHARS):
            raise _field_error(
                index, f"不支持 `{_UNSUPPORTED_CHARS}` 这类写法（只支持 5 段式的基础语法）"
            )
        if _LETTER_RE.search(token):
            raise _field_error(index, "不支持英文月份 / 星期名（如 JAN / MON），只认数字")
        raise _field_error(
            index, f"看不懂的写法 `{token}`（每段只支持 `*` / 单值 / `a-b` / `a,b` / `*/n` / `a-b/n`）"
        )

    if not values:
        # 理论上到不了（空段会在上面报「看不懂的写法」），留个兜底免得算出空集合、
        # 让 `next_fire_at` 变成「永远算不出来」这种更难查的现象。
        raise _field_error(index, "解析后没有任何取值")
    return frozenset(values)


def parse(expr: str) -> CronSchedule:
    """解析 5 段表达式。语法 / 取值范围不合法时抛 `CronExprError`。

    这里**不检查「能不能算出下一次」** —— 那是 `next_fire_at` 的事（M9b 与 M9c 分开）。
    """
    if not isinstance(expr, str):
        raise CronExprError("cron 表达式必须是字符串")
    if _ALIAS_RE.match(expr.strip()):
        raise CronExprError("不支持 @daily / @hourly 这类别名写法（必须是 5 段式）")

    parts = expr.split()
    if len(parts) != 5:
        raise CronExprError(
            f"cron 表达式必须是 5 段（分 时 日 月 周），当前是 {len(parts)} 段：`{expr}`"
        )

    minutes = _parse_field(0, parts[0])
    hours = _parse_field(1, parts[1])
    days = _parse_field(2, parts[2])
    months = _parse_field(3, parts[3])
    weekdays_raw = _parse_field(4, parts[4])

    # 星期归一：cron 的 `0` 与 `7` 都是周日，统一落成 isoweekday() 的 `7`
    weekdays = frozenset(7 if v == 0 else v for v in weekdays_raw)

    return CronSchedule(
        expr=expr,
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=tuple(sorted(days)),
        months=tuple(sorted(months)),
        weekdays=tuple(sorted(weekdays)),
        # 「被限定」的判据 = **首字符是不是 `*`**（与 Vixie cron 一致）：
        # `*/1` / `*/5` 这类以 `*` 开头的一律算「没被限定」，`1-31` 算「被限定」。
        # 别改成「集合是不是全集」—— 那是另一套口径，会在 `*/1` 上与标准分道。
        dom_star=parts[2].startswith("*"),
        dow_star=parts[4].startswith("*"),
    )


def _search_deadline(start: datetime) -> date:
    """搜索窗口的最后一天：`start` 往后 9 年（2/29 落到非闰年时退回 2/28）。"""
    today = start.date()
    try:
        return today.replace(year=today.year + MAX_SEARCH_YEARS)
    except ValueError:
        # 起点是 2/29，9 年后不是闰年
        return today.replace(year=today.year + MAX_SEARCH_YEARS, day=28)


def next_fire_at(schedule: CronSchedule, after: Optional[datetime] = None) -> Optional[datetime]:
    """算「**严格大于** `after` 的下一个匹配时刻」（§3.3.2 ⑤）。

    - `after` 不传就取 `datetime.now()`（**服务所在时区**，§3.3.2 ⑥）；
    - **严格大于**：刚建好的 `0 3 * * *`，若「现在」正好是 03:00，算出来是**明天** 03:00，
      **不会立刻触发一次**；
    - **粒度到分钟**：返回值的**秒一律是 `00`**（表达式本身没有秒段）；
    - **`after` 往后 9 年都算不出** → 返回 `None`（如 `0 0 30 2 *` = 2 月 30 日）。
    """
    if after is None:
        after = datetime.now()

    # 从「after 所在分钟的下一分钟」起找：这一分钟自身可能还没过完，但它整分钟的
    # 时刻戳（秒=00）比 after 小或相等，**不满足严格大于**，所以一律从下一分钟起。
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    deadline = _search_deadline(start)
    day = start.date()
    while day <= deadline:
        if day.month in schedule.months and schedule.day_matches(day):
            for hour in schedule.hours:
                for minute in schedule.minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute)
                    # hours / minutes 都是升序，所以第一个 **≥ start** 的就是答案；
                    # 而 start 本身已经严格大于 after，所以 candidate 也严格大于 after。
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    return None


def compute_next_fire_at(expr: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """`parse` + `next_fire_at` 的便捷入口（调用方最常用的就是这个）。

    契约与模块 docstring 一致：**语法非法抛 `CronExprError`；算不出返回 `None`**。
    """
    return next_fire_at(parse(expr), after)


__all__ = [
    "CronExprError",
    "CronSchedule",
    "MAX_SEARCH_YEARS",
    "compute_next_fire_at",
    "next_fire_at",
    "parse",
]
