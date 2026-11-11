# Staging: try changes before they go live

A second copy of the app runs next to the live one at
`http://13.246.163.179/user-management-staging/`. Put any branch on it,
try it, and only merge to `main` (and update the live copy) once you're
happy.

|                   | Live                              | Staging                                   |
|-------------------|-----------------------------------|-------------------------------------------|
| URL               | `/user-management/`               | `/user-management-staging/`               |
| Folder            | `/home/ubuntu/user-management`    | `/home/ubuntu/user-management-staging`    |
| Port              | 5010                              | 5011                                      |
| Service           | `user-management`                 | `user-management-staging`                 |
| Code              | `main`                            | whichever branch you're testing           |
| History / Sheet   | the team's audit Sheet            | a separate staging Sheet                  |

The staging page has a coral **STAGING** banner and `[Staging]` in the
tab title, and its own sign-in, so the two can't be mixed up.

> **Staging still changes real Pipefy cards.** There is no test Pipefy.
> "Find their jobs" (the test run) never changes anything, so scan freely,
> but only press **Apply** on test cards (e.g. `dydxtest` cards).

## One-time setup

Run these on the server. Check there's memory to spare first; staging
needs roughly another 150–250 MB:

```bash
free -h
```

### 1. Code

```bash
cd /home/ubuntu
git clone https://github.com/upandupbot/user-management.git user-management-staging
cd user-management-staging
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. A separate Google Sheet

So test changes don't show up in the team's History or notifications:

1. Create a new, empty Google Sheet (e.g. "User management – staging audit").
2. Share it as **Editor** with the service account's email:

   ```bash
   grep -o '"client_email": *"[^"]*"' /home/ubuntu/user-management-secrets/service-account.json
   ```

3. Copy its ID from the address bar (between `/d/` and `/edit`).

The app creates the tabs and header rows itself on first use.

### 3. Settings

Start from the live copy's settings and change the Sheet ID:

```bash
cp /home/ubuntu/user-management/config.ini /home/ubuntu/user-management-staging/config.ini
chmod 600 config.ini
touch .env && chmod 600 .env
nano config.ini
```

Under `[Audit]`, set `sheet_id = <the staging Sheet ID>`. Everything else
(Pipefy token, key path, password) can stay the same. The URL, banner and
separate local history are set in the service file, not here.

### 4. Service

```bash
sudo cp deploy/user-management-staging.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now user-management-staging
curl -s http://127.0.0.1:5011/user-management-staging/api/session
```

The last command should print JSON ending in `"password_required":true`.

### 5. nginx

Back up, then paste the two `location` blocks from
`deploy/nginx-user-management-staging.conf` into the same `server { }`
block as the live `/user-management` blocks:

```bash
sudo cp /etc/nginx/sites-enabled/default /home/ubuntu/nginx-default.backup
sudo nano /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Open `http://13.246.163.179/user-management-staging/`. You should see the
STAGING banner.

## Testing a change

1. Push your branch and open a PR. GitHub runs the tests automatically
   and shows a tick or a cross on the PR.
2. Put the branch on staging:

   ```bash
   cd /home/ubuntu/user-management-staging && git fetch && git checkout <branch> && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart user-management-staging
   ```

3. Try it at `/user-management-staging/`.
4. Happy? Merge the PR, then update the live copy:

   ```bash
   cd /home/ubuntu/user-management && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart user-management
   ```

5. Put staging back on `main` so it matches live until the next test:

   ```bash
   cd /home/ubuntu/user-management-staging && git checkout main && git pull && sudo systemctl restart user-management-staging
   ```

## Good to know

- **Which branch is staging on?**

  ```bash
  git -C /home/ubuntu/user-management-staging branch --show-current
  ```

- **Logs:** `sudo journalctl -u user-management-staging -n 50 --no-pager`
- **Turn staging off** when you're not using it, to free memory:

  ```bash
  sudo systemctl stop user-management-staging
  ```

  Start it again with `sudo systemctl start user-management-staging`.
  While it's stopped, the staging URL shows "502 Bad Gateway"; the live
  site isn't affected.
- If a change needs a new setting, add it to the staging `config.ini`
  first, then to the live one when you deploy.
