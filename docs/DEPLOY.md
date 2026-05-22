# Deploying Signal Tracker on Oracle Cloud (Always Free tier)

This guide walks you from "I have an Oracle account" to "my dashboard is live
on `https://tracker.mydomain.com`" in ~45 minutes. Everything used here is
free forever within Oracle's Always Free quota (1 ARM Ampere A1 VM with up to
4 OCPU / 24 GB RAM) plus a Cloudflare account (also free).

## Architecture

```
┌──────────────────────────────────────────────┐
│ Cloudflare DNS + Tunnel + HTTPS              │
└──────────────────────┬───────────────────────┘
                       │  (encrypted tunnel)
┌──────────────────────▼───────────────────────┐
│ Oracle ARM A1 VM (Ubuntu 22.04)              │
│ ┌──────────────┐  ┌──────────────────────┐   │
│ │ cloudflared  │──│ dashboard  :8000     │   │
│ │ (systemd)    │  │ (uvicorn / FastAPI)  │   │
│ └──────────────┘  └──────────┬───────────┘   │
│                              │               │
│                   ┌──────────▼───────────┐   │
│                   │ postgres  :5432       │   │
│                   └──────────┬───────────┘   │
│                              │               │
│                   ┌──────────▼───────────┐   │
│                   │ scheduler             │   │
│                   │ (APScheduler daemon)  │   │
│                   └──────────────────────┘   │
└──────────────────────────────────────────────┘
```

All three application services run as docker-compose containers sharing a
private Docker network. Only the dashboard binds to `127.0.0.1:8000` on the
host. The Cloudflare Tunnel daemon on the host forwards public
`https://tracker.yourdomain.com` requests into that port. The Postgres
database is reachable only via the Docker network.

## Step 1 — Provision the Oracle VM (15 min)

1. **Create an Oracle Cloud account**: <https://signup.oraclecloud.com/>.
   A credit card is required for verification but nothing is charged on
   Always Free.

2. From the Oracle Cloud console: **Compute → Instances → Create instance**.

3. Fill in:
   - **Name**: `signal-tracker`
   - **Image**: Ubuntu 22.04 (the LTS minimal image)
   - **Shape**: Click **Change shape** → tab **Ampere** → pick
     `VM.Standard.A1.Flex`. Allocate **2 OCPU / 12 GB RAM** (you can go up
     to 4/24 — still free).
   - **VCN / Subnet**: accept the defaults (a new public subnet is created).
   - **Add SSH keys**: paste the contents of your `~/.ssh/id_ed25519.pub`
     (generate one locally with `ssh-keygen -t ed25519` if you don't have
     it).
   - **Boot volume**: 50 GB (still free).
4. **Create**.

> If Oracle says "out of capacity" for ARM, try a different Home Region
> (Settings → Tenancy → Home Region) or wait a few hours. It rotates.

5. Once green, note the **public IP** of the instance.

## Step 2 — Open ports in Oracle's Security List (3 min)

Oracle's VCN has its own firewall on top of the VM. By default it allows
only port 22 (SSH).

1. Console → **Networking → Virtual Cloud Networks** → click your VCN.
2. Click on the public subnet → click the **Default Security List**.
3. **Add Ingress Rules**:
   - **Source CIDR**: `0.0.0.0/0`, **Destination Port Range**: `80`,
     **Protocol**: TCP.
   - Same with port `443`.

