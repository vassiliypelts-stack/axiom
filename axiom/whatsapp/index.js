/**
 * AXIOM — WhatsApp канал на Baileys.
 *
 * Что делает:
 *  • держит коннект к WhatsApp (multi-device, как WhatsApp Web), логин по QR один раз;
 *  • LISTEN  — ловит входящие, отдаёт их Python-мосту (ИИ-агент), шлёт ответ по частям;
 *  • OUTREACH — берёт у моста список «кому писать первым» и рассылает с антибан-паузами.
 *
 * Мозг (агент, книжка, встречи) живёт в Python: channels/wa_bridge.py.
 * Этот процесс — только транспорт WhatsApp.
 *
 * Запуск:
 *    npm install            # один раз
 *    node index.js          # listen (слушать и отвечать)
 *    node index.js --outreach 3   # сначала разослать 3 первых, потом слушать
 *
 * При первом старте в терминале появится QR — отсканируй в WhatsApp:
 *   Настройки → Связанные устройства → Привязать устройство.
 * Сессия сохранится в ./auth, второй раз QR не нужен.
 */
import baileys, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import pino from "pino";

const makeWASocket = baileys.default || baileys;

const BRIDGE = process.env.AXIOM_BRIDGE || "http://127.0.0.1:8100";
let AUTH_DIR = "./auth"; // отдельная папка-сессия на каждый WhatsApp-номер (см. --auth)

// --- Антибан (зеркало telegram.py) ---
const OUTREACH_PAUSE = [40000, 130000]; // мс между первыми сообщениями
const REPLY_DELAY = [4000, 18000];      // мс перед началом ответа
const TYPING_CPS = [12, 22];            // знаков/сек (время набора ∝ длине)
const MAX_TYPING_MS = 9000;             // потолок имитации набора
const PART_PAUSE = [1200, 3500];        // мс между соседними сообщениями

const rnd = (a, b) => Math.floor(a + Math.random() * (b - a));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const logger = pino({ level: "silent" });

// --- аргументы ---
const argv = process.argv.slice(2);
let outreachLimit = 0;
const oi = argv.indexOf("--outreach");
if (oi !== -1) outreachLimit = parseInt(argv[oi + 1] || "0", 10) || 0;
let matchLimit = 0;
const mi = argv.indexOf("--match");
if (mi !== -1) matchLimit = parseInt(argv[mi + 1] || "300", 10) || 300;
let pingNums = [];
let pingN = 3;
const pi = argv.indexOf("--ping");
if (pi !== -1) pingNums = (argv[pi + 1] || "").split(",").map((s) => s.trim()).filter(Boolean);
const ni = argv.indexOf("--n");
if (ni !== -1) pingN = parseInt(argv[ni + 1] || "3", 10) || 3;
// отдельная сессия на номер: --auth 79673189708 → папка ./auth_79673189708
const ai = argv.indexOf("--auth");
if (ai !== -1 && argv[ai + 1]) AUTH_DIR = "./auth_" + argv[ai + 1].replace(/[^\w]/g, "");
// прокси на этот номер: --proxy socks5://user:pass@host:port (или env AXIOM_PROXY)
let PROXY = process.env.AXIOM_PROXY || "";
const pxi = argv.indexOf("--proxy");
if (pxi !== -1 && argv[pxi + 1]) PROXY = argv[pxi + 1];
// привязка по КОДУ вместо QR (надёжнее в терминале): --pair (нужен --auth <номер>)
const usePair = argv.includes("--pair");
const authNumber = (ai !== -1 ? String(argv[ai + 1] || "") : "").replace(/\D/g, "");
// рассылка первого сообщения кампании по WhatsApp: --wacampaign <id кампании>
let waCampaign = 0;
const wci = argv.indexOf("--wacampaign");
if (wci !== -1) waCampaign = parseInt(argv[wci + 1] || "0", 10) || 0;

// короткие фразы для теста/прогрева
const WARM_CHATTER = ["привет)", "как дела?", "тест связи", "на связи", "всё ок?", "добрый день"];
let kickoffDone = false; // одноразовые действия (ping/wacampaign/match/outreach) — только при первом подключении

