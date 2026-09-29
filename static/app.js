/* ============================================================
   萌爪美容台前端逻辑（原生 JS，零依赖）
   ============================================================ */
'use strict';

const state = {
  view: 'calendar',
  meta: null,
  staffId: Number(localStorage.getItem('staffId') || 0),
  selectedDate: todayStr(),
  selectedOrderId: null,
  week: [],
  photoFilter: 'all',
};

const STATUS_LABELS = {
  booked: '已预约',
  reception_confirmed: '已接待',
  servicing: '护理中',
  completed: '待取宠',
  picked_up: '已取宠',
  cancelled: '已取消',
  noshow: '爽约',
};
const EVENT_ICON = {
  resistance: '🐶', skin: '🔍', cut: '🩹',
  change: '✏️', overdue: '⏰', allergy: '🌿',
};
const EVENT_COLOR = {
  resistance: 'peach', skin: 'amber', cut: 'danger',
  change: 'navy', overdue: 'amber', allergy: 'peach',
};

// ---------------- 基础工具 ----------------
function $(sel, root = document) { return root.querySelector(sel); }
function $all(sel, root = document) { return [...root.querySelectorAll(sel)];
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function todayStr(d = new Date()) {
  return d.toISOString().slice(0, 10);
}
function addDays(dateStr, n) {
  const d = new Date(dateStr + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return todayStr(d);
}
function hmToMin(hm) { const [h, m] = hm.split(':').map(Number); return h * 60 + m; }
function minToHm(min) {
  return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;
}
function photoUrl(filename) {
  if (!filename) return '';
  return `/api/photo_file?filename=${encodeURIComponent(filename)}&viewer=${state.staffId || ''}`;
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.staffId) headers['X-Staff-Id'] = state.staffId;
  const res = await fetch(path, {
    headers, ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `请求失败（${res.status}）`);
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, type = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

function openModal(title, html, large = false) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = html;
  $('#modalBox').classList.toggle('large', large);
  $('#modalMask').hidden = false;
}
function closeModal() { $('#modalMask').hidden = true; }
$('#modalClose').onclick = closeModal;
$('#modalMask').addEventListener('click', e => { if (e.target.id === 'modalMask') closeModal(); });

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1]);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

// ---------------- 启动 ----------------
async function init() {
  state.meta = await api('/api/meta');
  buildStaffSwitch();
  bindTabs();
  bindDrawer();
  await switchView('calendar');
  pollNotifications();
  setInterval(pollNotifications, 5000);
}

function currentStaff() {
  return state.meta.staff.find(s => s.id === state.staffId) || null;
}

function buildStaffSwitch() {
  const sel = $('#staffSelect');
  sel.innerHTML = '<option value="0">未登录（前台）</option>' +
    state.meta.staff.map(s =>
      `<option value="${s.id}">${esc(s.name)} · ${esc(s.title)}${s.can_view_sensitive ? ' 🔐' : ''}</option>`
    ).join('');
  sel.value = state.staffId;
  sel.onchange = async () => {
    state.staffId = Number(sel.value) || 0;
    localStorage.setItem('staffId', state.staffId);
    toast(state.staffId ? `已切换身份：${currentStaff()?.name}` : '已退出登录');
    await renderCurrent();
  };
  if (!state.staffId) {
    state.staffId = state.meta.staff[0]?.id || 0;
    sel.value = state.staffId;
    localStorage.setItem('staffId', state.staffId);
  }
}

function bindTabs() {
  $('#tabs').addEventListener('click', async e => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    await switchView(btn.dataset.view);
  });
}

