/* 暖爪宠物美容 —— 单页前端（原生 JS，无构建依赖） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { view: "calendar", user: null, cache: {}, pollTimer: null };

const EVENT_ICON = {
  resistance: "😤", skin: "🔍", cut: "🩸",
  owner_change: "🔁", overdue_pickup: "⏰", supply_allergy: "🌸",
};
const EVENT_LABEL = {
  resistance: "宠物抗拒", skin: "皮肤异常", cut: "剪伤",
  owner_change: "主人临时改项", overdue_pickup: "超时未取", supply_allergy: "用品过敏",
};
const EVENT_KINDS = Object.keys(EVENT_LABEL);

/* ---------------- 请求 ---------------- */
async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(s) { return (s || "").replace(/^\d{4}-\d{2}-\d{2}\s?/, ""); }
function todayStr() { return new Date().toISOString().slice(0, 10); }
function nowLocalInput() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}
function photoUrl(id) { return `/api/photos/${id}`; }

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return el;
}

/* ---------------- 模态框 ---------------- */
function modal(contentBuilder) {
  const mask = h("div", { class: "modal-mask" });
  const box = h("div", { class: "modal" });
  mask.append(box);
  mask.addEventListener("click", e => { if (e.target === mask) mask.remove(); });
  const close = () => mask.remove();
  contentBuilder(box, close);
  document.body.append(mask);
  return { box, close };
}

function confirmDlg(title, text, onOk, okClass = "btn-green") {
  modal((box, close) => {
    box.append(h("h3", {}, title), h("p", { class: "muted", style: "margin:8px 0 16px" }, text));
    box.append(h("div", { class: "actions" },
      h("button", { class: "btn btn-light", onclick: close }, "取消"),
      h("button", {
        class: "btn " + okClass, onclick: async () => {
          try { await onOk(); close(); } catch (e) { toast(e.message, true); }
        }
      }, "确认")));
  });
}

/* ---------------- 登录 ---------------- */
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  try {
    const data = await api("POST", "/api/login", {
      username: f.username.value, password: f.password.value,
    });
    state.user = data.user;
    enterApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});
$$(".demo-hint button").forEach(b => b.addEventListener("click", () => {
  const f = $("#login-form");
  f.username.value = b.dataset.u;
  f.password.value = b.dataset.p;
  f.requestSubmit();
}));
$("#logout-btn").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  state.user = null;
  clearInterval(state.pollTimer);
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
});

async function boot() {
  try {
    const { user } = await api("GET", "/api/me");
    state.user = user;
    enterApp();
  } catch {
    $("#login").classList.remove("hidden");
  }
}

async function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#user-label").textContent = `${state.user.display_name}（${roleLabel()}）`;
  await loadCaches();
  switchView("calendar");
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshCurrent, 12000);
}
function roleLabel() {
  return { front: "前台", groomer: "美容师", manager: "店长" }[state.user.role] || state.user.role;
}
const canIncident = () => !!state.user.can_view_incident;

$$(".tab").forEach(tab => tab.addEventListener("click", () => switchView(tab.dataset.view)));

async function loadCaches() {
  const [p, s, g] = await Promise.all([
    api("GET", "/api/pets"), api("GET", "/api/services"), api("GET", "/api/groomers"),
  ]);
  state.cache.pets = p.pets;
  state.cache.services = s.services;
  state.cache.groomers = g.groomers;
  state.cache.petMap = Object.fromEntries(p.pets.map(x => [x.id, x]));
  state.cache.svcMap = Object.fromEntries(s.services.map(x => [x.id, x]));
}

async function switchView(v) {
  state.view = v;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
  await refreshCurrent();
}
async function refreshCurrent() {
  if (!state.user) return;
  const view = $("#view");
  view.innerHTML = "";
  try {
    await RENDER[state.view](view);
  } catch (e) {
    view.append(h("div", { class: "card" }, "加载失败：" + e.message));
  }
}

const RENDER = {};

/* ---------------- 预约日历 ---------------- */
RENDER.calendar = async function (root, date = todayStr()) {
  const { appointments } = await api("GET", `/api/appointments?date=${date}`);
  const bySlot = {};
  appointments.forEach(a => (bySlot[a.start_time] ||= []).push(a));

  const head = h("div", { class: "page-head" },
    h("h2", {}, "📅 预约日历"),
    h("div", { style: "display:flex;gap:10px;align-items:center" },
      h("input", { type: "date", value: date, onchange: e => RENDER.calendar(root, e.target.value) }),
      h("button", {
        class: "btn btn-navy", onclick: () => appointmentModal(() => RENDER.calendar(root, date))
      }, "＋ 新建预约")));

  const grid = h("div", { class: "cal-grid" });
  const slots = Object.keys(bySlot).sort();
  if (!slots.length) grid.append(h("div", { class: "card muted" }, "当天暂无预约，点击右上角「新建预约」。"));
  slots.forEach(slot => {
    const list = bySlot[slot];
    const active = list.filter(a => !["canceled", "no_show"].includes(a.status));
    const capNote = `工位 ${active.length}/3`;
    const col = h("div", { class: "cal-slot" },
      h("div", { class: "slot-head" },
        h("span", {}, "🕙 " + fmtTime(slot)),
        h("span", { class: "cap" + (active.length >= 3 ? " full" : "") }, capNote)));
    list.forEach(a => col.append(apptCard(a, date)));
    grid.append(col);
  });
  root.append(head, grid);
};

