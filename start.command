#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -f library.sqlite ]]; then
  python3 app.py seed --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

url="http://127.0.0.1:8765/"

# Double-clicking should reuse an already-running local service when possible.
if curl -fsS --max-time 1 "$url" >/dev/null 2>&1; then
  /usr/bin/open "$url"
  exit 0
fi

python3 app.py serve &
server_pid=$!
cleanup() {
  kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Open the browser as soon as the HTTP endpoint is ready, then keep the
# terminal attached to the server so closing the double-clicked window stops it.
for _ in {1..50}; do
  if curl -fsS --max-time 1 "$url" >/dev/null 2>&1; then
    /usr/bin/open "$url"
    break
  fi
  sleep 0.1
done

wait "$server_pid"
