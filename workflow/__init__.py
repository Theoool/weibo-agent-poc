"""阶段执行器：一次运行的四个阶段里，可被命令行和队列共用的三个。

`collect`（纯浏览器链路验证）不在这里——它不产出任何持久化语义数据，
是一次性的接线检查，进队列没有意义。
"""

from __future__ import annotations

from .context import OUTPUT_ROOT, RunContext, out_dir_for
from .factcheck import FactcheckOutcome, execute_factcheck
from .handoff import HandoffOutcome, execute_handoff
from .investigate import InvestigateOutcome, execute_investigate
from .sinks import build_sinks

__all__ = [
    "OUTPUT_ROOT",
    "FactcheckOutcome",
    "HandoffOutcome",
    "InvestigateOutcome",
    "RunContext",
    "build_sinks",
    "execute_factcheck",
    "execute_handoff",
    "execute_investigate",
    "out_dir_for",
]
