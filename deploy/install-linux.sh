#!/bin/bash
set -euo pipefail
umask 027
if [[ $# != 2 || $EUID != 0 ]]; then
    echo 'Usage: sudo install-linux.sh /absolute/bundle /absolute/private-accounts.json' >&2
    exit 2
fi
bundle=$1
accounts=$2
app_root=/opt/movie-review
state=/var/lib/movie-review
service_user=movie-review

if ! getent passwd "$service_user" >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir "$state" --shell /usr/sbin/nologin "$service_user"
fi
install -d -m 0755 "$app_root"
install -d -o "$service_user" -g "$service_user" -m 0750 "$state" "$state/tmp"
if [[ ! -e /etc/movie-review.env ]]; then
    (umask 077; /usr/bin/python3 -c 'import secrets; print("MOVIE_REVIEW_SECRET=" + secrets.token_hex(32))' > /etc/movie-review.env)
fi
install -m 0700 -d "$app_root/.incoming"
cp -R "$bundle/app" "$bundle/lib" "$app_root/.incoming/"
chown -R root:root "$app_root/.incoming"
chmod -R a+rX,go-w "$app_root/.incoming"
if systemctl is-active --quiet movie-review.service; then systemctl stop movie-review.service; fi
for part in app lib; do
    if [[ -e "$app_root/$part" ]]; then
        if [[ -e "$app_root/$part.previous" ]]; then rm -rf -- "$app_root/$part.previous"; fi
        mv -- "$app_root/$part" "$app_root/$part.previous"
    fi
    mv -- "$app_root/.incoming/$part" "$app_root/$part"
done
rmdir "$app_root/.incoming"

install -m 0644 "$bundle/movie-review.service" /etc/systemd/system/movie-review.service
install -o "$service_user" -g "$service_user" -m 0600 "$accounts" "$state/tmp/deploy-accounts.json"
trap 'rm -f -- /var/lib/movie-review/tmp/deploy-accounts.json' EXIT
set -a
source /etc/movie-review.env
set +a
run() {
    runuser -u "$service_user" -- env MOVIE_REVIEW_SECRET="$MOVIE_REVIEW_SECRET" \
        MOVIE_REVIEW_DATA="$state" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$app_root/lib:$app_root/app" \
        /usr/bin/python3 -m flask --app 'app:create_app()' "$@"
}
run provision "$state/tmp/deploy-accounts.json"
run seed "$app_root/app/demo"

systemctl daemon-reload
systemctl enable --now movie-review.service
