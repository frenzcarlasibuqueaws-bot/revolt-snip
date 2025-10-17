const fs = require("fs");
const WebSocket = require("ws");
const net = require("net");

// Load config
const configPath = process.argv[2];
if (!configPath || !fs.existsSync(configPath)) {
  console.error("[✗] CONFIG_FILE not provided or invalid.");
  process.exit(1);
}

const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
const DEVTOOLS_PORT = config.ports.ws;
const BRIDGE_PORT = config.ports.tcp;

console.log(`[✓] Starting WebSocket server at ws://localhost:${DEVTOOLS_PORT}`);
const wss = new WebSocket.Server({ port: DEVTOOLS_PORT });

let browserInjectedClient = null;

// WebSocket: page (injected JS) <-> Node.js
wss.on("connection", (ws) => {
  console.log("[✓] Browser page (injected script) connected.");
  browserInjectedClient = ws;

  ws.on("message", (msg) => {
    console.log("[⇦] From Browser:", msg.toString());
  });

  ws.on("close", () => {
    console.warn("[!] Browser WebSocket disconnected.");
    browserInjectedClient = null;
  });
});

// TCP server: monitor <-> bridge <-> browser
const bridgeServer = net.createServer((socket) => {
  socket.on("data", (data) => {
    const lines = data.toString().split("\n").filter(Boolean);
    for (const line of lines) {
      console.log("[⇨] Forwarding to browser:", line);
      if (browserInjectedClient && browserInjectedClient.readyState === WebSocket.OPEN) {
        browserInjectedClient.send(line);
      } else {
        console.warn("[!] No browser WebSocket client connected.");
      }
    }
  });

  socket.on("error", (err) => {
    console.error("[✗] TCP socket error:", err.message);
  });
});

bridgeServer.listen(BRIDGE_PORT, () => {
  console.log(`[✓] TCP bridge listening on port ${BRIDGE_PORT}`);
});
