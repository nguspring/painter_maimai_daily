"""联网日历上下文：共用中国大陆调休、节日及节气数据，并保存 UTF-8 缓存。"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
import re
import time

import aiohttp

logger = logging.getLogger(__name__)
BEIJING = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
FESTIVAL_URL = "https://yangh9.github.io/ChinaCalendar/cal_festival.ics"
SOLAR_TERM_URL = "https://yangh9.github.io/ChinaCalendar/cal_solarTerm.ics"
EXTRA_URL = "https://raw.githubusercontent.com/wzx-xle/china-holidays-extra/main/china-holidays-extra.ics"
HOLIDAY_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def now_beijing() -> dt.datetime:
    """返回带 UTC+8 时区的当前时间，统一日程查询与生成的日期边界。"""
    return dt.datetime.now(BEIJING)


@dataclass(frozen=True)
class CalendarDay:
    """指定日期的日历事实；未知休假状态用 None 表示，不冒充普通工作日。"""

    date: dt.date
    day_type: str
    is_off_day: bool | None
    festivals: tuple[str, ...]
    solar_terms: tuple[str, ...]
    status: str
    festivals_known: bool = True
    solar_terms_known: bool = True

    def render(self) -> str:
        """返回聊天注入和日程生成共用的日历文本，不包含节日百科正文。"""
        festival_text = "、".join(self.festivals) or "来源未收录当天节日"
        solar_text = "、".join(self.solar_terms) or "无"
        # 数据源尚未覆盖目标年份时，不能将空列表解释为当天没有节日。
        if not self.festivals_known:
            festival_text = (festival_text + "（完整节日日历未确认）") if self.festivals else "未确认"
        if not self.solar_terms_known:
            solar_text = "未确认"
        return "\n".join([
            f"日期：{self.date.isoformat()}（{WEEKDAYS[self.date.weekday()]}）",
            f"日历类型：{self.day_type}",
            f"当日节日及纪念日：{festival_text}",
            f"当日节气：{solar_text}",
            f"日历数据状态：{self.status}",
        ])


def parse_ics(text: str) -> list[dict[str, str]]:
    """读取已核实订阅源的全天事件；展开折行，拒绝未支持的时间或重复规则。

    来源：https://github.com/YangH9/ChinaCalendar 和 https://github.com/wzx-xle/china-holidays-extra。
    返回日期、结束日期、名称及重复字段；不执行下载内容中的任何代码。
    """
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    if not unfolded.startswith("BEGIN:VCALENDAR") or "END:VCALENDAR" not in unfolded:
        raise ValueError("节日日历不是完整的 VCALENDAR")
    events = []
    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        fields = {}
        # 只读取事件内的已知字段，避免误读后续时区或日历级属性。
        for line in block.split("END:VEVENT", 1)[0].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name = key.split(";", 1)[0]
            if name in {"DTSTART", "DTEND", "RDATE"}:
                if key != f"{name};VALUE=DATE":
                    raise ValueError(f"日历字段不支持非全天日期：{key}")
                fields[name] = value
            elif name in {"SUMMARY", "RRULE"}:
                fields[name] = value
            elif name in {"EXDATE", "EXRULE", "RECURRENCE-ID"}:
                raise ValueError(f"日历出现未支持的重复例外字段：{name}")
        start = dt.datetime.strptime(fields["DTSTART"], "%Y%m%d").date()
        end = dt.datetime.strptime(fields["DTEND"], "%Y%m%d").date()
        if end <= start:
            raise ValueError("日历事件结束日期必须晚于开始日期")
        fields["SUMMARY"] = re.sub(r"\\([nN,;\\])", _unescape_ics, fields["SUMMARY"])
        fields["SUMMARY"] = fields["SUMMARY"].replace("『", "").replace("』", "")
        # 在保存缓存前验证重复规则，未知规则直接报错，不留下无法消费的缓存。
        event_matches(fields, start)
        events.append(fields)
    if not events:
        raise ValueError("节日日历没有任何事件")
    return events


def _unescape_ics(match: re.Match) -> str:
    """还原日历文本的转义字符；换行变为空格，避免名称破坏注入字段布局。"""
    return " " if match[1] in {"n", "N"} else match[1]


def event_matches(event: dict[str, str], date: dt.date) -> bool:
    """按具体日期、附加日期或年度规则判断全天事件是否覆盖目标日期。"""
    start = dt.datetime.strptime(event["DTSTART"], "%Y%m%d").date()
    end = dt.datetime.strptime(event["DTEND"], "%Y%m%d").date()
    starts = {start}
    if "RDATE" in event:
        starts.update(dt.datetime.strptime(value, "%Y%m%d").date() for value in event["RDATE"].split(","))
    if "RRULE" in event:
        rule = dict(part.split("=", 1) for part in event["RRULE"].split(";"))
        if rule["FREQ"] != "YEARLY" or set(rule) - {"FREQ", "BYMONTH", "BYDAY"}:
            raise ValueError(f"不支持的日历重复规则：{event['RRULE']}")
        month = int(rule.get("BYMONTH", start.month))
        day = start.day
        # 已核实的浮动节日用正序第 N 个星期几，例如五月第二个星期日。
        if "BYDAY" in rule:
            match = re.fullmatch(r"([1-5])(MO|TU|WE|TH|FR|SA|SU)", rule["BYDAY"])
            if not match:
                raise ValueError(f"不支持的 BYDAY：{rule['BYDAY']}")
            weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU").index(match[2])
            first = dt.date(date.year, month, 1)
            day = 1 + (weekday - first.weekday()) % 7 + (int(match[1]) - 1) * 7
        occurrence = dt.date(date.year, month, day)
        if occurrence >= start:
            starts.add(occurrence)
    return any(value <= date < value + (end - start) for value in starts)


class CalendarService:
    """共用联网缓存；串行刷新避免多个群聊同时下载，断网使用已获授权的旧缓存。"""

    def __init__(self, cache_dir: Path | None = None):
        """指定缓存目录；默认位于插件 data/calendar，初始化不发网络请求也不写文件。"""
        self.cache_dir = cache_dir or Path(__file__).resolve().parents[2] / "data" / "calendar"
        self._entries: dict[str, dict] = {}
        self._attempts: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _read_cache(self, path: Path) -> dict | None:
        """读取 UTF-8 缓存；损坏缓存显式报错，避免把格式错误当作断网。"""
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _write_cache(self, path: Path, entry: dict) -> None:
        """用 UTF-8 原子替换单个来源缓存，避免进程中断留下半份 JSON。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    async def _get_source(self, session: aiohttp.ClientSession, url: str, kind: str) -> tuple[dict | None, str]:
        """读取或联网刷新一个来源；返回已验证内容和状态，网络错误记录日志并保留旧缓存。"""
        path = self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")
        if url not in self._entries:
            cached = await asyncio.to_thread(self._read_cache, path)
            if cached is not None:
                self._entries[url] = cached
        entry = self._entries.get(url)
        now = time.time()
        if entry and now - entry["fetched_at"] < 86400:
            return entry, "已缓存"
        # 失败后五分钟内不再次请求；无后台重试，后续正常调用才触发下一次尝试。
        if now - self._attempts.get(url, 0) < 300:
            return entry, "旧缓存（刷新失败）" if entry else "未确认（获取失败）"
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                text = await response.text(encoding="utf-8")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._attempts[url] = now
            logger.warning("[Calendar] 日历获取失败 %s：%s", url, exc)
            return entry, "旧缓存（刷新失败）" if entry else "未确认（获取失败）"
        if kind == "holiday":
            data = json.loads(text)
            if data["year"] != int(url.rsplit("/", 1)[1].split(".")[0]):
                raise ValueError("放假日历年份与请求年份不一致")
            # 来源会提前建立下一年的空文件；无公告不是普通工作日，需明确标记未发布。
            if not data["papers"]:
                self._attempts[url] = now
                logger.warning("[Calendar] %s 尚未收录国务院公告，放假和调休未确认", url)
                return entry, "旧缓存（公告未发布）" if entry else "未确认（公告未发布）"
            for day in data["days"]:
                dt.date.fromisoformat(day["date"])
                if type(day["isOffDay"]) is not bool or not isinstance(day["name"], str):
                    raise ValueError("放假日历的日期类型或名称不符合数据契约")
        else:
            data = parse_ics(text)
        entry = {"fetched_at": now, "data": data}
        await asyncio.to_thread(self._write_cache, path, entry)
        self._entries[url] = entry
        return entry, "已更新"

    async def get_day(self, date: dt.date) -> CalendarDay:
        """取得指定日期的完整日历上下文；可能联网、写缓存，不读取用户聊天内容。"""
        async with self._lock:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                # 独立来源并行获取，首次联网只等待最慢的一项，不串行叠加四份超时时间。
                results = await asyncio.gather(
                    self._get_source(session, HOLIDAY_URL.format(year=date.year), "holiday"),
                    self._get_source(session, FESTIVAL_URL, "ics"),
                    self._get_source(session, EXTRA_URL, "ics"),
                    self._get_source(session, SOLAR_TERM_URL, "ics"),
                )
                (holidays, holiday_status), (festival, festival_status), (extra, extra_status), (solar, solar_status) = results
                # 国务院下一年度公告可能覆盖本年十二月的调休，必须合并两份公告。
                if date.month == 12:
                    adjacent, adjacent_status = await self._get_source(
                        session, HOLIDAY_URL.format(year=date.year + 1), "holiday"
                    )
                else:
                    adjacent, adjacent_status = None, ""
        holiday_days = holidays["data"]["days"] if holidays else []
        if adjacent:
            holiday_days = holiday_days + adjacent["data"]["days"]
        matching = [day for day in holiday_days if day["date"] == date.isoformat()]
        is_off_day = None
        day_type = "未确认（放假与调休数据不可用）"
        if matching:
            day = matching[-1]
            is_off_day = day["isOffDay"]
            day_type = f"法定节假日休息（{day['name']}假期）" if is_off_day else f"调休工作日（{day['name']}补班）"
        elif holidays:
            is_off_day = date.weekday() >= 5
            day_type = "周末休息日" if is_off_day else "普通工作日"
            if date.month == 12 and not adjacent:
                is_off_day = None
                day_type += "（下一年度公告未确认，跨年调休待核实）"
        names = []
        # 两份节日源的同名事件去重，保留不同纪念日，绝不把假期名称当作当天节日。
        for source in (festival, extra):
            if source:
                for event in source["data"]:
                    if event_matches(event, date) and event["SUMMARY"] not in names:
                        names.append(event["SUMMARY"])
        terms = tuple(dict.fromkeys(
            event["SUMMARY"] for event in solar["data"] if event_matches(event, date)
        )) if solar else ()
        # 春节等跨年农历事件不能代表全年覆盖；元旦与国庆均存在才认为该年节日表完整。
        festival_known = bool(festival and all(
            any(event["DTSTART"] == f"{date.year}{suffix}" for event in festival["data"])
            for suffix in ("0101", "1001")
        ) and extra)
        solar_known = bool(solar and sum(event["DTSTART"].startswith(str(date.year)) for event in solar["data"]) == 24)
        statuses = [holiday_status, festival_status, extra_status, solar_status]
        if adjacent_status:
            statuses.append(adjacent_status)
        if not festival_known or not solar_known:
            statuses.append("部分日历未覆盖目标年份或不可用")
        return CalendarDay(date, day_type, is_off_day, tuple(names), terms, "；".join(dict.fromkeys(statuses)),
                           festival_known, solar_known)


_calendar_service: CalendarService | None = None


def get_calendar_service() -> CalendarService:
    """返回进程共用服务，使回复注入和生成日程共用联网数据与缓存。"""
    global _calendar_service
    if _calendar_service is None:
        _calendar_service = CalendarService()
    return _calendar_service