function apptCard(a, date) {
  const svcs = a.services.map(s => h("span", { class: "svc-chip" }, s.name));
  return h("div", { class: "appt-card", onclick: () => appointmentDetailModal(a, date) },
    h("div", { class: "line1" },
      h("span", {}, `🐾 ${a.pet_name}`),
      h("span", { class: `badge ${a.status}` }, a.status_label)),
    h("div", { class: "line2" },
      h("span", {}, `美容师 ${a.groomer_name}`),
      h("span", {}, `${a.contact_name} · ${a.contact_phone}`),
      h("span", {}, `约 ${a.estimated_minutes} 分钟`)),
    h("div", { class: "svc-chips" }, ...svcs));
}

function appointmentModal(onCreated, preset = {}) {
  modal((box, close) => {
    const svcPicks = new Set(preset.service_ids || []);
    const pickWrap = h("div", { class: "tag-pick" });
    state.cache.services.forEach(s => {
      const cb = h("input", {
        type: "checkbox", value: s.id,
        onchange: e => e.target.checked ? svcPicks.add(s.id) : svcPicks.delete(s.id),
      });
      if (svcPicks.has(s.id)) cb.checked = true;
      pickWrap.append(h("label", {}, cb, ` ${s.name}（${s.duration_minutes}分钟/¥${s.price}）`));
    });
    const totalEl = h("b", { style: "color:var(--green-deep)" }, "0");
    const petSel = h("select", {},
      ...state.cache.pets.map(p => h("option", { value: p.id }, `${p.name} · ${p.breed} · ${p.size}型`)));
    if (preset.pet_id) petSel.value = preset.pet_id;
    const gSel = h("select", {},
      ...state.cache.groomers.map(g => h("option", { value: g.id }, `${g.name}（${g.title}）`)));
    const timeInp = h("input", { type: "datetime-local", value: nowLocalInput() });
    const noteInp = h("textarea", { rows: 2, placeholder: "备注（可选）" });
    const recompute = () => {
      const mins = [...svcPicks].reduce((m, id) => m + state.cache.svcMap[id].duration_minutes, 0);
      totalEl.textContent = mins + " 分钟";
    };
    pickWrap.addEventListener("change", recompute);
    setTimeout(recompute, 0);

    box.append(
      h("h3", {}, preset.appointment_id ? "主人临时改项" : "新建预约"),
      h("div", { class: "form-row" }, h("label", {}, "宠物（品种/体型/禁忌在保存时快照）"), petSel),
      h("div", { class: "form-row" }, h("label", {}, "美容师"), gSel),
      h("div", { class: "form-row" }, h("label", {}, "开始时间"), timeInp),
      h("div", { class: "form-row" }, h("label", {}, "服务组合 · 预计时长 "), pickWrap,
        h("div", { style: "margin-top:6px" }, "合计：", totalEl)),
      h("div", { class: "form-row" }, h("label", {}, "备注"), noteInp),
      h("div", { class: "actions" },
        h("button", { class: "btn btn-light", onclick: close }, "取消"),
        h("button", {
          class: "btn btn-green",
          onclick: async () => {
            try {
              const start = timeInp.value.replace("T", " ");
              if (!preset.appointment_id) {
                await api("POST", "/api/appointments", {
                  pet_id: +petSel.value, groomer_id: +gSel.value,
                  start_time: start, service_ids: [...svcPicks].map(Number),
                  note: noteInp.value,
                });
                toast("预约成功，工位已锁定");
              }
              close();
              onCreated && onCreated();
            } catch (e) { toast(e.message, true); }
          }
        }, "锁定工位并保存")));
  });
}

