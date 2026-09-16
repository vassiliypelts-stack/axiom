/* Selective import. Only the server's preview can be submitted; no auto-send. */
window.showGoogleContacts = async function(panel) {
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  if (panel.dataset.ready) return;
  panel.dataset.ready = '1';
  const q = s => panel.querySelector(s);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Не удалось выполнить запрос.');
    return data;
  };
  const post = (url, body) => request(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  panel.innerHTML = `
    <div class="ttoolbar"><b>Выбрать контакты по маркерам в названии</b><span class="grow"></span><button data-close>Закрыть</button></div>
    <p class="hint">Введите один или несколько маркеров через запятую. Регистр и пробелы не важны. Например: эск2023, ск2025/2026.</p>
    <div class="ttoolbar" style="flex-wrap:wrap">
      <input data-markers value="эск2023, ск2025/2026" aria-label="Маркеры в названии" style="flex:1;min-width:230px">
      <select data-source aria-label="Источник поиска"><option value="google">Google Контакты</option><option value="csv">Загруженный CSV</option></select>
      <button class="primary" data-search>Найти</button>
      <button data-csv-button>Загрузить Google CSV</button>
      <input data-csv type="file" accept=".csv,text/csv" hidden>
    </div>
    <details style="margin:10px 0"><summary>Подключение Google Контактов</summary>
      <p data-connection class="hint">Проверяю подключение…</p>
      <p class="hint">Для прямого поиска нужен отдельный доступ на чтение контактов. Подключение календаря остаётся прежним.</p>
      <button data-token-button>Загрузить файл доступа</button><input data-token type="file" accept=".json" hidden>
      <p class="hint">Файл google_contacts_token.json получается при входе через tools/google_contacts_login.py на вашем компьютере.</p>
      <p class="hint">Без подключения: Google Контакты → Экспортировать → Google CSV. Загрузите файл здесь — в CRM попадут только отмеченные записи.</p>
    </details>
    <div data-message class="hint" role="status" style="margin:10px 0"></div>
    <div data-results></div>`;
  q('[data-close]').onclick = () => { panel.style.display = 'none'; };
  let savedFile = null, snapshot = null, busy = false;
  let campaigns = [];
  let connection = false;
  const message = text => { q('[data-message]').textContent = text; };
  const busySet = value => {
    busy = value;
    panel.querySelectorAll('button').forEach(b => { if (!b.hasAttribute('data-close')) b.disabled = value; });
  };
  try {
    const [status, list] = await Promise.all([request('/api/google-contacts/status'), request('/api/campaigns')]);
    connection = status.connected;
    campaigns = Array.isArray(list) ? list : [];
    q('[data-connection]').textContent = connection ? 'Файл доступа сохранён. Можно выполнить поиск.' : 'Прямой доступ к контактам ещё не подключён. Доступна загрузка Google CSV.';
  } catch (e) { message(e.message); }
  const render = data => {
    snapshot = data;
    const available = campaigns.filter(c => ['draft','paused'].includes(c.status) && !c.archived);
    message(`Проверено записей: ${data.scanned}. По маркерам найдено: ${data.matched}. Выберите нужные контакты и проверьте имена для обращения.`);
    const root = q('[data-results]');
    root.innerHTML = `
      <div class="ttoolbar"><button data-all>Выбрать доступные</button><button data-none>Снять выбор</button><span data-count class="hint">Выбрано: 0</span></div>
      <div style="overflow:auto;max-height:440px"><table style="width:100%"><thead><tr><th></th><th>Название в Google</th><th>Телефон</th><th>Имя для обращения</th><th>В AXIOM</th></tr></thead><tbody>
      ${data.items.map(r => `<tr data-row="${escape(r.id)}">
        <td><input type="checkbox" data-pick aria-label="Выбрать ${escape(r.name)}" ${r.phones.length?'':'disabled'}></td>
        <td>${escape(r.name)}<div class="hint">${escape(r.matched.join(', '))}</div></td>
        <td>${r.phones.length ? `<select data-phone aria-label="Телефон">${r.phones.map(p=>`<option value="${escape(p)}">${escape(p)}</option>`).join('')}</select>` : 'Нет подходящего телефона'}</td>
        <td><input data-person value="${escape(r.person_name)}" placeholder="Проверьте имя" aria-label="Имя для обращения" maxlength="100"></td>
        <td data-existing class="hint"></td></tr>`).join('')}
      </tbody></table></div>
      <div class="ttoolbar" style="margin-top:12px;flex-wrap:wrap">
        <label>Добавить в <select data-campaign aria-label="Кампания"><option value="">Новый черновик</option>${available.map(c=>`<option value="${c.id}">${escape(c.name)}</option>`).join('')}</select></label>
        <input data-campaign-name value="Клиенты — недвижимость" maxlength="150" aria-label="Название нового черновика">
        <button class="primary" data-import>Добавить выбранных</button>
      </div>
      <p class="hint">Контакты закрепляются только за выбранной кампанией. Добавление не запускает рассылку. Карточки с историей общения, тестовые и удалённые пропускаются.</p>`;
    const selectedCount = () => { q('[data-count]').textContent = 'Выбрано: ' + root.querySelectorAll('[data-pick]:checked').length; };
    root.querySelectorAll('[data-row]').forEach(tr => {
      const row = data.items.find(r=>r.id===tr.dataset.row);
      const phone = tr.querySelector('[data-phone]');
      const check = tr.querySelector('[data-pick]');
      const update = () => {
        const matches = row.existing[phone?.value] || [];
        const blocked = matches.length > 1 || matches.some(c=>c.deleted_at || c.is_test || c.status !== 'new');
        check.disabled = !phone || blocked;
        if (check.disabled) check.checked = false;
        tr.querySelector('[data-existing]').textContent = !matches.length ? 'Новый контакт' : matches.length > 1 ? 'Несколько дублей — пропуск' : blocked ? 'Уже в работе / исключён — пропуск' : 'Есть в CRM — без дубля';
        selectedCount();
      };
      if (phone) phone.onchange = update;
      check.onchange = selectedCount;
      update();
    });
    q('[data-all]').onclick = () => { root.querySelectorAll('[data-pick]:not(:disabled)').forEach(x=>{x.checked=true;}); selectedCount(); };
    q('[data-none]').onclick = () => { root.querySelectorAll('[data-pick]').forEach(x=>{x.checked=false;}); selectedCount(); };
    q('[data-campaign]').onchange = () => { q('[data-campaign-name]').hidden = !!q('[data-campaign]').value; };
    q('[data-import]').onclick = async () => {
      if (busy) return;
      const selected = [...root.querySelectorAll('[data-row]')].filter(tr=>tr.querySelector('[data-pick]').checked).map(tr=>({id:tr.dataset.row, phone:tr.querySelector('[data-phone]').value, person_name:tr.querySelector('[data-person]').value.trim()}));
      if (!selected.length) { message('Отметьте нужные контакты.'); return; }
      if (selected.some(r=>!r.person_name)) { message('Заполните имя для обращения у выбранных контактов.'); return; }
      busySet(true);
      try {
        const result = await post('/api/google-contacts/import', {token:snapshot.token, selected,
          campaign_id:q('[data-campaign]').value || null, campaign_name:q('[data-campaign-name]').value});
        message(`Добавлено: ${result.added}. Уже были в CRM: ${result.existing}. Пропущено: ${result.skipped.length}. Рассылка не запускалась.`);
        if (result.campaign_id) {
          q('[data-campaign]').value = String(result.campaign_id);
          if (!q('[data-campaign]').value) {
            const option = new Option(q('[data-campaign-name]').value, String(result.campaign_id));
            q('[data-campaign]').add(option); q('[data-campaign]').value = String(result.campaign_id);
          }
          q('[data-campaign-name]').hidden = true;
        }
        root.querySelectorAll('[data-pick]').forEach(x=>{x.checked=false;}); selectedCount();
        if (result.skipped.length) {
          const reasons = document.createElement('p'); reasons.className='hint';
          reasons.textContent = result.skipped.map(x=>`${x.name}: ${x.reason}`).join(' · '); root.appendChild(reasons);
        }
      } catch(e) { message(e.message); }
      finally { busySet(false); }
    };
  };
  const search = async useFile => {
    if (busy) return;
    const markers = q('[data-markers]').value.trim();
    if (!markers) { message('Введите маркеры.'); return; }
    if (!useFile && !connection) { q('details').open=true; message('Сначала подключите Google Контакты или загрузите Google CSV.'); return; }
    busySet(true); q('[data-results]').innerHTML=''; snapshot=null;
    message(useFile ? 'Ищу маркеры в файле…' : 'Читаю контакты Google и ищу маркеры во всех названиях…');
    try {
      let data;
      if (useFile) {
        const fd = new FormData(); fd.append('file', savedFile); fd.append('markers', markers);
        data = await request('/api/google-contacts/csv-preview', {method:'POST', body:fd});
      } else data = await post('/api/google-contacts/preview', {markers});
      render(data);
    } catch(e) { message(e.message); }
    finally { busySet(false); }
  };
  q('[data-search]').onclick = () => {
    if (q('[data-source]').value==='csv' && !savedFile) { q('[data-csv]').click(); return; }
    search(q('[data-source]').value==='csv');
  };
  q('[data-csv-button]').onclick = () => q('[data-csv]').click();
  q('[data-csv]').onchange = () => { savedFile=q('[data-csv]').files[0]; if(savedFile) { q('[data-source]').value='csv'; search(true); } };
  q('[data-token-button]').onclick = () => q('[data-token]').click();
  q('[data-token]').onchange = async () => {
    const file=q('[data-token]').files[0]; if(!file || busy)return;
    busySet(true);
    try {
      const fd=new FormData(); fd.append('file',file);
      await request('/api/google-contacts/token',{method:'POST',body:fd}); connection=true;
      q('[data-connection]').textContent='Доступ к контактам проверен. Можно выполнять поиск.';
      snapshot=null; q('[data-results]').innerHTML=''; message('Google Контакты подключены.');
    } catch(e) { message(e.message); }
    finally { busySet(false); }
  };
};
