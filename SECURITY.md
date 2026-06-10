# Security

## No secrets in the repo
- All secrets (Reality private key, admin password, session/health tokens,
  provider & Telegram tokens) live in a **gitignored `.env`** or in
  `/data/settings.json`. `config.py` ships only generic placeholders.
- `.gitignore` excludes `.env`, `/data`, `*.pem`, `staging/`, and the rendered
  `docker-compose.yml`/`nginx.conf`.
- A pre-commit hook (`scripts/pre-commit-secretscan.sh`) blocks commits that
  contain key/token-shaped strings. Install it with:
  ```bash
  ln -sf ../../scripts/pre-commit-secretscan.sh .git/hooks/pre-commit
  ```
  CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push.

## If you forked from a live box
Rotate anything that ever touched a commit: `git log -p | grep -i`-audit, and if
in doubt regenerate the Reality keypair (note: this invalidates existing client
links), admin password, and all tokens.

## Reporting a vulnerability
Open a private security advisory on GitHub, or email the maintainer. Please do
not file public issues for exploitable bugs.

## Hardening notes
- The admin holds the Docker socket (root-equivalent). Keep `:8080` bound to
  localhost / behind the panel domain; never expose it raw to the internet.
- Dangerous actions (restart, cert reissue, disk cleanup) validate config
  (`xray -test`, `nginx -t`) before applying.