function appointmentDetailModal(a, date) {
  modal((box, close) => {
    const pet = state.cache.petMap[a.pet_id];
    const chips = a.services.map(s => h("span", { class: "svc-chip" }, s.name));
    const actions = h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:14px" });

    const refresh = async () => { close(); await refreshCurrent(); };
    if (a.status === "booked") {
      actions.append(h("button", {
        class: "btn btn-green",
        onclick: async () => {
          try { await api("POST", `/api/appointments/${a.id}/reception`); toast("接待已确认，护理单已生成"); switchView("care"); }
          catch (e) { toast(e.message, true); }
        }
      }, "✔ 接待确认，生成护理单"));
      actions.append(changeServicesBtn(a, refresh));
      actions.append(cancelBtn(a, refresh));
    }
    if (["reception_confirmed", "in_care", "done"].includes(a.status)) {
      actions.append(h("button", {
        class: "btn btn-navy",
        onclick: () => { close(); openCare(a.id); }
      }, "✂️ 进入护理录入"));
    }
    if (!["canceled", "picked_up"].includes(a.status)) {
      actions.append(h("button", {
        class: "btn btn-peach", onclick: () => eventModal(a, refresh)
      }, "⚠ 登记异常事件"));
    }

    box.append(
      h("h3", {}, `🐾 ${a.pet_name} 的预约`),
      h("div", { class: "snapshot" },
        h("span", { class: "badge " + a.status }, a.status_label),
        h("span", { class: "tag" }, `品种：${a.breed_snapshot}`),
        h("span", { class: "tag" }, `体型：${a.size_snapshot}型`),
        h("span", { class: "tag" }, `预计 ${a.estimated_minutes} 分钟`)),
      h("div", { class: "svc-chips", style: "margin:10px 0" }, ...chips),
      h("dl", { class: "kv" },
        h("dt", {}, "开始/结束"), h("dd", {}, `${a.start_time} → ${a.end_time}`),
        h("dt", {}, "美容师"), h("dd", {}, a.groomer_name),
        h("dt", {}, "联系人"), h("dd", {}, `${a.contact_name} ${a.contact_phone}`),
        h("dt", {}, "禁忌快照"), h("dd", {}, a.contraindication_snapshot || "无"),
        h("dt", {}, "皮肤毛发"), h("dd", {}, pet ? pet.skin_coat_note : "—"),
        h("dt", {}, "备注"), h("dd", {}, a.note || "无")),
      actions);
  });
}

function cancelBtn(a, refresh) {
  return h("button", {
    class: "btn btn-danger",
    onclick: () => confirmDlg("取消预约", `确认取消 ${a.pet_name} 的该笔预约？工位将释放。`,
      async () => {
        await api("POST", `/api/appointments/${a.id}/cancel`, { reason: "前台操作" });
        toast("预约已取消"); refresh();
      })
  }, "取消预约");
}

function changeServicesBtn(a, refresh) {
  return h("button", {
    class: "btn btn-peach",
    onclick: () => {
      modal((box, close) => {
        const picks = new Set(a.services.map(s => s.id));
        const wrap = h("div", { class: "tag-pick" });
        state.cache.services.forEach(s => {
          const cb = h("input", {
            type: "checkbox",
            onchange: e => e.target.checked ? picks.add(s.id) : picks.delete(s.id),
          });
          if (picks.has(s.id)) cb.checked = true;
          wrap.append(h("label", {}, cb, ` ${s.name}（${s.duration_minutes}分钟）`));
        });
        const reason = h("input", { type: "text", placeholder: "变更原因（可选）" });
        box.append(
          h("h3", {}, `🔁 ${a.pet_name} · 主人临时改项`),
          h("p", { class: "muted" }, "重算预计时长；改项会记录为事件。已完成的项目不可删除。"),
          h("div", { class: "form-row", style: "margin-top:10px" }, h("label", {}, "新的服务组合"), wrap),
          h("div", { class: "form-row" }, h("label", {}, "原因"), reason),
          h("div", { class: "actions" },
            h("button", { class: "btn btn-light", onclick: close }, "取消"),
            h("button", {
              class: "btn btn-green",
              onclick: async () => {
                try {
                  await api("POST", `/api/appointments/${a.id}/change-services`,
                    { service_ids: [...picks].map(Number), reason: reason.value });
                  toast("服务已变更并记录事件"); close(); refresh();
                } catch (e) { toast(e.message, true); }
              }
            }, "保存变更")));
      });
    }
  }, "🔁 临时改项");
}

