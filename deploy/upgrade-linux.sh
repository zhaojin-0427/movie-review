#!/bin/bash
set -euo pipefail
umask 027
if [[ $# != 1 || $EUID != 0 || ! -d $1 ]]; then
    echo 'Usage: sudo upgrade-linux.sh /absolute/staged-app' >&2
    exit 2
fi
staged=$1
app_root=/opt/movie-review
service=movie-review.service
health_url=http://10.10.10.247:8080/health
if [[ -f /etc/systemd/system/movie-review.service.d/public-listeners.conf ]]; then
    health_url=http://127.0.0.1:18080/health
fi
exec 9>"$app_root/.upgrade-lock"
flock -x 9
incoming=$(mktemp -d "$app_root/.upgrade.XXXXXXXX")
previous="$app_root/app.previous-${incoming##*.}"
old_moved=0
new_moved=0
server_stopped=0
cleanup() {
    status=$?
    trap - EXIT
    if (( status != 0 )); then
        if (( new_moved )); then
            systemctl stop "$service" || true
            mv -- "$app_root/app" "$incoming/failed"
        fi
        if (( old_moved )); then mv -- "$previous" "$app_root/app"; fi
        if (( server_stopped )); then systemctl start "$service" || true; fi
    fi
    rm -rf -- "$incoming"
    exit "$status"
}
trap cleanup EXIT
cp -R -- "$staged" "$incoming/app"
chown -R root:root "$incoming/app"
chmod -R a+rX,go-w "$incoming/app"
systemctl stop "$service"
server_stopped=1
bash "$incoming/app/deploy/backup-linux.sh"
mv -- "$app_root/app" "$previous"
old_moved=1
mv -- "$incoming/app" "$app_root/app"
new_moved=1
set -a
source /etc/movie-review.env
set +a
runuser -u movie-review -- env MOVIE_REVIEW_SECRET="$MOVIE_REVIEW_SECRET" \
    MOVIE_REVIEW_DATA=/var/lib/movie-review PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$app_root/lib:$app_root/app" /usr/bin/python3 -m flask --app 'app:create_app()' init-db
systemctl start "$service"
for (( attempt=0; attempt < 15; attempt++ )); do
    if curl --noproxy '*' -fsS --max-time 2 "$health_url"; then
        printf '\nPrevious application: %s\n' "$previous"
        exit 0
    fi
    sleep 1
done
exit 1
