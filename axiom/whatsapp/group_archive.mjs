/**
 * Архив рабочих групп WhatsApp (тексты + голосовые + картинки/документы).
 * Основной мост группы игнорирует — этот модуль только пишет их на диск, ничего не отправляет.
 * Список групп: group_archive/watch.json  (["120363...@g.us", ...]).
 * Данные: group_archive/<gid>/messages.jsonl, media/<id>.<ext>
 * При первом сообщении группы после старта процесса подтягивает историю за DAYS дней.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { downloadMediaMessage } from "@whiskeysockets/baileys";
import pino from "pino";

const BASE = path.join(path.dirname(fileURLToPath(import.meta.url)), "group_archive");
const DAYS = 3, MAX_ROUNDS = 12;
const silent = pino({ level: "silent" });
const seen = new Map();      // gid -> Set(id)
const hist = new Map();      // gid -> {oldest:{key,ts}, rounds, busy}

function watched() {
  try { return new Set(JSON.parse(fs.readFileSync(path.join(BASE, "watch.json"), "utf8"))); }
  catch { return new Set(); }
}
const txt = (m) => m.conversation || m.extendedTextMessage?.text || m.imageMessage?.caption ||
  m.videoMessage?.caption || m.documentMessage?.caption || "";
const kind = (m) => m.audioMessage ? "voice" : m.imageMessage ? "image" : m.videoMessage ? "video"
  : m.documentMessage ? "document" : m.reactionMessage ? "reaction" : m.protocolMessage ? "protocol" : "text";

function seenSet(gid, dir) {
  if (!seen.has(gid)) {
    const s = new Set();
    try { for (const l of fs.readFileSync(path.join(dir, "messages.jsonl"), "utf8").split("\n"))
      if (l) try { s.add(JSON.parse(l).id); } catch {} } catch {}
    seen.set(gid, s);
  }
  return seen.get(gid);
}

export async function archiveGroupMessage(sock, m) {
  const gid = m?.key?.remoteJid || "";
  if (!gid.endsWith("@g.us") || !watched().has(gid)) return;
  const msg = m.message?.ephemeralMessage?.message || m.message?.viewOnceMessage?.message || m.message;
  if (!msg) return;
  const ts = (Number(m.messageTimestamp) || 0) * 1000;
  const h = hist.get(gid) || { oldest: null, rounds: 0, busy: false };
  if (!h.oldest || ts < h.oldest.ts) h.oldest = { key: m.key, ts };
  hist.set(gid, h);

  const dir = path.join(BASE, gid.split("@")[0]);
  fs.mkdirSync(path.join(dir, "media"), { recursive: true });
  const s = seenSet(gid, dir);
  if (s.has(m.key.id)) return;
  s.add(m.key.id);

  const k = kind(msg);
  if (k === "protocol") return;
  let media = null;
  if (["voice", "image", "document", "video"].includes(k)) {
    const ext = k === "voice" ? "ogg" : k === "image" ? "jpg" : k === "video" ? "mp4"
      : (msg.documentMessage?.fileName?.split(".").pop() || "bin");
    try {
      const f = path.join(dir, "media", `${m.key.id}.${ext}`);
      fs.writeFileSync(f, await downloadMediaMessage(m, "buffer", {}, { logger: silent, reuploadRequest: sock.updateMediaMessage }));
      media = path.relative(dir, f);
    } catch (e) { media = "ERR " + e.message; }
  }
  const q = msg.extendedTextMessage?.contextInfo?.quotedMessage;
  fs.appendFileSync(path.join(dir, "messages.jsonl"), JSON.stringify({
    id: m.key.id, ts: new Date(ts).toISOString(), sender: m.pushName || "", participant: m.key.participant || "",
    fromMe: !!m.key.fromMe, kind: k, text: txt(msg), fileName: msg.documentMessage?.fileName || "",
    quoted: q ? txt(q) : "", reaction: msg.reactionMessage?.text || "", media,
  }) + "\n");
}

/** Подтянуть историю группы назад от самого старого известного сообщения (до DAYS дней). */
export async function pullGroupHistory(sock, gid) {
  const h = hist.get(gid);
  if (!h || !h.oldest || h.busy || h.rounds >= MAX_ROUNDS) return;
  if (h.oldest.ts < Date.now() - DAYS * 864e5) return;
  h.busy = true; h.rounds++;
  try { await sock.fetchMessageHistory(50, h.oldest.key, h.oldest.ts); console.log(`[group-archive] история ${gid} раунд ${h.rounds}`); }
  catch (e) { console.log(`[group-archive] история: ${e.message}`); }
  setTimeout(() => { h.busy = false; }, 8000);
}

export async function onGroupBatch(sock, messages) {
  const gids = new Set();
  for (const m of messages || []) {
    const rj = m?.key?.remoteJid || "";
    if (rj.endsWith("@g.us")) console.log(`[group-archive dbg] ${rj} from=${m.key.participant||""} me=${m.key.fromMe} keys=${Object.keys(m.message||{}).join(",")} stub=${m.messageStubType||""} ${(m.messageStubParameters||[]).join("|").slice(0,80)}`);
    try { await archiveGroupMessage(sock, m); } catch (e) { console.log(`[group-archive] ${e.message}`); }
    const g = m?.key?.remoteJid;
    if (g && g.endsWith("@g.us") && hist.has(g)) gids.add(g);
  }
  for (const g of gids) setTimeout(() => pullGroupHistory(sock, g).catch(() => {}), 9000);
}
