"""历史产物导入。

`output/` 下那些 `investigation_*.json` / `*factcheck*.json` 是改造前唯一的
历史记录，不导入就等于改造把历史全丢了。

**它们不是 A 级数据**：`Evidence.content` 当年被截到 160 字，正文快照从来没存过。
所以导入的 run 一律标 `quality='degraded'`，不假装和新数据等价——
这个标记会出现在 `--runs` 和 API 里，避免日后拿它当审计依据。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent.schemas import Investigation
from factcheck.schemas import FactCheckReport

from .repositories import RunStore


def _topic_of(raw: dict, fallback: str) -> str:
    for key in ("topic", "title"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value[:80]
    return fallback


def import_investigation(store: RunStore, path: str | Path) -> str:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    inv = Investigation.model_validate(raw)
    try:
        started = inv.generated_at.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        started = None

    run_id = store.create_run(
        inv.topic,
        trigger="import",
        quality="degraded",
        mode=inv.mode,
        status="running",
        started_at=started,
        config={"source_file": path.name, "imported_at": datetime.now().isoformat(timespec="seconds")},
    )
    stage_id = store.begin_stage(run_id, "investigate")
    # 不传 posts：历史产物里没有原始帖子，只能从 Evidence 的截断摘录建文档
    store.save_investigation(inv, run_id, stage_id)
    store.end_stage(stage_id, "succeeded")
    store.finish_run(run_id, "succeeded", stop_reason=inv.stop_reason)
    return run_id


def import_factcheck(store: RunStore, path: str | Path) -> str:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    report = FactCheckReport.model_validate(raw)
    try:
        started = report.checked_at.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        started = None

    run_id = store.create_run(
        _topic_of(raw, path.stem),
        trigger="import",
        quality="degraded",
        status="running",
        started_at=started,
        config={"source_file": path.name, "imported_at": datetime.now().isoformat(timespec="seconds")},
    )
    stage_id = store.begin_stage(run_id, "factcheck")
    # 报告里已经带了校验结果，原样写入；不重新联网校验（那是重跑，不是导入）
    checked = any(s.http_status or s.reachable or s.quote_found is not None for s in report.sources)
    store.save_factcheck(report, run_id, stage_id, checked=checked)
    store.end_stage(stage_id, "succeeded")
    store.finish_run(run_id, "succeeded")
    return run_id


def import_handoff(store: RunStore, path: str | Path) -> str:
    """导入 handoff 产物（待核查清单）。

    它的结构和核查报告长得很像（都有 claims / sources），区别是**还没有结论**：
    说法一律是「待核查」，也没有 checked_at。所以这里按 checked=False 落库，
    不写 source_check——那批数据从来没被校验过。
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    report = FactCheckReport.model_validate(raw)
    run_id = store.create_run(
        _topic_of(raw, path.stem),
        trigger="import",
        quality="degraded",
        status="running",
        config={
            "source_file": path.name,
            "kind": "handoff",
            "source_run_id": raw.get("source_run_id", ""),
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    stage_id = store.begin_stage(run_id, "handoff")
    store.save_artifact(run_id, stage_id, "claims_json", path.read_text(encoding="utf-8"))
    store.save_factcheck(report, run_id, stage_id, checked=False)
    store.end_stage(stage_id, "succeeded")
    store.finish_run(run_id, "succeeded")
    return run_id


def import_file(store: RunStore, path: str | Path) -> tuple[str, str] | None:
    """按内容判断类型再导入。返回 (run_id, 类型)；无法识别返回 None。

    判据顺序有讲究：`claims_handoff.json` 和核查报告都带 claims/sources，
    唯一可靠的区分是核查报告有 `checked_at`（它真的跑过一遍校验）。
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if "checked_at" in raw and "claims" in raw:
        return import_factcheck(store, path), "factcheck"
    if "claims" in raw:
        return import_handoff(store, path), "handoff"
    if "events" in raw:
        return import_investigation(store, path), "investigate"
    return None


def import_dir(store: RunStore, directory: str | Path, log=print) -> list[tuple[str, str, str]]:
    """导入目录下所有历史产物。返回 [(run_id, 类型, 文件名)]。

    同一个文件重复导入会产生新的 run（而不是报错），因为"导了两次"本身
    就是要留痕的事实；需要去重时按 config_json.source_file 查。
    """
    directory = Path(directory)
    results: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("day1_"):
            # 原始帖子没法挂到某次调查上（不知道哪次运行用过它们），
            # 强行导入只会造出一批孤立的 run，宁可明确跳过并说明
            skipped.append(path.name)
            continue
        got = import_file(store, path)
        if got is None:
            skipped.append(path.name)
            continue
        run_id, kind = got
        results.append((run_id, kind, path.name))
        log(f"  导入 {path.name} -> {run_id}（{kind}，degraded）")
    if skipped:
        log(f"  跳过 {len(skipped)} 个文件：{'、'.join(skipped[:6])}"
            + ("……" if len(skipped) > 6 else ""))
    return results
