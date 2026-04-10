"""
MCP (Model Context Protocol) server for WeChat Article to Markdown.

Exposes tools for external AI agents to submit download tasks and query status.
Shares config and task_queue with the Flask web server when started together.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_config = None
_task_queue = None

server = Server("wechat-article-to-markdown")


def init(config, task_queue):
    global _config, _task_queue
    _config = config
    _task_queue = task_queue


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="add_download_task",
            description="Add a WeChat article URL to the download queue. Returns existing article if already downloaded, or retries if previously failed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "WeChat article URL (mp.weixin.qq.com)"},
                    "passwd": {"type": "string", "description": "Access password"},
                },
                "required": ["url", "passwd"],
            },
        ),
        Tool(
            name="list_tasks",
            description="List all download tasks with their current status (pending/running/success/failed/timeout).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_task",
            description="Get the status of a specific download task by its article ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Article ID (extracted from URL path)"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="list_articles",
            description="List all successfully downloaded articles.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="read_article",
            description="Read the Markdown content of a downloaded article by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "article_id": {"type": "string", "description": "Article ID (folder name)"},
                },
                "required": ["article_id"],
            },
        ),
        Tool(
            name="retry_all",
            description="Retry all failed and timed-out tasks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "passwd": {"type": "string", "description": "Access password"},
                },
                "required": ["passwd"],
            },
        ),
        Tool(
            name="retry_task",
            description="Retry a single failed or timed-out task by its article ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Article ID of the task to retry"},
                    "passwd": {"type": "string", "description": "Access password"},
                },
                "required": ["task_id", "passwd"],
            },
        ),
        Tool(
            name="lookup_url",
            description="Check whether a WeChat article URL has been downloaded. Returns task status; if successful, also returns the full Markdown content and browsable URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "WeChat article URL to look up"},
                },
                "required": ["url"],
            },
        ),
    ]


def _json_resp(obj):
    return [TextContent(type="text", text=json.dumps(obj, ensure_ascii=False))]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "add_download_task":
        url = arguments.get("url", "").strip()
        passwd = arguments.get("passwd", "").strip()
        if passwd != _config.passwd:
            return _json_resp({"error": "Invalid password", "status": "error"})
        if not url:
            return _json_resp({"error": "URL cannot be empty", "status": "error"})
        try:
            task = _task_queue.add_task(url)
            action = task.pop("action", "created")
            return _json_resp({"message": "Task processed", "task": task, "action": action, "status": "success"})
        except ValueError as e:
            return _json_resp({"error": str(e), "status": "error"})

    elif name == "list_tasks":
        tasks = _task_queue.get_tasks()
        return _json_resp({"data": tasks, "total": len(tasks), "status": "success"})

    elif name == "get_task":
        task_id = arguments.get("task_id", "")
        task = _task_queue.get_task(task_id)
        if task:
            return _json_resp({**task, "status_result": "success"})
        return _json_resp({"error": "Task not found", "status": "error"})

    elif name == "list_articles":
        tasks = _task_queue.get_tasks(include_success=True)
        articles = [t for t in tasks if t.get("status") == "success"]
        return _json_resp({"data": articles, "total": len(articles), "status": "success"})

    elif name == "read_article":
        article_id = arguments.get("article_id", "")
        md_path = Path(_config.output_dir) / article_id / "index.md"
        if not md_path.exists():
            return _json_resp({"error": "Article not found", "status": "error"})
        content = md_path.read_text(encoding="utf-8")
        return [TextContent(type="text", text=content)]

    elif name == "retry_all":
        passwd = arguments.get("passwd", "").strip()
        if passwd != _config.passwd:
            return _json_resp({"error": "Invalid password", "status": "error"})
        count = _task_queue.retry_all()
        return _json_resp({"message": f"Retried {count} tasks", "count": count, "status": "success"})

    elif name == "retry_task":
        passwd = arguments.get("passwd", "").strip()
        if passwd != _config.passwd:
            return _json_resp({"error": "Invalid password", "status": "error"})
        task_id = arguments.get("task_id", "")
        try:
            task = _task_queue.retry_task(task_id)
        except ValueError as e:
            return _json_resp({"error": str(e), "status": "error"})
        if task is None:
            return _json_resp({"error": "Task not found", "status": "error"})
        return _json_resp({"message": "Task retried", "task": task, "status": "success"})

    elif name == "lookup_url":
        url = arguments.get("url", "").strip()
        if not url:
            return _json_resp({"error": "URL cannot be empty", "status": "error"})
        result = _task_queue.lookup_by_url(url)
        if result is None:
            return _json_resp({"found": False, "status": "success", "message": "URL has not been submitted"})
        return _json_resp({"found": True, **result, "status": "success"})

    return _json_resp({"error": f"Unknown tool: {name}", "status": "error"})


async def run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def start_in_thread():
    """Start the MCP stdio server in a daemon thread."""
    def _run():
        asyncio.run(run_stdio())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("MCP stdio server started in background thread")
    return t


if __name__ == "__main__":
    from config_loader import Config
    from task_queue import TaskQueue

    cfg = Config()
    tq = TaskQueue(output_dir=cfg.output_dir, max_queue_size=cfg.max_queue_size)
    tq.start_worker()
    init(cfg, tq)
    asyncio.run(run_stdio())