function eventModal(a, onDone, kindPreset = null) {
  modal((box, close) => {
    let dataUrl = "", fileName = "";
    const kindBtns = EVENT_KINDS.map(k => {
      const b = h("button", { type: "button", class: "btn btn-light", style: "margin:3px" },
        `${EVENT_ICON[k]} ${EVENT_LABEL[k]}`);
      b.dataset.kind = k;
      b.onclick = () => { kindBtns.forEach(x => x.classList.remove("on")); b.classList.add("on"); };
      return b;
    });
    const getKind = () => kindBtns.find(b => b.classList.contains("on"))?.dataset.kind;
    if (kindPreset) {
      const t = kindBtns.find(b => b.dataset.kind === kindPreset);
      if (t) t.classList.add("on");
    }
    const detail = h("textarea", { rows: 3, placeholder: "详细描述：发现部位、处理方式、是否已告知主人…" });
    const fileInp = h("input", {
      type: "file", accept: "image/*",
      onchange: () => {
        const f = fileInp.files[0];
        if (!f) return;
        fileName = f.name;
        const rd = new FileReader();
        rd.onload = () => { dataUrl = rd.result; preview.src = dataUrl; };
        rd.readAsDataURL(f);
      }
    });
    const preview = h("img", { style: "width:90px;height:90px;object-fit:cover;border-radius:10px;margin-top:8px" });
    const authNote = canIncident()
      ? h("p", { class: "muted" }, "📷 附带的照片为异常照片，仅授权人员可查看。")
      : h("p", { class: "muted", style: "color:var(--danger)" }, "你暂无异常照片权限，可登记文字事件；照片请由店长/前台补录。");

    box.append(
      h("h3", {}, `⚠ 为 ${a ? a.pet_name : ""} 登记事件`),
      h("div", { class: "form-row" }, h("label", {}, "事件类型"), h("div", {}, ...kindBtns)),
      h("div", { class: "form-row" }, h("label", {}, "描述"), detail),
      h("div", { class: "form-row" }, h("label", {}, "异常照片（可选，授权可见）"), fileInp, preview, authNote),
      h("div", { class: "actions" },
        h("button", { class: "btn btn-light", onclick: close }, "取消"),
        h("button", {
          class: "btn btn-navy",
          onclick: async () => {
            const kind = getKind();
            if (!kind) return toast("请选择事件类型", true);
            if (!detail.value.trim()) return toast("请填写事件描述", true);
            if (dataUrl && !canIncident()) return toast("无权限上传异常照片", true);
            try {
              await api("POST", `/api/appointments/${a.id}/events`,
                { kind, detail: detail.value, photo: dataUrl || undefined, caption: fileName });
              toast("事件已记录"); close(); onDone && onDone();
            } catch (e) { toast(e.message, true); }
          }
        }, "提交事件")));
  });
}

/* ---------------- 宠物档案 ---------------- */
RENDER.pets = async function (root) {
  if (!state.cache.pets) await loadCaches();
  const head = h("div", { class: "page-head" },
    h("h2", {}, "🐶 宠物档案"),
    h("span", { class: "muted" }, "照片 + 标签布局，点击卡片查看皮肤毛发备注与联系人"));
  const grid = h("div", { class: "pet-grid" });
  state.cache.pets.forEach(p => {
    const tags = [
      ...p.tags.map(t => h("span", { class: "tag" }, t)),
      h("span", { class: "tag warn" }, p.species),
    ];
    const avatar = p.photos.length
      ? h("img", { src: photoUrl(p.photos[0].id), alt: p.name, loading: "lazy" })
      : h("span", {}, "🐶");
    const card = h("div", { class: "pet-card", onclick: () => petDetailModal(p) },
      h("div", { class: "pet-photo" }, avatar,
        h("span", { class: "size-flag" }, `${p.size}型`)),
      h("div", { class: "pet-body" },
        h("div", { class: "pname" }, p.name, h("span", { class: "muted", style: "font-size:13px" }, p.breed)),
        h("div", { class: "tags" }, ...tags),
        h("div", { class: "pet-meta" },
          h("div", {}, h("b", {}, "皮肤毛发："), p.skin_coat_note || "—")),
        h("div", { class: "contra" }, "🚫 禁忌：" + (p.contraindications || "无"))));
    grid.append(card);
  });
  root.append(head, grid);
};

function petDetailModal(p) {
  modal((box, close) => {
    const imgs = p.photos.map(ph => h("img", {
      src: photoUrl(ph.id), title: ph.caption || "", onclick: () => lightbox(photoUrl(ph.id)),
    }));
    const events = p.recent_events.map(ev => h("div", { class: "event-row" },
      h("div", { class: "ev-main" },
        h("span", { class: "ev-icon" }, EVENT_ICON[ev.kind] || "•"),
        h("div", {}, h("b", {}, EVENT_LABEL[ev.kind] || ev.kind),
          h("div", { class: "muted" }, ev.detail))),
      h("span", { class: "muted" }, fmtTime(ev.created_at))));
    box.append(
      h("h3", {}, `🐾 ${p.name} 的档案`),
      h("div", { class: "detail-grid" },
        h("div", { class: "detail-photo" },
          imgs.length ? h("img", { src: photoUrl(p.photos[0].id) }) : h("div", { class: "pet-photo", style: "height:230px;border-radius:12px" }, "🐶"),
          h("div", { class: "ph-gallery" }, ...imgs.slice(1))),
        h("div", {},
          h("dl", { class: "kv" },
            h("dt", {}, "品种"), h("dd", {}, `${p.species} · ${p.breed}`),
            h("dt", {}, "体型"), h("dd", {}, p.size),
            h("dt", {}, "出生年"), h("dd", {}, p.birth_year || "—"),
            h("dt", {}, "联系人"), h("dd", {}, `${p.contact_name}　${p.contact_phone}`),
            h("dt", {}, "联系备注"), h("dd", {}, p.contact_note || "—"),
            h("dt", {}, "标签"), h("dd", {}, p.tags.join("、") || "—"),
            h("dt", {}, "皮肤毛发"), h("dd", {}, p.skin_coat_note || "—"),
            h("dt", {}, "禁忌"), h("dd", {}, h("span", { style: "color:var(--danger)" }, p.contraindications || "无"))))),
      h("div", { class: "sub-section", style: "margin-top:16px" },
        h("h4", {}, "近期异常记录"),
        events.length ? h("div", { class: "event-list" }, ...events) : h("p", { class: "muted" }, "暂无")));
  });
}

