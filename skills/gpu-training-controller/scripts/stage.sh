#!/usr/bin/env bash
# Run long control-host stages detached, with a log and an exit code, so an SSH drop never kills them.
#
#   stage.sh run NAME -- COMMAND [ARGS...]   start detached; log ~/stages/NAME.log, exit code ~/stages/NAME.exit
#   stage.sh wait NAME [MAX_MINUTES]         block until it ends; prints the log tail; returns its exit code
#                                            (a stage whose process died without an exit code returns 125)
#   stage.sh stop NAME                       stop a running stage (TERM, then KILL) and record exit 143
#   stage.sh status                          every stage: running | exit N | died
#
# Examples (CPU work on the control host, never on a rented GPU): an image build and push, a dataset
# download/conversion/upload, and both at once: `stage.sh run image -- bash -c 'docker build ... && docker push ...'`
# and `stage.sh run data -- python3 -m project.prepare_data ...`, then `stage.sh wait image` and `stage.sh wait data`.
set -euo pipefail
DIR=${STAGE_DIR:-$HOME/stages}
mkdir -p "$DIR"

case "${1:-}" in
  run)
    name=${2:?stage name}; shift 2; [ "${1:-}" = "--" ] && shift
    [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "stage name: letters, digits, . _ -" >&2; exit 1; }
    if [ -f "$DIR/$name.pid" ] && kill -0 "$(cat "$DIR/$name.pid")" 2>/dev/null; then
      echo "stage $name is already running (pid $(cat "$DIR/$name.pid"))" >&2; exit 1
    fi
    rm -f "$DIR/$name.exit"
    S=$(command -v setsid || true)
    nohup $S bash -c '"$@"; echo $? > "'"$DIR/$name"'.exit"' _ "$@" > "$DIR/$name.log" 2>&1 < /dev/null &
    echo $! > "$DIR/$name.pid"
    echo "started $name (pid $!, log $DIR/$name.log)";;
  wait)
    name=${2:?stage name}; max=${3:-120}; end=$(( $(date +%s) + max * 60 ))
    while [ ! -f "$DIR/$name.exit" ]; do
      if ! kill -0 "$(cat "$DIR/$name.pid" 2>/dev/null)" 2>/dev/null; then
        sleep 1; [ -f "$DIR/$name.exit" ] && break      # the exit code may land just after the process ends
        echo "stage $name died without an exit code (killed, or the host rebooted)" >&2; tail -5 "$DIR/$name.log"; exit 125
      fi
      [ "$(date +%s)" -ge "$end" ] && { echo "stage $name still running after $max min" >&2; tail -5 "$DIR/$name.log"; exit 124; }
      sleep 5
    done
    tail -20 "$DIR/$name.log"; code=$(head -n1 "$DIR/$name.exit")
    [[ "$code" =~ ^[0-9]+$ ]] || code=125                       # an empty exit file: killed while writing it
    echo "stage $name exit $code"; exit "$code";;
  stop)
    name=${2:?stage name}; pid=$(cat "$DIR/$name.pid" 2>/dev/null) || { echo "no stage $name" >&2; exit 1; }
    if kill -0 "$pid" 2>/dev/null; then
      pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done   # let it record its exit
      kill -0 "$pid" 2>/dev/null && { pkill -KILL -P "$pid" 2>/dev/null; kill -KILL "$pid" 2>/dev/null; sleep 0.5; }
      [ -s "$DIR/$name.exit" ] || echo 143 > "$DIR/$name.exit"; echo "stopped $name"
    else echo "stage $name is not running"; fi;;
  status)
    for pid in "$DIR"/*.pid; do
      [ -e "$pid" ] || continue; n=$(basename "$pid" .pid)
      if [ -s "$DIR/$n.exit" ]; then echo "$n: exit $(head -n1 "$DIR/$n.exit")"
      elif kill -0 "$(cat "$pid")" 2>/dev/null; then echo "$n: running"
      else echo "$n: died (no exit code)"; fi
    done;;
  *) echo "usage: $0 run NAME -- CMD... | wait NAME [MAX_MINUTES] | stop NAME | status" >&2; exit 1;;
esac
