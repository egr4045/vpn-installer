#!/usr/bin/env bash
# update-xray.sh [vVERSION] — inspect stable releases, or update THIS node to an explicit version.
# Test on the standby first. Do not let the upstream installer stop every live node.
set -euo pipefail
VERSION=${1:-}
CURRENT=$(xray version | head -1 | awk '{print $2}')
if [ -z "$VERSION" ]; then
  LATEST=$(curl -fsS --max-time 20 https://api.github.com/repos/XTLS/Xray-core/releases/latest | jq -r .tag_name)
  echo "installed: $CURRENT; latest stable: $LATEST"
  echo "To update one node explicitly: update-xray.sh $LATEST (standby first)."
  exit 0
fi
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'expected an explicit vYY.M.D version' >&2; exit 1; }
[ "${VERSION#v}" != "$CURRENT" ] || { echo "already running $VERSION"; exit 0; }
[ "$(uname -m)" = x86_64 ] || { echo 'only the existing x86_64 nodes are supported' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
WORK=$(mktemp -d /tmp/safechill-xray-update.XXXXXX)
trap 'rm -rf -- "$WORK"' EXIT
curl -fsS --max-time 20 "https://api.github.com/repos/XTLS/Xray-core/releases/tags/$VERSION" > "$WORK/release.json"
URL=$(jq -r '.assets[] | select(.name=="Xray-linux-64.zip") | .browser_download_url' "$WORK/release.json")
DIGEST=$(jq -r '.assets[] | select(.name=="Xray-linux-64.zip") | .digest // empty' "$WORK/release.json")
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'release has no verifiable SHA-256 digest; nothing changed' >&2; exit 1; }
[[ "$URL" == "https://github.com/XTLS/Xray-core/releases/download/$VERSION/Xray-linux-64.zip" ]] || exit 1
curl -fLsS --max-time 120 "$URL" -o "$WORK/xray.zip"
printf '%s  %s\n' "${DIGEST#sha256:}" "$WORK/xray.zip" | sha256sum -c -
python3 - "$WORK" <<'PY'
import pathlib,sys,zipfile
root=pathlib.Path(sys.argv[1])
with zipfile.ZipFile(root/'xray.zip') as archive:
    (root/'xray').write_bytes(archive.read('xray'))
PY
chmod 755 "$WORK" "$WORK/xray"
USER_NAME=$(systemctl show xray -p User --value); USER_NAME=${USER_NAME:-root}
runuser -u "$USER_NAME" -- env XRAY_LOCATION_ASSET=/usr/local/share/xray "$WORK/xray" run -test -c /usr/local/etc/xray/config.json >/dev/null
xray-safe-apply.py checkpoint
BACKUP=/var/lib/safechill/xray/binary-before-update
install -m700 /usr/local/bin/xray "$BACKUP"
install -m755 "$WORK/xray" /usr/local/bin/xray.next
mv -f /usr/local/bin/xray.next /usr/local/bin/xray
if ! xray-safe-apply.py restart; then
  install -m755 "$BACKUP" /usr/local/bin/xray.next
  mv -f /usr/local/bin/xray.next /usr/local/bin/xray
  xray-safe-apply.py restart
  echo "update failed; previous binary $CURRENT restored" >&2
  exit 1
fi
xray version | head -1