function lightbox(src) {
  modal((box, close) => {
    box.style.width = "auto";
    box.style.padding = "10px";
    const img = h("img", {
      src, style: "max-width:88vw;max-height:84vh;border-radius:10px;display:block",
      onclick: close,
    });
    img.addEventListener("error", () => {
      box.innerHTML = "";
      box.append(h("div", { style: "padding:40px;text-align:center;color:var(--danger)" }, "🔒 无权查看该异常照片"));
    });
    box.append(img);
  });
}

/* ---------------- 工位排程（时间轴）---------------- */
RENDER.stations = async function (root, date = todayStr()) {
  const { timeline } = await api("GET", `/api/stations?date=${date}`);
  const head = h("div", { class: "page-head" },
    h("h2", {}, "🏗️ 工位排程"),
    h("input", { type: "date", value: date, onchange: e => RENDER.stations(root, e.target.value) }));
  const legend = h("div", { class: "tl-legend" },
    h("span", {}, h("i", { style: "background:var(--green)" }), "空闲美容师"),
    h("span", {}, h("i", { style: "background:var(--peach)" }), "服务中"),
    h("span", {}, "工位容量按时段锁定，满员时段底纹标橙"));

  const axis = h("div", { class: "card timeline", style: "padding:0;overflow:hidden" });
  if (!timeline.length) axis.append(h("p", { class: "muted", style: "padding:18px" }, "当天没有任何排班。"));
  timeline.forEach(row => {
    const lane = h("div", { class: "tl-lane" + (row.full ? " full" : "") });
    const busyGroomers = new Set(row.cards.map(c => c.groomer_id));
    state.cache.groomers.forEach(g => {
      const card0 = row.cards.find(c => c.groomer_id === g.id);
      if (card0) {
        lane.append(h("div", { class: "groomer-card busy", onclick: () => openCare(card0.id) },
          h("div", { class: "gc-name" }, h("span", {}, "✂️ " + g.name),
            h("span", { class: "badge " + card0.status }, statusLabel(card0.status))),
          h("div", { class: "gc-pet" }, `🐾 ${card0.pet_name}（${card0.size}型）`),
          h("div", { class: "gc-meta" },
            `${fmtTime(card0.start_time)}–${fmtTime(card0.end_time)} · 约${card0.estimated_minutes}分`)));
      } else {
        lane.append(h("div", { class: "groomer-card" },
          h("div", { class: "gc-name" }, h("span", {}, g.name)),
          h("div", { class: "gc-meta", style: "color:var(--green-deep)" }, "空闲")));
      }
    });
    axis.append(
      h("div", { class: "tl-row" },
        h("div", { class: "tl-time" }, fmtTime(row.slot_time),
          h("span", { class: "cap-note" }, `${row.locked}/${row.capacity} 位`)),
        lane));
  });
  root.append(head, legend, axis);
};
function statusLabel(s) {
  return { booked: "已预约", reception_confirmed: "接待确认", in_care: "护理中", done: "待取宠", picked_up: "已取宠", canceled: "已取消" }[s] || s;
}

/* ---------------- 护理录入 ---------------- */
let careApptId = null;
RENDER.care = async function (root) {
  const date = todayStr();
  const { appointments } = await api("GET", `/api/appointments?date=${date}`);
  const active = appointments
    .filter(a => ["reception_confirmed", "in_care", "done"].includes(a.status))
    .sort((a, b) => a.start_time.localeCompare(b.start_time));

  const head = h("div", { class: "page-head" },
    h("h2", {}, "✂️ 护理录入"),
    h("div", {},
      h("select", {
        onchange: e => e.target.value && openCare(+e.target.value),
      }, h("option", { value: "" }, active.length ? "选择今天的护理单…" : "今天暂无护理单"),
        ...active.map(a => h("option", { value: a.id },
          `${fmtTime(a.start_time)} ${a.pet_name}（${statusLabel(a.status)}）`)))));

  root.append(head);
  if (careApptId) {
    try { root.append(await carePanel(careApptId)); }
    catch (e) {
      careApptId = null;
      root.append(h("div", { class: "card muted" }, "护理单尚未生成。请先在「预约日历」接待确认。"));
    }
  } else {
    root.append(h("div", { class: "card muted" },
      active.length
        ? "从右上角选择一笔护理单开始录入；服务项目完成一项记录一项。"
        : "今天还没有护理单。可在「预约日历」对已预约单点「接待确认」。"));
  }
};