async function switchView(view) {
  state.view = view;
  $all('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
  await renderCurrent();
}
async function renderCurrent() {
  const renderers = {
    calendar: renderCalendar, pets: renderPets, stations: renderStations,
    care: renderCare, photos: renderPhotos, pickup: renderPickup,
  };
  await renderers[state.view]();
}

// ============================================================
// 视图一：预约日历
// ============================================================
async function renderCalendar() {
  const d = state.selectedDate;
  state.week = [-3, -2, -1, 0, 1, 2, 3].map(n => addDays(d, n));
  const from = state.week[0], to = state.week[6];
  const appts = await api(`/api/appointments?from=${from}&to=${to}`);
  const byDate = {};
  appts.forEach(a => (byDate[a.appt_date] ||= []).push(a));

  const weekHtml = state.week.map((day, i) => {
    const dow = ['日', '一', '二', '三', '四', '五', '六'][new Date(day + 'T00:00:00').getDay()];
    const list = byDate[day] || [];
    return `<div class="day-cell ${day === d ? 'active' : ''}" data-day="${day}">
      <div class="dow">周${dow}</div>
      <div class="d">${Number(day.slice(8))}</div>
      <div class="count">${list.length ? list.length + ' 个预约' : ' '}</div>
    </div>`;
  }).join('');

  const todays = (byDate[d] || []);
  const listHtml = todays.length
    ? `<div class="appt-list">${todays.map(appointmentRowHtml).join('')}</div>`
    : `<div class="card empty"><span class="big">🌿</span>当天暂无预约，点击右上角「新建预约」开始安排</div>`;

  $('#main').innerHTML = `
    <div class="cal-wrap">
      <div class="card">
        <div class="view-head">
          <div class="cal-nav">
            <button class="btn btn-ghost btn-sm" id="prevDay">‹ 前一天</button>
            <div class="date-label">${d}</div>
            <button class="btn btn-ghost btn-sm" id="nextDay">后一天 ›</button>
            <button class="btn btn-ghost btn-sm" id="todayBtn">今天</button>
          </div>
          <button class="btn btn-peach" id="newApptBtn">＋ 新建预约</button>
        </div>
        <div class="week-strip">${weekHtml}</div>
      </div>
      <div>
        <div class="section-title">${d} 当日预约（${todays.length}）</div>
        ${listHtml}
      </div>
    </div>`;

  $('#prevDay').onclick = () => { state.selectedDate = addDays(d, -1); renderCalendar(); };
  $('#nextDay').onclick = () => { state.selectedDate = addDays(d, 1); renderCalendar(); };
  $('#todayBtn').onclick = () => { state.selectedDate = todayStr(); renderCalendar(); };
  $('#newApptBtn').onclick = () => openBookingModal(d);
  $all('.day-cell').forEach(c => c.onclick = () => {
    state.selectedDate = c.dataset.day; renderCalendar();
  });
  $all('[data-appt-action]').forEach(bindApptAction);
  $all('[data-appt-open]').forEach(el => el.onclick = () =>
    openApptModal(Number(el.dataset.apptOpen)));
}

function appointmentRowHtml(a) {
  const tags = [];
  tags.push(`<span class="chip">${esc(a.breed || '未填品种')}</span>`);
  tags.push(`<span class="chip navy">${esc(a.size || '')}</span>`);
  if (a.taboos_snapshot) tags.push(`<span class="chip danger">⚠ ${esc(a.taboos_snapshot.split(',')[0])}</span>`);
  const svcs = a.services.map(s => esc(s.name)).join(' · ');
  const borderColor = a.staff_color || 'var(--green)';
  return `
  <div class="appt-row" style="border-left-color:${borderColor}">
    <div class="time">${a.start_time}<span>${a.end_time} 结束</span></div>
    <div class="avatar">${a.pet_photo ? `<img src="${photoUrl(a.pet_photo)}" style="width:100%;height:100%;border-radius:13px;object-fit:cover">` : '🐾'}</div>
    <div data-appt-open="${a.id}" style="cursor:pointer">
      <div class="main"><strong>${esc(a.pet_name)}</strong>
        <span class="badge-status st-${a.status}">${STATUS_LABELS[a.status] || a.status}</span>
        <span class="chip peach" style="margin-left:6px">${esc(a.staff_name || '未分配')} · ${esc(a.station_name || '')}</span>
      </div>
      <div class="svcs">${svcs} · 预计 ${a.estimated_min} 分钟 · 主人 ${esc(a.contact_name)} 📞 ${esc(a.contact_phone)}</div>
      <div class="tags">${tags.join('')}</div>
    </div>
    <div class="actions">${apptActionButtons(a)}</div>
  </div>`;
}

function apptActionButtons(a) {
  const btns = [];
  if (a.status === 'booked')
    btns.push(`<button class="btn btn-primary btn-sm" data-appt-action="confirm" data-id="${a.id}">✅ 接待确认</button>`);
  if (['reception_confirmed', 'servicing'].includes(a.status))
    btns.push(`<button class="btn btn-navy btn-sm" data-appt-action="care" data-id="${a.id}">✂️ 护理录入</button>`);
  if (a.status === 'completed')
    btns.push(`<button class="btn btn-peach btn-sm" data-appt-action="pickup" data-id="${a.id}">🔑 取宠核验</button>`);
  btns.push(`<button class="btn btn-ghost btn-sm" data-appt-open="${a.id}">详情</button>`);
  return btns.join('');
}

function bindApptAction(el) {
  const id = Number(el.dataset.id);
  const act = el.dataset.apptAction;
  if (act === 'confirm') el.onclick = () => openReceptionModal(id);
  if (act === 'care') el.onclick = async () => {
    const a = await api(`/api/appointments/${id}`);
    if (a.order) {
      state.selectedOrderId = a.order.id;
      await switchView('care');
    } else {
      toast('该预约尚未接待确认', 'error');
    }
  };
  if (act === 'pickup') el.onclick = () => {
    state.pickupApptId = id;
    switchView('pickup');
  };
}

// -- 新建预约弹窗 --
async function openBookingModal(date, petId = null) {
  const petOptions = await statePetsOptions(petId);
  const svcs = state.meta.services;
  const html = `
    <div class="grid" style="gap:12px">
      <div class="form-row">
        <div><label>宠物 *</label>
          <select id="bkPet">${petOptions}</select></div>
        <div><label>日期 *</label><input type="date" id="bkDate" value="${date}"></div>
      </div>
      <div class="form-row-3">
        <div><label>开始时间 *</label><input type="time" id="bkStart" value="10:00" step="300"></div>
        <div><label>美容师</label>
          <select id="bkStaff">
            <option value="">暂不分配</option>
            ${state.meta.staff.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}
          </select></div>
        <div><label>工位 *</label>
          <select id="bkStation">${state.meta.stations.map(s =>
            `<option value="${s.id}">${esc(s.name)}（容量 ${s.capacity}）</option>`).join('')}</select></div>
      </div>
      <div>
        <label>服务组合（预计时长自动合计，美容师排班按工位容量锁定）</label>
        <div class="grid" style="grid-template-columns:1fr 1fr;gap:6px;margin-top:4px" id="bkServices">
          ${svcs.map(s => `<label style="display:flex;gap:8px;align-items:center;margin:0;padding:8px 10px;border:1px solid var(--line);border-radius:10px;cursor:pointer">
            <input type="checkbox" style="width:auto" value="${s.id}" data-dur="${s.duration_min}">
            <span>${esc(s.name)} <span style="color:var(--muted)">${s.duration_min}分钟 ¥${s.price}</span></span></label>`).join('')}
        </div>
        <div class="ok-note" style="margin-top:8px" id="bkEstimate">请选择服务项目</div>
      </div>
      <div class="form-row">
        <div><label>提前提醒（分钟）</label><input type="number" id="bkReminder" value="30"></div>
        <div><label>备注</label><input id="bkNote" placeholder="到店注意事项"></div>
      </div>
      <div class="danger-note" id="bkAvail" hidden></div>
    </div>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" id="bkCheck">检查时段</button>
      <button class="btn btn-primary" id="bkSave">保存预约</button>
    </div>`;
  openModal('新建预约', html, true);

  const updateEst = () => {
    const ids = chosenServices();
    const total = ids.reduce((s, id) => s + svcs.find(x => x.id === id).duration_min, 0);
    const start = $('#bkStart').value;
    $('#bkEstimate').textContent = total && start
      ? `已选 ${ids.length} 项 · 预计 ${total} 分钟 · ${start} – ${minToHm(hmToMin(start) + total)}`
      : '请选择服务项目';
  };
  const chosenServices = () =>
    $all('#bkServices input:checked').map(cb => Number(cb.value));
  $all('#bkServices input').forEach(cb => cb.onchange = updateEst);
  $('#bkStart').onchange = updateEst;

  const collect = () => ({
    pet_id: Number($('#bkPet').value),
    staff_id: Number($('#bkStaff').value) || null,
    station_id: Number($('#bkStation').value),
    date: $('#bkDate').value,
    start: $('#bkStart').value,
    service_ids: chosenServices(),
    note: $('#bkNote').value,
    reminder_minutes: Number($('#bkReminder').value || 30),
  });

  $('#bkCheck').onclick = async () => {
    try {
      const b = collect();
      if (!b.service_ids.length) throw new Error('请先选择服务项目');
      const total = b.service_ids.reduce((s, id) =>
        s + svcs.find(x => x.id === id).duration_min, 0);
      const end = minToHm(hmToMin(b.start) + total);
      const q = new URLSearchParams({
        pet: b.pet_id, station: b.station_id, date: b.date,
        start: b.start, end,
      });
      if (b.staff_id) q.set('staff', b.staff_id);
      const r = await api(`/api/availability?${q}`);
      const note = $('#bkAvail');
      if (r.available) {
        note.className = 'ok-note';
        note.textContent = '✅ 时段可用：宠物无冲突、工位有容量、美容师排班覆盖';
      } else {
        const map = {
          pet_busy: '该宠物同一时段已有预约',
          station_full: '工位该时段已满（容量锁定）',
          staff_busy: '美容师该时段已有预约',
          no_shift: '美容师排班不覆盖该时段',
          no_station: '工位不存在',
        };
        note.className = 'danger-note';
        note.hidden = false;
        note.textContent = '⚠ ' + r.problems.map(p => map[p] || p).join('；');
      }
      note.hidden = false;
    } catch (e) { toast(e.message, 'error'); }
  };

  $('#bkSave').onclick = async () => {
    try {
      const b = collect();
      if (!b.service_ids.length) return toast('请至少选择一个服务项目', 'error');
      await api('/api/appointments', { method: 'POST', body: b });
      closeModal();
      toast('预约已保存，到点提醒已进入任务队列', 'success');
      state.selectedDate = b.date;
      await renderCalendar();
    } catch (e) {
      toast(e.message, e.status === 409 ? 'error' : 'error');
    }
  };
}

async function statePetsOptions(selected) {
  const pets = await api('/api/pets');
  return pets.map(p => `<option value="${p.id}" ${p.id === selected ? 'selected' : ''}>
    ${esc(p.name)}（${esc(p.breed)} · ${esc(p.size)} / ${esc(p.contact_name)}）</option>`).join('');
}

// -- 接待确认 --
function openReceptionModal(apptId) {
  openModal('接待确认', `
    <div class="grid" style="gap:12px">
      <p>接待确认后将根据预约中的服务组合生成护理单，护理项目逐项记录。</p>
      <div><label>皮肤毛发初检备注</label>
        <textarea id="rcSkin" rows="2" placeholder="如：腹部有约 2cm 红疹……"></textarea></div>
      <div><label>被毛状态备注</label>
        <textarea id="rcCoat" rows="2" placeholder="如：腋下轻度打结……"></textarea></div>
    </div>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-primary" id="rcSave">确认接待并生成护理单</button>
    </div>`);
  $('#rcSave').onclick = async () => {
    try {
      const r = await api(`/api/appointments/${apptId}/confirm-reception`, {
        method: 'POST',
        body: { by: currentStaff()?.name || '前台',
                skin_note: $('#rcSkin').value, coat_note: $('#rcCoat').value },
      });
      closeModal();
      toast('已接待，护理单 #' + r.order_id + ' 已生成', 'success');
      state.selectedOrderId = r.order_id;
      await switchView('care');
    } catch (e) { toast(e.message, 'error'); }
  };
}

// -- 预约详情 --
async function openApptModal(apptId) {
  const a = await api(`/api/appointments/${apptId}`);
  const svcs = a.services.map(s =>
    `<div class="order-item-row"><span>✂️ ${esc(s.name)}</span>
      <span class="chip gray" style="margin-left:auto">${s.duration_min}分钟 ¥${s.price}</span></div>`
  ).join('');
  const pickup = a.pickup
    ? `<div class="info-block"><div class="t">🔑 取宠核验</div>
       取宠码 <strong style="font-size:18px;letter-spacing:3px">${esc(a.pickup.code)}</strong>
       ${a.pickup.verified ? `· 已于 ${esc(a.pickup.verified_at)} 由 ${esc(a.pickup.verifier)} 核验放行` : '· 待核验'}</div>`
    : '';
  openModal(`预约详情 #${a.id}`, `
    <div class="kv">
      <dt>宠物</dt><dd><strong>${esc(a.pet_name)}</strong>（${esc(a.breed)} · ${esc(a.size)}）</dd>
      <dt>主人</dt><dd>${esc(a.contact_name)} 📞 ${esc(a.contact_phone)}</dd>
      <dt>时间</dt><dd>${esc(a.appt_date)} ${esc(a.start_time)} – ${esc(a.end_time)}（预计 ${a.estimated_min} 分钟）</dd>
      <dt>美容师</dt><dd>${esc(a.staff_name || '未分配')} / ${esc(a.station_name || '')}</dd>
      <dt>禁忌快照</dt><dd>${esc(a.taboos_snapshot) || '无'}</dd>
      <dt>备注</dt><dd>${esc(a.note) || '无'}</dd>
    </div>
    <div class="section-title">服务组合</div>${svcs}
    ${a.order ? `<div class="info-block"><div class="t">护理单</div>#${a.order.id} ·
      ${a.order.status === 'closed' ? '已结单' : '护理中'}
      ${a.order.confirmed ? ' · 记录已确认（不可删除）' : ''}</div>` : ''}
    ${pickup}`, true);
}

// ============================================================
// 视图二：宠物档案（照片 + 标签）
// ============================================================
async function renderPets() {
  const pets = await api('/api/pets');
  $('#main').innerHTML = `
    <div class="view-head">
      <div><h2>🐶 宠物档案</h2>
        <div class="sub">照片 + 标签布局 · 共 ${pets.length} 只 · 联系人资料为演示数据</div></div>
    </div>
    <div class="pet-grid">${pets.map(petCardHtml).join('')}</div>`;
  $all('[data-pet-open]').forEach(el => el.onclick = () =>
    openPetModal(Number(el.dataset.petOpen)));
}

function petCardHtml(p) {
  const tags = (p.tags || '').split(',').filter(Boolean);
  return `
  <div class="pet-card" data-pet-open="${p.id}">
    <div class="cover">${p.photo
      ? `<img src="${photoUrl(p.photo)}" alt="${esc(p.name)}">`
      : '<span style="font-size:54px">🐾</span>'}</div>
    <div class="body">
      <div class="name-row">
        <strong>${esc(p.name)}</strong>
        <span class="chip navy">${esc(p.species)} · ${esc(p.size)}</span>
      </div>
      <div class="contact">${esc(p.breed)} · ${p.gender || ''} · ${p.weight || '-'}kg</div>
      <div class="tag-row">
        ${tags.map(t => `<span class="chip">${esc(t)}</span>`).join('')}
      </div>
      ${p.taboos ? `<div class="danger-note">⚠ 禁忌：${esc(p.taboos)}</div>` : ''}
      <div class="footer-row">
        <span>👤 ${esc(p.contact_name)} · ${esc(p.contact_phone)}</span>
        <span class="btn btn-ghost btn-sm">查看档案</span>
      </div>
    </div>
  </div>`;
}

async function openPetModal(petId) {
  const p = await api(`/api/pets/${petId}`);
  const tags = (p.tags || '').split(',').filter(Boolean);
  const appts = p.appointments.map(a =>
    `<div class="order-item-row"><span>${a.appt_date} ${a.start_time}</span>
      <span class="badge-status st-${a.status}" style="margin-left:auto">${STATUS_LABELS[a.status]}</span></div>`
  ).join('') || '<p>暂无历史预约</p>';
  openModal(`${p.name} 的档案`, `
    <div class="pet-detail-head">
      ${p.photo ? `<img src="${photoUrl(p.photo)}">` : '<span style="font-size:60px">🐾</span>'}
      <div>
        <h2 style="margin-bottom:6px">${esc(p.name)}
          <span class="chip navy">${esc(p.species)} · ${esc(p.breed)}</span>
          <span class="chip">${esc(p.size)}</span></h2>
        <div class="tag-row">${tags.map(t => `<span class="chip">${esc(t)}</span>`).join('')}</div>
      </div>
    </div>
    <dl class="kv">
      <dt>性别 / 年龄</dt><dd>${esc(p.gender || '-')} · ${p.birth_year ? new Date().getFullYear() - p.birth_year + ' 岁' : '-'}</dd>
      <dt>体重</dt><dd>${p.weight ?? '-'} kg</dd>
      <dt>性格</dt><dd>${esc(p.temperament) || '-'}</dd>
      <dt>主人</dt><dd>${esc(p.contact_name)} 📞 ${esc(p.contact_phone)} · ${esc(p.contact_address || '')}</dd>
      <dt>主人备注</dt><dd>${esc(p.contact_note) || '-'}</dd>
    </dl>
    <div class="info-block"><div class="t">🚫 禁忌</div>${esc(p.taboos) || '无'}</div>
    <div class="info-block"><div class="t">🔍 皮肤毛发备注</div>
      皮肤：${esc(p.skin_note) || '无异常'}<br>被毛：${esc(p.coat_note) || '无'}</div>
    <div class="section-title">最近预约</div>${appts}
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-peach" id="petBookBtn">＋ 为 ${esc(p.name)} 预约</button>
    </div>`, true);
  $('#petBookBtn').onclick = () => { closeModal(); openBookingModal(state.selectedDate, petId); };
}

// ============================================================
// 视图三：工位排程（时间轴）
// ============================================================
const TL_START = 9, TL_END = 19; // 09:00 - 19:00
async function renderStations() {
  const d = state.selectedDate;
  const stations = await api(`/api/timeline?date=${d}`);
  const hours = Array.from({ length: TL_END - TL_START }, (_, i) => TL_START + i);
  const hoursHtml = `<div style="display:flex;width:800px">
    ${hours.map(h => `<div style="width:80px;font-size:11px;color:var(--muted)">${h}:00</div>`).join('')}</div>`;

  const lanes = stations.map(st => {
    const shiftBlocks = st.shifts.map(sh => {
      const left = (hmToMin(sh.start_time) - TL_START * 60) / 60 * 80;
      const width = (hmToMin(sh.end_time) - hmToMin(sh.start_time)) / 60 * 80;
      return `<div class="tl-shift" style="left:${left}px;width:${width}px"
        title="${esc(sh.staff_name)} 排班 ${sh.start_time}-${sh.end_time}"></div>`;
    }).join('');
    const blocks = st.appointments.map(a => {
      const left = Math.max(0, (hmToMin(a.start_time) - TL_START * 60) / 60 * 80);
      const width = Math.max(46, (hmToMin(a.end_time) - hmToMin(a.start_time)) / 60 * 80 - 4);
      const color = a.color || '#4E8D7C';
      return `<div class="tl-block" data-appt-open="${a.id}"
        style="left:${left}px;width:${width}px;background:${color}"
        title="${esc(a.pet_name)} ${a.start_time}-${a.end_time}">
        <strong>${a.start_time} ${esc(a.pet_name)}</strong>
        <span class="bar-sub">${esc(a.breed || '')} · ${esc(a.staff_name || '')} · ${STATUS_LABELS[a.status]}</span>
      </div>`;
    }).join('');
    const staff = [...new Set(st.shifts.map(s => s.staff_name))].join(' / ') || '今日无人排班';
    return `<div class="tl-station">
      <div class="tl-station-head">
        <strong>🧺 ${esc(st.name)}</strong>
        <span class="chip gray">容量 ${st.capacity} · ${esc(staff)}</span>
      </div>
      <div class="tl-scroll">
        <div style="width:800px">
          ${hoursHtml}
          <div class="tl-track" style="width:800px">${shiftBlocks}${blocks}</div>
        </div>
      </div>
    </div>`;
  }).join('');

  $('#main').innerHTML = `
    <div class="view-head">
      <div><h2>🧺 工位排程</h2>
        <div class="sub">按时间轴展开 · 虚线框为美容师排班区间，色块为已锁定预约</div></div>
      <div class="cal-nav">
        <button class="btn btn-ghost btn-sm" id="stPrev">‹</button>
        <input type="date" id="stDate" value="${d}" style="width:150px">
        <button class="btn btn-ghost btn-sm" id="stNext">›</button>
      </div>
    </div>
    <div class="tl-scroll"><div class="tl" style="min-width:820px">${lanes}</div></div>`;

  $('#stPrev').onclick = () => { state.selectedDate = addDays(d, -1); renderStations(); };
  $('#stNext').onclick = () => { state.selectedDate = addDays(d, 1); renderStations(); };
  $('#stDate').onchange = e => { state.selectedDate = e.target.value; renderStations(); };
  $all('.tl-block').forEach(el => el.onclick = () => openApptModal(Number(el.dataset.apptOpen)));
}

// ============================================================
// 视图四：护理录入
// ============================================================
async function renderCare() {
  const orders = await api('/api/orders');
  if (!state.selectedOrderId && orders[0]) state.selectedOrderId = orders[0].id;
  const listHtml = orders.length ? orders.map(o => `
    <div class="order-pick ${o.id === state.selectedOrderId ? 'active' : ''}" data-order="${o.id}">
      <strong>#${o.id} ${esc(o.pet_name)}</strong>
      <div class="meta">
        <span>${o.appt_date} ${o.start_time}</span>
        <span>${esc(o.staff_name || '未分配')}</span>
        <span class="badge-status ${o.status === 'closed' ? 'st-completed' : 'st-servicing'}">
          ${o.status === 'closed' ? '已结单' : '护理中'}</span>
        ${o.confirmed ? '<span class="chip navy">已确认</span>' : ''}
      </div>
    </div>`).join('')
    : '<div class="empty"><span class="big">📋</span>暂无护理单<br>请先在日历中进行接待确认</div>';

  $('#main').innerHTML = `
    <div class="view-head"><div><h2>✂️ 护理录入</h2>
      <div class="sub">服务项目完成一项记录一项 · 存在未完成项目时不能结单</div></div></div>
    <div class="care-layout">
      <div class="card" style="padding:12px"><div class="orders-list">${listHtml}</div></div>
      <div id="careDetail"><div class="card empty"><span class="big">←</span>请选择左侧护理单</div></div>
    </div>`;
  $all('.order-pick').forEach(el => el.onclick = () => {
    state.selectedOrderId = Number(el.dataset.order); renderCare();
  });
  if (state.selectedOrderId) await renderCareDetail(state.selectedOrderId);
}

async function renderCareDetail(orderId) {
  let o;
  try {
    o = await api(`/api/orders/${orderId}`);
  } catch (e) { toast(e.message, 'error'); return; }
  const authorized = !!currentStaff()?.can_view_sensitive;
  const closed = o.status === 'closed';

  const itemsHtml = o.items.map(i => {
    const done = i.status === 'done';
    return `<div class="care-item ${done ? 'done' : ''}">
      <div>
        <div class="ci-name">${done ? '✅' : '▫️'} ${esc(i.name)}</div>
        <div class="ci-meta">标准 ${i.duration_min} 分钟
          ${done ? `· 完成于 ${esc(i.completed_at)} · ${esc(i.completed_by)}` : '· 待完成'}
          ${i.remark ? `<br>📝 ${esc(i.remark)}` : ''}</div>
      </div>
      ${closed
        ? `<span class="big-status done-state" style="min-width:110px;min-height:48px;font-size:15px">${done ? '已完成' : '已跳过'}</span>`
        : `<button class="big-status ${done ? 'undo' : 'do'}" data-item="${i.id}" data-status="${done ? 'pending' : 'done'}">
             ${done ? '↩ 撤销完成<span class="bs-sub">登记有误时回退</span>' : '✅ 完成此项<span class="bs-sub">点击即记录时间</span>'}
           </button>`}
    </div>`;
  }).join('');

  const eventsHtml = o.events.length ? o.events.map(ev => {
    const photos = o.photos.filter(p => p.event_id === ev.id);
    return `<div class="event-line">
      <span class="ev-icon">${EVENT_ICON[ev.type] || '•'}</span>
      <div style="flex:1">
        <div class="ev-title"><span class="chip ${EVENT_COLOR[ev.type] || ''}">${esc(ev.title)}</span></div>
        <div class="ev-detail">${esc(ev.detail)}</div>
        <div class="ev-meta">${esc(ev.created_by)} · ${esc(ev.created_at)}</div>
        ${photos.length ? `<div class="photo-row" style="margin-top:7px">${photos.map(photoThumbHtml).join('')}</div>` : ''}
      </div>
    </div>`;
  }).join('') : '<p style="color:var(--muted);font-size:13px">暂无异常事件</p>';

  const usagesHtml = o.usages.length ? o.usages.map(u =>
    `<div class="order-item-row"><span>${u.caused_allergy ? '🌿⚠️' : '🧴'} ${esc(u.supply_name)} × ${u.qty}</span>
      <span style="margin-left:auto" class="chip ${u.caused_allergy ? 'danger' : 'gray'}">
      ${u.caused_allergy ? '引发过敏' : '正常消耗'}</span></div>`).join('')
    : '<p style="color:var(--muted);font-size:13px">暂无用品消耗记录</p>';

  const photosHtml = o.photos.length
    ? `<div class="photo-row">${o.photos.map(photoThumbHtml).join('')}</div>`
    : '<p style="color:var(--muted);font-size:13px">暂无照片</p>';

  const progress = o.items.length
    ? Math.round(o.done_count / o.items.length * 100) : 0;

  $('#careDetail').innerHTML = `
  <div class="grid" style="gap:14px">
    <div class="card">
      <div class="view-head" style="margin-bottom:10px">
        <div>
          <h2>#${o.id} ${esc(o.pet_name)}
            <span class="badge-status ${closed ? 'st-completed' : 'st-servicing'}">
              ${closed ? '已结单' : '护理中'}</span>
            ${o.confirmed ? '<span class="chip navy">🔒 记录已确认</span>' : ''}</h2>
          <div class="sub">${esc(o.appt_date)} ${esc(o.start_time)}-${esc(o.end_time)}
            · ${esc(o.staff_name || '未分配')} · 主人 ${esc(o.contact_name)} ${esc(o.contact_phone)}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:24px;font-weight:900;color:var(--green-deep)">${progress}%</div>
          <div class="sub">${o.done_count}/${o.items.length} 项完成</div>
        </div>
      </div>
      <div class="snapshot-box">
        <div>🐾 品种/体型快照：<strong>${esc(o.breed || '未填')} · ${esc(o.size || '')}</strong></div>
        <div>⚠️ 禁忌：${esc(o.taboos) || '无'}</div>
        <div>🔍 接待皮肤备注：${esc(o.skin_note) || '无'}　|　档案皮肤史：${esc(o.pet_skin_note) || '无'}</div>
        <div>🌿 被毛备注：${esc(o.coat_note) || '无'}　|　档案被毛史：${esc(o.pet_coat_note) || '无'}</div>
      </div>
      ${closed ? `<div class="ok-note" style="margin-top:10px">护理单已结单${o.confirmed ? '，记录确认后不可删除、不可修改' : ''}。
        总结：${esc(o.summary) || '（无）'}</div>` : ''}
    </div>

    <div class="card">
      <div class="section-title">护理项目（大号状态按钮逐项记录）</div>
      <div class="grid" style="gap:10px">${itemsHtml}</div>
      ${!closed ? `
      <div class="view-head" style="margin-top:14px; margin-bottom:0">
        <div class="filter-row" style="margin:0">
          <button class="btn btn-ghost btn-sm" id="changeBtn">✏️ 主人临时改项</button>
          <button class="btn btn-ghost btn-sm" id="supplyBtn">🧴 登记用品</button>
          <button class="btn btn-ghost btn-sm" id="eventBtn">⚠️ 登记事件</button>
          <button class="btn btn-peach btn-sm" id="uploadBtn">📷 上传照片</button>
        </div>
        <div style="display:flex;gap:8px">
          ${o.confirmed ? '' : `<button class="btn btn-danger btn-sm" id="deleteBtn">删除</button>`}
          <button class="btn btn-navy btn-sm" id="closeBtn">🔒 结单</button>
        </div>
      </div>
      ${o.pending_count ? `<div class="danger-note" style="margin-top:10px">
        还有 ${o.pending_count} 项未完成，结单将被系统拒绝</div>`
        : `<div class="ok-note" style="margin-top:10px">全部项目已完成，可以结单（结单后生成取宠码）</div>`}`
        : !o.confirmed
          ? `<div class="view-head" style="margin-top:14px;margin-bottom:0">
               <button class="btn btn-primary" id="confirmBtn">✅ 确认护理记录（确认后不可删除）</button>
             </div>` : ''}
    </div>

    <div class="card">
      <div class="section-title">异常与事件
        ${!authorized ? '<span class="chip danger" style="margin-left:8px">当前身份无权查看异常照片</span>' : ''}
      </div>
      <div class="grid" style="gap:8px">${eventsHtml}</div>
      <div class="section-title">用品消耗</div>${usagesHtml}
      <div class="section-title">照片（图标 + 文字 + 缩略图）</div>${photosHtml}
    </div>
  </div>`;

  // 项目状态按钮
  $all('[data-item]').forEach(btn => btn.onclick = async () => {
    try {
      await api(`/api/orders/${orderId}/items/${btn.dataset.item}/status`, {
        method: 'POST',
        body: { status: btn.dataset.status, by: currentStaff()?.name || '美容师' },
      });
      await renderCareDetail(orderId);
    } catch (e) { toast(e.message, 'error'); }
  });

  const $b = id => document.getElementById(id);
  if ($b('closeBtn')) $b('closeBtn').onclick = () => openCloseModal(o);
  if ($b('confirmBtn')) $b('confirmBtn').onclick = async () => {
    if (!confirm('确认后护理记录将无法删除，确定确认？')) return;
    try {
      await api(`/api/orders/${orderId}/confirm`, {
        method: 'POST', body: { by: currentStaff()?.name || '店长' } });
      toast('护理记录已确认', 'success');
      await renderCareDetail(orderId);
    } catch (e) { toast(e.message, 'error'); }
  };
  if ($b('deleteBtn')) $b('deleteBtn').onclick = async () => {
    if (!confirm('删除该护理单？已确认的记录无法删除。')) return;
    try {
      const r = await fetch(`/api/orders/${orderId}`, { method: 'DELETE' });
      if (!r.ok) { toast((await r.json()).error, 'error'); return; }
      toast('护理单已删除', 'success');
      state.selectedOrderId = null;
      await renderCare();
    } catch (e) { toast(e.message, 'error'); }
  };
  if ($b('changeBtn')) $b('changeBtn').onclick = () => openChangeModal(o);
  if ($b('supplyBtn')) $b('supplyBtn').onclick = () => openSupplyModal(o);
  if ($b('eventBtn')) $b('eventBtn').onclick = () => openEventModal(o);
  if ($b('uploadBtn')) $b('uploadBtn').onclick = () => openUploadModal(o);

  // 缩略图点击
  $all('[data-photo-open]').forEach(img => img.onclick = () =>
    openPhotoModal(Number(img.dataset.photoOpen)));
  $all('[data-photo-locked]').forEach(el => el.onclick = () =>
    toast('🔒 异常照片仅限授权人员查看，请切换到店长/高级美容师身份', 'error'));
}

function photoThumbHtml(p) {
  if (p.locked) {
    return `<div class="thumb locked" data-photo-locked="1" title="异常照片 · 无权查看">🔒</div>`;
  }
  return `<img class="thumb" data-photo-open="${p.id}" src="${photoUrl(p.filename)}"
    title="${esc(p.caption || '')}">`;
}

async function openPhotoModal(photoId) {
  try {
    const p = await api(`/api/photos/${photoId}`);
    openModal(p.kind === 'sensitive' ? '🔐 异常照片（授权查看）' : '照片',
      `<img src="${photoUrl(p.filename)}" style="width:100%;border-radius:12px">
       <div class="info-block"><div class="t">${esc(p.caption || '（无说明）')}</div>
       上传：${esc(p.uploaded_by)} · ${esc(p.created_at)}</div>`, true);
  } catch (e) { toast(e.message, 'error'); }
}

// -- 结单 --
function openCloseModal(o) {
  openModal('结单确认', `
    <p>结单后护理项目不可再修改，并自动生成 6 位取宠码。</p>
    ${o.pending_count ? `<div class="danger-note" style="margin:10px 0">
      ❌ 仍有 ${o.pending_count} 项未完成，系统将拒绝结单：
      ${o.items.filter(i => i.status === 'pending').map(i => esc(i.name)).join('、')}</div>` : ''}
    <label style="margin-top:10px">护理总结</label>
    <textarea id="closeSummary" rows="3" placeholder="本次护理情况、给主人的建议……"></textarea>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-navy" id="closeSave">确认结单</button>
    </div>`);
  $('#closeSave').onclick = async () => {
    try {
      const r = await api(`/api/orders/${o.id}/close`, {
        method: 'POST',
        body: { by: currentStaff()?.name || '美容师', summary: $('#closeSummary').value } });
      closeModal();
      toast(`结单成功，取宠码：${r.pickup_code}`, 'success');
      await renderCareDetail(o.id);
    } catch (e) {
      toast('结单被拒绝：' + e.message, 'error');
    }
  };
}

// -- 主人改项 --
function openChangeModal(o) {
  const currentIds = new Set(o.items.map(i => i.service_id).filter(Boolean));
  const html = `
    <p>勾选改项后的完整服务组合；已完成但被去掉的项目会保留并登记事件。</p>
    <div class="grid" style="grid-template-columns:1fr 1fr;gap:6px;margin:10px 0">
      ${state.meta.services.map(s => `<label style="display:flex;gap:8px;align-items:center;margin:0;padding:8px 10px;border:1px solid var(--line);border-radius:10px">
        <input type="checkbox" style="width:auto" value="${s.id}" ${currentIds.has(s.id) ? 'checked' : ''}>
        <span>${esc(s.name)}（${s.duration_min}分钟）</span></label>`).join('')}
    </div>
    <label>变更原因 / 主人要求</label>
    <input id="changeReason" placeholder="如：主人临时追加洁牙">
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-primary" id="changeSave">保存改项</button>
    </div>`;
  openModal('✏️ 主人临时改项', html, true);
  $('#changeSave').onclick = async () => {
    const ids = $all('#modalBox input[type=checkbox]:checked').map(cb => Number(cb.value));
    if (!ids.length) return toast('至少保留一个服务项目', 'error');
    try {
      await api(`/api/orders/${o.id}/change-services`, {
        method: 'POST',
        body: { service_ids: ids, by: currentStaff()?.name || '前台',
                reason: $('#changeReason').value } });
      closeModal();
      toast('已改项并生成「主人临时改项」事件，预计时长已重算', 'success');
      await renderCareDetail(o.id);
    } catch (e) { toast(e.message, 'error'); }
  };
}

// -- 用品消耗 --
function openSupplyModal(o) {
  openModal('🧴 登记用品消耗', `
    <div class="grid" style="gap:12px">
      <div class="form-row">
        <div><label>用品</label><select id="spSupply">
          ${state.meta.supplies.map(s => `<option value="${s.id}">${esc(s.name)}（库存 ${s.stock}）</option>`).join('')}
        </select></div>
        <div><label>数量</label><input type="number" id="spQty" value="1" min="0.5" step="0.5"></div>
      </div>
      <label style="display:flex;gap:8px;align-items:center;margin:0">
        <input type="checkbox" style="width:auto" id="spAllergy">
        使用后出现过敏反应（将自动生成「用品过敏」事件与通知）</label>
      <div id="allergyBox" hidden><label>过敏表现 / 处置</label>
        <textarea id="spDetail" rows="2" placeholder="如：腹部红斑、频繁舔舐，已清水冲净……"></textarea></div>
    </div>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-primary" id="spSave">保存</button>
    </div>`);
  $('#spAllergy').onchange = e => { $('#allergyBox').hidden = !e.target.checked; };
  $('#spSave').onclick = async () => {
    try {
      await api(`/api/orders/${o.id}/supplies`, {
        method: 'POST',
        body: { supply_id: Number($('#spSupply').value), qty: Number($('#spQty').value),
                by: currentStaff()?.name || '美容师', caused_allergy: $('#spAllergy').checked,
                allergy_detail: $('#spDetail').value } });
      closeModal();
      toast($('#spAllergy').checked ? '已登记用品消耗，并生成过敏事件' : '用品消耗已登记', 'success');
      await renderCareDetail(o.id);
    } catch (e) { toast(e.message, 'error'); }
  };
}

// -- 登记事件 --
function openEventModal(o) {
  const types = [
    ['resistance', '🐶 宠物抗拒'], ['skin', '🔍 皮肤异常'], ['cut', '🩹 剪伤'],
    ['overdue', '⏰ 超时未取'], ['allergy', '🌿 用品过敏'],
  ];
  openModal('⚠️ 登记异常事件', `
    <div class="grid" style="gap:12px">
      <div><label>事件类型</label><select id="evType">
        ${types.map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}
      </select></div>
      <div><label>标题</label><input id="evTitle" placeholder="一句话概述"></div>
      <div><label>详细经过 / 处置 / 是否已告知主人</label>
        <textarea id="evDetail" rows="4"></textarea></div>
      <div class="danger-note">剪伤、皮肤异常、过敏、超时未取将同时生成店长通知；
        事件照片请在保存后通过「上传照片 → 异常照片」留档。</div>
    </div>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-primary" id="evSave">保存事件</button>
    </div>`);
  $('#evSave').onclick = async () => {
    try {
      await api('/api/events', {
        method: 'POST',
        body: { type: $('#evType').value, title: $('#evTitle').value,
                detail: $('#evDetail').value, by: currentStaff()?.name || '前台',
                pet_id: o.pet_id, appointment_id: o.appointment_id, order_id: o.id } });
      closeModal();
      toast('事件已记录', 'success');
      await renderCareDetail(o.id);
    } catch (e) { toast(e.message, 'error'); }
  };
}

// -- 上传照片 --
function openUploadModal(o) {
  const authorized = !!currentStaff()?.can_view_sensitive;
  openModal('📷 上传照片', `
    <div class="grid" style="gap:12px">
      <div><label>照片文件</label><input type="file" id="upFile" accept="image/*"></div>
      <div class="form-row">
        <div><label>照片类型</label><select id="upKind">
          <option value="normal">普通照片（护理后状态等）</option>
          <option value="sensitive" ${authorized ? '' : 'disabled'}>
            异常照片（剪伤/皮肤，仅授权人员可看）${authorized ? '' : '——当前身份无权限'}</option>
        </select></div>
        <div><label>说明</label><input id="upCaption" placeholder="如：剪伤处理后留档"></div>
      </div>
      ${authorized ? '' : '<div class="danger-note">当前身份不是授权人员，只能上传普通照片。</div>'}
    </div>
    <div class="modal-foot" style="padding-right:0;padding-bottom:0">
      <button class="btn btn-ghost" onclick="document.querySelector('#modalClose').click()">取消</button>
      <button class="btn btn-primary" id="upSave">上传（保存到本地目录）</button>
    </div>`);
  $('#upSave').onclick = async () => {
    const file = $('#upFile').files[0];
    if (!file) return toast('请选择照片文件', 'error');
    try {
      const content_base64 = await fileToBase64(file);
      await api('/api/photos', {
        method: 'POST',
        body: { content_base64, filename: file.name, kind: $('#upKind').value,
                caption: $('#upCaption').value, pet_id: o.pet_id,
                appointment_id: o.appointment_id,
                uploaded_by: currentStaff()?.name || '前台' } });
      closeModal();
      toast('照片已保存到本地 uploads 目录', 'success');
      await renderCareDetail(o.id);
    } catch (e) { toast(e.message, 'error'); }
  };
}

// ============================================================
// 视图五：异常照片
// ============================================================
async function renderPhotos() {
  const photos = await api('/api/photos');
  const authorized = !!currentStaff()?.can_view_sensitive;
  const counts = {
    all: photos.length,
    sensitive: photos.filter(p => p.kind === 'sensitive').length,
    normal: photos.filter(p => p.kind === 'normal').length,
  };
  const list = photos.filter(p =>
    state.photoFilter === 'all' ? true : p.kind === state.photoFilter);

  const card = p => {
    const locked = p.locked;
    return `<div class="photo-card">
      <div class="ph ${locked ? 'locked' : ''}" ${locked ? 'data-photo-locked="1"' : `data-photo-open="${p.id}"`}>
        ${locked
          ? `<div class="lock-overlay"><div class="lk-ico">🔒</div>
             <div style="font-size:13px;font-weight:700">异常照片 · 授权可见</div>
             <div style="font-size:11.5px">切换到 周海 / 夏栀 身份查看</div></div>`
          : `<img src="${photoUrl(p.filename)}">`}
        ${p.kind === 'sensitive'
          ? `<span class="sensitive-flag">${locked ? '🔐 异常 · 已锁定' : '🔐 异常照片'}</span>` : ''}
      </div>
      <div class="body">
        <div class="cap">${esc(p.caption || '（无说明）')}</div>
        <div class="meta">🐾 ${esc(p.pet_name || '—')} · ${esc(p.uploaded_by)}</div>
        <div class="meta">${esc(p.created_at)}</div>
      </div>
    </div>`;
  };

  $('#main').innerHTML = `
    <div class="view-head">
      <div><h2>📷 异常照片</h2>
        <div class="sub">图标 + 文字 + 缩略图组合 · 异常照片仅授权人员可查看 / 下载</div></div>
      <span class="chip ${authorized ? '' : 'danger'}">
        ${authorized ? '🔓 当前身份：' + currentStaff().name + '（已授权查看异常照片）' : '🔒 当前身份无权查看异常照片'}</span>
    </div>
    <div class="filter-row">
      ${[['all', '全部', counts.all], ['sensitive', '🔐 异常照片', counts.sensitive],
         ['normal', '普通照片', counts.normal]]
        .map(([k, l, n]) => `<button class="filter-btn ${state.photoFilter === k ? 'active' : ''}"
          data-filter="${k}">${l}（${n}）</button>`).join('')}
    </div>
    ${list.length ? `<div class="photo-grid">${list.map(card).join('')}</div>`
      : '<div class="card empty"><span class="big">📷</span>暂无照片</div>'}`;

  $all('[data-filter]').forEach(b => b.onclick = () => {
    state.photoFilter = b.dataset.filter; renderPhotos();
  });
  $all('[data-photo-open]').forEach(el => el.onclick = () =>
    openPhotoModal(Number(el.dataset.photoOpen)));
  $all('[data-photo-locked]').forEach(el => el.onclick = () =>
    toast('🔒 权限不足：异常照片只能由授权人员查看', 'error'));
}

// ============================================================
// 视图六：取宠核验
// ============================================================
async function renderPickup() {
  const from = addDays(todayStr(), -7);
  const appts = (await api(`/api/appointments?from=${from}&to=${todayStr()}`))
    .filter(a => ['completed', 'picked_up'].includes(a.status));
  const presetId = state.pickupApptId || appts[0]?.id || '';

  $('#main').innerHTML = `
    <div class="pickup-hero">
      <div>
        <h2>🔑 取宠核验</h2>
        <p>主人报 6 位取宠码，核验通过后宠物方可放行。取宠码在护理单结单时自动生成。</p>
      </div>
      <div class="card" style="color:var(--ink)">
        <label>预约单号</label>
        <select id="pkAppt" style="margin-bottom:10px">
          ${appts.length ? appts.map(a => `<option value="${a.id}" ${a.id === presetId ? 'selected' : ''}>
            #${a.id} ${esc(a.pet_name)} · ${a.appt_date} · ${STATUS_LABELS[a.status]}</option>`).join('')
            : '<option value="">近 7 天无待取宠物</option>'}
        </select>
        <label>取宠码</label>
        <div class="code-input">
          <input id="pkCode" maxlength="6" placeholder="6位码" style="text-transform:uppercase">
          <button class="btn btn-peach" id="pkVerify" style="white-space:nowrap">核验放行</button>
        </div>
      </div>
    </div>
    <div class="section-title">待取 / 已取列表</div>
    ${appts.length ? `<div class="grid" style="gap:10px">${appts.map(a => `
      <div class="pick-card">
        <div class="pc-main">
          <strong>#${a.id} ${esc(a.pet_name)}</strong>
          <span class="badge-status st-${a.status}" style="margin-left:8px">${STATUS_LABELS[a.status]}</span>
          <div style="font-size:12.5px;color:var(--muted);margin-top:3px">
            ${a.appt_date} ${a.start_time} · 主人 ${esc(a.contact_name)} ${esc(a.contact_phone)}</div>
        </div>
        ${a.status === 'completed'
          ? `<button class="btn btn-ghost btn-sm" data-use-code="${a.id}">填入此单</button>`
          : `<span class="chip">✅ 已核验放行</span>`}
      </div>`).join('')}</div>`
      : '<div class="card empty"><span class="big">🔑</span>暂无待取宠物</div>'}`;

  $('#pkVerify').onclick = async () => {
    const id = Number($('#pkAppt').value);
    const code = $('#pkCode').value.trim();
    if (!id || !code) return toast('请选择预约并输入取宠码', 'error');
    try {
      const r = await api(`/api/appointments/${id}/verify-pickup`, {
        method: 'POST', body: { code, verifier: currentStaff()?.name || '前台' } });
      toast(`✅ 核验通过，已于 ${r.verified_at} 放行`, 'success');
      state.pickupApptId = null;
      await renderPickup();
    } catch (e) {
      toast('核验失败：' + e.message, 'error');
    }
  };
  $all('[data-use-code]').forEach(async el => {
    el.onclick = async () => {
      const id = Number(el.dataset.useCode);
      $('#pkAppt').value = id;
      const a = await api(`/api/appointments/${id}`);
      $('#pkCode').value = a.pickup?.code || '';
      toast('已自动填入取宠码（演示便利），点击「核验放行」');
    };
  });
}

// ============================================================
// 通知抽屉（进程内任务）
// ============================================================
function bindDrawer() {
  $('#bellBtn').onclick = async () => {
    $('#drawer').hidden = false;
    await renderDrawer();
  };
  $('#drawerClose').onclick = () => { $('#drawer').hidden = true; };
}

async function renderDrawer() {
  const list = await api('/api/notifications?limit=30');
  const kindLabel = { reminder: '到点提醒', overdue: '超时未取', event: '异常通知', pickup: '取宠通知' };
  $('#drawerBody').innerHTML = list.length ? list.map(n => `
    <div class="notif ${n.status}">
      <div style="display:flex;gap:8px;align-items:center">
        <span class="chip ${n.status === 'sent' ? '' : n.status === 'failed' ? 'danger' : 'amber'}">
          ${n.status === 'sent' ? '✅ 已发送' : n.status === 'failed' ? '❌ 已失败' : '⏳ 待发送/重试中'}</span>
        <span class="chip navy">${kindLabel[n.kind] || n.kind}</span>
        <span style="font-size:11.5px;color:var(--muted);margin-left:auto">第 ${n.attempts}/${n.max_attempts} 次</span>
      </div>
      <div style="font-size:13px">${esc(n.payload.replace(/^fail:[^:]+:/, ''))}</div>
      <div class="meta">
        <span>${esc(n.channel)}</span>
        <span>计划：${esc(n.next_run_at)}</span>
        ${n.status === 'sent' && n.sent_at ? `<span>发送于 ${esc(n.sent_at)}</span>` : ''}
        ${n.last_error ? `<span style="color:var(--danger)">${esc(n.last_error)}</span>` : ''}
        ${n.status !== 'sent' ? `<button class="btn btn-ghost btn-sm" style="padding:2px 8px" data-send="${n.id}">立即发送</button>` : ''}
      </div>
    </div>`).join('')
    : '<div class="empty">暂无通知</div>';
  $all('[data-send]').forEach(b => b.onclick = async () => {
    try {
      const r = await api(`/api/notifications/${b.dataset.send}/send`, { method: 'POST' });
      toast(r.status === 'sent' ? '发送成功' :
        r.status === 'failed' ? '已达最大重试次数，标记失败' :
        '发送失败，已安排退避重试', r.status === 'sent' ? 'success' : '');
      await renderDrawer();
    } catch (e) { toast(e.message, 'error'); }
  });
}

async function pollNotifications() {
  try {
    const list = await api('/api/notifications?limit=30');
    const attention = list.filter(n => n.status !== 'sent').length;
    $('#bellDot').classList.toggle('show', attention > 0);
    if (!$('#drawer').hidden) await renderDrawer();
  } catch { /* 忽略轮询错误 */ }
}

init().catch(e => {
  $('#main').innerHTML =
    `<div class="card empty"><span class="big">⚠️</span>启动失败：${esc(e.message)}<br>
     请确认已运行 <code>python3 app.py</code></div>`;
});
