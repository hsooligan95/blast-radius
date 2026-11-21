#!/usr/bin/env bash
# BlastRadius demo: happy path, then a kill -9 mid-run with resume + rollback.
#
#   Part 1  idle-remediation: plan -> approve -> execute -> verify passes.
#   Part 2  failed-deployment: the agent is killed with SIGKILL after two
#           checkpointed steps, a fresh process resumes from the SQLite
#           checkpoint, verification fails on the persistently-faulted
#           node-3, and every executed step is rolled back. The demo ends
#           by proving the final cloud state is byte-identical to the
#           initial snapshot.
#
# The "cloud" is a moto server process, so it survives the agent's death —
# exactly like a real cloud would. Run from the repo root with the venv
# active dependencies installed:  bash scripts/demo_kill_resume.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
PORT="${BLASTRADIUS_DEMO_PORT:-5555}"
export BLASTRADIUS_AWS_ENDPOINT="http://127.0.0.1:${PORT}"
export BLASTRADIUS_RUNS_DIR="$PWD/runs"
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing
export AWS_DEFAULT_REGION=us-east-1 AWS_EC2_METADATA_DISABLED=true
OUT="$PWD/demo-output"
mkdir -p "$OUT"
rm -rf runs/demo-idle runs/demo-crash

echo "### starting moto server (the mock cloud) on port ${PORT}"
$PY -m moto.server -H 127.0.0.1 -p "$PORT" >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:${PORT}/moto-api/" >/dev/null && break || sleep 0.5
done

summarize() {
  $PY - "$1" <<'EOF'
import json, sys
snap = json.load(open(sys.argv[1]))
for iid, i in sorted(snap["instances"].items(), key=lambda kv: kv[1]["tags"].get("Name", "")):
    t = i["tags"]
    print(f"  instance {t.get('Name', iid)}: state={i['state']} version={t.get('version')} health={t.get('health')}")
for vid, v in sorted(snap["volumes"].items(), key=lambda kv: kv[1]["tags"].get("Name", "")):
    t = v["tags"]
    print(f"  volume {t.get('Name', vid)}: state={v['state']} {v['size']}GiB {v['type']} protected={t.get('protected')} attached={v['attachments']}")
EOF
}

echo
echo "==================================================================="
echo "PART 1 — idle-remediation (happy path)"
echo "==================================================================="
$PY -m blastradius.cli seed --scenario idle-remediation >/dev/null
echo "seeded: 2 orphan volumes, 1 protected volume, 1 attached volume, 1 idle + 1 active instance"
$PY -m blastradius.cli run --scenario idle-remediation --run-id demo-idle --auto-approve
echo
echo "--- audit log (demo-idle) ---"
$PY -m blastradius.cli audit --run-id demo-idle

echo
echo "### resetting the mock cloud"
curl -sf -X POST "http://127.0.0.1:${PORT}/moto-api/reset" >/dev/null

echo
echo "==================================================================="
echo "PART 2 — failed-deployment: kill -9 mid-run, resume, rollback"
echo "==================================================================="
$PY -m blastradius.cli seed --scenario failed-deployment >/dev/null
$PY -m blastradius.cli snapshot --out "$OUT/initial.json" >/dev/null
echo "--- initial cloud state ---"
summarize "$OUT/initial.json"

echo
echo "### starting the agent in the background (3s delay before each mutation)"
$PY -u -m blastradius.cli run --scenario failed-deployment --run-id demo-crash \
  --auto-approve --step-delay 3 >"$OUT/run1.log" 2>&1 &
AGENT_PID=$!
for _ in $(seq 1 120); do
  n=$(grep -c '"event": "step_applied"' runs/demo-crash/audit.jsonl 2>/dev/null || true)
  [ "${n:-0}" -ge 2 ] && break
  sleep 0.5
done
echo "### two steps are durably checkpointed. Killing the agent: kill -9 $AGENT_PID"
kill -9 "$AGENT_PID"
wait "$AGENT_PID" 2>/dev/null || true
echo "--- what the agent printed before it died ---"
cat "$OUT/run1.log"
$PY -m blastradius.cli snapshot --out "$OUT/mid.json" >/dev/null
echo "--- cloud state at the moment of the crash (node-2 fixed, node-3 untouched) ---"
summarize "$OUT/mid.json"
echo "--- checkpoint state a fresh process would see ---"
$PY -m blastradius.cli status --run-id demo-crash

echo
echo "### resuming with a fresh process (no step delay)"
$PY -m blastradius.cli resume --run-id demo-crash

echo
$PY -m blastradius.cli snapshot --out "$OUT/final.json" >/dev/null
echo "--- final cloud state ---"
summarize "$OUT/final.json"
echo "--- proof: initial vs final snapshot ---"
$PY -m blastradius.cli compare --a "$OUT/initial.json" --b "$OUT/final.json"
echo
echo "--- full audit log (demo-crash): plan, approval, crash, resume, verify, rollback ---"
$PY -m blastradius.cli audit --run-id demo-crash
echo
echo "DEMO OK — killed mid-run, resumed from checkpoint, verification failed, full rollback, state identical."
