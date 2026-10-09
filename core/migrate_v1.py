"""
v1 → v2 配置自动迁移模块

当用户在同一目录下通过 git pull 从 selfie_painter (v3.5.x) 升级到
selfie_painter_v2 (v3.6.0) 时，自动检测旧版 config.toml 并将用户配置
迁移到 v2 格式。仅迁移 config.toml，不迁移 JSON 日程数据或自拍状态。

迁移流程：
    1. is_v1_config() 判断当前 config.toml 是否为 v1 格式
    2. migrate_v1_to_v2() 执行字段映射，生成 v2 格式配置字典
    3. 由 plugin.py __init__ 调用，迁移前自动备份旧配置到 old/ 目录
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from src.common.logger import get_logger

logger = get_logger("selfie_painter_v2.migrate_v1")

# ────────────────────────────────────────────────────────────────
# 检测
# ────────────────────────────────────────────────────────────────


def is_v1_config(config: Dict[str, Any]) -> bool:
    """判断 config.toml 是否为 v1 (selfie_painter 3.5.x) 格式。

    检测策略（满足任一即判定为 v1）：
        - plugin.name == "selfie_painter"
        - plugin.config_version 以 "3.5" 开头
        - 存在 v1 独有的 [logging] 节
        - 存在 auto_selfie.chat_id_list（v2 改为 target_groups/target_users）

    Args:
        config: 从 config.toml 解析出的字典

    Returns:
        True 表示为 v1 格式，需要迁移
    """
    plugin_section: Dict[str, Any] = config.get("plugin", {})

    # 检查 plugin.name
    plugin_name: str = plugin_section.get("name", "")
    if plugin_name == "selfie_painter":
        return True

    # 检查 config_version
    config_version: str = str(plugin_section.get("config_version", ""))
    if config_version.startswith("3.5"):
        return True

    # 检查 v1 独有节
    if "logging" in config:
        return True

    # 检查 v1 独有字段
    auto_selfie: Dict[str, Any] = config.get("auto_selfie", {})
    if "chat_id_list" in auto_selfie:
        return True

    return False


# ────────────────────────────────────────────────────────────────
# 辅助函数
# ────────────────────────────────────────────────────────────────

_CHAT_ID_RE = re.compile(r"^qq:(\d+):(group|private)$", re.IGNORECASE)


def _parse_chat_id_list(
    chat_id_list: List[str],
) -> Tuple[List[str], List[str]]:
    """将 v1 的 chat_id_list 解析为 v2 的 target_groups 和 target_users。

    v1 格式示例：["qq:123456:group", "qq:789012:private"]
    v2 格式：target_groups=["123456"], target_users=["789012"]

    Args:
        chat_id_list: v1 格式的聊天 ID 列表

    Returns:
        (target_groups, target_users) 二元组
    """
    target_groups: List[str] = []
    target_users: List[str] = []

    for entry in chat_id_list:
        entry = entry.strip()
        match = _CHAT_ID_RE.match(entry)
        if match:
            number: str = match.group(1)
            chat_type: str = match.group(2).lower()
            if chat_type == "group":
                target_groups.append(number)
            else:
                target_users.append(number)
        else:
            logger.warning("[迁移] 无法解析 chat_id_list 条目，跳过：%s", entry)

    return target_groups, target_users


def _get(d: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """按点分隔路径从嵌套字典中取值。

    Args:
        d: 嵌套字典
        dotted_key: 点分隔的键路径，如 "auto_selfie.sleep_start_time"
        default: 键不存在时的默认值

    Returns:
        取到的值，或 default
    """
    keys: List[str] = dotted_key.split(".")
    cur: Any = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _set(d: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """按点分隔路径向嵌套字典写值（自动创建中间节）。

    Args:
        d: 嵌套字典
        dotted_key: 点分隔的键路径
        value: 要设置的值
    """
    keys: List[str] = dotted_key.split(".")
    cur: Dict[str, Any] = d
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def _copy_if_exists(
    src: Dict[str, Any],
    dst: Dict[str, Any],
    src_key: str,
    dst_key: Optional[str] = None,
) -> bool:
    """如果 src 中存在 src_key，则复制到 dst 的 dst_key。

    Args:
        src: 源字典
        dst: 目标字典
        src_key: 源键（点分隔路径）
        dst_key: 目标键（点分隔路径），默认与 src_key 相同

    Returns:
        True 如果成功复制
    """
    if dst_key is None:
        dst_key = src_key
    val: Any = _get(src, src_key)
    if val is not None:
        _set(dst, dst_key, val)
        return True
    return False


# ── v2 模型字段的默认值（v1 没有这些字段） ──────────────────
_V2_MODEL_DEFAULTS: Dict[str, Any] = {
    "cfg": 0,
    "sampler": "k_euler_ancestral",
    "nocache": 0,
    "noise_schedule": "karras",
}


# ────────────────────────────────────────────────────────────────
# 模型迁移
# ────────────────────────────────────────────────────────────────

# v1 和 v2 共有的模型字段（直接复制）
_SHARED_MODEL_FIELDS: List[str] = [
    "name",
    "base_url",
    "api_key",
    "format",
    "model",
    "fixed_size_enabled",
    "default_size",
    "seed",
    "guidance_scale",
    "num_inference_steps",
    "watermark",
    "custom_prompt_add",
    "negative_prompt_add",
    "artist",
    "support_img2img",
    "auto_recall_delay",
]


def _migrate_models(old_config: Dict[str, Any]) -> Dict[str, Any]:
    """迁移所有模型配置。

    对每个 v1 模型：
        - 直接复制所有共有字段（name, base_url, api_key 等）
        - 为 v2 新增字段（cfg, sampler, nocache, noise_schedule）填充默认值

    Args:
        old_config: v1 完整配置

    Returns:
        v2 格式的 [models] 节字典
    """
    old_models: Dict[str, Any] = old_config.get("models", {})
    new_models: Dict[str, Any] = {}

    for model_id, model_cfg in old_models.items():
        if not isinstance(model_cfg, dict):
            # 跳过 hint 等非字典字段
            continue

        new_model: Dict[str, Any] = {}

        # 复制共有字段
        for field in _SHARED_MODEL_FIELDS:
            if field in model_cfg:
                new_model[field] = model_cfg[field]

        # 填充 v2 新增字段
        for field, default_val in _V2_MODEL_DEFAULTS.items():
            if field not in new_model:
                new_model[field] = default_val

        new_models[model_id] = new_model
        logger.info(
            "[迁移] 模型 %s (%s) 已迁移",
            model_id,
            model_cfg.get("name", "未知"),
        )

    return new_models


# ────────────────────────────────────────────────────────────────
# 主迁移逻辑
# ────────────────────────────────────────────────────────────────


def migrate_v1_to_v2(old_config: Dict[str, Any]) -> Dict[str, Any]:
    """将 v1 (selfie_painter 3.5.x) 配置迁移为 v2 (selfie_painter_v2 3.6.0) 格式。

    迁移原则：
        - 用户可见的配置值尽量保留（API 密钥、模型、提示词等）
        - v1 独有但 v2 已删除的字段静默丢弃
        - v2 新增字段使用合理默认值
        - 字段重命名/移动按映射表处理

    Args:
        old_config: 从 v1 config.toml 解析出的完整字典

    Returns:
        v2 格式的完整配置字典，可直接写入 config.toml
    """
    new: Dict[str, Any] = {}
    migrated_fields: List[str] = []
    discarded_fields: List[str] = []

    # ── [plugin] ──────────────────────────────────────────────
    _set(new, "plugin.name", "麦麦绘卷")
    _set(new, "plugin.config_version", "3.6.0")
    _copy_if_exists(old_config, new, "plugin.enabled")
    migrated_fields.append("plugin.*")

    # ── [generation] ──────────────────────────────────────────
    _copy_if_exists(old_config, new, "generation.default_model")
    migrated_fields.append("generation.default_model")

    # ── [cache] ───────────────────────────────────────────────
    _copy_if_exists(old_config, new, "cache.enabled")
    _copy_if_exists(old_config, new, "cache.max_size")
    migrated_fields.append("cache.*")

    # ── [components] ──────────────────────────────────────────
    for field in [
        "enable_unified_generation",
        "enable_pic_command",
        "enable_pic_config",
        "enable_pic_style",
        "pic_command_model",
        "enable_debug_info",
        "enable_verbose_debug",
        "admin_users",
        "max_retries",
    ]:
        _copy_if_exists(old_config, new, f"components.{field}")
    migrated_fields.append("components.*")

    # ── [proxy] ───────────────────────────────────────────────
    _copy_if_exists(old_config, new, "proxy.enabled")
    _copy_if_exists(old_config, new, "proxy.url")
    _copy_if_exists(old_config, new, "proxy.timeout")
    migrated_fields.append("proxy.*")

    # ── [styles] ──────────────────────────────────────────────
    old_styles: Dict[str, Any] = old_config.get("styles", {})
    if old_styles:
        new_styles: Dict[str, Any] = {}
        for k, v in old_styles.items():
            if k == "hint":
                continue  # 丢弃 hint
            new_styles[k] = v
        if new_styles:
            new["styles"] = new_styles
    migrated_fields.append("styles.*")

    # ── [style_aliases] ──────────────────────────────────────
    old_aliases: Dict[str, Any] = old_config.get("style_aliases", {})
    if old_aliases:
        new_aliases: Dict[str, Any] = {}
        for k, v in old_aliases.items():
            if k == "hint":
                continue
            new_aliases[k] = v
        if new_aliases:
            new["style_aliases"] = new_aliases
    migrated_fields.append("style_aliases.*")

    # ── [selfie] ──────────────────────────────────────────────
    _copy_if_exists(old_config, new, "selfie.enabled")
    _copy_if_exists(old_config, new, "selfie.reference_image_path")
    _copy_if_exists(old_config, new, "selfie.prompt_prefix")
    _copy_if_exists(old_config, new, "selfie.negative_prompt")

    # v1 auto_selfie.selfie_style → v2 selfie.default_style
    v1_selfie_style: Optional[str] = _get(old_config, "auto_selfie.selfie_style")
    if v1_selfie_style:
        _set(new, "selfie.default_style", v1_selfie_style)
        migrated_fields.append("auto_selfie.selfie_style → selfie.default_style")
    else:
        _set(new, "selfie.default_style", "standard")

    # v2 新增字段
    _set(new, "selfie.schedule_enabled", True)

    # v1 丢弃字段
    discarded_fields.extend(
        [
            "selfie.negative_prompt_standard",
            "selfie.negative_prompt_mirror",
            "selfie.scene_standard",
            "selfie.scene_mirror",
        ]
    )
    migrated_fields.append("selfie.*")

    # ── [auto_recall] ────────────────────────────────────────
    _copy_if_exists(old_config, new, "auto_recall.enabled")
    migrated_fields.append("auto_recall.enabled")

    # ── [prompt_optimizer] ────────────────────────────────────
    _copy_if_exists(old_config, new, "prompt_optimizer.enabled")
    # v2 新增字段使用默认空值
    _set(new, "prompt_optimizer.custom_api_base_url", "")
    _set(new, "prompt_optimizer.custom_api_key", "")
    _set(new, "prompt_optimizer.custom_api_model", "")
    migrated_fields.append("prompt_optimizer.*")

    # ── [search_reference] ───────────────────────────────────
    _copy_if_exists(old_config, new, "search_reference.enabled")
    # v1 的 vision_api_key / vision_base_url / vision_model 在 v2 中已移除
    discarded_fields.extend(
        [
            "search_reference.vision_api_key",
            "search_reference.vision_base_url",
            "search_reference.vision_model",
        ]
    )
    # v2 新增字段使用默认值
    _set(new, "search_reference.character_only", True)
    _set(new, "search_reference.max_images_per_role", 3)
    _set(new, "search_reference.search_top_k", 6)
    _set(new, "search_reference.max_cache_size_mb", 100)
    _set(new, "search_reference.feature_boost_weight", 1.25)
    _set(
        new,
        "search_reference.vision_prompt",
        "请用中文详细描述这张图片中主要人物的特征是什么，纯粹描述即可。输出为一段平文本，总字数最多不超过120字。",
    )
    migrated_fields.append("search_reference.*")

    # ── [auto_selfie] ────────────────────────────────────────
    _copy_if_exists(old_config, new, "auto_selfie.enabled")

    # v1 model_id → v2 selfie_model
    v1_model_id: Optional[str] = _get(old_config, "auto_selfie.model_id")
    if v1_model_id:
        _set(new, "auto_selfie.selfie_model", v1_model_id)
        migrated_fields.append("auto_selfie.model_id → auto_selfie.selfie_model")
    else:
        _set(new, "auto_selfie.selfie_model", "model1")

    # v1 sleep_start_time → v2 quiet_hours_start
    v1_sleep_start: Optional[str] = _get(old_config, "auto_selfie.sleep_start_time")
    if v1_sleep_start:
        _set(new, "auto_selfie.quiet_hours_start", v1_sleep_start)
        migrated_fields.append("auto_selfie.sleep_start_time → auto_selfie.quiet_hours_start")
    else:
        _set(new, "auto_selfie.quiet_hours_start", "00:00")

    # v1 sleep_end_time → v2 quiet_hours_end
    v1_sleep_end: Optional[str] = _get(old_config, "auto_selfie.sleep_end_time")
    if v1_sleep_end:
        _set(new, "auto_selfie.quiet_hours_end", v1_sleep_end)
        migrated_fields.append("auto_selfie.sleep_end_time → auto_selfie.quiet_hours_end")
    else:
        _set(new, "auto_selfie.quiet_hours_end", "07:00")

    # v1 chat_id_list → v2 target_groups + target_users
    v1_chat_ids: Optional[List[str]] = _get(old_config, "auto_selfie.chat_id_list")
    if v1_chat_ids:
        groups, users = _parse_chat_id_list(v1_chat_ids)
        _set(new, "auto_selfie.target_groups", groups)
        _set(new, "auto_selfie.target_users", users)
        migrated_fields.append(f"auto_selfie.chat_id_list → target_groups({len(groups)}) + target_users({len(users)})")
    else:
        _set(new, "auto_selfie.target_groups", [])
        _set(new, "auto_selfie.target_users", [])

    # v1 use_replyer_for_ask → v2 caption_enabled（语义映射）
    v1_use_replyer: Optional[bool] = _get(old_config, "auto_selfie.use_replyer_for_ask")
    if v1_use_replyer is not None:
        _set(new, "auto_selfie.caption_enabled", v1_use_replyer)
        migrated_fields.append("auto_selfie.use_replyer_for_ask → auto_selfie.caption_enabled")
    else:
        _set(new, "auto_selfie.caption_enabled", True)

    # v2 新增字段使用默认值
    _set(new, "auto_selfie.interval_minutes", _get(old_config, "auto_selfie.interval_minutes", 120))
    _set(new, "auto_selfie.send_to_qzone", False)
    _set(new, "auto_selfie.send_to_chat", True)
    _set(new, "auto_selfie.persist_state", True)

    # v1 丢弃字段
    discarded_fields.extend(
        [
            "auto_selfie.schedule_times",
            "auto_selfie.sleep_mode_enabled",
            "auto_selfie.list_mode",
            "auto_selfie.schedule_min_entries",
            "auto_selfie.schedule_max_entries",
            "auto_selfie.enable_interval_supplement",
            "auto_selfie.interval_probability",
            "auto_selfie.enable_narrative",
            "auto_selfie.ask_message",
            "auto_selfie.caption_model_id",
            "auto_selfie.caption_types",
            "auto_selfie.caption_weights",
            "auto_selfie.enable_visual_summary",
            "auto_selfie.enable_visual_consistency_check",
            "auto_selfie.caption_persona_enabled",
            "auto_selfie.caption_persona_text",
            "auto_selfie.caption_reply_style",
            "auto_selfie.schedule_retention_days",
        ]
    )
    migrated_fields.append("auto_selfie.*")

    # ── [schedule]（v2 新增，从 v1 的 auto_selfie 语义迁移） ─
    # v1 schedule_persona_text → v2 schedule.schedule_identity
    v1_persona: Optional[str] = _get(old_config, "auto_selfie.schedule_persona_text")
    if v1_persona:
        _set(new, "schedule.schedule_identity", v1_persona)
        migrated_fields.append("auto_selfie.schedule_persona_text → schedule.schedule_identity")
    else:
        _set(new, "schedule.schedule_identity", "")

    # v1 schedule_lifestyle → v2 schedule.schedule_lifestyle
    v1_lifestyle: Optional[str] = _get(old_config, "auto_selfie.schedule_lifestyle")
    if v1_lifestyle:
        _set(new, "schedule.schedule_lifestyle", v1_lifestyle)
        migrated_fields.append("auto_selfie.schedule_lifestyle → schedule.schedule_lifestyle")
    else:
        _set(new, "schedule.schedule_lifestyle", "")

    # v1 schedule_generator_model → v2 schedule.model_id
    v1_sched_model: Optional[str] = _get(old_config, "auto_selfie.schedule_generator_model")
    if v1_sched_model:
        _set(new, "schedule.model_id", v1_sched_model)
        migrated_fields.append("auto_selfie.schedule_generator_model → schedule.model_id")
    else:
        _set(new, "schedule.model_id", "planner")

    # v2 新增 schedule 字段使用默认值
    _set(new, "schedule.auto_generate_enabled", True)
    _set(new, "schedule.auto_generate_time", "00:30")
    _set(new, "schedule.schedule_interest", "")
    _set(new, "schedule.schedule_history_days", 1)
    _set(new, "schedule.schedule_history_retention_days", -1)
    _set(new, "schedule.schedule_custom_prompt", "")
    _set(new, "schedule.schedule_multi_round", True)
    _set(new, "schedule.schedule_max_rounds", 2)
    _set(new, "schedule.schedule_quality_threshold", 0.8)

    # ── [schedule_inject]（v2 全新，使用默认值） ──────────────
    _set(new, "schedule_inject.enabled", True)
    _set(new, "schedule_inject.mode", "always")
    _set(new, "schedule_inject.min_messages", 5)
    _set(new, "schedule_inject.min_seconds", 300)
    _set(new, "schedule_inject.schedule_intent_enable", False)
    _set(new, "schedule_inject.schedule_context_cache_ttl_minutes", 30)
    _set(new, "schedule_inject.schedule_context_cache_max_turns", 10)

    # ── [wardrobe]（v2 全新，使用默认值） ─────────────────────
    _set(new, "wardrobe.enabled", True)
    _set(
        new,
        "wardrobe.daily_outfits",
        [
            "哥特洛丽塔",
            "宽松休闲装",
            "黑丝JK",
            "白丝JK",
            "连衣裙",
            "短裙",
            "机能风服装",
            "可爱服装",
        ],
    )
    _set(new, "wardrobe.auto_scene_change", True)
    _set(
        new,
        "wardrobe.custom_scenes",
        [
            "睡觉的时候穿可爱睡衣",
            "运动的时候穿运动服",
        ],
    )

    # ── [models] ──────────────────────────────────────────────
    new["models"] = _migrate_models(old_config)
    migrated_fields.append("models.*")

    # ── [logging]（v1 独有，v2 已移除） ───────────────────────
    discarded_fields.extend(["logging.level", "logging.prefix"])

    # ── 迁移报告 ──────────────────────────────────────────────
    model_count: int = len(new.get("models", {}))
    logger.info("=" * 60)
    logger.info("[迁移] v1 → v2 配置迁移完成！")
    logger.info("[迁移] 迁移了 %d 个模型配置", model_count)
    logger.info("[迁移] 迁移字段：%s", ", ".join(migrated_fields))
    if discarded_fields:
        logger.info(
            "[迁移] 丢弃的 v1 字段（v2 不再需要）：%s",
            ", ".join(discarded_fields),
        )
    logger.info("[迁移] 请检查 config.toml 确认迁移结果")
    logger.info("=" * 60)

    return new