async function openCare(apptId) {
  careApptId = apptId;
  await refreshCurrent();
}

async function carePanel(apptId) {
  const data = await api("GET", `/api/care/${apptId}`);
  const { record, appointment: a, items, supplies, events } = data;
  const wrap = h("div", {});
  const doneN = items.filter(i => i.status === "done").length;

  wrap.append(h("div", { class: "card" },
    h("div", { class: "care-head" },
      h("div", { class: "care-title" },
        h("h3", {}, `🐾 ${a.pet_name} · ${a.breed}（${a.size_snapshot || a.size}型） · 美容师 ${a.groomer_name}`),
        h("div", { class: "snapshot" },
          h("span", { class: "tag" }, `开始 ${a.start_time}`),
          h("span", { class: "tag" }, `预计结束 ${a.end_time}`),
          h("span", { class: "tag" }, `禁忌：${a.contraindication_snapshot || "无"}`),
          h("span", { class: "tag" }, `皮肤毛发：${a.skin_coat_note || "无记录"}`))),
      h("div", {},
        record.confirmed_at
          ? h("span", { class: "badge done" }, "✔ 护理记录已确认（不可删除/修改）")
          : h("span", { class: "badge in_care" }, "护理中")))));

  // 大号状态按钮
  const itemBox = h("div", { class: "card care-items" });
  itemBox.append(h("h4", {}, "服务项目（完成一项，记录一项）"));
  items.forEach(it => {
    const isDone = it.status === "done";
    const btn = h("button", {
      class: "state-btn " + it.status,
      onclick: async () => {
        if (record.confirmed_at) return toast("护理记录已确认", true);
        try {
          if (isDone) {
            await api("POST", `/api/care/${record.id}/items/${it.id}/undo`);
          } else {
            await api("POST", `/api/care/${record.id}/items/${it.id}/complete`);
          }
          await refreshCurrent();
        } catch (e) { toast(e.message, true); }
      }
    }, h("span", {}, isDone ? "✔ 已完成" : "点按完成"),
      h("small", {}, isDone ? `再次点击撤销 · ${it.completed_by || ""} ${fmtTime(it.completed_at || "")}` : "PENDING"));
    itemBox.append(h("div", { class: "care-item " + it.status },
      h("div", { class: "ci-info" },
        h("div", { class: "ci-name" }, it.name_snapshot),
        h("div", { class: "ci-meta" },
          isDone ? `完成时间 ${it.completed_at} · 操作人 ${it.completed_by}` : `单价 ¥${it.price_snapshot} · 待完成`)),
      btn));
  });

  // 操作条
  const bar = h("div", { class: "close-bar" },
    h("div", { style: "display:flex;gap:14px;align-items:center" },
      h("div", {}, `进度 ${doneN}/${items.length}`),
      h("div", { class: "progress" }, h("i", { style: `width:${items.length ? doneN / items.length * 100 : 0}%` })),
      h("div", { class: "total" }, "合计 <b>" +
        `¥${items.reduce((s, i) => s + i.price_snapshot, 0)}</b>`)),
    h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end" },
      h("button", { class: "btn btn-peach", onclick: () => supplyModal(record, a) }, "🧴 用品消耗"),
      ["reception_confirmed", "in_care"].includes(a.status) && !record.confirmed_at
        ? changeServicesBtn(a, refreshCurrent) : null,
      h("button", { class: "btn btn-peach", onclick: () => eventModal(a, refreshCurrent) }, "⚠ 异常事件"),
      h("button", {
        class: "btn " + (doneN === items.length ? "btn-green" : "btn-light"),
        onclick: () => closeCare(record, items)
      }, record.confirmed_at ? "已结单" : "确认结单"),
      !record.confirmed_at ? h("button", {
        class: "btn btn-ghost", style: "border-color:rgba(255,255,255,.4)",
        onclick: () => confirmDlg("删除护理单", "确认后预约回到「已预约」状态。护理记录确认后不可删除。",
          async () => {
            try {
              await api("DELETE", `/api/care/${record.id}`);
              careApptId = null; toast("护理单已删除"); await refreshCurrent();
            } catch (e) { toast(e.message, true); }
          })
      }, "删除") : null));
  itemBox.append(bar);
  wrap.append(itemBox);

  // 用品消耗
  const supBox = h("div", { class: "card" });
  supBox.append(h("div", { class: "sub-section" }, h("h4", {}, "🧴 用品消耗")));
  const supList = h("div", { class: "supply-list" });
  (supplies.length ? supplies : []).forEach(s => supList.append(
    h("div", { class: "supply-row" },
      h("span", {}, s.supply_name),
      h("span", { class: "muted" }, `${s.quantity}${s.unit} · ${fmtTime(s.created_at)}`))));
  if (!supplies.length) supList.append(h("p", { class: "muted" }, "暂无用品消耗记录"));
  supBox.append(supList);
  wrap.append(supBox);

  // 事件 + 缩略图
  const evBox = h("div", { class: "card" },
    h("div", { class: "sub-section" }, h("h4", {}, "⚠ 异常事件（图标 + 文字 + 照片缩略图）")));
  const evList = h("div", { class: "event-list" });
  if (!events.length) evList.append(h("p", { class: "muted" }, "暂无事件"));
  for (const ev of events) {
    const thumbs = (ev.photos || []).map(ph => canIncident()
      ? h("img", { src: photoUrl(ph.id), title: ph.caption || "异常照片", onclick: () => lightbox(photoUrl(ph.id)) })
      : h("div", {
        title: "异常照片：授权可见",
        style: "width:54px;height:54px;border-radius:8px;background:#e4e8f0;display:flex;align-items:center;justify-content:center;font-size:20px;"
      }, "🔒"));
    evList.append(h("div", { class: "event-row" },
      h("div", { class: "ev-main" },
        h("span", { class: "ev-icon" }, EVENT_ICON[ev.kind] || "•"),
        h("div", {}, h("b", {}, EVENT_LABEL[ev.kind] || ev.kind),
          h("div", { class: "muted" }, ev.detail),
          h("div", { class: "muted", style: "font-size:12px" },
            `${ev.created_by} · ${ev.created_at}`))),
      h("div", { class: "ev-thumbs" }, ...thumbs)));
  }
  evBox.append(evList);
  wrap.append(evBox);
  return wrap;
}

