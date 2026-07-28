"""default_tag 绑定解析 — 按 AGENTCLAW_DEFAULT_TAG 查找评测沙箱 binding_id。

从 DeviceServiceRouter 中提取，保持 router 在 1000 行上限内。
"""

from agentclaw.community.core.devices.protocols import BotQueryProtocol
from agentclaw.community.core.devices.repository.protocol import (
    DeviceBindingRepository,
)
from agentclaw.community.log import get_logger
from agentclaw.community.utils import env_utils

logger = get_logger()

# 评测沙箱环境标记键，与 device_props 中的键名一致
DEFAULT_ENV_TAG_KEY = "AGENTCLAW_DEFAULT_TAG"


def resolve_binding_id_by_default_tag(
    *,
    bot_id: str,
    default_tag: str,
    bot_query: BotQueryProtocol,
    repo: DeviceBindingRepository,
) -> int:
    """按 default_tag 在绑定表中查找评测沙箱的 binding_id。

    查找路径：
    1. 通过 BotQueryProtocol 获取 bot 的 owner_id / entity_type
    2. 通过 DeviceBindingRepository.get_active_bindings_by_entity 查该实体下所有 ACTIVE 绑定
    3. 按 device_props[bot_id] == bot_id 过滤本 bot 的绑定（向后兼容：无 bot_id 字段时不过滤）
    4. 按 device_props[AGENTCLAW_DEFAULT_TAG] == default_tag 精确匹配
    5. 无精确匹配时取第一个含 AGENTCLAW_DEFAULT_TAG 的绑定并打 warning

    Raises:
        DeviceNotBoundError: bot 不存在或无匹配的评测沙箱绑定
    """
    from agentclaw.community.core.devices.services.device_context import DeviceNotBoundError

    bot_info = bot_query.get_by_id(bot_id)
    if not bot_info:
        raise DeviceNotBoundError(
            f"Bot {bot_id!r} 不存在，无法解析 default_tag 绑定"
        )

    owner_id = bot_info.get("owner_id", "")
    entity_type = bot_info.get("entity_type", "staff")
    env = env_utils.get_current_env()

    bindings = repo.get_active_bindings_by_entity(
        entity_id=owner_id,
        entity_type=entity_type,
        env=env,
    )

    # 按 bot_id 过滤：有 bot_id 字段的绑定必须匹配；无 bot_id 字段（存量数据）仅全量回退
    matched_binds = []      # bot_id 匹配的绑定
    has_bot_id_field = False
    for b in bindings:
        props = b.device_props or {}
        if "bot_id" in props:
            has_bot_id_field = True
            if props["bot_id"] == bot_id:
                matched_binds.append(b)

    # 决策候选集：
    # - 至少有一条带 bot_id 字段的绑定 → 仅使用 bot_id 精确匹配的结果
    # - 全部绑定都没有 bot_id 字段（存量数据）→ 使用全部绑定（向后兼容）
    candidate_bindings = matched_binds if has_bot_id_field else bindings

    # 精确匹配 default_tag
    matched = None
    fallback = None
    for b in candidate_bindings:
        props = b.device_props or {}
        tag_value = props.get(DEFAULT_ENV_TAG_KEY)
        if tag_value == default_tag:
            matched = b
            break
        if tag_value is not None and fallback is None:
            fallback = b

    result = matched or fallback
    if result is None:
        raise DeviceNotBoundError(
            f"未找到 bot_id={bot_id!r} 的 default_tag={default_tag!r} 评测沙箱绑定"
        )

    if fallback and not matched:
        logger.warning(
            "[DefaultTagResolver] default_tag=%s 无精确匹配，"
            "回退到 binding_id=%d (tag=%s, bot_id=%s)",
            default_tag, result.id,
            (result.device_props or {}).get(DEFAULT_ENV_TAG_KEY),
            (result.device_props or {}).get("bot_id", "<legacy>"),
        )

    return result.id