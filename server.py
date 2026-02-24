import subprocess
import threading
import uuid
import os
from flask import Flask, request, jsonify, render_template, Response

app = Flask(__name__)

OUTPUT_DIR = "/mnt/yt-dlp/"

# In-memory job store: { job_id: { "status": ..., "log": [...] } }
jobs = {}
jobs_lock = threading.Lock()


def run_download(job_id, url, fmt):
    cmd = [
        "/home/rickh/yt-dlp-server/.venv/bin/yt-dlp",
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

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "log": []}

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
        import time
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)