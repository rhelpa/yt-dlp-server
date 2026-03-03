import subprocess
import threading
import uuid
import time
import os
from collections import defaultdict, deque
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template, Response, abort

app = Flask(__name__)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/mnt/yt-dlp/")
YTDLP_BIN = os.getenv("YTDLP_BIN", "/home/rickh/yt-dlp-server/.venv/bin/yt-dlp")
ALLOWED_NETWORKS = ("127.", "192.168.", "10.", "172.")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT", "10"))
RATE_LIMIT_WINDOW = 60  # seconds

ALLOWED_EXTRA_FLAGS = {"--write-subs", "--embed-subs", "--embed-thumbnail", "--add-metadata"}

# In-memory job store: { job_id: { status, log, finished_at, process, url } }
jobs = {}
jobs_lock = threading.Lock()

# Concurrent download semaphore
_dl_semaphore = threading.Semaphore(MAX_CONCURRENT)

# Per-IP rate limiting: { ip: deque of request timestamps }
_rate_store = defaultdict(deque)
_rate_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


def check_rate_limit(ip):
    now = time.time()
    with _rate_lock:
        timestamps = _rate_store[ip]
        while timestamps and timestamps[0] < now - RATE_LIMIT_WINDOW:
            timestamps.popleft()
        if len(timestamps) >= RATE_LIMIT_MAX:
            return False
        timestamps.append(now)
        return True


def job_to_dict(job_id, job):
    """Return a JSON-serialisable view of a job (excludes subprocess handle)."""
    return {
        "job_id": job_id,
        "status": job["status"],
        "log": job["log"],
        "finished_at": job["finished_at"],
        "url": job.get("url", ""),
    }


# ── Background Workers ────────────────────────────────────────────────────────

def run_download(job_id, url, fmt, extra_flags):
    cmd = [
        YTDLP_BIN,
        "-f", fmt,
        "-o", f"{OUTPUT_DIR}%(title)s.%(ext)s",
        "--newline",
        "--js-runtimes", "node",
        "--no-overwrites",
        "--restrict-filenames",
    ]
    cmd.extend(extra_flags)
    cmd.append(url)

    with jobs_lock:
        jobs[job_id]["status"] = "running"

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        with jobs_lock:
            jobs[job_id]["process"] = process

        for line in process.stdout:
            with jobs_lock:
                jobs[job_id]["log"].append(line.strip())

        process.wait()

        with jobs_lock:
            if jobs[job_id]["status"] != "cancelled":
                jobs[job_id]["status"] = "done" if process.returncode == 0 else "error"
            jobs[job_id]["finished_at"] = time.time()
            jobs[job_id]["process"] = None

    finally:
        _dl_semaphore.release()


def cleanup_jobs():
    """Purge completed/errored/cancelled jobs older than 1 hour, every 5 minutes."""
    while True:
        time.sleep(300)
        cutoff = time.time() - 3600
        with jobs_lock:
            to_delete = [
                jid for jid, job in jobs.items()
                if job["status"] in ("done", "error", "cancelled")
                and job.get("finished_at", 0) < cutoff
            ]
            for jid in to_delete:
                del jobs[jid]


# ── Request Hooks ─────────────────────────────────────────────────────────────

@app.before_request
def limit_to_local():
    ip = request.remote_addr
    if not any(ip.startswith(prefix) for prefix in ALLOWED_NETWORKS):
        abort(403)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download", methods=["POST"])
def download():
    ip = request.remote_addr

    if not check_rate_limit(ip):
        return jsonify({"error": f"Rate limit exceeded — max {RATE_LIMIT_MAX} downloads per minute."}), 429

    data = request.json or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "bestvideo+bestaudio/best")
    raw_flags = data.get("extra_flags", [])

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    extra_flags = [f for f in raw_flags if f in ALLOWED_EXTRA_FLAGS]
    if "--embed-subs" in extra_flags and "--write-subs" not in extra_flags:
        extra_flags.insert(0, "--write-subs")

    if not _dl_semaphore.acquire(blocking=False):
        return jsonify({"error": f"Too many concurrent downloads (max {MAX_CONCURRENT}). Try again soon."}), 429

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "log": [], "finished_at": None, "process": None, "url": url}

    try:
        thread = threading.Thread(target=run_download, args=(job_id, url, fmt, extra_flags))
        thread.daemon = True
        thread.start()
    except Exception:
        _dl_semaphore.release()
        with jobs_lock:
            del jobs[job_id]
        raise

    return jsonify({"job_id": job_id})


@app.route("/jobs")
def list_jobs():
    with jobs_lock:
        return jsonify([job_to_dict(jid, j) for jid, j in jobs.items()])


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job_to_dict(job_id, job))


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    process = job.get("process")
    if job["status"] != "running" or not process:
        return jsonify({"error": "Job is not running"}), 400
    process.terminate()
    with jobs_lock:
        jobs[job_id]["status"] = "cancelled"
        jobs[job_id]["finished_at"] = time.time()
    return jsonify({"ok": True})


@app.route("/stream/<job_id>")
def stream(job_id):
    """Server-Sent Events endpoint for live log streaming."""
    def event_stream():
        sent = 0
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                yield "data: Job not found\n\n"
                break
            log = job["log"]
            while sent < len(log):
                yield f"data: {log[sent]}\n\n"
                sent += 1
            if job["status"] in ("done", "error", "cancelled"):
                yield f"data: [DONE: {job['status']}]\n\n"
                break
            time.sleep(0.25)

    return Response(event_stream(), mimetype="text/event-stream")


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=cleanup_jobs, daemon=True).start()
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 5000)),
        debug=False
    )
