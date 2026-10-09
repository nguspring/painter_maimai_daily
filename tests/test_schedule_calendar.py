"""验证日历与精确日程注入的真实模块，隔离宿主、网络和运行数据库。"""

import datetime as dt
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_selfie_calendar_tests"


def load_modules():
    """以独立包名加载真实日程模块，绕过插件注册；仅替换宿主人设读取和事件基类。"""
    for suffix in ("", ".core", ".core.schedule", ".core.inject"):
        module = ModuleType(PACKAGE + suffix)
        module.__path__ = [str(ROOT.joinpath(*suffix.strip(".").split(".")))] if suffix else [str(ROOT)]
        sys.modules[module.__name__] = module
    persona = ModuleType(PACKAGE + ".core.schedule.persona_builder")
    persona.build_persona_context = Mock(return_value="测试人设")
    sys.modules[persona.__name__] = persona
    return SimpleNamespace(**{
        name: importlib.import_module(PACKAGE + ".core.schedule." + filename)
        for name, filename in (
            ("calendar", "calendar_context"), ("models", "schedule_models"),
            ("snapshot", "schedule_snapshot"), ("templates", "schedule_templates"),
            ("manager", "schedule_manager"), ("generator", "schedule_llm_generator"),
            ("builder", "prompt_builder"),
        )
    })


MODULES = load_modules()
CAL = MODULES.calendar
RENDER = importlib.import_module(PACKAGE + ".core.inject.schedule_template").render_schedule_context


def holiday_payload(year=2026):
    """提供来源契约一致的测试数据，包含放假和周末补班。"""
    return {"year": year, "papers": ["https://www.gov.cn/test"], "days": [
        {"date": "2026-10-01", "name": "国庆节", "isOffDay": True},
        {"date": "2026-10-10", "name": "国庆节", "isOffDay": False},
    ]}


def ics_event(start, end, summary, rule=""):
    """构建全天测试事件，附带可选年度规则，用于解析与日期匹配。"""
    return ("BEGIN:VEVENT\n" + f"DTSTART;VALUE=DATE:{start}\nDTEND;VALUE=DATE:{end}\n"
            + f"SUMMARY:{summary}\n" + rule + "END:VEVENT\n")


def calendar_day(date=dt.date(2026, 10, 9)):
    """返回普通工作日的测试上下文，避免测试依赖当前日期或在线数据。"""
    return CAL.CalendarDay(date, "普通工作日", False, (), (), "已缓存")


