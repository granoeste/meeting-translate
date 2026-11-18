// 会場の参加者に翻訳を見せる中継。手元の Mac の plugins/relay.py が発言を POST し、参加者のスマホへ WebSocket で配る。
// 会場(チャンネル)ごとに Durable Object を 1 つ持つ。会場の一覧は wrangler.toml の CHANNELS で決める。
//
//   POST /publish/<会場>             発言を 1 件受け取る(Authorization: Bearer <PUBLISH_TOKEN>)
//   POST /clear/<会場>               その会場の履歴を消す(本番の前にテストの発言を消す。送信トークンが要る)。
//                                   CHANNELS から外した会場も、履歴が残っていれば消せる
//   GET  /<VIEW_KEY>/                1 つ目の会場の閲覧ページを返す(URL はページが /<VIEW_KEY>/<会場>/ に直す)
//   GET  /<VIEW_KEY>/<会場>/          スマホ向けの閲覧ページ。会場が 2 つ以上あれば、ページの中で切り替えられる
//   GET  /<VIEW_KEY>/<会場>/ws        閲覧ページがつなぐ WebSocket(?after=<最後に受け取った seq>)。
//                                   閲覧ページが "ping" を送ると、DO は眠ったまま "pong" を返す
//   POST /publish、POST /clear、GET /<VIEW_KEY>/ws
//                                   チャンネルが無かったころの URL。main が一覧にあれば main、無ければ 1 つ目の会場として受ける
//
// PUBLISH_TOKEN と VIEW_KEY は wrangler secret に入れる(手元で試すときは relay/.dev.vars)。
// 送信トークンは全会場で共通なので、どの会場の Mac からも、どの会場にも書き込める。
// 送られてくるのは main.py の emit() が作るイベント {id, ts, speaker, lang, text, translation, start, end}
// に、plugins/relay.py が session を足したもの。音声は来ない。
import { DurableObject } from "cloudflare:workers";
import VIEWER from "./viewer.html";

const MAX_BODY = 64 * 1024;
const BACKLOG = 250; // つないだときに送る、直近の発言の数(ライブで見るものなので、昔の分は多く要らない)

const COMMON_HEADERS = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer", // 閲覧キーの入った URL を外に漏らさない
  "X-Robots-Tag": "noindex, nofollow",
  "X-Content-Type-Options": "nosniff",
};

function respond(body, status = 200, headers = {}) {
  return new Response(body, { status, headers: { ...COMMON_HEADERS, ...headers } });
}

async function sameSecret(a, b) {
  // 長さが違っても同じ時間で比べるため、ハッシュにしてから比べる
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(x, y);
}

async function authorized(request, env) {
  const m = /^Bearer (.+)$/.exec(request.headers.get("Authorization") || "");
  return m !== null && (await sameSecret(m[1], env.PUBLISH_TOKEN));
}

// 受け取ったイベントから、決まった項目だけを型を確かめて取り出す。おかしければ null
function parseEvent(raw) {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const { id, ts, speaker, lang, text, translation, start, end, session } = raw;
  const ok =
    Number.isSafeInteger(id) && id >= 0 &&
    typeof session === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(session) &&
    [ts, start, end].every(Number.isFinite) &&
    [speaker, lang, text, translation].every((s) => typeof s === "string");
  return ok ? { id, ts, speaker, lang, text, translation, start, end, session } : null;
}

const CHANNEL_ID = /^[a-z0-9][a-z0-9-]{0,31}$/; // plugins/relay.py の CHANNEL_RE と合わせる
// 形式は合っていても、会場の ID にできない名前。"ws" は /<VIEW_KEY>/ws(チャンネルが無かったころの WebSocket)とぶつかる。
// plugins/relay.py の RESERVED_IDS と合わせる
const RESERVED_IDS = ["ws"];
const validId = (id) => CHANNEL_ID.test(id) && !RESERVED_IDS.includes(id);

