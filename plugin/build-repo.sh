#!/bin/sh
# Turn a directory of packages into a pkg(8) repository.
#
# Usage: plugin/build-repo.sh <repo-dir> <repo-url>
#
# <repo-dir> must contain the packages under All/. Writes the catalogue
# (meta.conf, packagesite.pkg, data.pkg) plus unifi-gateway.conf, the file
# users drop into /usr/local/etc/pkg/repos/ to add the repository.
set -eu

REPO_DIR="${1:?Usage: build-repo.sh <repo-dir> <repo-url>}"
REPO_URL="${2:?Usage: build-repo.sh <repo-dir> <repo-url>}"

pkg repo "$REPO_DIR"

# pkg links the catalogues under their legacy names; a static web host
# (such as GitHub Pages) serves links as text, so copy them
for link in "$REPO_DIR"/*; do
    if [ -L "$link" ]; then
        target="$(readlink -f "$link")"
        rm "$link" && cp "$target" "$link"
    fi
done

cat > "$REPO_DIR/unifi-gateway.conf" <<EOF
unifi-gateway: {
  url: "$REPO_URL",
  enabled: yes,
  priority: 10
}
EOF

ls -l "$REPO_DIR" "$REPO_DIR/All"
