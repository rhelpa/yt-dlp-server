# server.py
import os
import subprocess
import threading
import time
import uuid

from collections import defaultdict, deque
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)


app = Flask(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

OUTPUT_DIR = os.path.abspath(
    os.path.expanduser(
        os.getenv(
            "OUTPUT_DIR",
            "/mnt/plex_media/YouTube/YT_DLP",
        )
    )
)

MOUNT_ROOT = os.path.abspath(
    os.path.expanduser(
        os.getenv(
            "MOUNT_ROOT",
            "/mnt/plex_media",
        )
    )
)

YTDLP_BIN = os.path.abspath(
    os.path.expanduser(
        os.getenv(
            "YTDLP_BIN",
            "/home/rickh/yt-dlp-server/.venv/bin/yt-dlp",
        )
    )
)

YTDLP_JS_RUNTIME = os.getenv(
    "YTDLP_JS_RUNTIME",
    "deno:/home/rickh/.deno/bin/deno",
)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

MAX_CONCURRENT = int(
    os.getenv(
        "MAX_CONCURRENT",
        "3",
    )
)

RATE_LIMIT_MAX = int(
    os.getenv(
        "RATE_LIMIT",
        "10",
    )
)

RATE_LIMIT_WINDOW = 60  # seconds


ALLOWED_NETWORKS = (
    "127.",
    "192.168.",
    "10.",
    "172.",
)


ALLOWED_EXTRA_FLAGS = {
    "--write-subs",
    "--embed-subs",
    "--embed-thumbnail",
    "--add-metadata",
}



# ── In-Memory Job Storage ─────────────────────────────────────────────────────
#
# jobs structure:
#
# {
#     job_id: {
#         "status": "queued|running|done|error|cancelled",
#         "log": [],
#         "finished_at": None,
#         "process": subprocess.Popen | None,
#         "url": "...",
#     }
# }

jobs = {}
jobs_lock = threading.Lock()


# Limit how many downloads can run simultaneously.
_dl_semaphore = threading.Semaphore(MAX_CONCURRENT)


# Rate-limit storage:
#
# {
#     "192.168.1.x": deque([
#         timestamp,
#         timestamp,
#         ...
#     ])
# }

_rate_store = defaultdict(deque)
_rate_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_valid_url(url):
    """
    Perform basic validation that the supplied string
    is an HTTP or HTTPS URL.
    """

    try:
        result = urlparse(url)

        return (
            result.scheme in ("http", "https")
            and bool(result.netloc)
        )

    except Exception:
        return False


def check_rate_limit(ip):
    """
    Check whether an IP has exceeded the configured
    number of download requests within the rate-limit window.
    """

    now = time.time()

    with _rate_lock:
        timestamps = _rate_store[ip]

        # Remove timestamps that are outside the rate-limit window.
        while (
            timestamps
            and timestamps[0] < now - RATE_LIMIT_WINDOW
        ):
            timestamps.popleft()

        if len(timestamps) >= RATE_LIMIT_MAX:
            return False

        timestamps.append(now)

        return True


def job_to_dict(job_id, job):
    """
    Return a JSON-serialisable representation of a job.

    The subprocess object is intentionally excluded because
    it cannot be serialized to JSON.
    """

    return {
        "job_id": job_id,
        "status": job["status"],
        "log": job["log"],
        "finished_at": job["finished_at"],
        "url": job.get("url", ""),
    }


def verify_ytdlp_binary():
    """
    Verify that the configured yt-dlp executable exists
    and is executable.
    """

    if not os.path.isfile(YTDLP_BIN):
        raise RuntimeError(
            f"yt-dlp executable does not exist: {YTDLP_BIN}"
        )

    if not os.access(YTDLP_BIN, os.X_OK):
        raise RuntimeError(
            f"yt-dlp executable is not executable: {YTDLP_BIN}"
        )


def verify_output_directory():
    """
    Verify that the Synology NAS mount is actually mounted
    before allowing yt-dlp to write anything.

    This is important because /mnt/plex_media still exists as
    a normal local directory when the NAS is unavailable.

    Without this check, yt-dlp could accidentally start filling
    the Ubuntu VM's local filesystem instead of the NAS.
    """

    if not os.path.exists(MOUNT_ROOT):
        raise RuntimeError(
            f"NAS mount point does not exist: {MOUNT_ROOT}"
        )

    if not os.path.ismount(MOUNT_ROOT):
        raise RuntimeError(
            f"NAS is not mounted at: {MOUNT_ROOT}"
        )

    # Make sure OUTPUT_DIR really lives underneath MOUNT_ROOT.
    #
    # This protects against accidentally configuring OUTPUT_DIR
    # somewhere on the VM's local filesystem.
    try:
        common_path = os.path.commonpath(
            [
                MOUNT_ROOT,
                OUTPUT_DIR,
            ]
        )

    except ValueError:
        raise RuntimeError(
            f"OUTPUT_DIR is not located under MOUNT_ROOT: {OUTPUT_DIR}"
        )

    if common_path != MOUNT_ROOT:
        raise RuntimeError(
            f"OUTPUT_DIR must be inside {MOUNT_ROOT}: {OUTPUT_DIR}"
        )

    if not os.path.isdir(OUTPUT_DIR):
        raise RuntimeError(
            f"Output directory does not exist: {OUTPUT_DIR}"
        )

    if not os.access(OUTPUT_DIR, os.W_OK):
        raise PermissionError(
            f"Output directory is not writable: {OUTPUT_DIR}"
        )


def add_job_log(job_id, message):
    """
    Safely append one line to a job's log.
    """

    with jobs_lock:
        job = jobs.get(job_id)

        if job:
            job["log"].append(message)


# ── Background Workers ────────────────────────────────────────────────────────

def run_download(job_id, url, fmt, extra_flags):
    """
    Run yt-dlp for a queued job.

    This function runs inside its own background thread.
    """

    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            _dl_semaphore.release()
            return

        job["status"] = "running"

    try:
        # Verify all important filesystem/application dependencies
        # before launching yt-dlp.
        verify_ytdlp_binary()
        verify_output_directory()

        output_template = os.path.join(
            OUTPUT_DIR,
            "%(title)s.%(ext)s",
        )

        cmd = [
            YTDLP_BIN,
            "-f",
            fmt,
            "-o",
            output_template,
            "--newline",
            "--js-runtimes",
            YTDLP_JS_RUNTIME,
            "--no-overwrites",
            "--restrict-filenames",
        ]

        cmd.extend(extra_flags)
        cmd.append(url)

        add_job_log(
            job_id,
            f"Output directory: {OUTPUT_DIR}",
        )

        add_job_log(
            job_id,
            "Starting download...",
        )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with jobs_lock:
            job = jobs.get(job_id)

            if job:
                job["process"] = process

        if process.stdout is not None:
            for line in process.stdout:
                clean_line = line.rstrip()

                if clean_line:
                    add_job_log(
                        job_id,
                        clean_line,
                    )

        return_code = process.wait()

        with jobs_lock:
            job = jobs.get(job_id)

            if job:
                if job["status"] != "cancelled":
                    if return_code == 0:
                        job["status"] = "done"
                    else:
                        job["status"] = "error"

                job["finished_at"] = time.time()
                job["process"] = None

    except Exception as exc:
        with jobs_lock:
            job = jobs.get(job_id)

            if job:
                if job["status"] != "cancelled":
                    job["status"] = "error"

                    job["log"].append(
                        f"ERROR: {exc}"
                    )

                job["finished_at"] = time.time()
                job["process"] = None

    finally:
        _dl_semaphore.release()


def cleanup_jobs():
    """
    Remove completed, failed, or cancelled jobs
    after they have been stored for one hour.

    Cleanup runs every five minutes.
    """

    while True:
        time.sleep(300)

        cutoff = time.time() - 3600

        with jobs_lock:
            to_delete = [
                job_id
                for job_id, job in jobs.items()
                if (
                    job["status"]
                    in (
                        "done",
                        "error",
                        "cancelled",
                    )
                    and job.get("finished_at") is not None
                    and job["finished_at"] < cutoff
                )
            ]

            for job_id in to_delete:
                del jobs[job_id]


# ── Request Hooks ─────────────────────────────────────────────────────────────

@app.before_request
def limit_to_local():
    """
    Restrict access to localhost and common private
    IPv4 network ranges.
    """

    ip = request.remote_addr or ""

    if not any(
        ip.startswith(prefix)
        for prefix in ALLOWED_NETWORKS
    ):
        abort(403)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html"
    )