// CHANNELS("ID:表示名" をカンマで区切る)を {list: [{id, name}], errors: [ログに出す文]} にする。
// 正しい会場が 1 つも無ければ list は null(中継全体が 503)。
// 書き方の誤った項目(使えない ID、2 回目の同じ ID)は飛ばし、残りの会場は動かす。会議の途中で会場を足すときに、
// 1 か所の誤りで全会場を止めないため。飛ばした会場は 404 になる(その会場の Mac は起動時の確認で止まる)
function parseChannels(src) {
  const list = [];
  const errors = [];
  for (const part of src.split(",")) {
    const s = part.trim();
    if (!s) continue;
    const i = s.indexOf(":");
    const id = (i < 0 ? s : s.slice(0, i)).trim();
    const name = (i < 0 ? "" : s.slice(i + 1).trim()) || id;
    if (!CHANNEL_ID.test(id)) {
      errors.push(`CHANNELS: 会場の ID ${JSON.stringify(id)} は使えないので、この会場を飛ばします(英小文字・数字・-、32 文字まで)`);
    } else if (RESERVED_IDS.includes(id)) {
      errors.push(`CHANNELS: 会場の ID ${JSON.stringify(id)} は中継が URL に使う名前なので、この会場を飛ばします`);
    } else if (list.some((c) => c.id === id)) {
      errors.push(`CHANNELS: 会場の ID ${JSON.stringify(id)} が 2 回以上あるので、2 回目からを飛ばします`);
    } else {
      list.push({ id, name });
    }
  }
  if (list.length === 0) errors.push("CHANNELS: 正しい会場が 1 つもありません。wrangler.toml の [vars] CHANNELS を確かめてください");
  return { list: list.length > 0 ? list : null, errors };
}

// CHANNELS は deploy し直すまで変わらないので、読むのは Worker の起動ごとに 1 回にする。
// 誤りはログに出す(npx wrangler@4.156.2 tail や、ダッシュボードの Logs で見られる)。リクエストのたびに出すと
// tail が埋まるので 1 分に 1 回までにする。1 回きりにしないのは、後から tail を始めても見えるようにするため
let channelsCache = null; // {src, list, errors}
let loggedAt = -Infinity;
function channelsOf(env) {
  const src = String(env.CHANNELS ?? "");
  if (channelsCache?.src !== src) {
    channelsCache = { src, ...parseChannels(src) };
    loggedAt = -Infinity;
  }
  if (channelsCache.errors.length > 0 && Date.now() - loggedAt >= 60_000) {
    loggedAt = Date.now();
    for (const e of channelsCache.errors) console.error(e);
  }
  return channelsCache.list;
}

// 会場ごとに DO を分ける。会場の ID "main"(wrangler.toml の既定)は、チャンネルが無かったころと同じ
// idFromName("main") になるので、CHANNELS に "main" を残しておけば、前の履歴をそのまま使える
function room(env, id) {
  return env.ROOM.get(env.ROOM.idFromName(id));
}

async function handleWrite(request, env, action, id, channels) {
  if (request.method !== "POST") return respond("Method Not Allowed", 405, { Allow: "POST" });
  if (!(await authorized(request, env))) return respond("Unauthorized", 401); // 会場の有無は、トークンを持つ人にだけ教える
  const listed = channels.some((c) => c.id === id);
  if (action === "clear") {
    // CHANNELS から外した会場も消せるようにする。消せないと、同じ ID を足し直したときに、前の会議の発言が新しい参加者に見える。
    // 一覧に無く、消すものも無ければ、ID の打ち間違いとして 404 にする(消したつもりにさせない)
    if (!listed && !validId(id)) return respond("Unknown channel", 404);
    const deleted = await room(env, id).clear();
    return listed || deleted ? respond(null, 204) : respond("Unknown channel", 404);
  }
  // plugins/relay.py は起動時に、空の本文で /publish/<会場> を送って設定を確かめる。
  // 401(トークン違い)→ 404(会場が無い)→ 400(本文が無い = 設定は正しい)の順で答え、何も書き込まない
  if (!listed) return respond("Unknown channel", 404);
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY) return respond("Payload Too Large", 413);
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return respond("Payload Too Large", 413);
  let ev;
  try {
    ev = parseEvent(JSON.parse(new TextDecoder().decode(body)));
  } catch {
    ev = null;
  }
  if (ev === null) return respond("Bad Request", 400);
  await room(env, id).publish(ev);
  return respond(null, 204);
}

