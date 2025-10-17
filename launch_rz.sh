#!/bin/bash

chrome_port=$1
ws_port=$2
tcp_port=$3
temp_dir=$4
config=$5
add_port=$((chrome_port + 1))

echo "[*] Ports:"
echo "    Chrome: $chrome_port"
echo "    WebSocket: $ws_port"
echo "    TCP: $tcp_port"
echo "    TempDir: $temp_dir"
echo "    Config: $config"

echo "[*] Killing any previous instances on ports..."
fuser -k $chrome_port/tcp 2>/dev/null
fuser -k $ws_port/tcp 2>/dev/null
fuser -k $tcp_port/tcp 2>/dev/null
fuser -k $add_port/tcp 2>/dev/null

echo "[*] Launching Chrome..."
google-chrome \
  --no-sandbox --remote-debugging-port="$chrome_port" \
  --user-data-dir="$temp_dir" \
  "about:blank" &

# Wait until Chrome is listening
echo "[*] Waiting for Chrome debugger to be available on port $chrome_port..."
until nc -z localhost "$chrome_port"; do sleep 0.5; done

echo "[*] Injecting DevTools script..."
node inject.js "$config" &

echo "[*] Launching WebSocket <-> TCP bridge..."
node bridge.js "$config" &

# Wait until TCP bridge is ready
echo "[*] Waiting for TCP bridge on port $tcp_port..."
until nc -z localhost "$tcp_port"; do sleep 0.5; done

echo "[*] Starting monitor with config: $config..."
node monitor.js "$config" "$chrome_port" "$tcp_port" "$ws_port"
