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

Keep the Google service-account key outside the repo folder:

```bash
mkdir -p /home/ubuntu/user-management-secrets
# copy the key file there, then:
chmod 600 /home/ubuntu/user-management-secrets/service-account.json
```

Create `.env` from the template and fill it in:

```bash
cp deploy/env.example .env
chmod 600 .env
nano .env
```

`.env` is git-ignored; never commit it (this repo is public).

## 3. Run the app with systemd

```bash
sudo cp deploy/user-management.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now user-management
sudo systemctl status user-management
curl -s http://127.0.0.1:5010/user-management/api/session
```

The last command should print JSON with `"authed": false`.

## 4. nginx

Paste the two `location` blocks from `deploy/nginx-user-management.conf`
inside the existing `server { listen 80; ... }` block, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Open `http://13.246.163.179/user-management/`.

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