@app.route(
    "/download",
    methods=["POST"],
)
def download():
    ip = request.remote_addr or ""

    if not check_rate_limit(ip):
        return jsonify(
            {
                "error": (
                    "Rate limit exceeded — "
                    f"max {RATE_LIMIT_MAX} downloads per minute."
                )
            }
        ), 429

    data = request.get_json(
        silent=True
    ) or {}

    url = data.get(
        "url",
        "",
    ).strip()

    fmt = data.get(
        "format",
        "bestvideo+bestaudio/best",
    )

    raw_flags = data.get(
        "extra_flags",
        [],
    )

    if not url:
        return jsonify(
            {
                "error": "No URL provided"
            }
        ), 400

    if not is_valid_url(url):
        return jsonify(
            {
                "error": "Invalid URL"
            }
        ), 400

    if not isinstance(raw_flags, list):
        return jsonify(
            {
                "error": "extra_flags must be a list"
            }
        ), 400

    # Only allow explicitly approved yt-dlp options.
    #
    # This prevents arbitrary command-line arguments from
    # being passed through the web interface.
    extra_flags = [
        flag
        for flag in raw_flags
        if flag in ALLOWED_EXTRA_FLAGS
    ]

    # Embedding subtitles requires subtitles to first be downloaded.
    if (
        "--embed-subs" in extra_flags
        and "--write-subs" not in extra_flags
    ):
        extra_flags.insert(
            0,
            "--write-subs",
        )

    # Don't queue an unlimited number of jobs.
    #
    # If all download slots are occupied, reject the request.
    if not _dl_semaphore.acquire(
        blocking=False
    ):
        return jsonify(
            {
                "error": (
                    "Too many concurrent downloads "
                    f"(max {MAX_CONCURRENT}). "
                    "Try again soon."
                )
            }
        ), 429

    job_id = str(
        uuid.uuid4()
    )

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "log": [],
            "finished_at": None,
            "process": None,
            "url": url,
        }

    try:
        thread = threading.Thread(
            target=run_download,
            args=(
                job_id,
                url,
                fmt,
                extra_flags,
            ),
            daemon=True,
        )

        thread.start()

    except Exception:
        _dl_semaphore.release()

        with jobs_lock:
            jobs.pop(
                job_id,
                None,
            )

        raise

    return jsonify(
        {
            "job_id": job_id
        }
    )


