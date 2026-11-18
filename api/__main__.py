"""`python -m api`：起一个只读服务。

    uv run python -m api                 # http://127.0.0.1:8000/docs
    uv run python -m api --port 8010
    uv run python -m api --db data/workflow.db
    uv run python -m api --reload        # 开发用

默认只监听 127.0.0.1：这里能读到未公开的调查中间产物，不该默认暴露到局域网。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微博舆情调查 Agent 只读 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 data/workflow.db）")
    parser.add_argument("--reload", action="store_true", help="代码变更自动重启（开发用）")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    # 通过环境变量传库路径：--reload 时 uvicorn 会另起子进程，只有环境能带过去
    if args.db:
        os.environ["WEIBO_DB_PATH"] = str(Path(args.db).resolve())

    import uvicorn

    print(f"只读 API：http://{args.host}:{args.port}/docs")
    print(f"数据库  ：{os.environ.get('WEIBO_DB_PATH') or 'data/workflow.db（默认）'}")
    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