/** Извлекает текст из входящего сообщения (разные типы Baileys). */
function extractText(m) {
  const c = m.message || {};
  return (
    c.conversation ||
    c.extendedTextMessage?.text ||
    c.imageMessage?.caption ||
    c.videoMessage?.caption ||
    ""
  ).trim();
}

/** Отправляет части как живой человек: «печатает…», паузы ∝ длине, паузы между. */
async function sendParts(sock, jid, parts) {
  const clean = (parts || []).map((p) => (p || "").trim()).filter(Boolean);
  for (let i = 0; i < clean.length; i++) {
    const part = clean[i];
    const typing = Math.min((part.length / rnd(...TYPING_CPS)) * 1000, MAX_TYPING_MS);
    await sock.sendPresenceUpdate("composing", jid);
    await sleep(Math.max(1200, typing));
    await sock.sendPresenceUpdate("paused", jid);
    await sock.sendMessage(jid, { text: part });
    if (i < clean.length - 1) await sleep(rnd(...PART_PAUSE));
  }
}

async function bridgeGet(path) {
  const r = await fetch(`${BRIDGE}${path}`);
  return r.json();
}
async function bridgePost(path, body) {
  const r = await fetch(`${BRIDGE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

/** Рассылка первых сообщений: берём список у моста, шлём, отчитываемся. */
async function runOutreach(sock, limit) {
  let data;
  try {
    data = await bridgeGet(`/wa/outreach?limit=${limit}`);
  } catch (e) {
    console.error(`[outreach] мост недоступен (${BRIDGE}). Запусти: python -m channels.wa_bridge`);
    return;
  }
  const list = data.contacts || [];
  if (!list.length) {
    console.log("[outreach] некого писать (нет новых контактов с телефоном).");
    return;
  }
  console.log(`[outreach] кандидатов: ${list.length}`);
  let sent = 0;
  for (const c of list) {
    const digits = String(c.phone || "").replace(/\D/g, "");
    if (digits.length < 10) {
      console.log(`[skip] contact ${c.contact_id}: кривой телефон «${c.phone}»`);
      continue;
    }
    let jid;
    try {
      const res = await sock.onWhatsApp(digits);
      if (!res || !res[0]?.exists) {
        console.log(`[skip] contact ${c.contact_id}: номер ${digits} не в WhatsApp`);
        continue;
      }
      jid = res[0].jid;
    } catch (e) {
      console.log(`[skip] contact ${c.contact_id}: onWhatsApp error ${e.message}`);
      continue;
    }
    await sendParts(sock, jid, c.parts);
    const text = (c.parts || []).join("\n");
    await bridgePost("/wa/sent", { contact_id: c.contact_id, jid, text });
    sent++;
    console.log(`[sent ${sent}/${list.length}] -> ${c.contact_id} (${jid})`);
    if (sent < list.length) await sleep(rnd(...OUTREACH_PAUSE));
  }
  console.log(`[outreach] готово: отправлено ${sent}.`);
}

/** Матчинг телефонов → WhatsApp: проверяем номера, помечаем has_wa и сохраняем jid. */
async function runMatch(sock, limit) {
  let data;
  try {
    data = await bridgeGet(`/wa/to_check?limit=${limit}`);
  } catch (e) {
    console.error(`[match] мост недоступен (${BRIDGE}). Запусти: python -m channels.wa_bridge`);
    return;
  }
  const list = data.contacts || [];
  if (!list.length) {
    console.log("[match] нечего проверять (все номера уже размечены).");
    return;
  }
  console.log(`[match] к проверке номеров: ${list.length}`);
  let yes = 0, no = 0;
  for (const c of list) {
    const digits = String(c.phone || "").replace(/\D/g, "");
    if (digits.length < 10) {
      await bridgePost("/wa/mark", { contact_id: c.contact_id, has_wa: "no" });
      no++;
      continue;
    }
    try {
      const res = await sock.onWhatsApp(digits);
      if (res && res[0]?.exists) {
        await bridgePost("/wa/mark", { contact_id: c.contact_id, has_wa: "yes", jid: res[0].jid });
        yes++;
        console.log(`[wa ✓] ${c.contact_id} ${digits}`);
      } else {
        await bridgePost("/wa/mark", { contact_id: c.contact_id, has_wa: "no" });
        no++;
      }
    } catch (e) {
      console.log(`[match err] ${c.contact_id} (${digits}): ${e.message}`);
    }
    await sleep(rnd(1500, 3500)); // антибан: не строчим проверки подряд
  }
  console.log(`[match] готово: в WhatsApp ${yes}, нет ${no}, проверено ${yes + no}.`);
}

/** Рассылка ПЕРВОГО сообщения кампании по WhatsApp (агент пишет первым). */
async function runWaCampaign(sock, cid) {
  let data;
  try {
    data = await bridgeGet(`/wa/campaign_outreach?cid=${cid}&limit=10`);
  } catch (e) {
    console.error(`[wacampaign] мост недоступен (${BRIDGE}). Запусти: python -m channels.wa_bridge`);
    return;
  }
  const list = data.contacts || [];
  if (!list.length) { console.log("[wacampaign] некому слать (нет WA-контактов кампании)"); return; }
  const accId = data.account_id;
  console.log(`[wacampaign #${cid}] кандидатов: ${list.length}`);
  let sent = 0;
  for (const c of list) {
    const digits = String(c.phone || "").replace(/\D/g, "");
    if (digits.length < 10) { console.log(`[skip] ${c.contact_id} кривой телефон`); continue; }
    let jid;
    try {
      const r = await sock.onWhatsApp(digits);
      if (!r || !r[0]?.exists) { console.log(`[skip] ${digits} не в WhatsApp`); continue; }
      jid = r[0].jid;
    } catch (e) { console.log(`[err] ${digits}: ${e.message}`); continue; }
    await sendParts(sock, jid, c.parts);
    await bridgePost("/wa/sent", { contact_id: c.contact_id, jid, text: (c.parts || []).join("\n"), cid, account_id: accId });
    sent++;
    console.log(`[wacampaign sent ${sent}/${list.length}] -> ${c.contact_id} (${jid})`);
    if (sent < list.length) await sleep(rnd(...OUTREACH_PAUSE));
  }
  console.log(`[wacampaign] готово: отправлено ${sent}`);
}

/** Тест/прогрев: шлём N коротких сообщений на указанные номера. */
async function runPing(sock, nums, n) {
  for (const num of nums) {
    const digits = String(num).replace(/\D/g, "");
    if (digits.length < 10) { console.log(`[ping skip] кривой номер «${num}»`); continue; }
    let jid;
    try {
      const r = await sock.onWhatsApp(digits);
      if (!r || !r[0]?.exists) { console.log(`[ping skip] ${digits} не в WhatsApp`); continue; }
      jid = r[0].jid;
    } catch (e) { console.log(`[ping err] ${digits}: ${e.message}`); continue; }
    for (let i = 0; i < n; i++) {
      await sendParts(sock, jid, [WARM_CHATTER[i % WARM_CHATTER.length]]);
      await sleep(rnd(4000, 12000));
    }
    console.log(`[ping ✓] ${jid}: отправлено ${n}`);
  }
}

/** Обработка входящего: спрашиваем мост, отвечаем по частям. */
async function handleIncoming(sock, m) {
  if (m.key.fromMe) return;
  const jid = m.key.remoteJid || "";
  if (jid.endsWith("@g.us") || jid === "status@broadcast") return; // не группы/статусы
  const text = extractText(m);
  if (!text) return;
  const phone = jid.split("@")[0].replace(/\D/g, "");

  let res;
  try {
    res = await bridgePost("/wa/incoming", {
      jid,
      phone,
      push_name: m.pushName || null,
      text,
    });
  } catch (e) {
    console.error(`[incoming] мост недоступен: ${e.message}`);
    return;
  }
  if (res.ignore) {
    console.log(`[ignore] ${jid}: ${res.reason}`);
    return;
  }
  if (res.error) {
    console.error(`[agent error] ${jid}: ${res.error}`);
    return;
  }
  await sleep(rnd(...REPLY_DELAY));
  await sendParts(sock, jid, res.reply_parts);
  if (res.extra_parts?.length) await sendParts(sock, jid, res.extra_parts);
  console.log(`[reply -> ${jid}] intent=${res.intent} agreed=${res.meeting_agreed}`);
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  let proxyAgent;
  if (PROXY) {
    try {
      if (PROXY.startsWith("socks")) {
        const { SocksProxyAgent } = await import("socks-proxy-agent");
        proxyAgent = new SocksProxyAgent(PROXY);
      } else {
        const { HttpsProxyAgent } = await import("https-proxy-agent");
        proxyAgent = new HttpsProxyAgent(PROXY);
      }
      console.log(`[proxy] через ${PROXY.replace(/:\/\/.*@/, "://***@")}`);
    } catch (e) {
      console.error(`[proxy] не смог поднять агент (${e.message}). Поставь: npm i socks-proxy-agent https-proxy-agent`);
    }
  }
  const sock = makeWASocket({
    version, auth: state, logger, printQRInTerminal: false,
    ...(proxyAgent ? { agent: proxyAgent, fetchAgent: proxyAgent } : {}),
  });

  sock.ev.on("creds.update", saveCreds);

  // Привязка по коду (вместо QR): запрашиваем 8-значный код для номера.
  if (usePair && !state.creds.registered) {
    if (!authNumber) {
      console.error("Для --pair укажи номер: node index.js --auth 79673189708 --pair");
    } else {
      setTimeout(async () => {
        try {
          const code = await sock.requestPairingCode(authNumber);
          console.log("\n================ КОД ПРИВЯЗКИ WhatsApp ================");
          console.log("   КОД:  " + code);
          console.log(`На телефоне ${authNumber}: WhatsApp → Связанные устройства →`);
          console.log("Привязать устройство → «Привязать по номеру телефона» → введи код");
          console.log("======================================================\n");
        } catch (e) {
          console.error("[pair] не удалось получить код:", e.message);
        }
      }, 3000);
    }
  }

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr && !usePair) {
      console.log("\nОтсканируй QR в WhatsApp → Связанные устройства → Привязать устройство:\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      console.log(`\n[AXIOM WhatsApp] подключён как ${sock.user?.id || "?"}`);
      console.log(`[bridge] ${BRIDGE}  [auth] ${AUTH_DIR}`);
      if (!kickoffDone) {                 // одноразовые действия — только при первом подключении
        kickoffDone = true;
        if (pingNums.length) await runPing(sock, pingNums, pingN);
        if (waCampaign > 0) await runWaCampaign(sock, waCampaign);
        if (matchLimit > 0) await runMatch(sock, matchLimit);
        if (outreachLimit > 0) await runOutreach(sock, outreachLimit);
      }
      console.log("Слушаю входящие. Ctrl+C для остановки.");
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      const replaced = code === DisconnectReason.connectionReplaced; // 440
      if (replaced) {
        console.log("[conn] 440: сессию перехватило ДРУГОЕ подключение. Закрываюсь, чтобы не зациклиться.");
        console.log("       → не держи два окна/процесса на один номер и не открывай WhatsApp Web с этим аккаунтом.");
        process.exit(0);
      }
      console.log(`[conn] закрыт (code=${code}). ${loggedOut ? "Вышли из аккаунта — удали папку auth_* и залогинься заново." : "Переподключаюсь…"}`);
      if (!loggedOut) start();
    }
  });

  sock.ev.on("messages.upsert", async (ev) => {
    if (ev.type !== "notify") return;
    for (const m of ev.messages) {
      try {
        await handleIncoming(sock, m);
      } catch (e) {
        console.error(`[handle error] ${e.message}`);
      }
    }
  });
}

start().catch((e) => {
  console.error("Фатальная ошибка:", e);
  process.exit(1);
});
