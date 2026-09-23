#!/bin/bash
# Non-interactive. Pass JOBD_TOKEN in the environment of this script.
# systemd if available, else nohup + pidfile. Token never on argv.
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

persist_token() {
  if [ -n "${JOBD_TOKEN:-}" ]; then
    case "$JOBD_TOKEN" in
      *$'\n'*) echo "JOBD_TOKEN must be one line" >&2; exit 2 ;;
    esac
    umask 077
    printf 'JOBD_TOKEN=%s\n' "$JOBD_TOKEN" > "$ENVFILE"
    chmod 600 "$ENVFILE"
  elif [ -f "$ENVFILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENVFILE"
    set +a
  fi
  if [ -z "${JOBD_TOKEN:-}" ]; then
    echo "JOBD_TOKEN missing (export it in the delivered script, or $ENVFILE)" >&2
    exit 2
  fi
  export JOBD_TOKEN
}

install_unit() {
  if has_systemd; then
    cp "$UNIT_SRC" "$UNIT_DST"
    systemctl daemon-reload
    echo "installed $UNIT_DST"
  fi
}

start_jobd() {
  persist_token
  install_unit
  if has_systemd; then
    systemctl enable --now jobd
    systemctl --no-pager --full status jobd || true
    return 0
  fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running pid=$(cat "$PIDFILE")"
    return 0
  fi
  nohup python3 "$HERE/jobd.py" \
    --bind 127.0.0.1 --port 6006 \
    --machine-env /root/.machine.env \
    --workdir "$HERE" \
    --default-cwd /root \
    >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started pid=$(cat "$PIDFILE") (nohup)"
}

cmd=${1:-up}
case "$cmd" in
  up|install|start)
    start_jobd
    ;;
  stop)
    if has_systemd && [ -f "$UNIT_DST" ]; then
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
    if has_systemd && [ -f "$UNIT_DST" ]; then
      systemctl --no-pager --full status jobd
    elif [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDFILE")"
    else
      echo stopped
      exit 1
    fi
    ;;
  *)
    echo "usage: JOBD_TOKEN=... $0 up|stop|status" >&2
    exit 2
    ;;
esac
