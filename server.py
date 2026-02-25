import subprocess
import threading
import uuid
import time
import os
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template, Response, abort

app = Flask(__name__)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/mnt/yt-dlp/")
YTDLP_BIN = os.getenv("YTDLP_BIN", "/home/rickh/yt-dlp-server/.venv/bin/yt-dlp")
ALLOWED_NETWORKS = ("127.", "192.168.", "10.", "172.")

# In-memory job store: { job_id: { "status": ..., "log": [...], "finished_at": ... } }
jobs = {}
jobs_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False


# ── Background Workers ────────────────────────────────────────────────────────

def run_download(job_id, url, fmt):
    cmd = [
        YTDLP_BIN,
        "-f", fmt,
        "-o", f"{OUTPUT_DIR}%(title)s.%(ext)s",
        "--newline",
        "--js-runtimes", "node",
        "--no-overwrites",
        url
    ]
    with jobs_lock:
        jobs[job_id]["status"] = "running"

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    for line in process.stdout:
        with jobs_lock:
            jobs[job_id]["log"].append(line.strip())

    process.wait()

    with jobs_lock:
        jobs[job_id]["status"] = "done" if process.returncode == 0 else "error"
        jobs[job_id]["finished_at"] = time.time()


def cleanup_jobs():
    """Purge completed/errored jobs older than 1 hour, every 5 minutes."""
    while True:
        time.sleep(300)
        cutoff = time.time() - 3600
        with jobs_lock:
            to_delete = [
                jid for jid, job in jobs.items()
                if job["status"] in ("done", "error")
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
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "bestvideo+bestaudio/best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Invalid URL"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "log": [], "finished_at": None}

    thread = threading.Thread(target=run_download, args=(job_id, url, fmt))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


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
            if job["status"] in ("done", "error"):
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