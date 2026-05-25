# Deploying Signal Tracker — pick your host

The project ships a generic `docker-compose.yml` + `Dockerfile` + `scripts/deploy.sh`
that work on any Linux host with Docker. The differences between providers are
in the **provisioning** (how you get a VM) and the **HTTPS** layer (Cloudflare
Tunnel vs Let's Encrypt vs the provider's load balancer).

Pick the guide that matches the host you can actually get:

| Provider | Cost | Pros | Cons | Guide |
|----------|------|------|------|-------|
| **GCP e2-micro** | 0 € | Truly free forever (Always Free tier). Reliable. | Only 1 GB RAM — uses the lite compose with SQLite (or Neon Postgres). | [`gcp.md`](deploy/gcp.md) |
| **Hetzner Cloud** | ~€4 / month | Cheapest reliable host with full RAM headroom. Instant provisioning. EU data centers. | Paid. | [`hetzner.md`](deploy/hetzner.md) |
| **Fly.io** | $0–$3 / month (after trial credit) | ARM-native, no VM management. | Different deploy model (`fly.toml` instead of compose). Postgres is paid; pair with **Neon** free Postgres. | [`flyio.md`](deploy/flyio.md) |
| **Oracle Always Free** | 0 € | Free forever, generous shape (up to 4 OCPU + 24 GB on ARM). | Capacity often unavailable for weeks. | [`oracle.md`](deploy/oracle.md) |

## My recommendation in 1 line

**If you want zero euros and can deal with 1 GB RAM: GCP e2-micro Always Free.**
**If you're done fighting cloud quotas and don't mind ~€4/month: Hetzner.**
Both deploy the same Docker image, so you can move between them later.

## Shared building blocks (all guides reference these)

- **Code**: `git clone -b <branch> https://github.com/M13-Prime/Score_Simulator.git`
- **Bootstrap**: `bash scripts/deploy.sh` (idempotent, safe to re-run)
- **HTTPS**: Cloudflare Tunnel (zero-config, no Let's Encrypt needed)
- **Auth**: HTTP Basic Auth on every dashboard route, gated on
  `DASHBOARD_AUTH_USER` / `DASHBOARD_AUTH_PASSWORD` in `.env`.

## What you need before starting (any provider)

- An Anthropic / OpenAI / Gemini API key (Anthropic recommended)
- A Cloudflare account (free) — used for DNS + HTTPS
- A domain name (~€10/year on Cloudflare Registrar, or any registrar)
- SSH key pair (`ssh-keygen -t ed25519` if you don't have one)
