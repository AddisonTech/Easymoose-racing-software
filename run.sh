#!/usr/bin/env bash
#
# Start the race timing console.
#
#   ./run.sh                          talk to the reader at the default IP
#   ./run.sh --reader-host 10.0.0.40  talk to a reader somewhere else
#   ./run.sh --simulate               no hardware, generated race
#
# Anything you pass is handed straight to app.py, so --port, --tx-power and
# the rest work here too.

set -euo pipefail

cd "$(dirname "$0")"

PORT=5000
for ((i = 1; i <= $#; i++)); do
    if [ "${!i}" = "--port" ]; then
        next=$((i + 1))
        PORT="${!next:-5000}"
    fi
done

if [ ! -d .venv ]; then
    echo "No .venv here. Create it first:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import flask" 2>/dev/null; then
    echo "Dependencies are missing. Run:"
    echo "  .venv/bin/pip install -r requirements.txt"
    exit 1
fi

mkdir -p races

# The console is meant to be opened from a laptop or a phone on the same
# network, so print the address they should actually type.
ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${ADDRESS}" ] || ADDRESS="$(hostname)"

echo
echo "  Easymoose race timing"
echo "  console:  http://${ADDRESS}:${PORT}/"
echo "  locally:  http://localhost:${PORT}/"
echo "  races:    $(pwd)/races"
echo

exec python app.py --host 0.0.0.0 "$@"
