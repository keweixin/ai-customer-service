#!/usr/bin/env python3
"""知识库上传脚本。

读取本地文本文件,调用 RagService.upload_document 完成切块 + 向量化 + 入库,
并打印切块数与文档 ID,便于批量初始化知识库。

用法:
    python scripts/upload_knowledge.py --file knowledge/退货政策.txt --title "退货政策"
    python scripts/upload_knowledge.py --file docs/faq.md --title "常见问题" --source-type markdown
    python scripts/upload_knowledge.py --file a.txt --title A --file b.txt --title B  # 批量
    python scripts/upload_knowledge.py --help

前置条件:
    - 数据库已初始化(pgvector 扩展 + 表已建,见 init_db.py)。
    - 已 pip install -e ".[dev]"。
    - .env 已配置 ARK_API_KEY / ARK_EMBEDDING_MODEL(RAG 向量化需要)。

注意:
    脚本走本地 RagService(直连数据库 + 直调方舟 embedding),
    也可改成调 HTTP 接口 POST /api/v1/knowledge/documents(需 admin token)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 把项目 src 加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。

    支持单文件上传(--file + --title)和批量上传(多组 --file/--title)。
    """
    parser = argparse.ArgumentParser(
        prog="upload_knowledge",
        description="上传知识库文档:读取文件 -> RagService 切块+向量化+入库。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--file",
        action="append",
        required=True,
        help="要上传的文件路径(.txt/.md);可重复指定以批量上传",
    )
    parser.add_argument(
        "--title",
        action="append",
        required=True,
        help="文档标题;与 --file 按顺序一一对应,可重复指定",
    )
    parser.add_argument(
        "--source-type",
        default="text",
        choices=["text", "markdown"],
        help="源文档类型,影响切块策略",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只切块不入库(预览切块数,不调 embedding,不写数据库)",
    )
    return parser.parse_args()


def _validate_pairs(args: argparse.Namespace) -> list[tuple[Path, str]]:
    """校验 --file 与 --title 数量一致且文件存在,返回 (path, title) 列表。"""
    if len(args.file) != len(args.title):
        print(
            f"[错误] --file({len(args.file)}) 与 --title({len(args.title)}) 数量不一致",
            file=sys.stderr,
        )
        sys.exit(2)

    pairs: list[tuple[Path, str]] = []
    for f, t in zip(args.file, args.title):
        path = Path(f)
        if not path.exists():
            print(f"[错误] 文件不存在: {path}", file=sys.stderr)
            sys.exit(2)
        if not path.is_file():
            print(f"[错误] 不是文件: {path}", file=sys.stderr)
            sys.exit(2)
        pairs.append((path, t))
    return pairs


def _read_file(path: Path) -> str:
    """读取文件内容,自动尝试 UTF-8 -> GBK 兼容中文 Windows。"""
    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    print(f"[错误] 无法解码文件(尝试 utf-8/gbk/utf-16 均失败): {path}", file=sys.stderr)
    sys.exit(1)


async def _upload_one(
    path: Path, title: str, source_type: str, dry_run: bool
) -> None:
    """上传单个文件并打印结果。"""
    content = _read_file(path)
    print(f"\n上传: {path} (标题: {title}, 类型: {source_type}, {len(content)} 字符)")

    if dry_run:
        # 预览模式:只切块,不入库不调 embedding
        try:
            from app.services.rag_service import RagService  # noqa: F401
            from app.services.rag_service import chunk_text
        except ImportError:
            # RagService / chunk_text 尚未实现时,用内置简易切块做预览
            chunks = _simple_chunk(content)
        else:
            chunks = chunk_text(content, source_type=source_type)
        print(f"  [dry-run] 预计切块数: {len(chunks)}")
        for i, c in enumerate(chunks[:3]):
            preview = c[:80].replace("\n", " ")
            print(f"    chunk[{i}]: {preview}...")
        if len(chunks) > 3:
            print(f"    ... 共 {len(chunks)} 块")
        return

    # 正式上传:走 RagService
    try:
        from app.services.rag_service import RagService
        from app.config import get_settings
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    except ImportError as e:
        print(f"  [错误] 依赖未就绪: {e}", file=sys.stderr)
        print("  请确认已实现 src/app/services/rag_service.py 并 pip install -e .", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    engine = create_async_engine(settings.database.url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with SessionLocal() as session:
            rag = RagService(session)
            result = await rag.upload_document(
                content=content,
                title=title,
                source_type=source_type,
            )
            print(f"  [成功] document_id={result['id']} chunks_count={result['chunks_count']}")
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 上传出错: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
    finally:
        await engine.dispose()


def _simple_chunk(content: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """简易切块(dry-run 预览用,与正式 RagService 切块可能不同)。

    按字符硬切 + overlap,不做语义边界处理。
    """
    if not content:
        return []
    chunks: list[str] = []
    step = chunk_size - overlap
    i = 0
    while i < len(content):
        chunks.append(content[i : i + chunk_size])
        if i + chunk_size >= len(content):
            break
        i += step
    return chunks


async def _amain(args: argparse.Namespace) -> None:
    """异步主流程。"""
    pairs = _validate_pairs(args)
    print(f"准备上传 {len(pairs)} 个文档(source_type={args.source_type}, dry_run={args.dry_run})")

    for path, title in pairs:
        await _upload_one(path, title, args.source_type, args.dry_run)

    print("\n上传流程结束。")


def main() -> None:
    """脚本入口。"""
    args = _parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"\n[错误] 运行失败: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
