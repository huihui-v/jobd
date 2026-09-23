#!/bin/bash
# systemd if available, else nohup + pidfile. Token from jobd.env, not argv.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ENVFILE="$HERE/jobd.env"
PIDFILE="$HERE/jobd.pid"
LOG="$HERE/jobd.daemon.log"
UNIT_SRC="$HERE/jobd.service"
UNIT_DST=/etc/systemd/system/jobd.service

has_systemd() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

ensure_envfile() {
  if [ ! -f "$ENVFILE" ]; then
    umask 077
    cp "$HERE/jobd.env.example" "$ENVFILE"
    chmod 600 "$ENVFILE"
    echo "wrote $ENVFILE — fill JOBD_TOKEN" >&2
  fi
}

cmd=${1:-}
case "$cmd" in
  install)
    ensure_envfile
    if has_systemd; then
      cp "$UNIT_SRC" "$UNIT_DST"
      systemctl daemon-reload
      echo "installed $UNIT_DST"
    else
      echo "no systemd; use: $0 start"
    fi
    ;;
  start)
    ensure_envfile
    if has_systemd; then
      systemctl enable --now jobd
      systemctl --no-pager --full status jobd || true
    else
      if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running pid=$(cat "$PIDFILE")"
        exit 0
      fi
      set -a
      # shellcheck disable=SC1090
      . "$ENVFILE"
      set +a
      if [ -z "${JOBD_TOKEN:-}" ]; then
        echo "set JOBD_TOKEN in $ENVFILE" >&2
        exit 2
      fi
      export JOBD_TOKEN
      nohup python3 "$HERE/jobd.py" \
        --bind 127.0.0.1 --port 6006 \
        --machine-env /root/.machine.env \
        --workdir "$HERE" \
        --default-cwd /root \
        >>"$LOG" 2>&1 &
      echo $! > "$PIDFILE"
      echo "started pid=$(cat "$PIDFILE") (nohup)"
    fi
    ;;
  stop)
    if has_systemd && systemctl list-unit-files jobd.service >/dev/null 2>&1; then
      systemctl stop jobd
    elif [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo stopped
    else
      echo "not running"
    fi
    ;;
  status)
    if has_systemd && systemctl list-unit-files jobd.service >/dev/null 2>&1; then
      systemctl --no-pager --full status jobd
    elif [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDFILE")"
    else
      echo stopped
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 install|start|stop|status" >&2
    exit 2
    ;;
esac