// viewer.html の、設定を埋め込む場所。整形ツールなどで書き換わると、閲覧ページが「接続中」のまま止まるので、
// 見つからなければ Worker の起動で例外にして、deploy(や wrangler dev)の時点で気づけるようにする
const CONFIG_SLOT = "/*CONFIG*/null";
if (!VIEWER.includes(CONFIG_SLOT)) {
  throw new Error(`relay/src/viewer.html に ${CONFIG_SLOT} がありません。const CONFIG = ${CONFIG_SLOT}; の形に戻してください`);
}

// 閲覧ページに、いまの会場と会場の一覧を埋め込む。</script> などで抜け出せないよう < をエスケープする。
// atRoot は /<VIEW_KEY>/ で開いたとき(ページが URL を /<VIEW_KEY>/<会場>/ に直す)
function viewer(channel, channels, host, atRoot) {
  const config = JSON.stringify({ channel: channel.id, channels, atRoot }).replace(/</g, "\\u003c");
  return respond(VIEWER.replace(CONFIG_SLOT, () => config), 200, {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy":
      "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
      `connect-src 'self' wss://${host}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
  });
}

export default {
  async fetch(request, env) {
    const channels = channelsOf(env);
    if (!env.PUBLISH_TOKEN || !env.VIEW_KEY || !channels) return respond("Service Unavailable", 503);
    const url = new URL(request.url);
    const path = url.pathname;
    const find = (id) => channels.find((c) => c.id === id) || null;
    // チャンネルが無かったころの URL(会場なしの /publish・/clear と /<VIEW_KEY>/ws)が使う会場。
    // そのころの DO は idFromName("main") なので、main が一覧にあれば main、無ければ 1 つ目の会場にする
    const legacy = find("main") || channels[0];
    const write = /^\/(publish|clear)(?:\/([^/]+))?$/.exec(path);
    // 会場なしの /publish と /clear は legacy の会場として受ける。deploy の前から動いている main.py の発言を、404 で落とさないように
    if (write) return handleWrite(request, env, write[1], write[2] ?? legacy.id, channels);

    const base = `/${env.VIEW_KEY}/`;
    // 末尾の / を付け忘れたとき。301 はブラウザが覚えるので、2 回目からは Worker まで来ない
    if (path === `/${env.VIEW_KEY}`) return respond(null, 301, { Location: base });
    if (!path.startsWith(base)) return respond("Not Found", 404);
    const rest = path.slice(base.length); // "", "ws", "<会場>", "<会場>/", "<会場>/ws"
    let channel, kind;
    if (rest === "") {
      // 1 つ目の会場のページを、転送せずにそのまま返す(URL はページが /<VIEW_KEY>/<会場>/ に直す)。
      // 転送すると、QR コードから開くたびに Worker のリクエストが 1 つ増える
      channel = channels[0];
      kind = "/";
    } else if (rest === "ws") {
      // チャンネルが無かったころの閲覧ページ(/<VIEW_KEY>/ws につなぐ)は、legacy の会場につなぐ。
      // deploy の前から開いたままのスマホが、404 でつなぎ直しを繰り返さないように
      channel = legacy;
      kind = "/ws";
      if (legacy.id !== "main") {
        // ページの ?after= は main の DO の番号なので、別の会場では使えない(2 つの会場の発言が混ざる)。
        // 先にページの表示を消させてから、最初から送る
        url.searchParams.set("after", "0");
        url.searchParams.set("reset", "1");
        request = new Request(url, request);
      }
    } else {
      const m = /^([^/]+)(\/|\/ws)?$/.exec(rest);
      channel = m && find(m[1]);
      kind = m && m[2];
    }
    if (!channel) return respond("Not Found", 404);
    // 会場の一覧は変えられるので、ブラウザに覚えられる恒久的な 301 にはしない
    if (kind === undefined) return respond(null, 302, { Location: `${base}${channel.id}/` });
    if (request.method !== "GET") return respond("Method Not Allowed", 405, { Allow: "GET" });
    if (kind === "/") return viewer(channel, channels, url.host, rest === "");
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") return respond("Upgrade Required", 426);
    return room(env, channel.id).fetch(request);
  },
};

// 発言の履歴と、つないでいる参加者を持つ。会場ごとに 1 つ(idFromName(<会場の ID>))。
export class Room extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    // seq は AUTOINCREMENT なので、/clear の後も前の番号を使い回さない
    this.sql.exec(`CREATE TABLE IF NOT EXISTS events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      session TEXT NOT NULL,
      id INTEGER NOT NULL,
      data TEXT NOT NULL,
      UNIQUE (session, id))`);
    // 閲覧ページが接続を確かめる ping には、DO を起こさずに返す。つなぎ直してもらうより、ずっと安く済む
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  // plugins/relay.py が送り直したもの((session, id) が同じ)は無視する
  publish(ev) {
    const rows = this.sql
      .exec("INSERT OR IGNORE INTO events (session, id, data) VALUES (?, ?, ?) RETURNING seq",
        ev.session, ev.id, JSON.stringify(ev))
      .toArray();
    if (rows.length > 0) this.broadcast(message(rows[0].seq, ev));
  }

  // 消した発言があれば true
  clear() {
    const deleted = this.sql.exec("DELETE FROM events").rowsWritten > 0;
    this.broadcast(JSON.stringify({ type: "clear" }));
    return deleted;
  }

  broadcast(msg) {
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(msg);
      } catch {
        // 切れかけの接続は放っておく。閉じたら一覧から消える
      }
    }
  }

  latestSeq() {
    const row = this.sql.exec("SELECT seq FROM sqlite_sequence WHERE name = 'events'").toArray()[0];
    return row ? row.seq : 0;
  }

  async fetch(request) {
    const params = new URL(request.url).searchParams;
    const latest = this.latestSeq();
    let after = Number(params.get("after"));
    if (!Number.isSafeInteger(after) || after < 0 || after > latest) after = 0; // 中継を作り直したときは最初から送る
    const oldest = this.sql.exec("SELECT MIN(seq) AS seq FROM events").toArray()[0].seq; // 空なら null

    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server); // Hibernation API: つないだまま DO を眠らせられる
    // チャンネルが無かったころの閲覧ページを main 以外の会場につないだとき(fetch の rest === "ws")。前の会場の表示を消させる
    if (params.get("reset") === "1") server.send(JSON.stringify({ type: "clear" }));
    // 閲覧ページは、hello を受けたら latest より新しい表示と oldest より古い表示を消す(切れている間の /clear の分)
    server.send(JSON.stringify({ type: "hello", latest, oldest }));
    // 新しい順に取って JS で並べ直す。SQL の副問い合わせで並べ直すと、読んだ行数が 2 倍に数えられる
    const rows = this.sql
      .exec("SELECT seq, data FROM events WHERE seq > ? ORDER BY seq DESC LIMIT ?", after, BACKLOG)
      .toArray()
      .reverse();
    for (const row of rows) server.send(message(row.seq, JSON.parse(row.data)));
    return new Response(null, { status: 101, webSocket: client });
  }

  webSocketMessage() {
    // 参加者からは何も受け取らない
  }

  webSocketClose(ws) {
    try {
      ws.close();
    } catch {
      // すでに閉じている
    }
  }
}

function message(seq, ev) {
  const { session, ...rest } = ev; // session は参加者に見せない
  return JSON.stringify({ type: "event", seq, ...rest });
}
