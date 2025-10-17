// monitor.js
/* eslint quotes: 0 */
const fs    = require("fs");
const net   = require("net");
const https = require("https");
const http  = require("http");
const path  = require("path");
const CDP   = require("chrome-remote-interface");

/* ───────────── CLI ───────────── */
const args = process.argv.slice(2);
if (args.length < 4) {
  console.error("Usage: node monitor.js <config.json> <chromePort> <bridgePort> <wsPort>");
  process.exit(1);
}

const CONFIG_PATH     = args[0];
let   REMOTE_DEBUG_PORT = +args;
let   BRIDGE_PORT       = +args;
let   WS_PORT           = +args;

/* ───────────── Extract username and read inject script ───────────── */
function extractUsernameFromConfig(configPath) {
  const filename = path.basename(configPath, '.json'); // "config_brendan"
  if (filename.startsWith('config_')) {
    return filename.substring('config_'.length); // "brendan" 
  }
  return null;
}


function readInjectScript(username) {
  if (!username) {
    console.warn("[!] No username extracted from config filename");
    return null;
  }
  
  const injectPath = `inject_${username}.txt`;
  try {
    if (!fs.existsSync(injectPath)) {
      console.error(`❌ Inject script not found at ${injectPath}`);
      return null;
    }
    const script = fs.readFileSync(injectPath, "utf8");
    console.log(`[✓] Loaded inject script for user: ${username}`);
    return script;
  } catch (e) {
    console.error(`[✗] Failed to read inject script: ${e.message}`);
    return null;
  }
}

