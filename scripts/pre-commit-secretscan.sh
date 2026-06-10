#!/usr/bin/env bash
# Block commits that contain secret-shaped *values*. Install:
#   ln -sf ../../scripts/pre-commit-secretscan.sh .git/hooks/pre-commit
# Prefer gitleaks if available; else fall back to value-pattern grep on staged files.
set -u

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks protect --staged --redact -v && exit 0 || { echo "✗ gitleaks found secrets"; exit 1; }
fi

files=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
[ -z "$files" ] && exit 0

# Match real secret VALUES, not variable names (so `FOO=$VAR` / empty `FOO=` pass).
patterns='(-----BEGIN [A-Z ]*PRIVATE KEY-----|[0-9]{8,10}:AA[A-Za-z0-9_-]{33}|(REALITY_PRIVATE_KEY|SECRET_KEY|HEALTH_TOKEN|ADMIN_PASS)=[A-Za-z0-9+/_-]{16,})'
hits=0
for f in $files; do
  # skip self, docs and binaries — they legitimately mention these names
  case "$f" in
    scripts/pre-commit-secretscan.sh|*.md|.env.example|*.png|*.jpg|*.ico|*.pem) continue;;
  esac
  if git show ":$f" 2>/dev/null | grep -nEq "$patterns"; then
    echo "✗ possible secret value in: $f"; hits=1
  fi
done
# never allow these paths to be committed at all
echo "$files" | grep -qE '(^|/)\.env$|(^|/)data/|\.pem$|/staging/' && { echo "✗ refusing to commit .env/data/cert/staging"; hits=1; }
[ "$hits" = 0 ] && { echo "✓ secret-scan CLEAN"; exit 0; } || { echo "Commit blocked. Move secrets to .env (gitignored)."; exit 1; }
