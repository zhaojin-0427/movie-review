#!/bin/bash
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
service=movie-review.service
backup_dir=/var/backups/movie-review
install -d -m 0700 "$backup_dir"
exec 8>"$backup_dir/.lock"
flock -x 8
work=$(mktemp -d "$backup_dir/.backup.XXXXXXXX")
was_active=0
cleanup() {
    status=$?
    trap - EXIT
    rm -rf -- "$work"
    if (( was_active )); then systemctl start "$service" || status=1; fi
    exit "$status"
}
trap cleanup EXIT
if systemctl is-active --quiet "$service"; then
    was_active=1
    systemctl stop "$service"
fi
backup_file="movie-review-$(date -u +%Y%m%dT%H%M%SZ)-${work##*.}.tar.gz"
/usr/bin/python3 - "$work/review.sqlite3" <<'PYTHON'
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect('file:/var/lib/movie-review/review.sqlite3?mode=ro', uri=True)) as source:
    with closing(sqlite3.connect(sys.argv[1])) as snapshot:
        source.backup(snapshot)
PYTHON
state_files=(assets)
if [[ -d /var/lib/movie-review/execution-incoming ]]; then
    state_files+=(execution-incoming)
fi
tar -czf "$work/$backup_file" -C "$work" review.sqlite3 -C /var/lib/movie-review "${state_files[@]}" -C /etc movie-review.env
if (( was_active )); then
    systemctl start "$service"
    was_active=0
fi
mkdir "$work/restored"
tar -xzf "$work/$backup_file" -C "$work/restored"
/usr/bin/python3 "$script_dir/verify-backup.py" "$work/restored"
(cd "$work" && sha256sum "$backup_file" > "$backup_file.sha256")
mv -- "$work/$backup_file" "$work/$backup_file.sha256" "$backup_dir/"
printf 'Backup: %s/%s\n' "$backup_dir" "$backup_file"