function supplyModal(record, a) {
  modal((box, close) => {
    const name = h("input", { type: "text", placeholder: "如：燕麦低敏浴液 / 止血粉" });
    const qty = h("input", { type: "number", value: "1", min: "0.1", step: "0.1" });
    const unit = h("input", { type: "text", value: "份" });
    const allergy = h("input", { type: "checkbox" });
    const reaction = h("textarea", { rows: 2, placeholder: "过敏反应描述（勾选过敏时填写）" });
    box.append(
      h("h3", {}, "🧴 登记用品消耗"),
      h("div", { class: "form-row" }, h("label", {}, "用品名称"), name),
      h("div", { style: "display:flex;gap:10px" },
        h("div", { class: "form-row", style: "flex:1" }, h("label", {}, "数量"), qty),
        h("div", { class: "form-row", style: "flex:1" }, h("label", {}, "单位"), unit)),
      h("div", { class: "form-row" }, h("label", { class: "chk-row" }, allergy, " 使用后疑似过敏（自动生成用品过敏事件）")),
      h("div", { class: "form-row" }, reaction),
      h("div", { class: "actions" },
        h("button", { class: "btn btn-light", onclick: close }, "取消"),
        h("button", {
          class: "btn btn-green",
          onclick: async () => {
            if (!name.value.trim()) return toast("请填写用品名称", true);
            try {
              await api("POST", `/api/care/${record.id}/supplies`, {
                supply_name: name.value, quantity: +qty.value, unit: unit.value,
                allergy: allergy.checked, reaction: reaction.value,
              });
              toast("用品消耗已记录"); close(); await refreshCurrent();
            } catch (e) { toast(e.message, true); }
          }
        }, "保存")));
  });
}

function closeCare(record, items) {
  if (record.confirmed_at) {
    return modal((box) => {
      box.append(
        h("h3", {}, "护理单已结单"),
        h("p", { style: "margin:10px 0" },
          `确认时间：${record.confirmed_at}`, h("br"),
          `合计：¥${record.total_price}`, h("br"),
          `取宠码：${record.pickup_code}`),
        h("p", { class: "muted" }, "护理记录确认后不能删除、不能重复结单。"));
    });
  }
  const pending = items.filter(i => i.status !== "done").length;
  if (pending) {
    return toast(`还有 ${pending} 个项目未完成，不能结单`, true);
  }
  confirmDlg("确认结单", "所有项目均已完成。结单后护理记录不可删除，并生成取宠码。",
    async () => {
      const r = await api("POST", `/api/care/${record.id}/close`);
      toast(`已结单，取宠码 ${r.pickup_code}，已发送取宠提醒`);
      await refreshCurrent();
    });
}

