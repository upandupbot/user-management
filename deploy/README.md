# Deploying to EC2 behind nginx

The app runs under gunicorn on `127.0.0.1:5010`, and nginx serves it at
`http://13.246.163.179/user-management/`. nginx forwards the full
`/user-management/...` path; the app strips the prefix itself.

## 1. Code and Python environment

```bash
cd /home/ubuntu
git clone https://github.com/upandupbot/user-management.git
cd user-management
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Secrets

### The Google service-account key

Keep the key outside the repo folder. Get it from Render (the
`SWAP_AUDIT_SERVICE_ACCOUNT_JSON` value) or download a new JSON key from
Google Cloud (IAM & Admin → Service Accounts → Keys). Paste it into:

```bash
mkdir -p /home/ubuntu/user-management-secrets
nano /home/ubuntu/user-management-secrets/service-account.json
chmod 600 /home/ubuntu/user-management-secrets/service-account.json
python3 -m json.tool /home/ubuntu/user-management-secrets/service-account.json > /dev/null && echo "key OK"
```

### Settings: `config.ini` (what the live server uses)

The app reads `config.ini` from the repo folder. Start from the template:

```bash
cd /home/ubuntu/user-management
cp config.example.ini config.ini
chmod 600 config.ini
nano config.ini
```

It needs at least:

```ini
[Pipefy]
pipefy_api_token = <Pipefy token>

[Audit]
sheet_id = <Google Sheet ID>
service_account_file = /home/ubuntu/user-management-secrets/service-account.json

[Web]
password = <shared team password>
```

- `service_account_file` must be the **full path on the server**. A file
  name copied from a laptop's `config.ini` won't exist there.
- Without `[Web] password`, anyone who can reach the page can use the
  tool. Check after starting (step 3) that the session check shows
  `"password_required":true`.

The systemd unit also loads `.env`, and the file must exist even if it's
empty:

```bash
touch .env && chmod 600 .env
```

### Or: settings in `.env`

Instead of `config.ini`, you can put the same settings in `.env` as
environment variables (`PIPEFY_API_TOKEN`, `SWAP_WEB_PASSWORD`,
`SWAP_AUDIT_SHEET_ID`, `SWAP_AUDIT_SERVICE_ACCOUNT_FILE`); see
`deploy/env.example`. Values in `.env` override `config.ini`.

`.env` must use `NAME=value` lines only. Don't paste `config.ini` content
(`[Pipefy]`, `name = value`) into it: systemd ignores those lines.

`config.ini` and `.env` are both git-ignored; never commit them (this
repo is public).

## 3. Run the app with systemd

```bash
sudo cp deploy/user-management.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now user-management
sudo systemctl status user-management
curl -s http://127.0.0.1:5010/user-management/api/session
```

The last command should print JSON ending in `"password_required":true`.
If it says `false`, the password isn't set: see step 2. If the service
doesn't start, check the log:

```bash
sudo journalctl -u user-management -n 50 --no-pager
```

## 4. nginx

Back up the site config, then paste the two `location` blocks from
`deploy/nginx-user-management.conf` inside the existing
`server { listen 80; ... }` block, next to the other `location` blocks
and before that block's closing `}`:

```bash
ls /etc/nginx/sites-enabled/
sudo cp /etc/nginx/sites-enabled/default /home/ubuntu/nginx-default.backup
sudo nano /etc/nginx/sites-enabled/default
```

Then test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Check through nginx, then open `http://13.246.163.179/user-management/`
and sign in. History should say "shared team history"; if it says "this
server only", the Google key or Sheet ID isn't working (see the log).

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/user-management/
```

If `nginx -t` fails, nothing changes; restore the backup with
`sudo cp /home/ubuntu/nginx-default.backup /etc/nginx/sites-enabled/default`.

## Notes

- **Workers:** gunicorn always runs a small manager process plus at least
  one worker. The unit uses one worker with 4 threads, which is the
  lowest-memory setup, and it must stay at one worker: test-run results
  are held in that process's memory.
- **Port 5010** only listens on localhost. Keep it closed in the EC2
  security group; nginx on port 80 is the way in.
- **Long applies:** nginx waits up to an hour for an Apply to finish
  (`proxy_read_timeout 3600s`).
- **HTTP only:** the shared password and session cookie travel
  unencrypted. The unit sets `SWAP_WEB_ALLOW_INSECURE_COOKIE=1` so sign-in
  works over HTTP. Add HTTPS (a domain plus a Let's Encrypt certificate),
  then remove that line.

## Updating

```bash
cd /home/ubuntu/user-management
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart user-management
```

Run these from inside `/home/ubuntu/user-management`; `git pull` and the
`deploy/` paths only work there.