This is harmless even with Cloudflare Tunnel (tunnel doesn't use them) but
keeps the door open if you ever switch to direct nginx + Let's Encrypt.

## Step 3 — Run the bootstrap script (10 min)

SSH into the VM:

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@<PUBLIC_IP>
```

Run the one-line bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/M13-Prime/Score_Simulator/main/scripts/deploy.sh | bash
```

(Replace `main` with your default branch name if it's different.)

The script will:
1. Install Docker, git, iptables-persistent.
2. Add you to the `docker` group (you'll need to log out / back in once).
3. Open firewall ports 80 / 443 on the VM itself.
4. Clone the repo to `/opt/signal-tracker`.
5. Copy `.env.example` to `.env` and stop, asking you to fill it in.

Edit the env file:

```bash
cd /opt/signal-tracker
nano .env
```

At minimum set:

```bash
LLM_MODEL=anthropic/claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...

# Dashboard auth — REQUIRED in prod (without this, anyone on the internet
# can browse and edit your signals)
DASHBOARD_AUTH_USER=admin
DASHBOARD_AUTH_PASSWORD=<long random password>

# Postgres credentials — REQUIRED, choose a strong password
POSTGRES_USER=signaltracker
POSTGRES_PASSWORD=<long random password>
POSTGRES_DB=signaltracker

# Optional digest email
DIGEST_TO_EMAIL=you@example.com
DIGEST_FROM_EMAIL=tracker@yourdomain.com
SMTP_HOST=smtp.example.com
SMTP_USER=...
SMTP_PASSWORD=...
```

Save (`Ctrl+O`, `Enter`, `Ctrl+X`) and **log out / SSH back in** so the
`docker` group membership takes effect. Then re-run the bootstrap:

```bash
cd /opt/signal-tracker
bash scripts/deploy.sh
```

This time it builds the image and starts the services. After ~1 min:

```bash
docker compose ps               # all 3 services Up
docker compose logs -f dashboard
```

Verify locally on the VM (still in SSH):

```bash
curl -i http://127.0.0.1:8000/healthz
# -> 401 (auth required) — good
curl -i -u admin:<your-password> http://127.0.0.1:8000/healthz
# -> 200 {"status":"ok"} — good
```

You can also run the doctor inside the container to verify the LLM key:

```bash
docker compose exec dashboard python scripts/doctor.py
```

## Step 4 — Cloudflare Tunnel: HTTPS without certificates (15 min)

This is the magic that gives you `https://tracker.yourdomain.com` without
configuring Let's Encrypt, opening ports, or registering a static IP.

### 4.1 Cloudflare account + domain

- Cloudflare account: <https://dash.cloudflare.com/sign-up> (free).
- Add a domain (free if you bring your own, or you can buy one in
  Cloudflare Registrar at cost).
- After Cloudflare imports your DNS records, change your domain's
  nameservers at your registrar to point at Cloudflare. Wait for
  propagation (usually <1 hour).

### 4.2 Install `cloudflared` on the VM

```bash
# ARM A1 = arm64
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb \
    -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

cloudflared --version
```

### 4.3 Authenticate, create the tunnel

```bash
cloudflared tunnel login
```

This prints a URL — open it on your laptop, log into Cloudflare, pick the
domain you want to associate. The certificate is saved to
`~/.cloudflared/cert.pem` on the VM.

```bash
cloudflared tunnel create signal-tracker
# -> Created tunnel signal-tracker with id <UUID>
#    Credentials file: /home/ubuntu/.cloudflared/<UUID>.json
```

### 4.4 Route a hostname through the tunnel

```bash
cloudflared tunnel route dns signal-tracker tracker.yourdomain.com
```

Cloudflare adds a CNAME `tracker.yourdomain.com -> <UUID>.cfargotunnel.com`
automatically.

### 4.5 Write the tunnel config

```bash
cat | sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: signal-tracker
credentials-file: /home/ubuntu/.cloudflared/<UUID>.json
ingress:
  - hostname: tracker.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404
EOF
```

(Replace `<UUID>` with the actual id you got at step 4.3.)

### 4.6 Run as a systemd service

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared       # should be "active (running)"
```

🎉 Visit `https://tracker.yourdomain.com` from your laptop. You'll get
the HTTP Basic Auth popup (login: `admin`, password from your `.env`),
then the dashboard.

## Step 5 — Loop the digest links back to the dashboard

The daily digest email contains "Marquer comme contacté" buttons. They
need to know where the dashboard lives. Update `.env`:

```bash
DASHBOARD_BASE_URL=https://tracker.yourdomain.com
```

Restart the stack:

```bash
docker compose restart dashboard scheduler
```

## Operating the deployment

| Task | Command |
|------|---------|
| See running services | `docker compose ps` |
| Tail logs | `docker compose logs -f` |
| Tail only the scheduler | `docker compose logs -f scheduler` |
| Run doctor | `docker compose exec dashboard python scripts/doctor.py` |
| Run a manual classify | `docker compose exec dashboard python scripts/daily.py --dry-run` |
| Update the code | `cd /opt/signal-tracker && git pull && docker compose build && docker compose up -d` |
| Stop everything | `docker compose down` |
| Stop + wipe DB | `docker compose down -v` (⚠️ deletes Postgres data) |
| Backup the DB | `docker compose exec db pg_dump -U signaltracker signaltracker > backup-$(date +%F).sql` |

## Tuning & quotas

- **Anthropic billing**: budget ~€1–3/day for ~500 articles/day classified
  with Claude Sonnet 4.5. Watch
  <https://console.anthropic.com/settings/usage>.
- **Oracle Always Free**: monitor under Account → Cost Analysis. The 4
  OCPU + 24 GB ARM allocation is a hard quota — you cannot accidentally
  pay.
- **Cloudflare Free**: the Tunnel has no traffic limit on the free plan.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| `502 Bad Gateway` when visiting the URL | dashboard container is down. `docker compose logs dashboard`. |
| Browser asks for password, but right password fails | `.env` not loaded into the container. `docker compose down && docker compose up -d`. |
| `Missing Anthropic API Key` in scheduler logs | The `dashboard` container has the key but `scheduler` doesn't. Both should read `.env` via `env_file:` — verify with `docker compose config`. |
| Postgres healthcheck failing on first run | First boot of Postgres takes 30s. Retry `docker compose ps` after a minute. |
| Cloudflare Tunnel shows `connection refused` | Dashboard isn't bound to `127.0.0.1`. Check `docker compose port dashboard 8000`. |
| Out of disk space | `docker system prune -a --volumes` (⚠️ this wipes everything, including the DB volume if you pass `--volumes`). |

## Going further

- **Backups**: schedule the `pg_dump` above as a daily cron job into
  Oracle Object Storage (also Always Free, 20 GB).
- **Monitoring**: install [Netdata](https://www.netdata.cloud/) (free,
  one-line install) for live CPU/memory/disk graphs.
- **Auto-deploy on push**: add a GitHub Action that SSHs into the VM and
  runs `git pull && docker compose up -d --build` on every push to `main`.
- **Multi-user mode**: when you outgrow Basic Auth, swap in something
  like [Authentik](https://goauthentik.io/) (also self-hostable for free
  on the same VM).
