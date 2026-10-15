#!/bin/sh
# Build the os-unifi-gateway OPNsense plugin package.
#
# Usage: plugin/build-pkg.sh <version> [output-dir]
#
# Needs FreeBSD's pkg(8) -- either on FreeBSD or built for Linux (see
# .github/workflows/release.yml). The package is architecture independent:
# PHP/XML for the GUI, Python for the daemon, both run by what OPNsense ships.
set -eu

VERSION="${1:?Usage: build-pkg.sh <version> [output-dir]}"
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$PLUGIN_DIR/.." && pwd)"
OUT_DIR="${2:-$ROOT_DIR/dist}"
NAME="os-unifi-gateway"
PREFIX="/usr/local"
APP_DIR="$PREFIX/opnsense/scripts/unifigateway"
WWW="https://github.com/rtm516/os-unifi-gateway"
MAINTAINER="rtm516@users.noreply.github.com"
COMMENT="Show OPNsense in UniFi Network as a UniFi gateway"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
META="$WORK/meta"
mkdir -p "$STAGE$PREFIX" "$META" "$OUT_DIR"

# ── Files ────────────────────────────────────────────────────────────────
cp -R "$PLUGIN_DIR/src/." "$STAGE$PREFIX/"
chmod 755 "$STAGE$PREFIX/etc/rc.d/unifi_gateway"

mkdir -p "$STAGE$APP_DIR/collectors"
for f in unifi_gateway.py unifi_protocol.py aes.py devices.py isp.py speedtest.py tools.py tlv.py daemon.py; do
    cp "$ROOT_DIR/$f" "$STAGE$APP_DIR/"
done
cp "$ROOT_DIR"/collectors/*.py "$STAGE$APP_DIR/collectors/"
cp "$ROOT_DIR/LICENSE" "$STAGE$APP_DIR/"

# What System > Firmware > Plugins shows for an installed plugin
VERSION_JSON=$(cat <<EOF
{
    "product_abi": "",
    "product_arch": "",
    "product_conflicts": "",
    "product_email": "$MAINTAINER",
    "product_hash": "",
    "product_id": "$NAME",
    "product_name": "unifi-gateway",
    "product_tier": "4",
    "product_version": "$VERSION",
    "product_website": "$WWW"
}
EOF
)
mkdir -p "$STAGE$PREFIX/opnsense/version"
printf '%s\n' "$VERSION_JSON" > "$STAGE$PREFIX/opnsense/version/unifi-gateway"

# ── Package scripts (same steps the opnsense/plugins framework generates) ─
cat > "$META/+POST_INSTALL" <<'EOF'
if [ -f /usr/local/etc/rc.d/configd ]; then /usr/local/etc/rc.d/configd restart; fi
if [ -f /usr/local/opnsense/mvc/script/run_migrations.php ]; then /usr/local/opnsense/mvc/script/run_migrations.php OPNsense/UnifiGateway; fi
if [ -f /usr/local/etc/rc.configure_plugins ]; then echo "Reloading plugin configuration"; /usr/local/etc/rc.configure_plugins POST_INSTALL; fi
if [ -f /usr/local/sbin/configctl ]; then echo -n "Reloading template OPNsense/UnifiGateway: "; /usr/local/sbin/configctl template reload OPNsense/UnifiGateway; fi
EOF
cat > "$META/+PRE_DEINSTALL" <<'EOF'
if [ -x /usr/local/etc/rc.d/unifi_gateway ]; then /usr/local/etc/rc.d/unifi_gateway stop || true; fi
EOF
cat > "$META/+POST_DEINSTALL" <<'EOF'
rm -f /etc/rc.conf.d/unifi_gateway
if [ -f /usr/local/etc/rc.configure_plugins ]; then echo "Reloading plugin configuration"; /usr/local/etc/rc.configure_plugins POST_DEINSTALL; fi
EOF

# ── Manifest ─────────────────────────────────────────────────────────────
DESC=$(cat "$PLUGIN_DIR/pkg-descr")
cat > "$META/+MANIFEST" <<EOF
name: "$NAME"
version: "$VERSION"
origin: "opnsense/$NAME"
comment: "$COMMENT"
maintainer: "$MAINTAINER"
www: "$WWW"
prefix: "$PREFIX"
categories: [ "net-mgmt" ]
licenselogic: "single"
licenses: [ "MIT" ]
abi: "FreeBSD:*:*"
arch: "freebsd:*:*"
desc: <<EOD
$DESC
EOD
annotations: {
    product_id: "$NAME",
    product_name: "unifi-gateway",
    product_version: "$VERSION",
    product_website: "$WWW",
    product_email: "$MAINTAINER",
    product_tier: "4"
}
EOF

# ── Packing list, owned by root ──────────────────────────────────────────
PLIST="$WORK/plist"
(cd "$STAGE" && find . -type f | sort | sed 's#^\.##') | while read -r file; do
    case "$file" in
        */rc.d/*) echo "@(root,wheel,0755) $file" ;;
        *) echo "@(root,wheel,0644) $file" ;;
    esac
done > "$PLIST"

pkg create -m "$META" -r "$STAGE" -p "$PLIST" -o "$OUT_DIR"
ls -l "$OUT_DIR/$NAME-$VERSION".pkg