class CalendarTests(unittest.IsolatedAsyncioTestCase):
    """验证时间归属、重复节日、调休、缓存及生成和宿主事件的连接。"""

    def test_snapshot_boundaries_gap_and_single_next(self):
        """活动结束瞬间属于下一项，空档不冒充活动，只输出最近的一项未来安排。"""
        item = MODULES.models.ScheduleItem
        items = [item("2026-10-09", 960, 1080, "hobby", "画画", "focused"),
                 item("2026-10-09", 1080, 1140, "eating", "吃晚饭"),
                 item("2026-10-09", 1200, 1260, "hobby", "听音乐")]
        snapshot = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 17, 20, tzinfo=CAL.BEIJING), items)
        text = RENDER(snapshot, calendar_day())
        self.assertIn("活动：画画", text)
        self.assertIn("2026-10-09 18:00—2026-10-09 19:00", text)
        self.assertIn("活动：吃晚饭", text)
        self.assertNotIn("听音乐", text)
        self.assertNotIn("自然地融入对话", text)
        boundary = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 18, tzinfo=CAL.BEIJING), items)
        self.assertEqual(boundary.current.item.description, "吃晚饭")
        gap = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 19, 30, tzinfo=CAL.BEIJING), items)
        self.assertIsNone(gap.current)
        self.assertEqual(gap.next_activity.item.description, "听音乐")
        early = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 8, tzinfo=CAL.BEIJING), items)
        self.assertIsNone(early.current)

    def test_snapshot_cross_midnight_and_no_next(self):
        """前日跨午夜活动在次日仍可命中，末项结束后不能保留已经结束的当前活动。"""
        item = MODULES.models.ScheduleItem
        items = [item("2026-10-08", 1380, 360, "sleeping", "睡觉"),
                 item("2026-10-09", 480, 540, "eating", "早餐")]
        snapshot = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 1, tzinfo=CAL.BEIJING), items)
        self.assertEqual(snapshot.current.end, dt.datetime(2026, 10, 9, 6, tzinfo=CAL.BEIJING))
        self.assertEqual(snapshot.next_activity.item.description, "早餐")
        late = MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9, 22, tzinfo=CAL.BEIJING), items)
        self.assertIsNone(late.current)
        self.assertNotIn("【下一项计划", RENDER(late, calendar_day()))
        with self.assertRaisesRegex(ValueError, "时区"):
            MODULES.snapshot.build_snapshot(dt.datetime(2026, 10, 9), items)

    def test_ics_fold_range_and_recurrence(self):
        """展开折行与转义，支持年度固定和浮动节日，农历附加日期不套用阳历重复。"""
        text = "BEGIN:VCALENDAR\n" + ics_event("20251225", "20251226", "圣诞\n 节")
        text += ics_event("20250511", "20250512", "母亲节", "RRULE:FREQ=YEARLY;BYMONTH=5;BYDAY=2SU\n")
        text += ics_event("20251127", "20251128", "感恩节", "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH\n")
        text += ics_event("20251225", "20251226", "圣诞节", "RRULE:FREQ=YEARLY\n")
        text += ics_event("20250301", "20250302", "龙抬头", "RDATE;VALUE=DATE:20250301,20260320\n")
        events = CAL.parse_ics(text + "END:VCALENDAR")
        self.assertEqual(events[0]["SUMMARY"], "圣诞节")
        self.assertTrue(CAL.event_matches(events[1], dt.date(2026, 5, 10)))
        self.assertFalse(CAL.event_matches(events[1], dt.date(2026, 5, 11)))
        self.assertTrue(CAL.event_matches(events[2], dt.date(2026, 11, 26)))
        self.assertTrue(CAL.event_matches(events[3], dt.date(2026, 12, 25)))
        self.assertTrue(CAL.event_matches(events[4], dt.date(2026, 3, 20)))
        self.assertFalse(CAL.event_matches(events[4], dt.date(2026, 3, 1)))
        with self.assertRaisesRegex(ValueError, "VCALENDAR"):
            CAL.parse_ics("网络错误页")

    async def test_calendar_day_adjustment_and_festival_separation(self):
        """补班周六不能当休息日，放假区间不被当成当天节日，未知数据不冒充无节日。"""
        service = CAL.CalendarService(Path(tempfile.gettempdir()) / "unused-calendar")

        async def source(_session, url, _kind):
            """模拟调休数据已获取、节日源未确认，验证各字段独立处理。"""
            return ({"data": holiday_payload()}, "已缓存") if url.endswith("2026.json") else (None, "未确认")

        service._get_source = source
        result = await service.get_day(dt.date(2026, 10, 10))
        self.assertFalse(result.is_off_day)
        self.assertIn("调休工作日", result.day_type)
        self.assertEqual(result.festivals, ())
        self.assertIn("未确认", result.render())
        holiday = await service.get_day(dt.date(2026, 10, 1))
        self.assertTrue(holiday.is_off_day)

    async def test_cache_fresh_stale_and_unknown(self):
        """新缓存避免联网，网络失败保留旧缓存，首次失败明确标未知并短时间抑制重复请求。"""
        with tempfile.TemporaryDirectory() as directory:
            service = CAL.CalendarService(Path(directory))
            url = CAL.HOLIDAY_URL.format(year=2026)
            fresh = {"data": holiday_payload(), "fetched_at": time.time()}
            service._entries[url] = fresh
            session = SimpleNamespace(get=Mock(side_effect=aiohttp.ClientConnectionError("断网")))
            self.assertEqual((await service._get_source(session, url, "holiday"))[1], "已缓存")
            session.get.assert_not_called()
            fresh["fetched_at"] = 0
            result, status = await service._get_source(session, url, "holiday")
            self.assertIs(result, fresh)
            self.assertIn("旧缓存", status)
            await service._get_source(session, url, "holiday")
            self.assertEqual(session.get.call_count, 1)
            unknown, status = await service._get_source(session, CAL.FESTIVAL_URL, "ics")
            self.assertIsNone(unknown)
            self.assertIn("未确认", status)

    async def test_cache_write_and_reuse(self):
        """成功联网写 UTF-8 缓存，新服务实例能从磁盘复用并保留中文字段。"""
        with tempfile.TemporaryDirectory() as directory:
            response = SimpleNamespace(raise_for_status=Mock(), text=AsyncMock(return_value=json.dumps(holiday_payload())))
            context = AsyncMock()
            context.__aenter__.return_value = response
            session = SimpleNamespace(get=Mock(return_value=context))
            url = CAL.HOLIDAY_URL.format(year=2026)
            service = CAL.CalendarService(Path(directory))
            result, status = await service._get_source(session, url, "holiday")
            self.assertEqual(status, "已更新")
            self.assertEqual(result["data"]["days"][0]["name"], "国庆节")
            other = CAL.CalendarService(Path(directory))
            session.get.reset_mock()
            self.assertEqual((await other._get_source(session, url, "holiday"))[1], "已缓存")
            session.get.assert_not_called()

    async def test_unpublished_holiday_is_unknown(self):
        """来源预建空年度文件不能被误判成全年普通工作日，也不能阻断节日查询。"""
        with tempfile.TemporaryDirectory() as directory:
            response = SimpleNamespace(raise_for_status=Mock(), text=AsyncMock(
                return_value=json.dumps({"year": 2027, "papers": [], "days": []})
            ))
            context = AsyncMock()
            context.__aenter__.return_value = response
            service = CAL.CalendarService(Path(directory))
            result, status = await service._get_source(
                SimpleNamespace(get=Mock(return_value=context)), CAL.HOLIDAY_URL.format(year=2027), "holiday"
            )
            self.assertIsNone(result)
            self.assertIn("公告未发布", status)

    async def test_malformed_source_does_not_become_network_fallback(self):
        """格式错误是数据契约失败，不能在下一次调用被失败限频悄悄改成无数据。"""
        with tempfile.TemporaryDirectory() as directory:
            response = SimpleNamespace(raise_for_status=Mock(), text=AsyncMock(return_value="错误格式"))
            context = AsyncMock()
            context.__aenter__.return_value = response
            service = CAL.CalendarService(Path(directory))
            session = SimpleNamespace(get=Mock(return_value=context))
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "VCALENDAR"):
                    await service._get_source(session, CAL.FESTIVAL_URL, "ics")
            self.assertNotIn(CAL.FESTIVAL_URL, service._attempts)
            self.assertNotIn(CAL.FESTIVAL_URL, service._entries)

    async def test_december_merges_next_year_adjustment(self):
        """十二月补班可能属于下一年公告，必须覆盖当前年的默认星期判断。"""
        service = CAL.CalendarService(Path(tempfile.gettempdir()) / "unused-calendar")
        current = {"year": 2026, "papers": ["公告"], "days": []}
        adjacent = {"year": 2027, "papers": ["公告"], "days": [
            {"date": "2026-12-26", "name": "元旦", "isOffDay": False}
        ]}

        async def source(_session, url, _kind):
            """返回模拟跨年补班公告，其他来源不提供数据。"""
            if url.endswith("2026.json"):
                return {"data": current}, "已缓存"
            if url.endswith("2027.json"):
                return {"data": adjacent}, "已缓存"
            return None, "未确认"

        service._get_source = source
        result = await service.get_day(dt.date(2026, 12, 26))
        self.assertFalse(result.is_off_day)
        self.assertIn("元旦补班", result.day_type)

    async def test_manager_snapshot_uses_today_calendar(self):
        """今日模板按已确认假日选择，前后日只读数据库；不会合成明天的新活动。"""
        manager = MODULES.manager.ScheduleManager.__new__(MODULES.manager.ScheduleManager)
        manager.list_schedule_items = AsyncMock(return_value=[])
        now = dt.datetime(2026, 10, 1, 10, tzinfo=CAL.BEIJING)
        day = CAL.CalendarDay(now.date(), "国庆假期", True, ("国庆节",), (), "已缓存")
        snapshot = await manager.get_injection_snapshot(now, day)
        self.assertEqual(snapshot.current.item.activity_type, "self_care")
        self.assertEqual(manager.list_schedule_items.await_count, 3)
        calls = [call.args[0] for call in manager.list_schedule_items.await_args_list]
        self.assertEqual(calls, ["2026-10-01", "2026-09-30", "2026-10-02"])
        self.assertEqual(MODULES.templates.get_template_schedule("2026-10-10", is_off_day=False)[3].activity_type, "working")

    async def test_generation_reads_target_calendar(self):
        """执行真实生成流程，模型收到目标日期节日和补班信息，仍使用原 JSON 契约。"""
        target = dt.date(2026, 10, 10)
        day = CAL.CalendarDay(target, "调休工作日（国庆补班）", False, ("测试纪念日",), (), "已缓存")
        service = SimpleNamespace(get_day=AsyncMock(return_value=day))
        items = [MODULES.models.ScheduleItem(target.isoformat(), 540, 1080, "working", "工作")]
        generator = MODULES.generator
        plugin = SimpleNamespace(get_config=Mock(side_effect=lambda _key, default: default))
        with patch.object(generator, "get_calendar_service", return_value=service), \
                patch.object(generator, "_generate_once", new=AsyncMock(return_value=(items, []))) as generate, \
                patch.object(generator, "evaluate_schedule_quality", return_value=SimpleNamespace(score=1, issues=[])):
            result = await generator.generate_schedule_via_llm(plugin, target.isoformat())
        self.assertEqual(result, items)
        service.get_day.assert_awaited_once_with(target)
        prompt = generate.await_args.kwargs["prompt"]
        self.assertIn("调休工作日", prompt)
        self.assertIn("测试纪念日", prompt)
        self.assertIn("北京时间", prompt)

    async def test_always_ignores_intent_throttle_and_sets_host_flag(self):
        """执行真实事件处理器，两次连续回复均注入；会话关闭和命令仍跳过。"""
        base = ModuleType("src.plugin_system.base.base_events_handler")
        base.BaseEventHandler = object
        types = ModuleType("src.plugin_system.base.component_types")
        types.EventType = SimpleNamespace(ON_MESSAGE="message", POST_LLM="prompt")
        types.CustomEventHandlerResult = object
        with patch.dict(sys.modules, {base.__name__: base, types.__name__: types}):
            handler_module = importlib.import_module(PACKAGE + ".core.schedule_inject_handler")
        obj = handler_module.ScheduleInjectHandler()
        obj.get_config = Mock(side_effect=lambda _key, default: default)
        obj._get_inject_override = AsyncMock(return_value=None)
        obj._should_inject_throttle = Mock(side_effect=AssertionError("always 不应检查限频"))
        obj._should_inject_by_intent = Mock(side_effect=AssertionError("always 不应分类"))
        now = dt.datetime(2026, 10, 9, 17, tzinfo=CAL.BEIJING)
        snapshot = MODULES.snapshot.build_snapshot(now, [])
        manager = SimpleNamespace(get_injection_snapshot=AsyncMock(return_value=snapshot))
        service = SimpleNamespace(get_day=AsyncMock(return_value=calendar_day()))
        with patch.object(handler_module, "now_beijing", return_value=now), \
                patch.object(handler_module, "get_calendar_service", return_value=service), \
                patch.object(MODULES.manager, "get_schedule_manager", return_value=manager):
            for _ in range(2):
                message = SimpleNamespace(stream_id="test", plain_text="你怎么还没睡？", llm_prompt="原提示词")
                message.modify_llm_prompt = Mock()
                result = await obj.execute(message)
                self.assertTrue(result[0])
                self.assertTrue(message.llm_prompt.endswith("原提示词"))
                message.modify_llm_prompt.assert_called_once_with(message.llm_prompt)
            obj._get_inject_override.return_value = False
            blocked = SimpleNamespace(stream_id="test", plain_text="你好", llm_prompt="原提示词")
            await obj.execute(blocked)
            self.assertEqual(blocked.llm_prompt, "原提示词")
            command = SimpleNamespace(stream_id="test", plain_text="/schedule", llm_prompt="原提示词")
            await obj.execute(command)
            self.assertEqual(command.llm_prompt, "原提示词")
        self.assertEqual(manager.get_injection_snapshot.await_count, 2)


if __name__ == "__main__":
    unittest.main()
