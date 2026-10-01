/* Google Контакты → выбор галочками → сегмент кампании.
   Только то, что сервер отдал в снимке книги, можно отправить в импорт; рассылку окно не запускает.
   openGoogleContacts({campaignId}) — добрать в конкретную кампанию (окно «Аудитория»);
   openGoogleContacts({tag, onApplied}) — сегмент для мастера, кампании ещё нет;
   openGoogleContacts({}) — из «Контактов»: выбрать черновик или создать новый. */
(function () {
  const SHOW = 400;                 // строк в DOM за раз: книга бывает на 10 000 записей
  const MAX_PICK = 500;             // столько же принимает сервер за один импорт
  let BOOK = null;                  // снимок книги переживает закрытие окна, пока жив токен
  const fresh = () => BOOK && Date.now() - BOOK.at < 25 * 60 * 1000;

  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const compact = text => String(text || '').normalize('NFKC').toLowerCase().replace(/\s+/g, '');
  // Та же грамматика, что у сервера: «эск2023, ск2025/2026» → эск2023 | ск2025 | ск2026.
  const terms = text => [...new Set(String(text || '').split(/[,;\n]+/).map(compact).filter(Boolean).flatMap(part => {
    const m = part.match(/^(.+?)(20\d{2})((?:\/20\d{2})+)$/);
    return m ? [m[2], ...m[3].slice(1).split('/')].map(y => m[1] + y) : [part];
  }))];
  const request = async (url, options) => {
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (e) { /* пустой ответ */ }
    if (!response.ok || data.error) throw new Error(data.error || 'Не удалось выполнить запрос.');
    return data;
  };
  const post = (url, body) => request(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});

  window.openGoogleContacts = async function (opts = {}) {
    const modal = document.createElement('div');
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';
    modal.innerHTML = `<div style="background:var(--bg);border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,0.3);width:min(1400px,96vw);height:90vh;display:flex;flex-direction:column">
      <div style="padding:12px 18px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <b style="font-size:16px">Google Контакты</b><span class="hint" data-state></span><span class="grow" style="flex:1"></span>
        <button data-reload title="перечитать адресную книгу из Google">↻ Обновить</button>
        <button data-csv-button title="contacts.google.com → Экспортировать → Google CSV">Загрузить Google CSV</button>
        <input data-csv type="file" accept=".csv,text/csv" hidden>
        <button data-disconnect hidden>Отключить</button><button data-close>✕</button></div>
      <div data-connect hidden style="padding:14px 18px;border-bottom:1px solid var(--border);background:var(--panel2)">
        <b>Подключить Google Контакты — один раз</b>
        <ol style="margin:8px 0 8px 18px;padding:0;line-height:1.6">
          <li><button class="primary" data-auth>Открыть Google</button> — войдите и разрешите «Просмотр контактов».</li>
          <li>Google откроет страницу <span class="mono">localhost</span> с ошибкой «Не удаётся получить доступ к сайту» — так и задумано. Скопируйте адрес этой вкладки целиком.</li>
          <li><input data-return placeholder="http://localhost:8765/?state=…&code=…" style="width:min(520px,70%)"> <button class="primary" data-finish>Готово</button></li>
        </ol>
        <div class="hint">Google пишет <b>redirect_uri_mismatch</b> — в Google Cloud → APIs &amp; Services → Credentials → OAuth-клиент добавьте <span class="mono" data-redirect>http://localhost:8765/</span> в Authorized redirect URIs.
          Пишет, что People API выключен — включите <b>People API</b> в том же проекте. Без подключения можно загрузить экспорт Google CSV (кнопка вверху).</div>
      </div>
      <div style="flex:1;min-height:0;display:flex">
        <div data-labels style="width:250px;flex:none;overflow-y:auto;border-right:1px solid var(--border);padding:8px 0"></div>
        <div style="flex:1;min-width:0;display:flex;flex-direction:column">
          <div class="ttoolbar" style="padding:8px 14px;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--border)">
            <input data-q placeholder="поиск: имя, маркер, телефон, почта. Через запятую — любое из (эск2023, ск2025/2026)" style="flex:1;min-width:240px">
            <label class="hint" style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-with-phone checked> только с телефоном</label>
            <label class="hint" style="display:inline-flex;gap:4px;align-items:center"><input type="checkbox" data-only-picked> только выбранные</label></div>
          <div style="padding:6px 14px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:center;flex-wrap:wrap">
            <label style="display:inline-flex;gap:6px;align-items:center;font-weight:600"><input type="checkbox" data-all> выбрать всех найденных (<span data-found>0</span>)</label>
            <b data-count>выбрано: 0</b><button data-none>Снять выбор</button><span class="hint" data-more></span></div>
          <div data-list style="flex:1;overflow-y:auto"></div>
        </div>
      </div>
      <div style="padding:12px 18px;border-top:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span data-dest style="display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap"></span>
        <button class="primary" data-import>Добавить выбранных</button>
        <span data-message class="hint" role="status" style="flex:1;min-width:200px"></span></div>
    </div>`;
    document.body.appendChild(modal);
    const q = s => modal.querySelector(s);
    const close = () => { modal.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    q('[data-close]').onclick = close;
    modal.onclick = e => { if (e.target === modal) close(); };
    const message = (text, bad) => { const m = q('[data-message]'); m.textContent = text; m.style.color = bad ? '#c0392b' : ''; };

    let label = null;                    // null — все контакты, '' — без ярлыка
    const picked = new Map();            // id → телефон
    const names = new Map();             // id → имя для обращения, правленное руками
    let campaigns = [];
    let busy = false;
    const busySet = value => { busy = value; modal.querySelectorAll('button').forEach(b => { if (!b.hasAttribute('data-close')) b.disabled = value; }); };

    // ---- куда добавляем ----
    const dest = q('[data-dest]');
    const target = () => opts.campaignId ? campaigns.find(c => c.id === +opts.campaignId)
      : opts.tag !== undefined ? null : campaigns.find(c => String(c.id) === (q('[data-camp]') || {}).value);
    const drawDest = () => {
      if (opts.campaignId) {
        const c = target();
        dest.innerHTML = `<span>В кампанию <b>${escape(c ? c.name : '#' + opts.campaignId)}</b>${c && c.status === 'running' ? ' <span style="color:#c0392b">· идёт рассылка — добавленным начнут писать</span>' : ''}</span>`;
      } else if (opts.tag !== undefined) {
        dest.innerHTML = `<label style="font-weight:600">Тег сегмента:</label><input data-tag value="${escape(opts.tag || '')}" placeholder="Клиенты ЭСК 2025" maxlength="100" style="min-width:220px">`;
      } else {
        const list = campaigns.filter(c => !c.archived && (c.audience_tag || '').trim());
        dest.innerHTML = `<label>Добавить в <select data-camp><option value="">новый черновик</option>${list.map(c =>
          `<option value="${c.id}">${escape(c.name)} · ${c.status === 'running' ? 'идёт рассылка' : c.status === 'paused' ? 'пауза' : 'черновик'}</option>`).join('')}</select></label>
          <input data-camp-name value="Google — ${new Date().toLocaleDateString('ru-RU')}" maxlength="150" aria-label="Название нового черновика">`;
        q('[data-camp]').onchange = () => { q('[data-camp-name]').hidden = !!q('[data-camp]').value; draw(); };
      }
    };

    // ---- состояние строки в CRM ----
    const crm = (row, phone) => {
      const matches = (row.existing || {})[phone] || [];
      const t = target();
      if (!phone) return {block: 'нет телефона'};
      if (matches.length > 1) return {block: 'несколько дублей в CRM'};
      const c = matches[0];
      if (!c) return {text: 'новый'};
      if (c.deleted_at) return {block: 'в корзине CRM'};
      if (c.is_test) return {block: 'тестовый'};
      if (c.status !== 'new') return {block: 'уже в работе'};
      if (c.outreach_campaign_id && (!t || c.outreach_campaign_id !== t.id)) return {block: 'закреплён за другой кампанией'};
      return {text: 'есть в CRM'};
    };
    const phoneOf = row => picked.get(row.id) || row.phones[0] || '';
    const nameOf = (row, active) => {
      if (names.has(row.id)) return names.get(row.id);
      const given = (row.person_name || '').trim();
      // Маркер вида «эск2025», записанный в «имя» Google, — это ярлык, а не обращение.
      // Обычное имя, по которому искали («алексей»), оставляем.
      return active.some(t => /\d/.test(t) && compact(given).includes(t)) || /\d/.test(given) ? '' : given;
    };

    // ---- фильтр ----
    const filtered = () => {
      if (!BOOK) return [];
      const active = terms(q('[data-q]').value);
      const withPhone = q('[data-with-phone]').checked, onlyPicked = q('[data-only-picked]').checked;
      return BOOK.data.items.filter(row => {
        if (onlyPicked && !picked.has(row.id)) return false;
        if (withPhone && !row.phones.length) return false;
        if (label === '' && row.groups.length) return false;
        if (label && !row.groups.includes(label)) return false;
        if (!active.length) return true;
        if (!row._hay) row._hay = compact([row.name, ...row.aliases, row.email, ...row.phones].join(' '));
        return active.some(t => row._hay.includes(t.replace(/^\+/, '')));
      });
    };

    const drawLabels = () => {
      const box = q('[data-labels]');
      if (!BOOK) { box.innerHTML = ''; return; }
      const items = BOOK.data.items;
      const none = items.filter(r => !r.groups.length).length;
      const item = (key, title, n) => `<div data-label="${escape(key === null ? '\u0000' : key)}" style="padding:7px 16px;cursor:pointer;display:flex;gap:8px;justify-content:space-between;${label === key ? 'background:var(--accent-soft);font-weight:700;border-radius:0 16px 16px 0' : ''}">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escape(title)}">${escape(title)}</span><span class="hint">${n}</span></div>`;
      box.innerHTML = item(null, 'Все контакты', items.length)
        + `<div class="hint" style="padding:10px 16px 4px;font-weight:600">Ярлыки</div>`
        + (BOOK.data.groups.length ? BOOK.data.groups.map(g => item(g.name, g.name, g.count)).join('')
          : `<div class="hint" style="padding:4px 16px">ярлыков нет${BOOK.source === 'csv' ? ' в файле' : ''}</div>`)
        + (none && BOOK.data.groups.length ? item('', 'Без ярлыка', none) : '');
      box.querySelectorAll('[data-label]').forEach(el => el.onclick = () => {
        label = el.dataset.label === '\u0000' ? null : el.dataset.label;
        drawLabels(); draw();
      });
    };

    const counter = () => {
      q('[data-count]').textContent = `выбрано: ${picked.size}`;
      q('[data-import]').textContent = picked.size ? `Добавить выбранных (${picked.size})` : 'Добавить выбранных';
    };
    const draw = () => {
      const list = q('[data-list]');
      if (!BOOK) { list.innerHTML = ''; q('[data-found]').textContent = '0'; counter(); return; }
      const rows = filtered(), active = terms(q('[data-q]').value);
      const pickable = rows.filter(r => !crm(r, phoneOf(r)).block);
      q('[data-found]').textContent = pickable.length;
      q('[data-all]').checked = pickable.length > 0 && pickable.every(r => picked.has(r.id));
      q('[data-more]').textContent = rows.length > SHOW ? `показаны первые ${SHOW} из ${rows.length} — уточните поиск или ярлык` : '';
      list.innerHTML = rows.length ? `<table style="width:100%;border-collapse:collapse">
        <thead><tr style="position:sticky;top:0;background:var(--bg);z-index:1">
          <th style="width:34px"></th><th style="text-align:left">Имя в Google</th><th style="text-align:left">Телефон</th>
          <th style="text-align:left">Почта</th><th style="text-align:left">Обращение в сообщении</th><th style="text-align:left">AXIOM</th></tr></thead><tbody>
        ${rows.slice(0, SHOW).map(row => {
          const phone = phoneOf(row), state = crm(row, phone), on = picked.has(row.id);
          return `<tr data-row="${escape(row.id)}" style="border-top:1px solid var(--border);${on ? 'background:var(--accent-soft)' : ''}">
            <td style="text-align:center"><input type="checkbox" data-pick ${on ? 'checked' : ''} ${state.block ? 'disabled' : ''} aria-label="Выбрать ${escape(row.name)}"></td>
            <td style="padding:6px 8px">${escape(row.name) || '<span class="hint">без имени</span>'}
              ${row.groups.length ? `<div>${row.groups.map(g => `<span class="badge" style="margin:2px 4px 0 0;font-size:10px">${escape(g)}</span>`).join('')}</div>` : ''}</td>
            <td class="mono">${row.phones.length > 1 ? `<select data-phone>${row.phones.map(p => `<option ${p === phone ? 'selected' : ''}>${escape(p)}</option>`).join('')}</select>` : escape(phone) || '—'}</td>
            <td class="hint" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escape(row.email)}</td>
            <td><input data-person value="${escape(nameOf(row, active))}" placeholder="как обратиться" maxlength="100" style="width:150px"></td>
            <td class="hint" style="${state.block ? 'color:#c0392b' : ''}">${escape(state.block || state.text)}</td></tr>`;
        }).join('')}</tbody></table>` : `<div class="empty" style="padding:30px">${q('[data-only-picked]').checked ? 'никто не выбран' : 'ничего не найдено'}</div>`;
      list.querySelectorAll('[data-row]').forEach(tr => {
        const row = BOOK.byId.get(tr.dataset.row);
        const box = tr.querySelector('[data-pick]');
        box.onchange = () => {
          if (box.checked && picked.size >= MAX_PICK) { box.checked = false; message(`За один раз — до ${MAX_PICK} человек.`, true); return; }
          if (box.checked) {
            picked.set(row.id, phoneOf(row));
            const input = tr.querySelector('[data-person]');
            names.set(row.id, input.value.trim());
            if (!input.value.trim()) input.focus();
          } else picked.delete(row.id);
          tr.style.background = box.checked ? 'var(--accent-soft)' : '';
          counter();
        };
        const sel = tr.querySelector('[data-phone]');
        if (sel) sel.onchange = () => {
          row.phones = [sel.value, ...row.phones.filter(p => p !== sel.value)];
          if (picked.has(row.id)) { if (crm(row, sel.value).block) picked.delete(row.id); else picked.set(row.id, sel.value); }
          draw();
        };
        tr.querySelector('[data-person]').oninput = e => { names.set(row.id, e.target.value.trim()); e.target.style.outline = ''; };
      });
      counter();
    };

    q('[data-q]').oninput = draw;
    q('[data-with-phone]').onchange = draw;
    q('[data-only-picked]').onchange = draw;
    q('[data-all]').onchange = () => {
      const active = terms(q('[data-q]').value);
      const rows = filtered().filter(r => !crm(r, phoneOf(r)).block);
      if (q('[data-all]').checked) {
        const room = MAX_PICK - picked.size;
        const add = rows.filter(r => !picked.has(r.id));
        if (add.length > room) message(`Отмечены первые ${room}: за один раз — до ${MAX_PICK} человек.`, true);
        add.slice(0, Math.max(room, 0)).forEach(r => { picked.set(r.id, phoneOf(r)); if (!names.has(r.id)) names.set(r.id, nameOf(r, active)); });
      } else rows.forEach(r => picked.delete(r.id));
      draw();
    };
    q('[data-none]').onclick = () => { picked.clear(); draw(); };

    // ---- загрузка книги ----
    const loaded = (data, source) => {
      BOOK = {data, source, at: Date.now(), byId: new Map(data.items.map(r => [r.id, r]))};
      picked.clear(); names.clear(); label = null;
      q('[data-state]').textContent = source === 'csv' ? `файл CSV · ${data.items.length} записей` : `подключено · ${data.items.length} записей`;
      drawLabels(); draw();
    };
    const loadGoogle = async () => {
      busySet(true); message('Читаю адресную книгу Google…');
      try { loaded(await post('/api/google-contacts/book'), 'google'); message('Выберите ярлык слева или ищите сверху и отмечайте людей галочками.'); }
      catch (e) { message(e.message, true); }
      finally { busySet(false); }
    };
    q('[data-reload]').onclick = () => { if (!busy) loadGoogle(); };
    q('[data-csv-button]').onclick = () => q('[data-csv]').click();
    q('[data-csv]').onchange = async () => {
      const file = q('[data-csv]').files[0]; if (!file || busy) return;
      busySet(true); message('Читаю файл…');
      try {
        const fd = new FormData(); fd.append('file', file);
        loaded(await request('/api/google-contacts/csv-book', {method:'POST', body:fd}), 'csv');
        message('Файл прочитан. В CRM попадут только отмеченные.');
      } catch (e) { message(e.message, true); }
      finally { busySet(false); q('[data-csv]').value = ''; }
    };

    // ---- подключение ----
    const showConnect = on => { q('[data-connect]').hidden = !on; q('[data-reload]').hidden = on; q('[data-disconnect]').hidden = on; };
    q('[data-auth]').onclick = async () => {
      const tab = window.open('', '_blank');   // открываем сразу, иначе блокировщик всплывающих окон
      try {
        const {url} = await post('/api/google-contacts/connect');
        if (tab) tab.location = url; else location.assign(url);
        message('Разрешите доступ в открывшейся вкладке, затем вставьте её адрес в поле 3.');
      } catch (e) { if (tab) tab.close(); message(e.message, true); }
    };
    q('[data-finish]').onclick = async () => {
      if (busy) return;
      busySet(true); message('Проверяю доступ в Google…');
      try {
        await post('/api/google-contacts/connect/finish', {url: q('[data-return]').value});
        showConnect(false); busySet(false); await loadGoogle();
      } catch (e) { message(e.message, true); }
      finally { busySet(false); }
    };
    q('[data-disconnect]').onclick = async () => {
      if (!confirm('Отключить доступ к Google Контактам? Уже добавленные в CRM люди останутся.')) return;
      await post('/api/google-contacts/disconnect'); BOOK = null; showConnect(true);
      q('[data-state]').textContent = 'не подключено'; drawLabels(); draw();
    };

    // ---- импорт ----
    q('[data-import]').onclick = async () => {
      if (busy || !BOOK) return;
      if (!picked.size) { message('Отметьте людей галочками.', true); return; }
      const selected = [...picked].map(([id, phone]) => ({id, phone, person_name: (names.get(id) || '').trim()}));
      const empty = selected.filter(s => !s.person_name).length;
      if (empty) {
        q('[data-only-picked]').checked = true; draw();
        modal.querySelectorAll('[data-row] [data-person]').forEach(i => { if (!i.value.trim()) i.style.outline = '2px solid #c0392b'; });
        message(`У ${empty} выбранных не заполнено обращение — впишите имя, как к человеку обращаться в сообщении.`, true); return;
      }
      const body = {token: BOOK.data.token, selected};
      const t = target();
      if (opts.campaignId) body.campaign_id = +opts.campaignId;
      else if (opts.tag !== undefined) {
        body.tag = (q('[data-tag]').value || '').trim();
        if (!body.tag) { message('Укажите тег сегмента.', true); return; }
      } else if (t) body.campaign_id = t.id;
      else body.campaign_name = q('[data-camp-name]').value;
      if (t && t.status === 'running') {
        if (!confirm(`Кампания «${t.name}» сейчас рассылает. ${selected.length} выбранным начнут уходить сообщения по её лимитам. Добавить?`)) return;
        body.allow_running = true;
      }
      busySet(true); message('Добавляю…');
      try {
        const r = await post('/api/google-contacts/import', body);
        const skipped = r.skipped.length ? ` Пропущено: ${r.skipped.length} (${r.skipped.slice(0, 3).map(x => `${x.name} — ${x.reason}`).join('; ')}${r.skipped.length > 3 ? '…' : ''}).` : '';
        message(`Готово: новых ${r.added}, уже были в CRM ${r.existing}.${skipped}`);
        if (opts.onApplied) { opts.onApplied(r); if (!r.skipped.length) setTimeout(close, 900); return; }
        // Окно из «Контактов»: можно добирать дальше в ту же кампанию.
        if (r.campaign_id && !opts.campaignId) {
          if (!campaigns.some(c => c.id === r.campaign_id)) campaigns.unshift({id: r.campaign_id, name: body.campaign_name || 'Новый черновик', status: 'draft', audience_tag: r.tag});
          drawDest(); q('[data-camp]').value = String(r.campaign_id); q('[data-camp-name]').hidden = true;
        }
        for (const id of r.contact_ids ? [...picked.keys()] : []) {
          const row = BOOK.byId.get(id), phone = picked.get(id);
          if (row && !(row.existing[phone] || []).length) row.existing[phone] = [{status: 'new', outreach_campaign_id: r.campaign_id}];
        }
        picked.clear(); q('[data-only-picked]').checked = false; draw();
      } catch (e) { message(e.message, true); }
      finally { busySet(false); }
    };

    // ---- старт ----
    try {
      const [status, list] = await Promise.all([request('/api/google-contacts/status'), request('/api/campaigns')]);
      campaigns = Array.isArray(list) ? list : [];
      q('[data-redirect]').textContent = status.redirect || 'http://localhost:8765/';
      drawDest();
      if (fresh() && (BOOK.source === 'csv' || status.connected)) {
        q('[data-state]').textContent = BOOK.source === 'csv' ? 'файл CSV' : 'подключено';
        showConnect(!status.connected && BOOK.source !== 'csv'); drawLabels(); draw();
      } else if (status.connected) { showConnect(false); await loadGoogle(); }
      else {
        showConnect(true);
        q('[data-state]').textContent = status.client ? 'не подключено' : 'нет OAuth-клиента — загрузите его в «Календаре»';
        draw();
      }
    } catch (e) { drawDest(); message(e.message, true); }
  };

  // Старое имя: экран «Контакты» раньше открывал встроенную панель.
  window.showGoogleContacts = () => window.openGoogleContacts({});
})();
