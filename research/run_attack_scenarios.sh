#!/bin/sh
# Generate labelled ATT&CK scenario events on isolated, loopback-only custom-lab sessions.
# Usage: sh research/run_attack_scenarios.sh r1 r2 r3 r4
# Output: .honeyprint/attack-scenarios/{events.sqlite3,labels.csv}. Your normal runtime is untouched.
set -u
cd "$(dirname "$0")/.."
RT="$PWD/.honeyprint/attack-scenarios"
mkdir -p "$RT"
PW=$(./.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(18))')
export HONEYPRINT_RESEARCH_PASSWORD="$PW"
for RUN in "$@"; do
  env HONEYPRINT_PLATFORM=custom HONEYPRINT_PORT=18181 HONEYPRINT_RUNTIME_DIR="$RT" \
      HONEYPRINT_INSTANCE_ID=custom_attack HONEYPRINT_ANALYST_ENABLED=false HONEYPRINT_ANALYSIS_ENABLED=false \
      HONEYPRINT_ML_MODEL_ZIP= HONEYPRINT_RUN_DURATION_SECONDS=0 \
      HONEYPRINT_SSH_ENABLED=true HONEYPRINT_SSH_HOST=127.0.0.1 HONEYPRINT_SSH_PORT=12223 \
      HONEYPRINT_SSH_USERNAME=admin HONEYPRINT_SSH_PASSWORD="$PW" \
      HONEYPRINT_TELNET_ENABLED=true HONEYPRINT_TELNET_HOST=127.0.0.1 HONEYPRINT_TELNET_PORT=12323 \
      HONEYPRINT_TELNET_USERNAME=admin HONEYPRINT_TELNET_PASSWORD="$PW" \
      HONEYPRINT_MQTT_ENABLED=true HONEYPRINT_MQTT_HOST=127.0.0.1 HONEYPRINT_MQTT_PORT=11884 \
      HONEYPRINT_MQTT_USERNAME=admin HONEYPRINT_MQTT_PASSWORD="$PW" \
      ./.venv/bin/python quickstart.py > "$RT/launcher-$RUN.log" 2>&1 &
  LAUNCHER=$!
  for i in $(seq 1 90); do
    grep -qi "ready" "$RT/launcher-$RUN.log" 2>/dev/null && break
    sleep 1
  done
  sleep 2
  ./.venv/bin/python research/generate_attack_scenarios.py --instance custom_attack --port 18181 \
    --ssh-port 12223 --telnet-port 12323 --mqtt-port 11884 --user admin \
    --database "$RT/events.sqlite3" --labels "$RT/labels.csv" --run-id "$RUN" || echo "GENERATOR FAILED $RUN"
  kill -INT $LAUNCHER
  wait $LAUNCHER
  sleep 2
done
echo ALL_DONE
