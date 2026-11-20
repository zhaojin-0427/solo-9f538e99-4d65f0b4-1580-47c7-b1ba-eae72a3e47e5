"""请求/响应模型。

年份用整数：公元后为正整数，公元前用负整数（如 -221 表示公元前 221 年）。
年代段为整年闭区间 [year_from, year_to]，一个层位可给多个区间，区间之间允许空档。
浮选部分：土袋按袋号 + 出土层位（layer/stratum）+ 原始体积登记；
浮选批登记批号、各袋取用毫升数与整批籽粒总数。
"""

from __future__ import annotations

from typing import Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class IntervalIn(BaseModel):
    year_from: int = Field(..., description="区间起始年（含），公元前用负数")
    year_to: int = Field(..., description="区间结束年（含），公元前用负数")

    @field_validator("year_to")
    @classmethod
    def _check_order(cls, value: int, info) -> int:
        start = info.data.get("year_from")
        if start is not None and start > value:
            raise ValueError("year_from 不得晚于 year_to（闭区间整年）")
        return value


class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="遗址名称")


class SiteOut(BaseModel):
    id: int
    name: str
    created: str


class StratumCreate(BaseModel):
    trench: str = Field(..., min_length=1, max_length=60, description="探方编号")
    layer_no: str = Field(..., min_length=1, max_length=60, description="层位编号（探方内唯一）")
    intervals: list[IntervalIn] = Field(
        ..., min_length=1, description="可能的沉积年代段，可多段、可有空档"
    )
    note: Optional[str] = Field(None, max_length=400)


class StratumOut(BaseModel):
    id: int
    site_id: int
    trench: str
    layer_no: str
    note: Optional[str]
    intervals: list[IntervalIn]
    created: str


class CorrelationCreate(BaseModel):
    stratum_a_id: int = Field(..., description="待对接层位 A")
    stratum_b_id: int = Field(..., description="待对接层位 B（须与 A 不同探方）")


class CorrelationOut(BaseModel):
    id: int
    site_id: int
    stratum_a_id: int
    stratum_b_id: int
    created: str


class SuperpositionCreate(BaseModel):
    lower_id: int = Field(..., description="下层层位 id")
    upper_id: int = Field(..., description="上层层位 id（不得与下层相同）")


class SuperpositionOut(BaseModel):
    id: int
    site_id: int
    lower_id: int
    upper_id: int
    created: str


class WitnessOut(BaseModel):
    kind: str
    record_id: int
    detail: str


class ContradictionOut(BaseModel):
    reason: str = Field(..., description="EMPTY_DOMAIN | GROUP_CYCLE | ORDER_CONFLICT")
    message: str
    witness: list[WitnessOut]


class GroupEdgeOut(BaseModel):
    from_group: int
    to_group: int
    edge_ids: list[int]


class GroupOut(BaseModel):
    group_id: int
    member_ids: list[int]
    domain: list[list[int]] = Field(..., description="组内共用的原始可能年代域（交集，含空档）")
    feasible_years: list[list[int]] = Field(
        ..., description="与所有其他层位约束可同时成立的全部年代段（含空档）"
    )
    earliest_year: Optional[int]
    latest_year: Optional[int]


class SolutionOut(BaseModel):
    feasible: bool
    version: int = Field(..., description="当前已完成提交的序号，每次成功写入 +1")
    groups: list[GroupOut]
    group_edges: list[GroupEdgeOut]
    group_of: dict[str, int]
    contradiction: Optional[ContradictionOut] = None
    earliest_assignment: Optional[dict[str, int]] = None
    latest_assignment: Optional[dict[str, int]] = None


class WriteResult(BaseModel):
    """所有写接口统一返回：操作对象 + 写入后的整站求解结果。"""

    record: dict
    solution: SolutionOut


# ---------- 浮选取样 ----------


