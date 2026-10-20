"""Read-only project tools; disk access requires an explicitly linked root."""

import json

from sqlalchemy import select

from waku.storage.models import ContextNode, Project


def file_tools(sessions, Arguments, ToolSpec):
    from pydantic import Field

    class ListArgs(Arguments):
        offset: int = Field(default=0, ge=0, le=500)
        limit: int = Field(default=100, ge=1, le=100)

    class ReadArgs(Arguments):
        path: str = Field(min_length=1, max_length=240)
        start_line: int = Field(default=1, ge=1)
        max_lines: int = Field(default=100, ge=1, le=200)

    class SearchArgs(Arguments):
        query: str = Field(min_length=1, max_length=200)

    def scope(context):
        with sessions() as session:
            project = session.get(Project, context.project_id)
            if not project or project.user_id != context.user_id:
                raise ValueError("project unavailable")
            return project.root_path

    def files(context):
        from waku.memory.postgres import root_uri
        from waku.server.workspace import folder, manifest

        root = scope(context)
        if root:
            return manifest(folder(root))["files"]
        prefix = root_uri(context.user_id, context.project_id) + "/resources/"
        with sessions() as session:
            rows = session.scalars(
                select(ContextNode)
                .where(
                    ContextNode.project_id == context.project_id,
                    ContextNode.kind == "resource",
                    ContextNode.status == "active",
                )
                .order_by(ContextNode.uri)
                .limit(500)
            ).all()
            from urllib.parse import unquote

            return [
                {"path": unquote(n.uri[len(prefix) :]), "size": len(n.content.encode())}
                for n in rows
                if n.uri.startswith(prefix)
            ]

    def content(path, context):
        from waku.memory.postgres import root_uri
        from waku.server.schemas import DocumentInput
        from waku.server.workspace import folder, resolve_file

        DocumentInput.safe_path(path)
        root = scope(context)
        if root:
            return resolve_file(folder(root), path).read_text(encoding="utf-8-sig")
        from urllib.parse import quote

        uri = root_uri(context.user_id, context.project_id) + "/resources/" + quote(path, safe="/")
        with sessions() as session:
            node = session.scalar(
                select(ContextNode).where(
                    ContextNode.project_id == context.project_id,
                    ContextNode.uri == uri,
                    ContextNode.status == "active",
                )
            )
            if node is None:
                raise ValueError("file unavailable")
            return node.content

    def read(args, ctx, _op):
        lines = content(args["path"], ctx).splitlines()
        first, count = args["start_line"] - 1, args["max_lines"]
        selected = []
        line_truncated = False
        for i, line in enumerate(lines[first : first + count]):
            candidate = f"{i + first + 1}: {line}"
            if (
                len(json.dumps("\n".join([*selected, candidate]), ensure_ascii=False).encode())
                > 4400
            ):
                if not selected:
                    while len(json.dumps(candidate, ensure_ascii=False).encode()) > 4400:
                        candidate = candidate[: len(candidate) // 2]
                    selected.append(candidate)
                    line_truncated = True
                break
            selected.append(candidate)
        next_line = first + len(selected) + 1
        return {
            "path": args["path"],
            "content": "\n".join(selected),
            "total_lines": len(lines),
            "truncated": next_line <= len(lines) or line_truncated,
            "next_line": next_line if next_line <= len(lines) else None,
            "line_truncated": line_truncated,
        }

    def search(args, ctx, _op):
        matches = []
        scanned = 0
        for item in files(ctx):
            scanned += 1
            try:
                text = content(item["path"], ctx)
            except (ValueError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if args["query"].lower() in line.lower():
                    match = {"path": item["path"], "line": i, "text": line[:180]}
                    if (
                        len(json.dumps([*matches, match], ensure_ascii=False).encode()) > 4400
                        or len(matches) == 30
                    ):
                        return {"matches": matches, "truncated": True, "scanned_files": scanned}
                    matches.append(match)
        return {"matches": matches, "truncated": False, "scanned_files": scanned}

    def listing(args, ctx, _op):
        rows = files(ctx)
        selected = []
        for item in rows[args["offset"] : args["offset"] + args["limit"]]:
            if len(json.dumps([*selected, item], ensure_ascii=False).encode()) > 4400:
                break
            selected.append(item)
        end = args["offset"] + len(selected)
        return {
            "files": selected,
            "has_more": end < len(rows),
            "next_offset": end if end < len(rows) else None,
            "listed_total": len(rows),
        }

    return [
        ToolSpec(
            "list_project_files",
            "List readable files in the linked project or imported snapshot (paginated)",
            ListArgs,
            listing,
        ),
        ToolSpec(
            "read_project_file", "Read bounded lines from a relative project file", ReadArgs, read
        ),
        ToolSpec(
            "search_project_files",
            "Search a literal string in readable project files",
            SearchArgs,
            search,
        ),
    ]
