"""一次运行的上下文：阶段执行器需要的一切，但不包含"怎么跑"。

刻意做得很薄。`workflow/*` 里的执行器只接受它，因此同一份代码既能被
`main.py`（命令行）调用，也能被 `worker.py`（队列消费者）调用——
两条路如果各写一份，迟早会出现"API 跑出来的报告和命令行不一样"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from store.db import PROJECT_ROOT
from store.repositories import RunStore

OUTPUT_ROOT = PROJECT_ROOT / "output"


def out_dir_for(run_id: str, root: str | Path | None = None) -> Path:
    """一次运行独占一个输出目录。

    命令行时代所有运行都往 `output/` 里写 `investigation.json`，后一次覆盖前一次；
    队列化之后同时排着多条调查，再用固定文件名就是必然的串档。
    """
    return Path(root or OUTPUT_ROOT) / "runs" / run_id


@dataclass
class RunContext:
    #: `--no-db` 时是 None：文件产物照写，执行器内部据此跳过入库
    store: RunStore | None
    run_id: str = ""
    stage_id: str = ""
    job_id: str = ""
    out_dir: Path = field(default_factory=lambda: OUTPUT_ROOT)
    log: Callable[[str], None] = print
    #: 返回非空字符串表示"该停了"，字符串会作为停止原因写进报告
    should_stop: Callable[[], str | None] | None = None
    #: 队列任务自带的参数（topic / seed_keywords / options / plan）
    payload: dict = field(default_factory=dict)

    def mkdir(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir
