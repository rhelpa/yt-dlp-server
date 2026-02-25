# yt-dlp-server

A lightweight local web server that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) and lets you download videos and audio from your browser. Built with Flask and designed to run on your local network only.

---

## Features

- Browser-based UI accessible from any device on your LAN
- Real-time download progress streaming via Server-Sent Events
- Format selection (best video, best MP4, audio only, etc.)
- Saves files to a configurable output directory (local or network share)
- Restricted to local network requests only (403 for external IPs)
- Automatic cleanup of completed jobs after 1 hour

---

## Requirements

- Ubuntu (or any Debian-based Linux)
- Python 3.10+
- ffmpeg
- Node.js (required by yt-dlp for YouTube extraction)
- A network share or local directory to save downloads to

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/rhelpa1/yt-dlp-server.git
cd yt-dlp-server
```

### 2. Install system dependencies

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg nodejs -y
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure your environment

Copy the example env file and edit it:

```bash
cp .env.example .env
nano .env
```

Set the following values:

```
OUTPUT_DIR=/path/to/your/output/directory/
YTDLP_BIN=/path/to/your/project/.venv/bin/yt-dlp
HOST=0.0.0.0
PORT=5000
```

> **Note:** `OUTPUT_DIR` must end with a trailing slash. `YTDLP_BIN` must be the full absolute path to the yt-dlp binary inside your `.venv`.

---

## Using a Windows Network Share (CIFS)

If you want to save downloads to a Windows share (e.g. `\\MYPC\yt-dlp`), mount it first:

```bash
sudo apt install cifs-utils -y
sudo mkdir -p /mnt/yt-dlp
sudo mount -t cifs //192.168.1.X/yt-dlp /mnt/yt-dlp -o username="YOUR_USER",password="YOUR_PASS",uid=1000,gid=1000
```

To persist across reboots, add to `/etc/fstab` (store credentials in `/etc/samba/creds` with `chmod 600`):

```
//192.168.1.X/yt-dlp /mnt/yt-dlp cifs credentials=/etc/samba/creds,uid=1000,gid=1000,_netdev,x-systemd.automount 0 0
```

Then set `OUTPUT_DIR=/mnt/yt-dlp/` in your `.env`.

---

## Running Manually

Make sure your venv is active, then:

```bash
python3 server.py
```

Open your browser and navigate to:

```
http://<your-server-ip>:5000
```

---

## Running as a systemd Service (Recommended)

This allows the server to start automatically on boot without any manual intervention.

### 1. Create the service file

```bash
sudo nano /etc/systemd/system/yt-dlp-server.service
```

Paste the following, adjusting paths and username to match your setup:

```ini
[Unit]
Description=yt-dlp Web Server
After=network.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/yt-dlp-server
ExecStart=/home/YOUR_USERNAME/yt-dlp-server/.venv/bin/python3 server.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 2. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable yt-dlp-server
sudo systemctl start yt-dlp-server
```

### 3. Check status

```bash
sudo systemctl status yt-dlp-server
```

### 4. View live logs

```bash
sudo journalctl -u yt-dlp-server -f
```

---

## Firewall

If your Ubuntu machine has `ufw` enabled, allow port 5000:

```bash
sudo ufw allow 5000/tcp
```

---

## Security Notes

- This server is intended for **local network use only**. It will reject any requests from IPs outside of `127.x`, `192.168.x`, `10.x`, and `172.x` ranges with a 403.
- Do not expose port 5000 to the internet.
- Credentials in `.env` and `/etc/samba/creds` should have restricted permissions (`chmod 600`).
- The `.env` file is excluded from version control via `.gitignore`.

---

## Project Structure

```
yt-dlp-server/
├── server.py           # Flask backend
├── templates/
│   └── index.html      # Browser UI
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Troubleshooting

**Stale file handle on the network share:**
```bash
sudo umount -f /mnt/yt-dlp
sudo mount -t cifs //192.168.1.X/yt-dlp /mnt/yt-dlp -o username="USER",password="PASS",uid=1000,gid=1000
```

**yt-dlp not found by systemd:**
Make sure `YTDLP_BIN` in your `.env` points to the full absolute path of the yt-dlp binary inside your `.venv`, e.g. `/home/youruser/yt-dlp-server/.venv/bin/yt-dlp`.

**No JavaScript runtime warning from yt-dlp:**
Install Node.js: `sudo apt install nodejs -y`

**Downloads succeed but files don't appear on the Windows share:**
Check the mount is still alive with `mount | grep yt-dlp` and that the share is accessible from the Windows side.

---

## License

MIT
