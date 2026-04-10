from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from wechat_article_to_markdown import (
    extract_article_id,
    fetch_article,
    normalize_wechat_url,
)

TIMEOUT_MINUTES = 30


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class TaskQueue:
    """Persistent task queue backed by per-article ``meta.json`` files.

    Directory layout::

        output/
          <article_id>/
            meta.json      # task state + article metadata
            index.html      # raw HTML (saved on fetch)
            index.md        # converted Markdown
            images/         # downloaded images
    """

    def __init__(self, output_dir: str = "output", max_queue_size: int = 100):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.max_queue_size = max_queue_size

    # ------------------------------------------------------------------
    # meta.json helpers
    # ------------------------------------------------------------------

    def _meta_path(self, article_id: str) -> Path:
        return self.output_dir / article_id / "meta.json"

    def _read_meta(self, article_id: str) -> Optional[dict]:
        p = self._meta_path(article_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_meta(self, article_id: str, meta: dict) -> None:
        d = self.output_dir / article_id
        d.mkdir(parents=True, exist_ok=True)
        self._meta_path(article_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _resolve_status(self, meta: dict) -> str:
        """Return the effective status, auto-detecting timeout."""
        status = meta.get("status", "pending")
        if status == "running":
            dt_task = _parse_dt(meta.get("dt_task", ""))
            if dt_task and datetime.now() - dt_task > timedelta(minutes=TIMEOUT_MINUTES):
                return "timeout"
        return status

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_task(self, url: str) -> dict:
        """Add a download task.  Returns the meta dict.

        - If already downloaded (success), returns existing meta with
          ``action=exists``.
        - If previously failed/timeout, resets to pending with
          ``action=retry``.
        - Otherwise creates a new pending task with ``action=created``.
        """
        normalized = normalize_wechat_url(url)
        if not normalized.startswith("https://mp.weixin.qq.com/"):
            raise ValueError("Invalid WeChat article URL")

        article_id = extract_article_id(normalized)

        with self._lock:
            existing = self._read_meta(article_id)
            if existing:
                eff_status = self._resolve_status(existing)
                if eff_status == "success":
                    existing["action"] = "exists"
                    existing["status"] = "success"
                    return existing
                if eff_status in ("failed", "timeout"):
                    existing["status"] = "pending"
                    existing["error"] = ""
                    existing["dt_task"] = _now_str()
                    existing["action"] = "retry"
                    self._write_meta(article_id, existing)
                    return existing
                # pending or running -- just return
                existing["action"] = "already_queued"
                existing["status"] = eff_status
                return existing

            # check queue size
            pending_count = sum(
                1
                for d in self.output_dir.iterdir()
                if d.is_dir()
                and not d.name.startswith(".")
                and self._resolve_status(self._read_meta(d.name) or {}) == "pending"
            )
            if pending_count >= self.max_queue_size:
                raise ValueError("Task queue is full")

            meta = {
                "id": article_id,
                "url": normalized,
                "status": "pending",
                "title": "",
                "author": "",
                "publish_time": "",
                "error": "",
                "dt_task": _now_str(),
                "dt_start": "",
                "dt_done": "",
                "action": "created",
            }
            self._write_meta(article_id, meta)
            return meta

    def get_tasks(self, limit: int = 50, include_success: bool = False) -> list[dict]:
        """List tasks from meta.json files, newest first.

        By default completed (success) tasks are excluded from the queue
        listing -- they appear in the articles view instead.
        """
        tasks = []
        if not self.output_dir.exists():
            return tasks
        for d in self.output_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            meta = self._read_meta(d.name)
            if meta is None:
                continue
            meta["status"] = self._resolve_status(meta)
            if not include_success and meta["status"] == "success":
                continue
            meta.pop("action", None)
            tasks.append(meta)
        tasks.sort(key=lambda t: t.get("dt_task", ""), reverse=True)
        return tasks[:limit]

    def get_task(self, article_id: str) -> Optional[dict]:
        meta = self._read_meta(article_id)
        if meta:
            meta["status"] = self._resolve_status(meta)
            meta.pop("action", None)
        return meta

    def lookup_by_url(self, url: str) -> Optional[dict]:
        """Look up a task by its original WeChat URL.

        Returns the meta dict with resolved status.  If status is
        ``success``, also includes ``markdown`` (full file content)
        and ``browse_url`` (relative path to view in browser).
        Returns *None* if the URL has never been submitted.
        """
        normalized = normalize_wechat_url(url)
        if not normalized.startswith("https://mp.weixin.qq.com/"):
            return None
        article_id = extract_article_id(normalized)
        meta = self.get_task(article_id)
        if meta is None:
            return None

        if meta.get("status") == "success":
            md_path = self.output_dir / article_id / "index.md"
            if md_path.exists():
                meta["markdown"] = md_path.read_text(encoding="utf-8")
            else:
                meta["markdown"] = ""
            meta["browse_url"] = f"/articles/{article_id}/index.md"
            meta["browse_html_url"] = f"/articles/{article_id}/index.html"

        return meta

    def retry_task(self, article_id: str) -> Optional[dict]:
        """Reset a single failed/timeout task to pending.

        Returns the updated meta dict, or *None* if not found.
        Raises ``ValueError`` if the task is not in a retryable state.
        """
        with self._lock:
            meta = self._read_meta(article_id)
            if meta is None:
                return None
            eff = self._resolve_status(meta)
            if eff not in ("failed", "timeout"):
                raise ValueError(f"任务状态为 {eff}，无法重试")
            meta["status"] = "pending"
            meta["error"] = ""
            meta["dt_task"] = _now_str()
            self._write_meta(article_id, meta)
        meta.pop("action", None)
        return meta

    def retry_all(self) -> int:
        """Reset all failed/timeout tasks to pending.  Returns count."""
        count = 0
        if not self.output_dir.exists():
            return count
        with self._lock:
            for d in self.output_dir.iterdir():
                if not d.is_dir() or d.name.startswith("."):
                    continue
                meta = self._read_meta(d.name)
                if meta is None:
                    continue
                eff = self._resolve_status(meta)
                if eff in ("failed", "timeout"):
                    meta["status"] = "pending"
                    meta["error"] = ""
                    meta["dt_task"] = _now_str()
                    self._write_meta(d.name, meta)
                    count += 1
        return count

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _next_pending_id(self) -> Optional[str]:
        """Find the oldest pending task."""
        if not self.output_dir.exists():
            return None
        candidates = []
        for d in self.output_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            meta = self._read_meta(d.name)
            if meta and self._resolve_status(meta) == "pending":
                candidates.append((meta.get("dt_task", ""), d.name))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _run_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while not self._stop_event.is_set():
            article_id = self._next_pending_id()
            if article_id is None:
                time.sleep(1)
                continue

            with self._lock:
                meta = self._read_meta(article_id)
                if not meta or self._resolve_status(meta) != "pending":
                    continue
                meta["status"] = "running"
                meta["dt_start"] = _now_str()
                self._write_meta(article_id, meta)

            url = meta["url"]
            try:
                result = loop.run_until_complete(
                    fetch_article(url, output_dir=self.output_dir, article_id=article_id)
                )
                with self._lock:
                    meta = self._read_meta(article_id) or meta
                    meta["status"] = "success"
                    meta["dt_done"] = _now_str()
                    meta["title"] = result.get("title", "")
                    meta["author"] = result.get("author", "")
                    meta["publish_time"] = result.get("publish_time", "")
                    meta["error"] = ""
                    self._write_meta(article_id, meta)
            except Exception as e:
                with self._lock:
                    meta = self._read_meta(article_id) or meta
                    meta["status"] = "failed"
                    meta["error"] = str(e)
                    meta["dt_done"] = _now_str()
                    self._write_meta(article_id, meta)

        loop.close()

    def start_worker(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
        self._worker_thread.start()

    def stop_worker(self):
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
