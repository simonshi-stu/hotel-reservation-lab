(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const modes = ['strict', 'upgrade', 'protected_upgrade'];
  const modeNames = {strict: '严格房型', upgrade: '允许升级', protected_upgrade: '保护库存升级'};
  const typeNames = {standard: '标准房', deluxe: '豪华房', family: '家庭房', suite: '套房'};
  const bedNames = {king: '大床', twin: '双床', family: '家庭床'};
  const statusLabels = {BOOKED: '预订成功', REJECTED: '业务拒单', CANCELLED: '已取消', ROLLED_BACK: '已回滚'};
  const reasonLabels = {
    BED_TYPE_INCOMPATIBLE: '床型不符合要求',
    PARTY_OR_CHILD_REQUIREMENTS_UNSUPPORTED: '人数或儿童入住条件不满足',
    NO_SAME_ROOM_FOR_ENTIRE_STAY: '没有整段连续可住的同一间房',
    UPGRADE_COST_BUDGET_EXCEEDED: '升级成本超出上限',
    PROTECTED_INVENTORY: '升级会占用受保护库存',
    AUXILIARY_INVENTORY_EXHAUSTED: '婴儿床或加床库存不足',
    NO_ELIGIBLE_ROOM_TYPE: '没有符合策略的房型'
  };
  const storageKey = 'hotel-room-lab:retry:v1';
  const emptyMemory = () => ({last: {}, pending: {}, archived: [], selectedMode: 'upgrade'});
  const validPayload = (kind, payload, selectedMode) => Boolean(payload && payload.mode === selectedMode && modes.includes(selectedMode)
    && typeof payload.generation === 'string' && payload.generation
    && (kind === 'reset' || (kind === 'cancel' && typeof payload.request_id === 'string')
      || (kind === 'reserve' && payload.request && typeof payload.request.request_id === 'string')));
  let remembered = emptyMemory();
  let storageAvailable = true;
  try {
    const stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (stored && typeof stored === 'object') {
      modes.forEach(selectedMode => {
        if (stored.last && validPayload('reserve', stored.last[selectedMode], selectedMode)) remembered.last[selectedMode] = stored.last[selectedMode];
        const operation = stored.pending && stored.pending[selectedMode];
        if (operation && validPayload(operation.kind, operation.payload, selectedMode)) remembered.pending[selectedMode] = {...operation, uncertain: operation.uncertain !== false};
      });
      remembered.archived = Array.isArray(stored.archived) ? stored.archived.filter(operation => operation && operation.payload && validPayload(operation.kind, operation.payload, operation.payload.mode)) : [];
      if (modes.includes(stored.selectedMode)) remembered.selectedMode = stored.selectedMode;
    }
  } catch (_) { storageAvailable = false; }
  // Reopen the unresolved operation first, rather than hiding it on another strategy.
  let mode = modes.find(selectedMode => remembered.pending[selectedMode]?.kind === 'reset')
    || (remembered.pending[remembered.selectedMode] ? remembered.selectedMode : modes.find(selectedMode => remembered.pending[selectedMode]))
    || remembered.selectedMode;
  let state = null;
  let busy = false;
  let loading = false;
  let stale = false;
  let stateSequence = 0;
  let stateController = null;
  let datesInitialized = false;
  const receipts = {};

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const typeName = value => typeNames[value] || value || '未分配';
  const bedName = value => bedNames[value] || value || '未分配';
  const shortDate = value => typeof value === 'string' ? value.slice(5).replace('-', '/') : '—';
  const datePlus = (date, days) => {
    const value = new Date(`${date}T00:00:00Z`);
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  };
  const yuan = cents => (Number(cents || 0) / 100).toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 2});
  const auditPassed = audit => Boolean(audit && audit.healthy === true && audit.verification_complete === true);
  const auditLabel = audit => !audit ? '尚未核对' : audit.verification_complete !== true ? '核对未完成' : audit.healthy === true ? '核对通过' : '发现异常';
  const persist = () => { try { sessionStorage.setItem(storageKey, JSON.stringify(remembered)); storageAvailable = true; } catch (_) { storageAvailable = false; } };
  const lastPayload = () => remembered.last[mode];
  const pending = () => remembered.pending[mode];
  const pendingReset = () => modes.map(selectedMode => remembered.pending[selectedMode]).find(operation => operation?.kind === 'reset');
  const blockingOperation = () => pendingReset() || pending();
  const validLast = () => Boolean(state && lastPayload() && lastPayload().generation === state.generation && lastPayload().mode === mode);
  const clearBanner = () => { $('message-banner').hidden = true; $('message-banner').textContent = ''; };
  const notify = (text, kind = '') => {
    $('message-banner').textContent = text;
    $('message-banner').className = `message-banner ${kind}`;
    $('message-banner').hidden = false;
  };
  function updateControls() {
    document.body.classList.toggle('is-loading', loading);
    $('metrics').setAttribute('aria-busy', String(loading));
    const unavailable = busy || loading || !state || stale;
    $('request-fields').disabled = unavailable || Boolean(blockingOperation());
    document.querySelectorAll('input[name="mode"]').forEach(input => { input.disabled = busy; input.checked = input.value === mode; });
    $('refresh-button').disabled = busy || loading;
    $('reset-button').disabled = unavailable || Object.keys(remembered.pending).length > 0;
    $('replay-button').disabled = busy || loading || !validLast() || Boolean(blockingOperation() && blockingOperation().kind !== 'reserve');
    $('pending-retry').disabled = busy || loading;
    $('reserve-button').textContent = busy ? '正在处理事务…' : '新建模拟预订 →';
    $('last-request-id').textContent = lastPayload()
      ? `请求键：${lastPayload().request.request_id}${state && !validLast() ? '（属于旧轮次，当前不可重放）' : ''}`
      : '尚无请求键';
    const operation = blockingOperation();
    $('pending-box').hidden = !operation;
    if (operation) {
      const action = operation.kind === 'reserve' ? '预订' : operation.kind === 'cancel' ? '取消' : '新开一轮';
      const key = operation.payload.request_id || (operation.payload.request && operation.payload.request.request_id);
      $('pending-title').textContent = operation.uncertain !== false ? '上次操作的结果尚未确认' : '数据库暂时繁忙，请重试原操作';
      $('pending-description').textContent = `${modeNames[operation.payload.mode]}：请${operation.uncertain !== false ? '核实' : '重试'}上次${action}，不要另建重复请求。${key ? `原请求键：${key}。` : ''}页面暂时停用新建操作。${storageAvailable ? '' : '浏览器未能保存重试信息，离开页面前请复制下面的原请求。'}`;
      $('pending-payload').textContent = JSON.stringify(operation.payload, null, 2);
      $('pending-retry').textContent = operation.kind === 'reset' ? '使用原轮次核实 / 重试' : `使用原键重试${action}`;
    }
    renderArchived();
    document.querySelectorAll('.cancel-booking').forEach(button => { button.disabled = unavailable || Boolean(blockingOperation()); });
  }

  function renderArchived() {
    const archived = remembered.archived.filter(operation => operation.payload.mode === mode);
    $('archived-box').hidden = archived.length === 0;
    $('archived-count').textContent = archived.length;
    $('archived-list').replaceChildren(...archived.map(operation => {
      const item = element('div', 'archived-record');
      const key = operation.payload.request_id || operation.payload.request?.request_id || operation.payload.generation;
      item.append(element('strong', '', `${operation.kind === 'cancel' ? '取消' : '预订'} · ${key}`));
      const detail = element('details'); detail.append(element('summary', '', '原 payload（仅查看，不会重新发送）'), element('pre', '', JSON.stringify(operation.payload, null, 2)));
      item.append(detail);
      if (operation.recovery) {
        const result = operation.recovery;
        const descriptions = {
          RECORDED: `已找到原预订决定，当前状态：${statusLabels[result.status] || result.status}。`,
          CANCELLED: '已核实原订单处于已取消状态。',
          NOT_CANCELLED: `原订单当前为${statusLabels[result.status] || result.status}，没有已提交的取消记录。`,
          NOT_RECORDED: '本次读取快照中未找到原请求记录；这不证明请求从未执行，也不会自动补单。'
        };
        item.append(element('p', 'archive-result', `只读核对：${descriptions[result.outcome] || '结果未确认'} 查询时间：${result.observed_at || '未知'}。未发送预订或取消。`));
      }
      if (operation.recoveryError) item.append(element('p', 'archive-error', operation.recoveryError));
      const button = element('button', 'button button-outline archive-lookup', operation.recovery ? '再次只读核实' : '只读核实原操作');
      button.type = 'button'; button.disabled = busy || loading;
      button.addEventListener('click', () => recoverArchived(operation));
      item.append(button); return item;
    }));
  }

  // A different generation makes old payloads inapplicable, but does not prove
  // the result of a reservation/cancellation. Preserve those unresolved bodies.
  function reconcileGeneration(generation) {
    let advancedReset = false;
    let archivedUnknown = false;
    modes.forEach(selectedMode => {
      const operation = remembered.pending[selectedMode];
      if (!operation || operation.payload.generation === generation) return;
      if (operation.kind === 'reset') advancedReset = true;
      else if (operation.uncertain !== false) {
        remembered.archived.push(operation);
        archivedUnknown = true;
      }
      delete remembered.pending[selectedMode];
    });
    Object.keys(receipts).forEach(selectedMode => {
      if (receipts[selectedMode].generation !== generation) delete receipts[selectedMode];
    });
    persist();
    return {advancedReset, archivedUnknown};
  }

  async function readState({quiet = false} = {}) {
    const sequence = ++stateSequence;
    const selectedMode = mode;
    if (stateController) stateController.abort();
    stateController = new AbortController();
    const controller = stateController;
    const timeout = setTimeout(() => controller.abort(), 15000);
    loading = true;
    updateControls();
    try {
      const response = await fetch(`/api/state?mode=${encodeURIComponent(selectedMode)}`, {signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error && data.error.message || `状态读取失败（HTTP ${response.status}）`);
      if (data.mode !== selectedMode || !data.generation || !data.catalog || !data.inventory || !Array.isArray(data.bookings)) throw new Error('状态响应不完整，尚未更新显示。');
      if (sequence !== stateSequence || selectedMode !== mode) return false;
      state = data;
      stale = false;
      const transition = reconcileGeneration(data.generation);
      renderState();
      if (transition.archivedUnknown) notify('演示轮次已改变。旧操作结果仍未确认，完整原请求已保存在“旧轮次待核实记录”，不会自动重新发送。', 'warning');
      else if (transition.advancedReset) notify('检测到新一轮已生效，三种策略使用新库；旧轮次保留归档。');
      else if (!quiet && !blockingOperation()) clearBanner();
      return true;
    } catch (error) {
      if (sequence !== stateSequence || selectedMode !== mode) return false;
      stale = true;
      notify(`${error.name === 'AbortError' ? '状态读取超时。' : error.message} 当前显示可能不是最新状态，请刷新后再新建。`, 'error');
      return false;
    } finally {
      clearTimeout(timeout);
      if (sequence === stateSequence) { loading = false; updateControls(); }
    }
  }

  function renderState() {
    const {catalog, inventory, bookings, audit} = state;
    const available = inventory.daily.reduce((sum, day) => sum + Object.values(day.available_by_type).reduce((n, amount) => n + amount, 0), 0);
    $('metric-bookings').textContent = bookings.filter(booking => booking.status === 'BOOKED').length;
    $('metric-occupied').textContent = inventory.occupied_room_nights;
    $('metric-available').textContent = available;
    $('metric-range').textContent = `${inventory.daily.length} 晚 · ${catalog.rooms.length} 间实体房`;
    $('metric-audit').textContent = auditLabel(audit);
    $('metric-audit-note').textContent = auditPassed(audit) ? `${audit.active_room_nights} 活动房晚 · ${audit.events} 条事件` : `${audit.issue_count ?? '—'} 项异常 · ${audit.verification_complete ? '已完成检查' : '检查不完整'}`;
    $('metric-audit').parentElement.classList.toggle('problem', !auditPassed(audit));
    $('calendar-range').textContent = `${catalog.horizon_start} → ${shortDate(catalog.horizon_end)}`;
    $('environment-note').textContent = `SQLite ${audit.sqlite_version || ''} · 本地持久化 · 不连接实际酒店库存`;
    const currentType = $('requested-type').value;
    $('requested-type').replaceChildren(...catalog.room_types.map(type => {
      const option = element('option', '', `${typeName(type.code)} · 基准 ¥${yuan(type.base_rate_cents)}/晚`);
      option.value = type.code;
      return option;
    }));
    if (catalog.room_types.some(type => type.code === currentType)) $('requested-type').value = currentType;
    $('check-in').min = catalog.horizon_start;
    $('check-in').max = datePlus(catalog.horizon_end, -1);
    $('check-out').min = datePlus(catalog.horizon_start, 1);
    $('check-out').max = catalog.horizon_end;
    if (!datesInitialized) {
      $('check-in').value = catalog.horizon_start;
      $('check-out').value = datePlus(catalog.horizon_start, 1);
      datesInitialized = true;
    }
    $('children-help').textContent = `0–17 岁，以逗号分隔；本场景 ${catalog.cot_age_limit} 岁以下儿童需要婴儿床。`;
    renderCatalog();
    renderInventory();
    renderBookings();
    updateFormHints();
    renderReceipt(receipts[mode]);
  }

  function renderCatalog() {
    $('catalog-cards').replaceChildren(...state.catalog.room_types.map(type => {
      const card = element('article', 'catalog-card');
      card.append(element('h3', '', typeName(type.code)), element('p', '', `最多 ${type.max_occupants} 人 / ${type.max_adults} 成人`), element('p', '', `婴儿床 ≤ ${type.max_cots ?? '—'} · 加床 ≤ ${type.max_extra_beds ?? '—'}`), element('p', 'catalog-rate', `基准 ¥${yuan(type.base_rate_cents)}/晚`));
      return card;
    }));
  }

  function renderInventory() {
    const days = state.inventory.daily;
    const header = element('tr');
    const corner = element('th', 'room-column', '实体房 / 床型'); corner.scope = 'col'; header.append(corner);
    days.forEach(day => {
      const cell = element('th', '', shortDate(day.date)); cell.scope = 'col';
      const weekday = ['日', '一', '二', '三', '四', '五', '六'][new Date(`${day.date}T00:00:00Z`).getUTCDay()];
      cell.append(element('span', 'date-weekday', `周${weekday}`)); header.append(cell);
    });
    $('inventory-head').replaceChildren(header);
    const occupancy = new Map(state.inventory.occupancy.map(item => [`${item.room_id}|${item.date}`, item.request_id]));
    $('inventory-body').replaceChildren(...state.catalog.rooms.map(room => {
      const row = element('tr');
      const label = element('th', 'room-column', room.room_id); label.scope = 'row';
      label.append(element('span', 'room-description', `${typeName(room.room_type)} · ${bedName(room.bed_type)}`)); row.append(label);
      days.forEach(day => {
        const requestID = occupancy.get(`${room.room_id}|${day.date}`);
        const blocked = (room.blocked_dates || []).includes(day.date);
        const status = requestID ? 'occupied' : blocked ? 'blocked' : 'free';
        const title = requestID ? `占用 · ${requestID}` : blocked ? '维修 / 停用' : '空闲';
        const cell = element('td', `inventory-cell ${status}`, requestID ? '住' : blocked ? '修' : '·');
        cell.title = `${room.room_id} · ${day.date} · ${title}`;
        cell.setAttribute('aria-label', cell.title);
        cell.dataset.date = day.date;
        row.append(cell);
      });
      return row;
    }));
    const resourceRows = [['cots', '婴儿床余量'], ['extra_beds', '加床余量']].map(([key, label]) => {
      const row = element('tr');
      const heading = element('th', 'room-column', label); heading.scope = 'row'; row.append(heading);
      days.forEach(day => {
        const remaining = day[`${key}_capacity`] - day[`${key}_used`];
        const cell = element('td', remaining === 0 ? 'resource-empty' : '', `${remaining}/${day[`${key}_capacity`]}`);
        cell.title = `${day.date} · ${label} ${remaining} / 总量 ${day[`${key}_capacity`]}`;
        row.append(cell);
      });
      return row;
    });
    $('inventory-foot').replaceChildren(...resourceRows);
    $('inventory-placeholder').hidden = true;
  }

  function updateFormHints() {
    if (!state) return;
    const start = $('check-in').value;
    const end = $('check-out').value;
    const nights = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000;
    $('stay-summary').textContent = nights > 0 ? `${nights} 晚 · 入住日计入，离店日不计入房晚。` : '离店日期必须晚于入住日期。';
    const type = state.catalog.room_types.find(item => item.code === $('requested-type').value);
    $('room-type-help').textContent = type ? `该房型最多 ${type.max_occupants} 人、${type.max_adults} 位成人；仍需满足实际床位与资源。` : '';
    const selected = state.inventory.daily.filter(day => day.date >= start && day.date < end);
    $('resource-summary').textContent = selected.length ? `所选日期每晚最低余量：婴儿床 ${Math.min(...selected.map(day => day.cots_capacity - day.cots_used))} 张 · 加床 ${Math.min(...selected.map(day => day.extra_beds_capacity - day.extra_beds_used))} 张` : '辅助资源显示：剩余量 / 总量。';
    document.querySelectorAll('.inventory-cell').forEach(cell => { cell.classList.toggle('in-stay', cell.dataset.date >= start && cell.dataset.date < end); });
  }

  function nightDescription(decision) {
    const nights = Array.isArray(decision.nights) ? decision.nights : [];
    return nights.length ? `${shortDate(nights[0])} → ${shortDate(datePlus(nights[nights.length - 1], 1))} · ${nights.length} 晚` : '没有记录入住日期';
  }
  function rejectionText(decision) {
    return (decision.rejection_reasons || []).map(reason => reasonLabels[reason] || reason).join('；') || '当前条件下没有可分配的房间。';
  }
  const statusClass = status => status === 'BOOKED' ? 'success' : status === 'REJECTED' || status === 'ROLLED_BACK' ? 'warning' : 'neutral';

  function renderBookings() {
    const list = $('bookings-list');
    $('booking-count').textContent = state.bookings.length;
    if (!state.bookings.length) { list.replaceChildren(element('p', 'empty-state', '这轮实验尚无订单。提交一个入住需求，开始观察。')); return; }
    list.replaceChildren(...state.bookings.map(booking => {
      const decision = booking.original_decision || {};
      const row = element('article', 'booking-row');
      const identity = element('div');
      identity.append(element('p', 'booking-id', booking.request_id), element('p', 'booking-meta', nightDescription(decision)));
      const detail = element('p', 'booking-detail');
      if (decision.accepted) {
        detail.append(element('strong', '', `${decision.room_id} · ${typeName(decision.assigned_type)} · ${bedName(decision.bed_type)}`), element('br'), document.createTextNode(`每晚婴儿床 ${decision.cots} / 加床 ${decision.extra_beds}${decision.upgraded ? ` · 升级成本 ¥${yuan(decision.upgrade_cost_cents)}` : ' · 未升级'}`));
      } else detail.textContent = rejectionText(decision);
      row.append(identity, detail, element('span', `status-badge ${statusClass(booking.status)}`, statusLabels[booking.status] || booking.status));
      if (booking.status === 'BOOKED') {
        const cancel = element('button', 'button button-outline cancel-booking', '取消预订');
        cancel.type = 'button'; cancel.setAttribute('aria-label', `取消预订 ${booking.request_id}`);
        cancel.addEventListener('click', () => perform('cancel', {mode, generation: state.generation, request_id: booking.request_id}));
        row.append(cancel);
      }
      return row;
    }));
  }

  function renderReceipt(receipt) {
    $('receipt-empty').hidden = Boolean(receipt);
    $('receipt-content').hidden = !receipt;
    $('receipt-status').textContent = receipt ? statusLabels[receipt.status] || receipt.status : '等待请求';
    $('receipt-status').className = `status-badge ${receipt ? statusClass(receipt.status) : 'neutral'}`;
    if (blockingOperation()) {
      $('receipt-status').textContent = busy ? '事务处理中' : blockingOperation().uncertain !== false ? '结果未确认' : '等待原请求重试';
      $('receipt-status').className = 'status-badge warning';
    }
    if (!receipt) return;
    const decision = receipt.record && receipt.record.original_decision || {};
    const titles = {
      BOOKED: receipt.replayed ? '返回已保存的订单，没有再次扣库存。' : `已分配 ${decision.room_id}，整段入住保持同一间房。`,
      REJECTED: receipt.replayed ? '返回原拒单决定，不重新尝试分配。' : '未找到符合全部条件的房间，未扣库存。',
      CANCELLED: receipt.replayed ? '该请求仍为已取消，不会恢复预订。' : '预订已取消，按原记录释放房间与辅助资源。',
      ROLLED_BACK: '教学故障已触发，本次写入整笔回滚。'
    };
    $('receipt-title').textContent = titles[receipt.status] || '已收到后台响应。';
    $('receipt-description').textContent = receipt.status === 'ROLLED_BACK'
      ? '这是预期的故障演示，不是实际事故。可以使用原请求键正常重试，不需要新建请求。'
      : receipt.status === 'REJECTED' ? rejectionText(decision)
      : `${typeName(decision.assigned_type)} · ${bedName(decision.bed_type)} · ${nightDescription(decision)}${decision.upgraded ? ` · 整段模拟升级成本 ¥${yuan(decision.upgrade_cost_cents)}` : ''}`;
    const delta = Number(receipt.inventory_delta_nights);
    const facts = [[`${delta > 0 ? '+' : ''}${delta} 房晚`, '本次净库存变化'], [auditLabel(receipt.audit), '独立只读对账'], [receipt.replayed ? '是' : '否', '返回已有记录']];
    $('receipt-facts').replaceChildren(...facts.map(([value, label]) => {
      const fact = element('div', 'receipt-fact'); fact.append(element('span', '', label), element('strong', '', value)); return fact;
    }));
    $('receipt-trace').replaceChildren(...(receipt.trace || []).map((step, index) => {
      const item = element('li'); const line = element('span', '', step.label); line.append(element('span', 'trace-code', step.code));
      item.append(element('span', 'trace-index', index + 1), line); return item;
    }));
    $('receipt-key').textContent = `请求键：${receipt.request_id} · ${modeNames[receipt.mode] || receipt.mode}`;
  }

  class ApiError extends Error {
    constructor(message, status = 0, code = '') { super(message); this.status = status; this.code = code; }
  }
  async function post(kind, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(`/api/${kind}`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-Demo-CSRF': document.querySelector('meta[name="demo-csrf"]').content},
        body: JSON.stringify(payload)
      });
      let body;
      try { body = await response.json(); } catch (_) { throw new ApiError('服务响应无法解析，操作结果尚未确认。'); }
      if (!response.ok) throw new ApiError(body.error && body.error.message || `请求失败（HTTP ${response.status}）`, response.status, body.error && body.error.code);
      if (kind === 'lookup') {
        const original = payload.payload;
        const key = original.request_id || original.request?.request_id;
        const expectedOutcome = body.status === 'NOT_FOUND' ? 'NOT_RECORDED'
          : payload.kind === 'reserve' ? 'RECORDED' : body.status === 'CANCELLED' ? 'CANCELLED' : 'NOT_CANCELLED';
        if (body.mode !== original.mode || body.generation !== original.generation || body.request_id !== key
            || body.operation !== payload.kind || body.read_only !== true || body.snapshot_only !== true
            || !['BOOKED', 'CANCELLED', 'REJECTED', 'NOT_FOUND'].includes(body.status)
            || body.outcome !== expectedOutcome || !auditPassed(body.audit)
            || typeof body.observed_at !== 'string'
            || (body.status === 'NOT_FOUND' ? body.record !== null
              : !body.record || body.record.request_id !== key || body.record.status !== body.status)) {
          throw new ApiError('查询响应不完整或与原请求不匹配，仍不能确认原操作。');
        }
        return body;
      }
      if (body.mode !== payload.mode || !body.generation || (kind !== 'reset' && (body.generation !== payload.generation
          || !Object.hasOwn(statusLabels, body.status) || !body.audit
          || body.request_id !== (payload.request_id || payload.request?.request_id)))) throw new ApiError('服务响应不完整或请求标识不匹配，操作结果尚未确认。');
      return body;
    } finally { clearTimeout(timeout); }
  }

  async function recoverArchived(operation) {
    if (busy || loading || !remembered.archived.includes(operation)) return;
    busy = true;
    delete operation.recoveryError;
    updateControls();
    try {
      const result = await post('lookup', {kind: operation.kind, payload: operation.payload});
      operation.recovery = result;
      persist();
      notify('已只读核实原轮次记录。没有重试预订、执行取消或改变当前轮次库存。');
    } catch (error) {
      operation.recoveryError = `本次查询未完成：${error.message || '网络连接中断。'} 原请求仍保留，请再次只读核实。`;
      persist();
      notify(operation.recoveryError, 'warning');
    } finally {
      busy = false;
      updateControls();
    }
  }

  async function perform(kind, payload) {
    if (busy || loading) return;
    const blockedBy = blockingOperation();
    if (blockedBy && (blockedBy.kind !== kind || blockedBy.payload.mode !== payload.mode
        || blockedBy.payload.generation !== payload.generation
        || JSON.stringify(blockedBy.payload.request || blockedBy.payload.request_id) !== JSON.stringify(payload.request || payload.request_id))) return;
    busy = true;
    clearBanner();
    const previousUnconfirmed = remembered.pending[payload.mode]?.uncertain === true;
    remembered.pending[payload.mode] = {kind, payload, uncertain: true};
    if (kind === 'reserve') remembered.last[payload.mode] = payload;
    persist(); updateControls();
    delete receipts[payload.mode];
    renderReceipt(null);
    try {
      const result = await post(kind, payload);
      delete remembered.pending[payload.mode];
      if (kind === 'reset') {
        reconcileGeneration(result.generation);
        remembered.last = {};
        Object.keys(receipts).forEach(key => { delete receipts[key]; });
        if (mode === result.mode) { state = result; stale = false; renderState(); }
        else await readState({quiet: true});
        notify('已为三种策略新开一轮。旧数据库保留归档，本轮从空库存占用开始。');
      } else {
        receipts[payload.mode] = result;
        renderReceipt(result);
        notify(result.status === 'ROLLED_BACK' ? '故障注入后已完整回滚。请点击“原请求键重放 / 正常重试”，验证同一请求可以正常完成。' : `${statusLabels[result.status] || result.status}。${result.replayed ? '本次使用原请求键，返回已有记录。' : '事务回执已更新。'}`, result.status === 'REJECTED' || result.status === 'ROLLED_BACK' ? 'warning' : '');
        await readState({quiet: true});
      }
      persist();
    } catch (error) {
      const unknown = !(error instanceof ApiError) || error.status === 0 || (error.status >= 500 && error.status !== 503);
      if (unknown) remembered.pending[payload.mode] = {kind, payload, uncertain: true};
      else if (error.status === 503) remembered.pending[payload.mode] = {kind, payload, uncertain: previousUnconfirmed};
      else if (!unknown && !previousUnconfirmed) delete remembered.pending[payload.mode];
      if (unknown || previousUnconfirmed) {
        notify(`${error.message || '网络连接中断。'} 操作结果未确认，已保留原请求键。请使用原键核实 / 重试，不要另建重复订单。`, 'warning');
        $('receipt-status').textContent = '结果未确认';
        $('receipt-status').className = 'status-badge warning';
      } else if (error.status === 503) {
        notify(`${error.message} 已保留完整原操作，请使用原键重试。`, 'warning');
        $('receipt-status').textContent = '等待原请求重试';
        $('receipt-status').className = 'status-badge warning';
      } else {
        notify(`${error.message}${error.status === 503 ? ' 请使用原请求键重放，避免改用新键。' : ''}`, 'error');
      }
      persist();
      if (error.code === 'STALE_ROUND') await readState({quiet: true});
    } finally { busy = false; renderReceipt(receipts[mode]); updateControls(); }
  }

  function buildPayload() {
    const ageText = $('child-ages').value.trim();
    const ageParts = ageText ? ageText.split(/[,，]/).map(value => value.trim()) : [];
    if (ageParts.length > 10 || ageParts.some(age => !/^\d{1,2}$/.test(age) || Number(age) > 17)) throw new Error('儿童年龄应为 0–17 的整数，以逗号分隔，最多 10 名儿童。');
    const beds = Array.from(document.querySelectorAll('input[name="bed_type"]:checked'), input => input.value);
    if (!beds.length) throw new Error('请至少选择一种可接受的床型。');
    const checkIn = $('check-in').value;
    const checkOut = $('check-out').value;
    if (checkOut <= checkIn) throw new Error('离店日期必须晚于入住日期。');
    const budget = $('upgrade-budget').value;
    if (!/^\d+(\.\d{1,2})?$/.test(budget)) throw new Error('升级成本上限应为非负金额，最多两位小数。');
    const [whole, fraction = ''] = budget.split('.');
    const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
    if (!Number.isSafeInteger(cents)) throw new Error('升级成本上限过大，请填写较小金额。');
    if (!globalThis.crypto || typeof crypto.randomUUID !== 'function') throw new Error('此页面需要在本机 localhost 或安全连接下打开，才能生成可靠的请求键。');
    return {mode, generation: state.generation, request: {
      request_id: `DEMO-${crypto.randomUUID()}`, check_in: checkIn, check_out: checkOut,
      requested_type: $('requested-type').value, accepted_bed_types: beds,
      adults: Number($('adults').value), child_ages: ageParts.map(Number),
      allow_upgrade: $('allow-upgrade').checked, allow_extra_bed: $('allow-extra-bed').checked,
      max_upgrade_cost_cents: cents
    }, simulate_failure: $('simulate-failure').checked};
  }

  $('reservation-form').addEventListener('submit', event => {
    event.preventDefault();
    if (busy || loading || !state || stale || blockingOperation()) return;
    try { const payload = buildPayload(); perform('reserve', payload); }
    catch (error) { notify(error.message, 'error'); }
  });
  $('replay-button').addEventListener('click', () => {
    if (!validLast()) return;
    perform('reserve', {...lastPayload(), simulate_failure: false});
  });
  $('pending-retry').addEventListener('click', async () => {
    const operation = blockingOperation();
    if (!operation) return;
    // Retrying a reset with its original generation cannot create a second round:
    // the server returns STALE_ROUND if that generation has already advanced.
    perform(operation.kind, operation.kind === 'reserve' ? {...operation.payload, simulate_failure: false} : operation.payload);
  });
  $('refresh-button').addEventListener('click', () => readState());
  document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => {
    if (busy || !input.checked || !modes.includes(input.value)) return;
    mode = input.value; remembered.selectedMode = mode; persist(); state = null; stale = false; clearBanner();
    $('inventory-head').replaceChildren(); $('inventory-body').replaceChildren(); $('inventory-foot').replaceChildren();
    $('inventory-placeholder').textContent = '正在读取该策略的独立库存…'; $('inventory-placeholder').hidden = false;
    $('bookings-list').replaceChildren(element('p', 'empty-state', '正在读取该策略的订单…'));
    ['metric-bookings', 'metric-occupied', 'metric-available'].forEach(id => { $(id).textContent = '—'; });
    $('metric-audit').textContent = '读取中'; renderReceipt(receipts[mode]); readState();
  }));
  ['check-in', 'check-out', 'requested-type'].forEach(id => $(id).addEventListener('change', updateFormHints));
  $('baby-example').addEventListener('click', () => {
    if (!state) return;
    $('check-in').value = state.catalog.horizon_start;
    $('check-out').value = datePlus(state.catalog.horizon_start, 3);
    $('requested-type').value = state.catalog.room_types.some(type => type.code === 'standard') ? 'standard' : state.catalog.room_types[0].code;
    $('adults').value = '2'; $('child-ages').value = '1';
    $('allow-upgrade').checked = true; $('allow-extra-bed').checked = true;
    $('upgrade-budget').value = '600'; $('simulate-failure').checked = false;
    document.querySelectorAll('input[name="bed_type"]').forEach(input => { input.checked = true; });
    clearBanner(); updateFormHints();
  });
  $('reset-button').addEventListener('click', () => {
    if (!state || busy || loading || Object.keys(remembered.pending).length) return;
    $('reset-mode').textContent = `当前查看：${modeNames[mode]}。此操作对三种策略同时生效。`;
    $('reset-dialog').returnValue = '';
    $('reset-dialog').showModal();
  });
  $('reset-dialog').addEventListener('close', () => {
    if ($('reset-dialog').returnValue === 'confirm' && state && !busy && !loading) perform('reset', {mode, generation: state.generation});
  });
  updateControls();
  readState();
})();
