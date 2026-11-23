#!/bin/sh
# Generate labelled routine sessions (logout, single failed logins, non-default account) on isolated, loopback-only custom-lab sessions.
# Usage: sh research/run_routine_sessions.sh r1 r2 r3
# Output: .honeyprint/routine-sessions/{events.sqlite3,labels.csv}. Your normal runtime is untouched.
set -u
cd "$(dirname "$0")/.."
RT="$PWD/.honeyprint/routine-sessions"
mkdir -p "$RT"
PW=$(./.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(18))')
export HONEYPRINT_RESEARCH_PASSWORD="$PW"
for RUN in "$@"; do
  env HONEYPRINT_PLATFORM=custom HONEYPRINT_PORT=18182 HONEYPRINT_RUNTIME_DIR="$RT" \
      HONEYPRINT_INSTANCE_ID=custom_routine HONEYPRINT_ANALYST_ENABLED=false HONEYPRINT_ANALYSIS_ENABLED=false \
      HONEYPRINT_ML_MODEL_ZIP= HONEYPRINT_RUN_DURATION_SECONDS=0 \
      HONEYPRINT_SSH_ENABLED=true HONEYPRINT_SSH_HOST=127.0.0.1 HONEYPRINT_SSH_PORT=12224 \
      HONEYPRINT_SSH_USERNAME=labtech HONEYPRINT_SSH_PASSWORD="$PW" \
      HONEYPRINT_TELNET_ENABLED=true HONEYPRINT_TELNET_HOST=127.0.0.1 HONEYPRINT_TELNET_PORT=12324 \
      HONEYPRINT_TELNET_USERNAME=labtech HONEYPRINT_TELNET_PASSWORD="$PW" \
      HONEYPRINT_MQTT_ENABLED=true HONEYPRINT_MQTT_HOST=127.0.0.1 HONEYPRINT_MQTT_PORT=11885 \
      HONEYPRINT_MQTT_USERNAME=labtech HONEYPRINT_MQTT_PASSWORD="$PW" \
      ./.venv/bin/python quickstart.py > "$RT/launcher-$RUN.log" 2>&1 &
  LAUNCHER=$!
  for i in $(seq 1 90); do
    grep -qi "ready" "$RT/launcher-$RUN.log" 2>/dev/null && break
    sleep 1
  done
  sleep 2
  ./.venv/bin/python research/generate_routine_sessions.py --instance custom_routine \
    --ssh-port 12224 --telnet-port 12324 --mqtt-port 11885 --user labtech \
    --database "$RT/events.sqlite3" --labels "$RT/labels.csv" --run-id "$RUN" || echo "GENERATOR FAILED $RUN"
  kill -INT $LAUNCHER
  wait $LAUNCHER
  sleep 2
done
echo ALL_DONE