/* ───────────── helpers ───────────── */
function readConfig () {
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")); }
  catch (e) { console.error("[✗] Failed to read config:", e.message); return null; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

if (!fs.existsSync(CONFIG_PATH)) {
  console.error("❌ config.json not found at", CONFIG_PATH);
  process.exit(1);
}

const initialCfg = readConfig();
if (!initialCfg) process.exit(1);

// Extract username and prepare inject script
const username = extractUsernameFromConfig(CONFIG_PATH);
const injectScript = readInjectScript(username);

REMOTE_DEBUG_PORT ||= initialCfg.ports?.chrome;
BRIDGE_PORT       ||= initialCfg.ports?.tcp;
WS_PORT           ||= initialCfg.ports?.ws;

if (!REMOTE_DEBUG_PORT || !BRIDGE_PORT || !WS_PORT) {
  console.error("❌ Missing port values.");
  process.exit(1);
}

/* ───────────── TCP bridge ───────────── */
const tcpSocket = net.createConnection({ port: BRIDGE_PORT }, () =>
  console.log(`[✓] Connected to TCP bridge on port ${BRIDGE_PORT}`)
);

tcpSocket.on("error", err => console.error("[✗] TCP bridge error:", err.message));

async function sendMessagesSequentially (lines, channelId) {
  for (const line of lines) {
    if (isPaused) { console.log("⏸️ Paused: message send cancelled"); return; }
    if (!tcpSocket.writable) { console.error("[✗] TCP socket not writable."); break; }
    tcpSocket.write(JSON.stringify({ type: "send", channelId, content: line }) + "\n");
    console.log(`[→] Sent: ${line}`);
    await sleep(10);
  }
}

/* ────────── Discord webhook notification ────────── */
function sendKeywordNotification (ticketNum, serverId, channelId, keyword) {
  const cfg = readConfig();
  const url = cfg?.discord?.webhook;
  if (!url) { console.warn("[!] No Discord webhook set."); return; }
  
  const payload = JSON.stringify({
    content: `**Keyword \`${keyword}\` detected in ticket #${ticketNum}**\n➡️ https://revolt.onech.at/server/${serverId}/channel/${channelId}`
  });
  
  const u = new URL(url);
  const req = https.request({
    hostname: u.hostname,
    path:     u.pathname + u.search,
    method:   "POST",
    headers:  {
      "Content-Type":   "application/json",
      "Content-Length": Buffer.byteLength(payload)
    }
  }, res => {
    (res.statusCode >= 200 && res.statusCode < 300)
      ? console.log(`[✓] Discord notified - keyword '${keyword}' found.`)
      : console.error(`[✗] Discord webhook failed: ${res.statusCode}`);
  });
  
  req.on("error", e => console.error("[✗] Keyword webhook error:", e.message));
  req.write(payload);
  req.end();
}

/* ────────── Extract text from embeds ────────── */
function extractEmbedText (embeds) {
  if (!embeds || !Array.isArray(embeds)) return "";
  let text = "";
  embeds.forEach(embed => {
    if (embed.title)               text += " " + embed.title;
    if (embed.description)         text += " " + embed.description;
    if (embed.fields && Array.isArray(embed.fields)) {
      embed.fields.forEach(field => {
        if (field.name)  text += " " + field.name;
        if (field.value) text += " " + field.value;
      });
    }
    if (embed.footer?.text) text += " " + embed.footer.text;
    if (embed.author?.name) text += " " + embed.author.name;
  });
  return text.toLowerCase();
}

/* ───────────── STATE ───────────── */
let trackedChannels = new Map();      // channelId -> { serverId, ticketNum, keywordFound, waitingForDeliveryInstructions, scfg, matchedKeyword }
let isPaused        = false;

/* ───────────── AUTO-PURGE ─────────────
   Clear the map every 2 minutes to avoid unbounded growth. */
const PURGE_INTERVAL_MS = 2 * 60 * 1000; // 2 min
setInterval(() => {
  if (trackedChannels.size) {
    console.log(`[♻] Auto-purge: cleared ${trackedChannels.size} tracked channel(s)`);
    trackedChannels.clear();
  }
}, PURGE_INTERVAL_MS);

/* ───────────── Control API ───────────── */
const CONTROL_PORT = REMOTE_DEBUG_PORT + 1;
http.createServer((req, res) => {
  if (req.method === "POST" && req.url === "/pause")  { isPaused = true;  console.log("⏸️ Detection paused");  return res.end("Paused");  }
  if (req.method === "POST" && req.url === "/resume") { isPaused = false; console.log("▶️ Detection resumed"); return res.end("Resumed"); }
  res.writeHead(404).end("Not Found");
}).listen(CONTROL_PORT, () =>
  console.log(`[ℹ] Control API at http://localhost:${CONTROL_PORT}`)
);

/* ───────────── Chrome Debugger ───────────── */
(async () => {
  const client = await CDP({ port: REMOTE_DEBUG_PORT });
  const { Network, Page, Runtime } = client;
  
  await Network.enable();
  await Page.enable();
  await Runtime.enable();
  
  console.log(`[✓] Connected to Chrome DevTools on port ${REMOTE_DEBUG_PORT}`);
  
  // Wait for page to be fully loaded before injecting script
  Page.domContentEventFired(async () => {
    if (injectScript) {
      try {
        console.log(`[🔧] Injecting user script for ${username}...`);
        
        // Wait a bit for the page to stabilize
        await sleep(2000);
        
        // Execute the inject script in the page context
        const result = await Runtime.evaluate({
          expression: injectScript,
          returnByValue: true,
          awaitPromise: true,
          userGesture: true
        });
        
        if (result.exceptionDetails) {
          console.error("[✗] Script injection failed:", result.exceptionDetails);
        } else {
          console.log(`[✅] Successfully injected script for ${username}`);
        }
      } catch (error) {
        console.error("[✗] Error during script injection:", error.message);
      }
    }
  });

  Network.webSocketFrameReceived(({ response }) => {
    try {
      const evt = JSON.parse(response.payloadData);
      
      /* 1) New ticket channel created - start monitoring */
      if (evt.type === "ChannelCreate" && evt.name?.startsWith("ticket-")) {
        if (isPaused) return;
        const channelId = evt._id;
        const serverId  = evt.server;
        const ticketNum = evt.name.split("ticket-")[1];
        
        trackedChannels.set(channelId, {
          serverId,
          ticketNum,
          keywordFound: false,
          waitingForDeliveryInstructions: false,
          scfg: null,
          matchedKeyword: null
        });
        
        console.log(`[🎫] New ticket #${ticketNum} - monitoring for keywords...`);
        return;
      }
      
      /* 2) Message in any tracked channel */
      if (evt.type === "Message" && trackedChannels.has(evt.channel)) {
        if (isPaused) return;
        const channelData = trackedChannels.get(evt.channel);
        
        const cfg  = readConfig();
        const scfg = cfg?.servers?.find(s => s.serverId === channelData.serverId);
        if (!scfg?.keywords) return;
        
        // Get full message text (content + embeds)
        let textToSearch = (evt.content || "").toLowerCase();
        if (evt.embeds?.length) textToSearch += " " + extractEmbedText(evt.embeds);
        
        // If keyword not found yet, check for keywords
        if (!channelData.keywordFound) {
          const matchedKeyword = scfg.keywords.find(kw =>
            textToSearch.includes(kw.toLowerCase())
          );
          
          if (matchedKeyword) {
            console.log(`[🔍] Keyword '${matchedKeyword}' detected in ticket #${channelData.ticketNum}! Now waiting for 'Delivery Instructions'...`);
            channelData.keywordFound = true;
            channelData.waitingForDeliveryInstructions = true;
            channelData.scfg = scfg;
            channelData.matchedKeyword = matchedKeyword;
            return;
          }
        }
        
        // If keyword found and waiting for delivery instructions
        if (channelData.keywordFound && channelData.waitingForDeliveryInstructions) {
          if (textToSearch.includes("buggy") || textToSearch.includes("instruction")) {
            console.log(`[📋] 'Delivery Instructions' found in ticket #${channelData.ticketNum}! Both conditions met - preparing to claim...`);
            
            // Both conditions fulfilled, proceed to claim
            queueClaim(channelData.scfg, evt.channel, channelData.ticketNum, channelData.matchedKeyword);
            return;
          }
        }
      }
    } catch { /* ignore non-JSON frames */ }
  });

  await Page.navigate({ url: "https://revolt.onech.at" });
  
  /* ─────── internal helpers ─────── */
  function queueClaim (scfg, channelId, ticketNum, matchedKeyword) {
    const delay    = Number(scfg.delay ?? 0);
    const template = scfg.claimMessage || "{num}";
    const parts    = template.split("|").map(t => t.replace("{num}", ticketNum).trim());
    
    (async () => {
      if (delay > 0) {
        console.log(`[⏱] Waiting ${delay}ms before claiming ticket #${ticketNum}...`);
        await sleep(delay);
      }
      
      if (isPaused) { console.log("⏸️ Paused: claim send skipped"); return; }
      await sendMessagesSequentially(parts, channelId);
      console.log(`[✅] Claim sent to ticket #${ticketNum}`);
      
      // Send Discord notification after successful claim
      const channelData = trackedChannels.get(channelId);
      if (channelData) {
        sendKeywordNotification(ticketNum, channelData.serverId, channelId, matchedKeyword);
      }
      
      // Clean up tracking after successful claim (small grace delay)
      setTimeout(() => {
        trackedChannels.delete(channelId);
        console.log(`[🗑️] Stopped monitoring ticket #${ticketNum}`);
      }, 1000);
    })();
  }
})();
