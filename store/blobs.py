"""内容寻址存储（CAS）：`artifacts/<sha256[:2]>/<sha256>`。

为什么不用数据库 TEXT 存全部：
- 帖子正文、提示词、模型原始输出都在 10KB~1MB 量级，塞进 SQLite 会让
  备份和查询一起变慢，而这些内容几乎只按整份读取；
- 内容寻址天然去重：同一条帖子被抓 5 次只占一份空间。

代价是有了数据库之外的第二个存储位置，所以备份必须同时覆盖 `data/` 和
`artifacts/`，只备份 .db 会得到一堆指向空文件的 `text_ref`。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: 小于这个字节数直接存 DB 的 TEXT 列，避免为几百字开一个文件
INLINE_LIMIT = 4096


class BlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_of(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_of(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            # 先写临时文件再 rename：中断只会留下一个 .tmp，不会留下半截正文
            # 被当成完整内容读出来
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest, len(data)

    def put_text(self, text: str) -> tuple[str | None, str, int]:
        """返回 (text_ref, content_hash, bytes)。

        `text_ref` 为 None 表示内容已内联在 DB，不需要读文件。
        """
        data = (text or "").encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if len(data) <= INLINE_LIMIT:
            return None, digest, len(data)
        self.put(data)
        return digest, digest, len(data)

    def get_bytes(self, digest: str) -> bytes:
        return self.path_of(digest).read_bytes()

    def get_text(self, digest: str) -> str:
        return self.get_bytes(digest).decode("utf-8", errors="replace")

    def exists(self, digest: str) -> bool:
        return bool(digest) and self.path_of(digest).exists()

    def stats(self) -> dict[str, int]:
        files = list(self.root.rglob("*")) if self.root.exists() else []
        blobs = [p for p in files if p.is_file() and not p.name.endswith(".tmp")]
        return {"blobs": len(blobs), "bytes": sum(p.stat().st_size for p in blobs)}
