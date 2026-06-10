# Contributing

Thanks for helping! A few ground rules keep this repo safe and easy to run.

## Setup
- The admin is a FastAPI app in `vpn-admin/app/`. Config lives in `config.py`
  (generic defaults only — **never commit real values**).
- Run locally with the stack via `docker compose up --build vpn-admin`.

## Before you commit
1. `python -m py_compile vpn-admin/app/*.py`
2. `bash scripts/pre-commit-secretscan.sh` — must print CLEAN.
3. Keep host-specific values out of source: put them in `.env` / the admin
   Settings page, not in `config.py`, templates, or scripts.

## Conventions
- Templates use `__PLACEHOLDER__` tokens substituted by `install.sh` / the admin.
- Core configs are rendered from `render.py` + `templates/` — one source of
  truth shared by the installer and the web admin. Don't hand-edit rendered
  files.
- Match the surrounding code style; no new heavyweight dependencies (the box is
  1.8 GB and assets are bundled locally so they survive censored networks).

## PRs
Small, focused PRs with a clear description. Note any change that affects the
live data path (xray/sing-box/nginx) so it can be tested carefully.
