let RELEASES = [];
let CURRENT = null;

async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.message || r.statusText), {data});
  return data;
}

function toast(msg, isErr) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), 4200);
}
async function act(pk, action, label, body) {
  try {
    const d = await api(`/api/releases/${pk}/${action}`, body || {});
    toast(`${label}完成：${d.state || ""} ${d.gen || ""}`.trim());
    await refresh(pk);
  } catch (e) {
    const x = e.data || {};
    toast(`✗ ${label}被拒绝 [${x.error || "ERROR"}]\n${e.message}` +
      (x.mismatches ? `\n变更检查：${x.mismatches.map(m => m.code).join(", ")}` : ""), true);
    await refresh(pk);
  }
}

function stateBadge(s) { return `<span class="badge b-${s}">${s}</span>`; }

async function refresh(selectPk) {
  const data = await api("/api/releases");
  RELEASES = data.releases;
  const el = document.getElementById("releases");
  el.innerHTML = RELEASES.length ? "" : '<div class="muted">暂无候选</div>';
  for (const r of RELEASES) {
    const d = document.createElement("div");
    d.className = "rel" + ((CURRENT === r.id) ? " active" : "");
    d.onclick = () => select(r.id);
    d.innerHTML = `<div><strong>${r.release_id}</strong> v${r.version}${stateBadge(r.state)}
      ${r.is_active ? '<span class="badge b-PUBLISHED">在线</span>' : ""}</div>
      <div class="meta">${r.id.slice(0, 14)}… 阻断 ${r.blockers.length} 项</div>`;
    el.appendChild(d);
  }
  const a = data.active_gen;
  document.getElementById("active").textContent = a
    ? `在线代次：${a.gen.slice(0, 14)}…  mf=${a.mf.slice(0, 12)}…`
    : "当前无在线代次";
  const pk = selectPk || CURRENT;
  if (pk && RELEASES.find(r => r.id === pk)) { CURRENT = pk; render(pk); }
}

function select(pk) { CURRENT = pk; refresh(pk); }

function findingsHtml(ev) {
  if (!ev.findings || !ev.findings.length) return "";
  const items = ev.findings.map(f =>
    `<li><span class="loc">[${f.locator_type}] ${f.locator}</span> — ${f.message}</li>`);
  return `<details><summary>${ev.findings.length} 条定位证据</summary>
    <ul class="findings">${items.join("")}</ul></details>`;
}

function render(pk) {
  const r = RELEASES.find(x => x.id === pk);
  const blockers = r.blockers;
  const nav = {
    DRAFT: [["evaluate", "长事务评估清单", "primary"]],
    BLOCKED: [["evaluate", "重新评估", "primary"]],
    SEALED: [["evaluate", "重新评估"], ["approve", "批准清单（经理）", "primary"]],
    APPROVED: [["evaluate", "重新评估"], ["prepare", "预检并暂存代次", "primary"]],
    PREPARED: [["prepare", "重新预检"], ["publish", "提交发布（同代次切换）", "primary"]],
    PUBLISHED: [],
  }[r.state] || [];

  const rows = r.checks.map(c => {
    const waived = c.waiver_id ? `<div class="waived">已豁免 ${c.waiver_id.slice(0, 10)}</div>` : "";
    const wb = c.status === "BLOCKED" && c.waivable && !c.waiver_id
      ? `<button class="danger" onclick="waive('${c.code}')">豁免</button>` : "";
    return `<tr>
      <td><code class="k">${c.code}</code><br><span class="muted">${c.evidence.title}</span>
        ${c.phase === "PREPARE" ? '<span class="badge b-PREPARED">预检</span>' : ""}</td>
      <td class="${c.status}">${c.status}${waived}</td>
      <td>${wb}${findingsHtml(c.evidence)}</td>
    </tr>`;
  }).join("");

  const blockerPanel = blockers.length ? `
    <div style="border-left:3px solid var(--red);padding:8px 12px;background:#fef0f0;border-radius:4px;margin-bottom:12px">
      <strong style="color:var(--red)">阻断项（前端可定位）：${blockers.length}</strong>
      <ul class="findings">${blockers.flatMap(c =>
        c.evidence.findings.filter(f => f.detail && f.detail.blocking !== false)
        .map(f => `<li><code class="k">${c.code}</code>
          <span class="loc">[${f.locator_type}] ${f.locator}</span> ${f.message}</li>`))
        .join("") || blockers.map(c => `<li><code class="k">${c.code}</code></li>`).join("")}
      </ul></div>` :
    (r.state !== "DRAFT" ? '<div style="color:var(--green);margin-bottom:12px">✓ 无未处理阻断项</div>' : "");

  const idemInput = r.state === "PREPARED"
    ? '<input id="idem" placeholder="幂等令牌（重复发布请求）" style="width:220px"/>' : "";

  document.getElementById("detail").innerHTML = `
    <h2 style="margin-top:0">${r.release_id} v${r.version} ${stateBadge(r.state)}</h2>
    <div class="muted">候选 mf: <code class="k">${r.mf.slice(0, 24)}…</code>
      ${r.seal ? `<br>清单 seal: <code class="k">${r.seal.slice(0, 24)}…</code>` : ""}
      ${r.baseline_mf ? `<br>基线 mf: <code class="k">${r.baseline_mf.slice(0, 24)}…</code>`
        : "<br>（首个版本，无基线）"}</div>
    <div class="row" style="margin-top:10px">
      ${nav.map(([a, l, cl]) =>
        `<button class="${cl || ""}" onclick="go('${a}','${l}')">${l}</button>`).join("")}
      ${idemInput}
      ${r.state === "PUBLISHED" && !r.is_active ? "" : ""}
    </div>
    ${blockerPanel}
    <table><thead><tr><th>检查项</th><th>状态</th><th>逐项证据 / 定位</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function go(action, label) {
  if (action === "publish") {
    const tok = document.getElementById("idem");
    return act(CURRENT, "publish", label, {idem_token: tok && tok.value || undefined});
  }
  return act(CURRENT, action, label);
}
async function waive(code) {
  const reason = prompt(`豁免 ${code} 的理由（经理审批，绑定当前依赖指纹）：`);
  if (reason === null) return;
  await act(CURRENT, "waiver", `豁免 ${code}`, {code, reason});
}
async function addFeedback() {
  try {
    await api("/api/feedback", {page_path: fbPage.value, severity: fbSev.value,
      summary: fbMsg.value});
    toast("反馈已登记（FEEDBACK_OPEN 依赖指纹变化，已批准清单将需重新评估）");
  } catch (e) { toast(e.message, true); }
}
async function setLic() {
  try {
    await api("/api/asset-licence", {path: assetPath.value, licence: lic.value});
    toast("素材授权状态已变更（构建资格依赖指纹变化）");
  } catch (e) { toast(e.message, true); }
}

refresh();
setInterval(() => refresh(), 5000);