/* ---------------- 异常照片 ---------------- */
RENDER.incidents = async function (root, filterKind = "") {
  const { events } = await api("GET", filterKind ? `/api/events?kind=${filterKind}` : "/api/events");
  const head = h("div", { class: "page-head" },
    h("h2", {}, "⚠️ 异常记录与照片"),
    h("span", { class: "muted" },
      canIncident() ? "你有异常照片查看权限" : "🔒 你无异常照片权限，仅显示锁定占位"));
  const filters = h("div", { class: "filter-row" },
    filterBtn("全部", "", filterKind),
    ...EVENT_KINDS.map(k => filterBtn(`${EVENT_ICON[k]} ${EVENT_LABEL[k]}`, k, filterKind)));
  function filterBtn(label, k, cur) {
    return h("button", {
      class: (k || "") === (cur || "") ? "on" : "",
      onclick: () => RENDER.incidents(root, k),
    }, label);
  }

  const grid = h("div", { class: "incident-grid" });
  if (!events.length) grid.append(h("div", { class: "card muted" }, "暂无异常记录"));
  events.forEach(ev => {
    const ph = ev.photos[0];
    let visual;
    if (ph && canIncident()) {
      visual = h("div", { class: "inc-thumb" },
        h("img", { src: photoUrl(ph.id), onclick: () => lightbox(photoUrl(ph.id)) }));
    } else if (ph) {
      visual = h("div", { class: "inc-thumb inc-locked" }, h("span", { style: "font-size:26px" }, "🔒"), "授权人员可见");
    } else {
      visual = h("div", { class: "inc-thumb" }, EVENT_ICON[ev.kind] || "📝");
    }
    grid.append(h("div", { class: "inc-card" }, visual,
      h("div", { class: "inc-body" },
        h("div", { class: "inc-kind" }, `${EVENT_ICON[ev.kind] || "•"} ${ev.kind_label}`,
          h("span", { class: "muted", style: "font-weight:400;font-size:12px" }, ev.pet_name || "")),
        h("p", {}, ev.detail),
        h("div", { class: "muted", style: "font-size:12px" },
          `${ev.created_by} · ${ev.created_at}${ev.photos.length ? " · 📷×" + ev.photos.length : ""}`))));
  });
  root.append(head, filters, grid);
};

/* ---------------- 取宠核验 + 通知 ---------------- */
RENDER.pickup = async function (root) {
  const [{ queue }, { notifications }] = await Promise.all([
    api("GET", "/api/pickup"), api("GET", "/api/notifications"),
  ]);
  const retrying = notifications.filter(n => n.status === "pending").length;
  $("#notify-dot").style.visibility = retrying ? "visible" : "hidden";
  $("#notify-dot").title = retrying ? `${retrying} 条通知重试中` : "无待重试通知";

  const head = h("div", { class: "page-head" },
    h("h2", {}, "🏷️ 取宠核验"),
    h("span", { class: "muted" }, "凭取宠码或预留手机号核验；已取宠不可重复核验"));
  const grid = h("div", { class: "pickup-grid" });
  if (!queue.length) grid.append(h("div", { class: "card muted" }, "暂无待取宠宠物"));
  queue.forEach(q => {
    const overdue = !q.picked_up_at && q.confirmed_at && isOverdue(q);
    const codeInp = h("input", { type: "text", placeholder: "输入 6 位取宠码或预留手机号" });
    const card = h("div", { class: "pickup-card " + (q.picked_up_at ? "done" : "") + (overdue ? " overdue" : "") },
      h("h4", {}, `🐾 ${q.pet_name}`,
        h("span", { class: "badge " + q.status }, statusLabel(q.status))),
      h("div", { class: "muted" }, `${q.contact_name} · ${q.phone}`),
      h("div", { class: "pickup-code" }, q.picked_up_at ? "已取宠" : q.pickup_code),
      q.picked_up_at
        ? h("div", { class: "muted" }, `取宠时间 ${q.picked_up_at}`)
        : h("div", { class: "verify-row" }, codeInp,
            h("button", {
              class: "btn btn-green",
              onclick: async () => {
                try {
                  await api("POST", `/api/pickup/${q.appointment_id}/verify`, { code: codeInp.value });
                  toast("核验通过，宠物已交还"); refreshCurrent();
                } catch (e) { toast(e.message, true); }
              }
            }, "核验")),
      overdue ? h("div", { style: "color:var(--danger);font-size:12px;margin-top:6px" }, "⏰ 超时未取，已生成事件并发送提醒") : null);
    grid.append(card);
  });

  const notifCard = h("div", { class: "card" },
    h("h3", { style: "margin-bottom:10px" }, "🔔 通知发送与重试（进程内任务）"));
  const list = h("div", { class: "notif-list" });
  if (!notifications.length) list.append(h("p", { class: "muted" }, "暂无通知"));
  notifications.forEach(n => list.append(h("div", { class: "notif-row" },
    h("span", {}, n.payload, h("div", { class: "muted", style: "font-size:11px" },
      `尝试 ${n.attempts} 次 · 下次 ${n.next_run_at}${n.last_error ? " · " + n.last_error : ""}`)),
    h("span", { class: "status-pill " + n.status },
      { sent: "已送达", pending: "待重试", failed: "已失败" }[n.status]))));
  notifCard.append(list);

  root.append(head, grid, notifCard);
};

function isOverdue(q) {
  return !!q.overdue;
}

boot();
