"""写接口的请求体。

校验放在这里而不是路由里，是因为"一条 curl 就能起一次调查"意味着参数可能来自
任何人——预算不设上限的话，一个手误的 `max_pages: 5000` 会让 worker 在微博上
翻两个小时，还会把账号翻进风控。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Stage = Literal["investigate", "handoff", "factcheck"]

#: 阶段必须按这个顺序跑：handoff 要读调查产物，factcheck 要读核查清单
STAGE_ORDER: tuple[str, ...] = ("investigate", "handoff", "factcheck")


class RunOptions(BaseModel):
    rounds: int = Field(3, ge=1, le=6, description="搜索总轮次上限（含第一轮）")
    max_searches: int = Field(8, ge=1, le=30)
    max_pages: int = Field(30, ge=1, le=200)
    max_minutes: float = Field(10.0, gt=0, le=60)
    max_posts: int = Field(30, ge=5, le=200, description="每个关键词保留的帖子数")
    detail_checks: int = Field(2, ge=0, le=10)
    min_evidence: int = Field(3, ge=1, le=10)
    semantic: bool | None = Field(None, description="核查阶段是否跑语义判定；null=有模型就跑")
    semantic_limit: int = Field(20, ge=0, le=200)
    check: bool = Field(True, description="核查阶段是否做链接与引文机检")
    headless: bool = Field(True, description="采集时是否无头；false 需本机有人能登录")


class RunCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=120, description="调查主题，也是搜索首词")
    seed_keywords: list[str] = Field(default_factory=list, max_length=10)
    stages: list[Stage] = Field(
        default_factory=lambda: ["investigate", "handoff"],
        description="要跑的阶段，必须按 investigate → handoff → factcheck 顺序",
    )
    options: RunOptions = Field(default_factory=RunOptions)
    priority: int = Field(0, ge=-10, le=10, description="越大越先被领取")

    @field_validator("topic")
    @classmethod
    def _topic_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("topic 不能为空")
        return value

    @field_validator("seed_keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        return [k.strip() for k in value if k and k.strip()]

    @model_validator(mode="after")
    def _stages_are_a_proper_chain(self) -> "RunCreate":
        if not self.stages:
            raise ValueError("stages 不能为空")
        if len(set(self.stages)) != len(self.stages):
            raise ValueError("stages 有重复项")
        order = [STAGE_ORDER.index(s) for s in self.stages]
        if order != sorted(order):
            raise ValueError("stages 必须按 investigate → handoff → factcheck 的顺序")
        if "factcheck" in self.stages and "handoff" not in self.stages:
            raise ValueError("factcheck 需要 handoff 先产出核查清单，请一并列入 stages")
        return self


class RetryRequest(BaseModel):
    stage: Stage | None = Field(None, description="要重跑的阶段；不填则取最近一个失败阶段")
    priority: int = Field(5, ge=-10, le=10, description="重跑默认插队：人工触发的比排队的急")