@app.route("/jobs")
def list_jobs():
    with jobs_lock:
        result = [
            job_to_dict(
                job_id,
                job,
            )
            for job_id, job in jobs.items()
        ]

    return jsonify(result)


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify(
                {
                    "error": "Job not found"
                }
            ), 404

        result = job_to_dict(
            job_id,
            job,
        )

    return jsonify(result)


@app.route(
    "/cancel/<job_id>",
    methods=["POST"],
)
def cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify(
                {
                    "error": "Job not found"
                }
            ), 404

        process = job.get(
            "process"
        )

        if (
            job["status"] != "running"
            or process is None
        ):
            return jsonify(
                {
                    "error": "Job is not running"
                }
            ), 400

        job["status"] = "cancelled"
        job["finished_at"] = time.time()

    try:
        process.terminate()

    except Exception as exc:
        add_job_log(
            job_id,
            f"WARNING: Could not terminate process cleanly: {exc}",
        )

    return jsonify(
        {
            "ok": True
        }
    )


@app.route("/stream/<job_id>")
def stream(job_id):
    """
    Server-Sent Events endpoint used by the browser
    to receive live yt-dlp log output.
    """

    def event_stream():
        sent = 0

        while True:
            with jobs_lock:
                job = jobs.get(job_id)

                if not job:
                    yield "data: Job not found\n\n"
                    break

                # Take snapshots while holding the lock.
                log_snapshot = list(
                    job["log"]
                )

                status_snapshot = job[
                    "status"
                ]

            while sent < len(
                log_snapshot
            ):
                # SSE messages must not contain raw newlines
                # inside one data event.
                line = log_snapshot[sent].replace(
                    "\n",
                    " ",
                )

                yield (
                    f"data: {line}\n\n"
                )

                sent += 1

            if status_snapshot in (
                "done",
                "error",
                "cancelled",
            ):
                yield (
                    "data: "
                    f"[DONE: {status_snapshot}]"
                    "\n\n"
                )

                break

            time.sleep(0.25)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cleanup_thread = threading.Thread(
        target=cleanup_jobs,
        daemon=True,
    )

    cleanup_thread.start()

    print(
        f"yt-dlp output directory: {OUTPUT_DIR}"
    )

    print(
        f"NAS mount root: {MOUNT_ROOT}"
    )

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
    )