# Shared Wishlist & Date Scheduler

A lightweight, self-hosted web app built with **Flask, SQLite, Gunicorn, and Docker** that lets couples save food spots and activity links, schedule outings with calendar reminders, and log photo memories.

---

## Problem & Solution

* **Problem:** My wife and I constantly share links to food spots and activities we are keen to try, but they quickly get buried in chat history—so we forget about them and never end up going.
* **Solution:** This app fixes that with one shared list: either of us can paste a link, pick **Dating** or **Family**, choose a **Date to visit** with an **auto-reminder synced to our calendar**, and later tap **"We went!"** to upload photos and log the memory.

---

## Tech Stack

* **Backend:** Python 3.12, Flask, Gunicorn
* **Database & Storage:** SQLite (`places.db`) + persistent volume storage for photos
* **Deployment & Networking:** Docker, Docker Compose, Cloudflare, Tailscale
* **CI/CD:** GitHub Actions (CI), Jenkins (CI/CD on homelab)

---

## Homelab Deployment & Zero-Trust Networking

* **Self-Hosted Container:** Runs 24/7 on my personal Linux homelab server via **Docker Compose** (`restart: unless-stopped`), backed by a persistent Docker volume (`wishlist_data:/data`) so the SQLite database and outing photos survive rebuilds.
* **Zero-Trust Access (Cloudflare & Tailscale):** Exposed securely via **Cloudflare** (HTTPS/TLS encryption and reverse proxy without opening inbound ports on my home router) alongside **Tailscale** for private mesh VPN management and SSH administration.

---

## CI/CD

* **GitHub Actions — CI:** Python syntax check (`python -m py_compile app.py`), Docker image build, smoke test (`curl -f http://localhost:5000/healthz`). Workflow: `.github/workflows/ci.yml`
* **Jenkins — CI/CD:** Runs on the homelab in Docker (port 8082, `jenkins/jenkins:lts`). Pipeline (`Jenkinsfile`): Checkout, Python check, Docker build, Smoke test (`smoke-test.sh`). Deploy stage next.
* **Smoke test:** Builds the image, runs it in a throwaway container with a dummy `SECRET_KEY`, checks `/healthz`, then stops and removes the container. Live site is not touched.

---

## Project Roadmap

- [x] Shared wishlist for food and activity links (`Dating` / `Family` categories)
- [x] Date picker with automated calendar reminders
- [x] "We went!" status toggle with photo memory uploads
- [x] Containerized deployment with Gunicorn, Docker Compose, Cloudflare & Tailscale
- [x] Auto-fetch link previews (title & thumbnail metadata)
- [x] GitHub Actions CI — syntax check, Docker build, smoke test
- [x] Jenkins running on homelab — CI pipeline (Checkout, Python check, Docker build, Smoke test)
- [ ] Jenkins CD — deploy to live site (`docker compose up -d --build` + `/healthz` check)
- [ ] Trivy image scan in CI