class SampleBagCreate(BaseModel):
    """登记一袋土：遗址内袋号唯一，标明出土层位与原始体积（毫升）。"""

    model_config = ConfigDict(populate_by_name=True)

    bag_no: str = Field(
        ...,
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices(
            "bag_no", "bag_number", "bag_code", "sample_no", "sample_number"
        ),
        description="袋号（遗址内唯一）",
    )
    volume_ml: float = Field(
        ...,
        gt=0,
        validation_alias=AliasChoices(
            "volume_ml",
            "volume",
            "original_volume_ml",
            "original_volume",
            "sample_volume_ml",
            "sample_volume",
        ),
        description="原始体积（毫升），所有批次累计取用不得超过该值",
    )
    stratum_id: Optional[int] = Field(
        None,
        validation_alias=AliasChoices("stratum_id", "layer_id", "stratum"),
        description="出土层位 id（与 trench+layer_no 二选一）",
    )
    trench: Optional[str] = Field(
        None,
        min_length=1,
        max_length=60,
        validation_alias=AliasChoices("trench", "trench_no", "trench_code"),
        description="出土层位所在探方（配合 layer_no 使用）",
    )
    layer_no: Optional[str] = Field(
        None,
        min_length=1,
        max_length=60,
        validation_alias=AliasChoices(
            "layer_no", "layer", "horizon", "layer_code", "layer_number"
        ),
        description="出土层位编号（配合 trench 使用；layer 为同义写法）",
    )
    note: Optional[str] = Field(None, max_length=400)

    @model_validator(mode="after")
    def _check_location(self):
        has_trench = bool(self.trench)
        has_layer = bool(self.layer_no)
        if self.stratum_id is None:
            if not has_layer:
                raise ValueError(
                    "必须给出出土层位：stratum_id，或 trench 与 layer_no/layer，"
                    "或遗址内唯一的 layer_no"
                )
        elif has_trench or has_layer:
            raise ValueError(
                "stratum_id 与 trench/layer_no 只可给出一种出土层位定位方式"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _accept_layer_shorthand(cls, data):
        """允许把出土层位写成 {"layer": ...} 或 {"horizon": ...}。"""
        if isinstance(data, dict):
            data = dict(data)
            if not data.get("layer_no"):
                for key in ("layer", "horizon", "layer_code", "layer_number"):
                    if data.get(key):
                        data["layer_no"] = data[key]
                        break
        return data

    @field_validator("bag_no")
    @classmethod
    def _strip_bag_no(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("袋号不能为空")
        return value


class SampleBagOut(BaseModel):
    id: int
    site_id: int
    bag_no: str
    stratum_id: int
    volume_ml: float
    used_ml: float
    remaining_ml: float
    batch_count: int
    note: Optional[str]
    created: str
    group_id: Optional[int] = None


class BatchItemIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bag_id: Optional[int] = Field(None, description="土袋 id（与 bag_no 二选一）")
    bag_no: Optional[str] = Field(
        None,
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices(
            "bag_no", "bag_number", "bag_code", "sample_no", "sample_number"
        ),
        description="土袋袋号（与 bag_id 二选一）",
    )
    used_ml: float = Field(
        ...,
        gt=0,
        validation_alias=AliasChoices(
            "used_ml", "used_volume_ml", "used_volume", "volume_ml", "volume"
        ),
        description="本批实际取用的毫升数（可小于原始体积）",
    )

    @model_validator(mode="after")
    def _check_bag_ref(self):
        if self.bag_id is None and not self.bag_no:
            raise ValueError("每个取用明细必须给出 bag_id 或 bag_no")
        if self.bag_id is not None and self.bag_no:
            raise ValueError("bag_id 与 bag_no 只可给出一个")
        return self


class FlotationBatchCreate(BaseModel):
    """登记浮选批：批号 + 各袋取用毫升数 + 整批籽粒总数。"""

    batch_no: str = Field(
        ...,
        min_length=1,
        max_length=80,
        validation_alias=AliasChoices("batch_no", "batch_number", "flotation_no"),
        description="浮选批号（遗址内唯一）",
    )
    seed_count: int = Field(
        ...,
        ge=0,
        validation_alias=AliasChoices(
            "seed_count", "seed_total", "seeds", "grains", "grain_count"
        ),
        description="整批筛出的籽粒总数",
    )
    items: list[BatchItemIn] = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("items", "bags", "samples"),
        description="本批所用土袋及各自取用毫升数",
    )
    note: Optional[str] = Field(None, max_length=400)

    @field_validator("batch_no")
    @classmethod
    def _strip_batch_no(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("批号不能为空")
        return value

    @model_validator(mode="after")
    def _check_unique_items(self):
        seen_ids: set[int] = set()
        seen_nos: set[str] = set()
        for item in self.items:
            if item.bag_id is not None:
                if item.bag_id in seen_ids:
                    raise ValueError(f"同一批中土袋 id {item.bag_id} 重复登记")
                seen_ids.add(item.bag_id)
            else:
                key = item.bag_no.strip()
                if key in seen_nos:
                    raise ValueError(f"同一批中袋号 {key!r} 重复登记")
                seen_nos.add(key)
        return self


class FlotationBatchOut(BaseModel):
    id: int
    site_id: int
    batch_no: str
    seed_count: int
    total_used_ml: float
    note: Optional[str]
    created: str
    items: list[dict]


class FlotationBatchResult(BaseModel):
    """写入成功当次返回：批记录、所用土袋的最新余量与整站浮选报告。"""

    record: FlotationBatchOut
    bags: list[SampleBagOut]
    report: dict


class GroupFlotationOut(BaseModel):
    group_id: int
    stratum_ids: list[int]
    strata: list[dict]
    bag_ids: list[int]
    bag_count: int
    volume_ml: float
    total_seed_count: int
    used_ml: float
    density_used_ml: float
    seeds_per_liter: Optional[float]
    batch_count: int
    density_batch_count: int
    batches: list[dict]
    mixed_batches: list[dict]


class FlotationReportOut(BaseModel):
    site_id: int
    version: int
    feasible: bool
    groups: list[GroupFlotationOut]
    mixed_batches: list[dict]
    totals: dict
