"""活动模板库。

第一版只需要几种轻量活动，且刻意避开任何医疗、运动处方类表述：
文案只说「做什么」，不说「有什么疗效」。
"""

from dataclasses import dataclass

from .errors import AppError


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str


CATALOG: tuple[Template, ...] = (
    Template("walk", "起身走动", "离开座位走动两三分钟，顺便去接杯水再回来。"),
    Template("water", "去接杯水", "站起来走到茶水间，路上看一眼窗外，让眼睛也歇一下。"),
    Template("shoulder", "肩颈放松", "坐直，肩膀向后缓慢绕几圈，再轻轻转一下脖子。"),
    Template("stretch", "站立伸展", "站起来双手向上伸展，停十几秒，再慢慢放下。"),
)

BY_KEY = {item.key: item for item in CATALOG}
DEFAULT_KEY = CATALOG[0].key


def template_for(key: str | None) -> Template:
    if not key:
        return CATALOG[0]
    found = BY_KEY.get(key)
    if found is None:
        raise AppError(
            400,
            "INVALID_ACTIVITY_TYPE",
            f"不支持的活动类型：{key}",
            field="activity_type",
        )
    return found


def rotating(index: int) -> Template:
    return CATALOG[index % len(CATALOG)]
