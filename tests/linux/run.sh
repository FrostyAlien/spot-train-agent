#!/usr/bin/env bash
# Linux tests of the stall watchdog (observe skill: progress_watch.py) in Docker, CPU only, about 15 minutes.
# Usage: tests/linux/run.sh [LOG_DIR] [SCENARIO ...]   (default: all of t1-t8)
# Builds gpu-skills-wdtest:1 (python:3.12-slim, CPU PyTorch, Lightning) once; the helpers are mounted read-only.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); SKILLS=$(cd "$HERE/../../skills" && pwd)
LOG=${1:-$HERE/logs}; shift || true; mkdir -p "$LOG"
IMG=gpu-skills-wdtest:1
docker image inspect $IMG >/dev/null 2>&1 || docker build -t $IMG "$HERE" > "$LOG/build.log" 2>&1 || { echo "build failed: $LOG/build.log"; exit 1; }
docker run --rm $IMG sh -c 'python3 --version; pip list 2>/dev/null | grep -E "^(torch|lightning) "' > "$LOG/versions.txt"
fails=0
scenarios=("$@"); [ ${#scenarios[@]} -gt 0 ] || scenarios=(t1 t2 t3 t4 t5 t6 t7 t7b t8)
for s in "${scenarios[@]}"; do
  extra=()
  [ "$s" = t6 ] && extra=(--device-write-bps "${WRITE_DEVICE:-/dev/vda}:10mb")   # the Docker VM's disk
  docker run --rm --shm-size 1g "${extra[@]}" -v "$SKILLS/gpu-training-observe/scripts:/opt/gpu-skills:ro" \
    -v "$SKILLS/gpu-training-recover/scripts:/opt/recover:ro" -v "$HERE:/w:ro" -e PYTHONPATH=/opt/gpu-skills \
    $IMG python3 /w/check.py "$s" > "$LOG/$s.log" 2>&1 || fails=$((fails + 1))
  head -1 "$LOG/$s.log"
done
echo "failed: $fails"
exit $((fails > 0))
