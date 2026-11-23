const PLANS = new Set([5, 10, 20]);
const MONTH_MS = 30 * 24 * 60 * 60 * 1000;
const SESSION_MS = 30 * 24 * 60 * 60 * 1000;

function json(body, status = 200) {
  return Response.json(body, { status });
}

function cors(response) {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type");
  headers.set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  return new Response(response.body, { status: response.status, headers });
}

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

function cookieValue(request, name) {
  const raw = request.headers.get("Cookie") || "";
  const match = raw.match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return match ? decodeURIComponent(match[1]) : "";
}

async function pbkdf2(password, saltBytes) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: saltBytes, iterations: 100000, hash: "SHA-256" },
    key,
    256,
  );
  return [...new Uint8Array(bits)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function randomHex(size) {
  const bytes = new Uint8Array(size);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

function publicUser(row, now) {
  const active = row.enabled === 1 && row.expires_at > now;
  return {
    id: row.id,
    username: row.username,
    plan_threads: row.plan_threads,
    expires_at: row.expires_at,
    enabled: row.enabled === 1,
    active,
    note: row.note || "",
  };
}

async function requireAdmin(request, env) {
  const headerToken = bearer(request);
  if (env.ADMIN_TOKEN && headerToken === env.ADMIN_TOKEN) return null;
  const cookie = cookieValue(request, "muse_admin");
  if (cookie) {
    const row = await env.DB.prepare(
      "SELECT token FROM admin_sessions WHERE token = ? AND expires_at > ?",
    ).bind(cookie, Date.now()).first();
    if (row) return null;
  }
  return json({ error: "Không có quyền admin." }, 401);
}

async function adminLogin(request, env) {
  const body = await readJson(request);
  const password = String(body?.password || "");
  if (!env.ADMIN_TOKEN || password !== env.ADMIN_TOKEN) {
    return json({ error: "Sai mật khẩu admin." }, 401);
  }
  const token = randomHex(32);
  const expires = Date.now() + 12 * 60 * 60 * 1000;
  await env.DB.prepare(
    "INSERT INTO admin_sessions (token, expires_at) VALUES (?, ?)",
  ).bind(token, expires).run();
  const headers = new Headers({ "Content-Type": "application/json" });
  headers.set("Set-Cookie", `muse_admin=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=43200`);
  return new Response(JSON.stringify({ ok: true }), { headers });
}

async function requireUser(request, env) {
  const token = bearer(request);
  if (!token) return { error: json({ error: "Chưa đăng nhập." }, 401) };
  const now = Date.now();
  const row = await env.DB.prepare(
    `SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
     WHERE s.token = ? AND s.expires_at > ?`,
  ).bind(token, now).first();
  if (!row) return { error: json({ error: "Phiên đăng nhập hết hạn." }, 401) };
  if (row.enabled !== 1 || row.expires_at <= now) {
    return { error: json({ error: "Gói đã hết hạn hoặc bị khóa." }, 403) };
  }
  return { user: row };
}

async function login(request, env) {
  const body = await readJson(request);
  const username = String(body?.username || "").trim().toLowerCase();
  const password = String(body?.password || "");
  if (!username || !password) return json({ error: "Nhập tên đăng nhập và mật khẩu." }, 400);
  const user = await env.DB.prepare("SELECT * FROM users WHERE username = ?").bind(username).first();
  if (!user) return json({ error: "Sai tên đăng nhập hoặc mật khẩu." }, 401);
  const hash = await pbkdf2(password, hexToBytes(user.password_salt));
  if (hash !== user.password_hash) return json({ error: "Sai tên đăng nhập hoặc mật khẩu." }, 401);
  const now = Date.now();
  if (user.enabled !== 1 || user.expires_at <= now) {
    return json({ error: "Gói đã hết hạn hoặc bị khóa. Liên hệ admin." }, 403);
  }
  const token = randomHex(32);
  await env.DB.prepare(
    "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
  ).bind(token, user.id, now + SESSION_MS).run();
  return json({ token, user: publicUser(user, now) });
}

async function createUser(request, env) {
  const denied = await requireAdmin(request, env);
  if (denied) return denied;
  const body = await readJson(request);
  const username = String(body?.username || "").trim().toLowerCase();
  const password = String(body?.password || "");
  const plan = Number(body?.plan_threads);
  const months = Number(body?.months || 1);
  if (!/^[a-z0-9._-]{3,40}$/.test(username)) {
    return json({ error: "Tên đăng nhập 3–40 ký tự, chữ thường, số, dấu chấm hoặc gạch." }, 400);
  }
  if (password.length < 8) return json({ error: "Mật khẩu tối thiểu 8 ký tự." }, 400);
  if (!PLANS.has(plan)) return json({ error: "Gói chỉ có 5, 10 hoặc 20 luồng." }, 400);
  if (!Number.isInteger(months) || months < 1 || months > 24) {
    return json({ error: "Số tháng từ 1 đến 24." }, 400);
  }
  const now = Date.now();
  const salt = randomHex(16);
  const hash = await pbkdf2(password, hexToBytes(salt));
  const id = randomHex(8);
  try {
    await env.DB.prepare(
      `INSERT INTO users (id, username, password_hash, password_salt, plan_threads, expires_at, enabled, note, created_at)
       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)`,
    ).bind(id, username, hash, salt, plan, now + months * MONTH_MS, String(body?.note || ""), now).run();
  } catch {
    return json({ error: "Tên đăng nhập đã tồn tại." }, 409);
  }
  const row = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(id).first();
  return json({ user: publicUser(row, now) }, 201);
}

async function listUsers(request, env) {
  const denied = await requireAdmin(request, env);
  if (denied) return denied;
  const now = Date.now();
  const { results } = await env.DB.prepare(
    "SELECT * FROM users ORDER BY created_at DESC",
  ).all();
  return json({ users: (results || []).map((row) => publicUser(row, now)) });
}

async function renewUser(request, env, id) {
  const denied = await requireAdmin(request, env);
  if (denied) return denied;
  const body = await readJson(request);
  const months = Number(body?.months || 1);
  const plan = body?.plan_threads == null ? null : Number(body.plan_threads);
  if (!Number.isInteger(months) || months < 1 || months > 24) {
    return json({ error: "Số tháng từ 1 đến 24." }, 400);
  }
  if (plan != null && !PLANS.has(plan)) return json({ error: "Gói chỉ có 5, 10 hoặc 20 luồng." }, 400);
  const row = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(id).first();
  if (!row) return json({ error: "Không thấy tài khoản." }, 404);
  const now = Date.now();
  const base = Math.max(now, row.expires_at);
  const nextPlan = plan == null ? row.plan_threads : plan;
  await env.DB.prepare(
    "UPDATE users SET expires_at = ?, plan_threads = ?, enabled = 1 WHERE id = ?",
  ).bind(base + months * MONTH_MS, nextPlan, id).run();
  const updated = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(id).first();
  return json({ user: publicUser(updated, now) });
}

async function setEnabled(request, env, id, enabled) {
  const denied = await requireAdmin(request, env);
  if (denied) return denied;
  const result = await env.DB.prepare("UPDATE users SET enabled = ? WHERE id = ?").bind(enabled, id).run();
  if (!result.meta.changes) return json({ error: "Không thấy tài khoản." }, 404);
  if (!enabled) {
    await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(id).run();
  }
  return json({ ok: true });
}

const ADMIN_HTML = `<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muse Admin</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; font: 14px/1.4 "Helvetica Neue", sans-serif; background: #f8fafc; color: #0f172a; }
  header { background: #0a0a0a; color: white; padding: 14px 20px; display: flex; justify-content: space-between; align-items: center; }
  main { max-width: 980px; margin: 24px auto; padding: 0 16px; }
  .card { background: white; border: 1px solid #e2e8f0; border-radius: 14px; padding: 16px; margin-bottom: 16px; }
  h1 { font-size: 16px; margin: 0; }
  h2 { font-size: 13px; letter-spacing: .04em; color: #64748b; margin: 0 0 12px; }
  label { display: block; font-size: 12px; color: #64748b; margin-bottom: 4px; }
  input, select, button { font: inherit; border-radius: 8px; border: 1px solid #cbd5e1; padding: 8px 10px; }
  button { background: #0f172a; color: white; border: none; font-weight: 700; cursor: pointer; }
  button.ghost { background: white; color: #0f172a; border: 1px solid #cbd5e1; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }
  .row > div { flex: 1; min-width: 140px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #e2e8f0; font-size: 13px; }
  .bad { color: #dc2626; font-weight: 700; }
  .ok { color: #16a34a; font-weight: 700; }
  #msg { min-height: 1.2em; color: #b45309; }
</style>
</head>
<body>
<header>
  <h1>MUSE ADMIN</h1>
  <button class="ghost" id="logout" hidden>Đăng xuất</button>
</header>
<main>
  <p id="msg"></p>
  <section class="card" id="login-card">
    <h2>ĐĂNG NHẬP ADMIN</h2>
    <form id="login">
      <label>Mật khẩu admin</label>
      <div class="row">
        <div><input id="password" type="password" autocomplete="current-password" required></div>
        <div><button type="submit">Vào trang quản trị</button></div>
      </div>
    </form>
  </section>
  <section class="card" id="create-card" hidden>
    <h2>CẤP TÀI KHOẢN SAU KHI MUA</h2>
    <form id="create" class="row">
      <div><label>Tên đăng nhập</label><input name="username" required placeholder="khach01"></div>
      <div><label>Mật khẩu</label><input name="password" required minlength="8"></div>
      <div><label>Gói luồng</label>
        <select name="plan_threads"><option>5</option><option selected>10</option><option>20</option></select>
      </div>
      <div><label>Số tháng</label><input name="months" type="number" min="1" max="24" value="1"></div>
      <div><label>Ghi chú</label><input name="note" placeholder="đã thanh toán"></div>
      <div><button type="submit">Tạo tài khoản</button></div>
    </form>
  </section>
  <section class="card" id="list-card" hidden>
    <h2>DANH SÁCH</h2>
    <table>
      <thead><tr><th>Tên</th><th>Gói</th><th>Hết hạn</th><th>Trạng thái</th><th></th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>
</main>
<script>
const msg = document.querySelector("#msg");
function show(text) { msg.textContent = text || ""; }
async function api(path, options) {
  const res = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Lỗi máy chủ");
  return data;
}
function when(ts) {
  return new Date(ts).toLocaleString("vi-VN");
}
async function load() {
  const data = await api("/admin/users");
  document.querySelector("#login-card").hidden = true;
  document.querySelector("#create-card").hidden = false;
  document.querySelector("#list-card").hidden = false;
  document.querySelector("#logout").hidden = false;
  const body = document.querySelector("#rows");
  body.innerHTML = data.users.map(function (user) {
    const state = user.active ? "Đang dùng" : "Khóa / hết hạn";
    const action = user.enabled ? "Khóa" : "Mở";
    const next = user.enabled ? "disable" : "enable";
    return "<tr><td>" + user.username + "</td><td>" + user.plan_threads +
      " luồng</td><td>" + when(user.expires_at) + "</td><td class='" +
      (user.active ? "ok" : "bad") + "'>" + state + "</td><td>" +
      "<button class='ghost' data-renew='" + user.id + "'>+1 tháng</button> " +
      "<button class='ghost' data-toggle='" + user.id + "' data-on='" + next + "'>" + action +
      "</button></td></tr>";
  }).join("");
}
document.querySelector("#login").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await api("/admin/login", { method: "POST", body: JSON.stringify({ password: password.value }) });
    await load();
  } catch (err) { show(err.message); }
};
document.querySelector("#create").onsubmit = async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api("/admin/users", { method: "POST", body: JSON.stringify({
      username: form.get("username"),
      password: form.get("password"),
      plan_threads: Number(form.get("plan_threads")),
      months: Number(form.get("months")),
      note: form.get("note")
    })});
    event.target.reset();
    await load();
    show("Đã tạo tài khoản.");
  } catch (err) { show(err.message); }
};
document.body.addEventListener("click", async (event) => {
  const renew = event.target.dataset.renew;
  const toggle = event.target.dataset.toggle;
  try {
    if (renew) await api("/admin/users/" + renew + "/renew", { method: "POST", body: JSON.stringify({ months: 1 }) });
    if (toggle) await api("/admin/users/" + toggle + "/" + event.target.dataset.on, { method: "POST", body: "{}" });
    if (renew || toggle) await load();
  } catch (err) { show(err.message); }
});
document.querySelector("#logout").onclick = async () => {
  await api("/admin/logout", { method: "POST", body: "{}" });
  location.reload();
};
load().catch(() => {});
</script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return cors(new Response(null, { status: 204 }));
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/$/, "") || "/";
    try {
      if (path === "/health" && request.method === "GET") return cors(json({ ok: true }));
      if (path === "/login" && request.method === "POST") return cors(await login(request, env));
      if (path === "/me" && request.method === "GET") {
        const session = await requireUser(request, env);
        if (session.error) return cors(session.error);
        return cors(json({ user: publicUser(session.user, Date.now()) }));
      }
      if (path === "/logout" && request.method === "POST") {
        const token = bearer(request);
        if (token) await env.DB.prepare("DELETE FROM sessions WHERE token = ?").bind(token).run();
        return cors(json({ ok: true }));
      }
      if (path === "/admin" && request.method === "GET") {
        return new Response(ADMIN_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
      if (path === "/admin/login" && request.method === "POST") return cors(await adminLogin(request, env));
      if (path === "/admin/logout" && request.method === "POST") {
        const cookie = cookieValue(request, "muse_admin");
        if (cookie) await env.DB.prepare("DELETE FROM admin_sessions WHERE token = ?").bind(cookie).run();
        const headers = new Headers({ "Content-Type": "application/json" });
        headers.set("Set-Cookie", "muse_admin=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0");
        return new Response(JSON.stringify({ ok: true }), { headers });
      }
      if (path === "/admin/users" && request.method === "GET") return cors(await listUsers(request, env));
      if (path === "/admin/users" && request.method === "POST") return cors(await createUser(request, env));
      const renew = path.match(/^\/admin\/users\/([0-9a-f]+)\/renew$/);
      if (renew && request.method === "POST") return cors(await renewUser(request, env, renew[1]));
      const lock = path.match(/^\/admin\/users\/([0-9a-f]+)\/(enable|disable)$/);
      if (lock && request.method === "POST") {
        return cors(await setEnabled(request, env, lock[1], lock[2] === "enable" ? 1 : 0));
      }
      return cors(json({ error: "Không thấy đường dẫn." }, 404));
    } catch {
      return cors(json({ error: "Lỗi máy chủ." }, 500));
    }
  },
};
