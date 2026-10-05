"""请求 / 响应数据模型与输入校验。"""
import math
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# 温度的合理物理范围（°C），超出即视为无效温度
MIN_TEMPERATURE = -273.15
MAX_TEMPERATURE = 1000.0

# 事件号允许字母、数字、点、下划线、连字符，且以字母或数字开头，
# 保证事件号可安全地出现在 URL 路径中（更正 / 撤销接口）。
EVENT_NO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def _validate_temperature(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("temperature must be a finite number")
    if not (MIN_TEMPERATURE <= v <= MAX_TEMPERATURE):
        raise ValueError(
            f"temperature must be within [{MIN_TEMPERATURE}, {MAX_TEMPERATURE}] °C"
        )
    return v


class ReadingIn(BaseModel):
    """提交读数请求体。"""

    event_no: str = Field(pattern=EVENT_NO_PATTERN)
    sampled_at: datetime
    temperature: float

    @field_validator("event_no", mode="before")
    @classmethod
    def coerce_event_no(cls, v):
        # 允许客户端以 JSON 数字形式提交事件号
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = format(v, "g")
        return v

    @field_validator("temperature")
    @classmethod
    def temperature_plausible(cls, v: float) -> float:
        return _validate_temperature(v)


class CorrectionIn(BaseModel):
    """更正读数请求体（按事件号整体替换采样时间与温度）。"""

    sampled_at: datetime
    temperature: float

    @field_validator("temperature")
    @classmethod
    def temperature_plausible(cls, v: float) -> float:
        return _validate_temperature(v)


class Interval(BaseModel):
    """超温区间。以起点读数的事件号为标识；未结束时终点字段为空。"""

    start_event_no: str
    start_sampled_at: str
    start_temperature: float
    end_event_no: Optional[str]
    end_sampled_at: Optional[str]
    end_temperature: Optional[float]
    reading_count: int = Field(description="区间内连续超限读数条数")
    max_temperature: float = Field(description="区间内最高温度")


class IntervalChanges(BaseModel):
    """本次操作引起的区间差异。"""

    added: List[Interval] = Field(description="本次新增的区间")
    changed: List[Interval] = Field(description="起点不变但内容变化的区间（变更后版本）")
    invalidated: List[Interval] = Field(description="本次失效的区间（变更前版本）")


class IntervalsResponse(BaseModel):
    """写操作与区间查询的统一响应：当前全部区间 + 本次差异。"""

    device_id: str
    limit: float
    intervals: List[Interval] = Field(description="该设备当前全部有效超温区间")
    changes: IntervalChanges


class ReadingOut(BaseModel):
    event_no: str
    sampled_at: str
    temperature: float
    revoked: bool


class ReadingsResponse(BaseModel):
    device_id: str
    readings: List[ReadingOut]
