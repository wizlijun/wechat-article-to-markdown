from flask import Flask, jsonify, request, render_template, send_from_directory
from flask_cors import CORS
import os
import time

from config_loader import Config
from task_queue import TaskQueue
import mcp_server

app = Flask(__name__)
CORS(app)

app.config["JSON_AS_ASCII"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
app.json.ensure_ascii = False

config = Config()
task_queue = TaskQueue(output_dir=config.output_dir, max_queue_size=config.max_queue_size)
task_queue.start_worker()

mcp_server.init(config, task_queue)
mcp_server.start_in_thread()


@app.route("/")
def index():
    return render_template("index.html", refresh_interval=config.auto_refresh_interval)


# ------------------------------------------------------------------
# Task API
# ------------------------------------------------------------------


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    tasks = task_queue.get_tasks()
    return jsonify({"data": tasks, "total": len(tasks), "status": "success"})


@app.route("/api/tasks/<task_id>", methods=["GET"])
def get_task(task_id):
    task = task_queue.get_task(task_id)
    if task:
        return jsonify({**task, "status_result": "success"})
    return jsonify({"error": "Task not found", "status": "error"}), 404


@app.route("/api/tasks", methods=["POST"])
def add_task():
    data = request.get_json()
    if not data or "url" not in data or "passwd" not in data:
        return jsonify({"error": "Missing required fields: url and passwd", "status": "error"}), 400

    passwd = data["passwd"].strip()
    if passwd != config.passwd:
        return jsonify({"error": "Invalid password", "status": "error"}), 401

    url = data["url"].strip()
    if not url:
        return jsonify({"error": "URL cannot be empty", "status": "error"}), 400

    try:
        task = task_queue.add_task(url)
        action = task.pop("action", "created")
        return jsonify({
            "message": {
                "created": "任务已添加到队列",
                "exists": "文章已下载完成，无需重复下载",
                "retry": "任务已重新加入队列",
                "already_queued": "任务已在队列中",
            }.get(action, "OK"),
            "task": task,
            "action": action,
            "status": "success",
        })
    except ValueError as e:
        return jsonify({"error": str(e), "status": "error"}), 400


@app.route("/api/tasks/retry_all", methods=["POST"])
def retry_all():
    data = request.get_json() or {}
    passwd = data.get("passwd", "").strip()
    if passwd != config.passwd:
        return jsonify({"error": "Invalid password", "status": "error"}), 401

    count = task_queue.retry_all()
    return jsonify({"message": f"已重新激活 {count} 个任务", "count": count, "status": "success"})


@app.route("/api/tasks/<task_id>/retry", methods=["POST"])
def retry_task(task_id):
    """Retry a single failed/timeout task."""
    data = request.get_json() or {}
    passwd = data.get("passwd", "").strip()
    if passwd != config.passwd:
        return jsonify({"error": "Invalid password", "status": "error"}), 401

    try:
        task = task_queue.retry_task(task_id)
    except ValueError as e:
        return jsonify({"error": str(e), "status": "error"}), 400

    if task is None:
        return jsonify({"error": "Task not found", "status": "error"}), 404

    return jsonify({"message": "任务已重新加入队列", "task": task, "status": "success"})


@app.route("/api/lookup", methods=["POST"])
def lookup_by_url():
    """Query whether a URL has been downloaded.

    Request body: ``{"url": "https://mp.weixin.qq.com/s/..."}``

    Returns task metadata.  If status is ``success``, the response also
    includes ``markdown`` (full Markdown text), ``browse_url`` (path to
    view the .md in browser), and ``browse_html_url`` (path to the raw
    HTML).
    """
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "Missing required field: url", "status": "error"}), 400

    url = data["url"].strip()
    if not url:
        return jsonify({"error": "URL cannot be empty", "status": "error"}), 400

    result = task_queue.lookup_by_url(url)
    if result is None:
        return jsonify({"found": False, "status": "success", "message": "该 URL 尚未提交过"})

    # Build full browsable URLs
    if "browse_url" in result:
        result["browse_url"] = request.host_url.rstrip("/") + result["browse_url"]
        result["browse_html_url"] = request.host_url.rstrip("/") + result["browse_html_url"]

    return jsonify({"found": True, **result, "status": "success"})


# ------------------------------------------------------------------
# Articles API  (reads from meta.json)
# ------------------------------------------------------------------


@app.route("/api/articles", methods=["GET"])
def list_articles():
    """List successfully downloaded articles (status=success in meta.json)."""
    output_dir = config.output_dir
    if not os.path.exists(output_dir):
        return jsonify({"data": [], "total": 0, "status": "success"})

    articles = []
    for entry in os.scandir(output_dir):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta_path = os.path.join(entry.path, "meta.json")
        md_path = os.path.join(entry.path, "index.md")
        if not os.path.isfile(meta_path) or not os.path.isfile(md_path):
            continue

        import json as _json
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = _json.load(f)
        except Exception:
            continue

        if meta.get("status") != "success":
            continue

        img_dir = os.path.join(entry.path, "images")
        has_images = os.path.isdir(img_dir)
        img_count = len(os.listdir(img_dir)) if has_images else 0

        articles.append({
            "id": entry.name,
            "title": meta.get("title", entry.name),
            "author": meta.get("author", ""),
            "publish_time": meta.get("publish_time", ""),
            "url": meta.get("url", ""),
            "path": f"/articles/{entry.name}/index.md",
            "mtime": os.path.getmtime(md_path),
            "mtime_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(md_path))
            ),
            "size": os.path.getsize(md_path),
            "image_count": img_count,
        })

    articles.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"data": articles[:50], "total": len(articles), "status": "success"})


@app.route("/articles/<path:filepath>")
def serve_article(filepath):
    return send_from_directory(config.output_dir, filepath)


if __name__ == "__main__":
    app.run(host=config.host, port=config.port, debug=config.debug)
