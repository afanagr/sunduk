/* Sunduk admin SPA — vanilla JS, no build step.
   All DOM is created with createElement / textContent so user-controlled
   file names can never be interpreted as HTML (XSS-safe, CSP-friendly). */
(function () {
  'use strict';

  const state = {
    user: null,
    csrf: null,
    mustChange: false,
    directories: [],       // [{id, name, host_path, available, usage, created_at}]
    directory: null,       // id of the selected directory
    path: '',
    entries: [],
    searchMode: false,
    view: 'list',          // 'list' | 'grid' (remembered in localStorage)
  };

  const KIND_ICON = {
    dir: '📁', image: '🖼️', video: '🎬', audio: '🎵', archive: '🗜️',
    pdf: '📕', doc: '📄', text: '📝', file: '📦',
  };

  const $ = (sel, root) => (root || document).querySelector(sel);

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach((key) => {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on') && typeof value === 'function') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, value);
      });
    }
    if (children !== undefined && children !== null) {
      [].concat(children).forEach((child) => {
        if (child === null || child === undefined || child === false) return;
        node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
      });
    }
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function fmtSize(bytes) {
    if (!bytes) return '0 Б';
    const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  // "через 3 ч" / "5 мин назад" — used for link expiry.
  function fmtAgo(ts) {
    if (!ts) return 'бессрочно';
    const delta = Math.round(ts - Date.now() / 1000);
    const abs = Math.abs(delta);
    const units = [['мин', 60], ['ч', 60], ['д', 24], ['г', 365]];
    let value = abs / 60;         // minutes
    let label = 'мин';
    let index = 0;
    while (index < units.length - 1 && value >= units[index + 1][1]) {
      value /= units[index + 1][1];
      label = units[index + 1][0];
      index += 1;
    }
    const text = `${Math.max(1, Math.round(value))} ${label}`;
    return delta >= 0 ? `через ${text}` : `${text} назад`;
  }

  function toast(kind, title, msg) {
    const node = el('div', { class: `toast ${kind || 'info'}` }, [
      el('div', {}, [
        el('div', { class: 't-title', text: title }),
        msg ? el('div', { class: 't-msg', text: msg }) : null,
      ]),
    ]);
    $('#toasts').appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  // navigator.clipboard exists only in a "secure context" (https:// or
  // localhost). The panel is normally opened over plain http on a LAN address
  // (http://192.168.x.x:8080), where that API is missing — so copy through a
  // hidden textarea + execCommand, which works everywhere including http.
  function legacyCopy(text) {
    const area = document.createElement('textarea');
    area.className = 'copy-sink';
    area.value = text;
    area.setAttribute('readonly', '');
    area.setAttribute('aria-hidden', 'true');
    area.tabIndex = -1;
    document.body.appendChild(area);
    const active = document.activeElement;
    const selection = document.getSelection();
    const saved = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
    let ok = false;
    try {
      area.select();
      area.setSelectionRange(0, area.value.length);
      ok = document.execCommand('copy');
    } catch (err) {
      ok = false;
    }
    area.remove();
    if (saved && selection) {
      selection.removeAllRanges();
      selection.addRange(saved);
    }
    if (active && typeof active.focus === 'function' && document.contains(active)) active.focus();
    return ok;
  }

  function copyText(text, label) {
    const value = text === null || text === undefined ? '' : String(text);
    const done = () => toast('ok', 'Скопировано', label || value);
    const manual = () => toast('info', 'Скопируйте вручную', value);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done).catch(() => {
        if (legacyCopy(value)) done(); else manual();
      });
    } else if (legacyCopy(value)) {
      done();
    } else {
      manual();
    }
  }

  // ---------------------------------------------------------------- ui state
  function storedView() {
    try { return localStorage.getItem('sunduk.view') === 'grid' ? 'grid' : 'list'; }
    catch (err) { return 'list'; }
  }

  function rememberView(view) {
    try { localStorage.setItem('sunduk.view', view); } catch (err) { /* private mode */ }
  }

  function setNav(open) {
    $('#main-view').classList.toggle('nav-open', open);
    $('#nav-backdrop').classList.toggle('hidden', !open);
  }

  // --------------------------------------------------------------- api client
  async function api(path, options) {
    const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options || {});
    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    const method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') opts.headers['X-CSRF-Token'] = state.csrf || '';

    const res = await fetch(path, opts);
    const ctype = res.headers.get('content-type') || '';
    if (res.status === 401 && path !== '/api/login') {
      showLogin('Сессия истекла — войдите заново');
    }
    if (res.status === 428) {
      // The backend refuses everything until the default password is replaced.
      state.mustChange = true;
      openPasswordDialog(true);
    }
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      if (ctype.indexOf('application/json') !== -1) {
        try {
          const data = await res.json();
          detail = data.detail || data.error || detail;
        } catch (err) { /* ignore */ }
      }
      const error = new Error(detail);
      error.status = res.status;
      throw error;
    }
    if (ctype.indexOf('application/json') !== -1) return res.json();
    return res;
  }

  function qs(params) {
    return Object.keys(params)
      .filter((k) => params[k] !== undefined && params[k] !== null && params[k] !== '')
      .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`)
      .join('&');
  }

  // -------------------------------------------------------------------- modal
  // size: '' | 'wide' | 'links' | 'narrow'.  dismissible:false = modal dialog
  // that can only be left by completing its action (used for the first-login
  // password change).
  function openModal(opts) {
    const overlay = el('div', { class: 'overlay' });
    const modal = el('div', { class: 'modal' + (opts.size ? ' ' + opts.size : '') });
    let onEsc = null;
    const close = () => {
      if (onEsc) document.removeEventListener('keydown', onEsc);
      overlay.remove();
    };
    const dismissible = opts.dismissible !== false;

    const head = el('div', { class: 'modal-head' }, el('h3', { text: opts.title }));
    if (dismissible) {
      const closeBtn = el('button', { class: 'btn ghost icon', type: 'button', title: 'Закрыть' }, '✕');
      closeBtn.addEventListener('click', close);
      head.appendChild(closeBtn);
    }
    const body = el('div', { class: 'modal-body' }, opts.body || null);
    modal.appendChild(head);
    modal.appendChild(body);
    if (opts.footer) modal.appendChild(el('div', { class: 'modal-foot' }, opts.footer));
    overlay.appendChild(modal);
    if (dismissible) {
      overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) close(); });
      onEsc = (ev) => { if (ev.key === 'Escape') close(); };
      document.addEventListener('keydown', onEsc);
    }
    $('#modal-root').appendChild(overlay);
    if (opts.onOpen) opts.onOpen(body, close);
    return { close, body, overlay };
  }

  function promptDialog(title, label, value) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (result) => { if (!done) { done = true; resolve(result); } };
      const input = el('input', { class: 'input', value: value || '', autocomplete: 'off' });
      const form = el('form', {}, [
        el('div', { class: 'field' }, [el('label', { text: label }), input]),
      ]);
      const modal = openModal({
        title,
        body: form,
        footer: [
          el('button', { class: 'btn ghost', type: 'button', text: 'Отмена', onclick: () => { finish(null); modal.close(); } }),
          el('button', { class: 'btn primary', type: 'submit', text: 'Готово' }),
        ],
      });
      form.addEventListener('submit', (ev) => {
        ev.preventDefault();
        finish(input.value.trim() || null);
        modal.close();
      });
      setTimeout(() => { input.focus(); input.select(); }, 30);
    });
  }

  function confirmDialog(title, message, confirmLabel) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (result) => { if (!done) { done = true; resolve(result); } };
      const modal = openModal({
        title,
        body: el('div', { text: message }),
        footer: [
          el('button', { class: 'btn ghost', type: 'button', text: 'Отмена', onclick: () => { finish(false); modal.close(); } }),
          el('button', {
            class: 'btn danger', type: 'button', text: confirmLabel || 'Удалить',
            onclick: () => { finish(true); modal.close(); },
          }),
        ],
      });
    });
  }

  function iconButton(label, title, handler) {
    const btn = el('button', { class: 'btn ghost icon', type: 'button', title, text: label });
    btn.addEventListener('click', (ev) => { ev.stopPropagation(); handler(); });
    return btn;
  }

  // ------------------------------------------------------------------- views
  function showLogin(message) {
    $('#main-view').classList.add('hidden');
    $('#login-view').classList.remove('hidden');
    $('#login-error').textContent = message || '';
    state.user = null;
    state.csrf = null;
    state.directories = [];
    state.directory = null;
    state.entries = [];
  }

  function showApp() {
    $('#login-view').classList.add('hidden');
    $('#main-view').classList.remove('hidden');
    $('#user-name').textContent = state.user || '';
    $('#logout-btn').classList.toggle('hidden', state.mustChange);
    renderViewButton();
  }

  function currentDirectory() {
    return state.directories.filter((d) => d.id === state.directory)[0] || null;
  }

  // ------------------------------------------------------------- directories
  async function loadDirectories() {
    const data = await api('/api/directories');
    state.directories = data.directories || [];
    renderDirectories();
    const stillThere = state.directories.filter((d) => d.id === state.directory).length > 0;
    if (!stillThere) state.directory = null;
    if (state.directory) {
      renderUsage();
      return;
    }
    const first = state.directories.filter((d) => d.available)[0] || state.directories[0];
    if (first) await selectDirectory(first.id);
    else renderNoDirectories();
  }

  function renderDirectories() {
    const list = $('#dir-list');
    clear(list);
    state.directories.forEach((dir) => {
      const item = el('div', { class: 'share-item' + (dir.id === state.directory ? ' active' : '') }, [
        el('span', { class: 'ico', text: '🗄️' }),
        el('div', { class: 'meta' }, [
          el('div', { class: 'nm', text: dir.name }),
          el('div', { class: 'sub', text: dir.available ? dir.host_path : 'каталог недоступен' }),
        ]),
        el('div', { class: 'dir-actions' }, [
          iconButton('✏', 'Изменить', () => openDirectoryDialog(dir)),
          iconButton('🗑', 'Убрать из панели', () => removeDirectory(dir)),
        ]),
      ]);
      item.addEventListener('click', () => { selectDirectory(dir.id); });
      list.appendChild(item);
    });
    if (!state.directories.length) {
      list.appendChild(el('div', { class: 'side-empty', text: 'Каталогов нет — добавьте первый' }));
    }
  }

  function renderUsage() {
    const box = $('#usage');
    clear(box);
    const dir = currentDirectory();
    if (!dir || !dir.usage || !dir.usage.total) {
      box.appendChild(el('div', { class: 'muted', text: 'Диск: нет данных' }));
      return;
    }
    const percent = Math.min(100, Math.round((dir.usage.used / dir.usage.total) * 100));
    const bar = el('div', { class: 'usage-bar' }, [el('span')]);
    box.appendChild(el('div', { class: 'usage-text' }, [
      el('span', { text: dir.name }),
      el('span', { text: `${fmtSize(dir.usage.used)} / ${fmtSize(dir.usage.total)}` }),
    ]));
    box.appendChild(bar);
    bar.firstChild.setAttribute('style', `width:${percent}%`);
  }

  function renderNoDirectories() {
    const area = $('#file-area');
    clear(area);
    clear($('#breadcrumb'));
    $('#brand-sub').textContent = 'Каталог не выбран';
    const btn = el('button', { class: 'btn primary', type: 'button', text: '＋ Добавить каталог' });
    btn.addEventListener('click', () => openDirectoryDialog(null));
    area.appendChild(el('div', { class: 'empty' }, [
      el('div', { class: 'big', text: '🗂️' }),
      el('div', { text: 'Ни один каталог не подключён' }),
      el('div', { class: 'muted', style: 'margin:8px 0 18px;', text: 'Укажите путь к папке на сервере — например /mnt/media — и задайте имя для панели.' }),
      btn,
    ]));
    renderUsage();
  }

  async function selectDirectory(id) {
    state.directory = id;
    state.path = '';
    state.searchMode = false;
    $('#search-input').value = '';
    renderDirectories();
    renderUsage();
    renderViewButton();
    setNav(false);
    const dir = currentDirectory();
    $('#brand-sub').textContent = dir ? dir.host_path : 'Каталог не выбран';
    await loadDir();
  }

  // -------------------------------------------------------------- navigation
  async function navigate(path) {
    state.path = (path || '').replace(/^\/+|\/+$/g, '');
    $('#search-input').value = '';
    state.searchMode = false;
    await loadDir();
  }

  async function loadDir() {
    if (!state.directory) return;
    try {
      const data = await api(`/api/list?${qs({ directory: state.directory, path: state.path })}`);
      state.entries = data.entries || [];
      renderBreadcrumb();
      renderFiles(state.entries);
    } catch (err) {
      renderMessage('⚠️', err.message);
    }
  }

  function fileUrl(base, entry) {
    return `${base}?${qs({ directory: state.directory, path: entry.path })}`;
  }

  function renderBreadcrumb() {
    const box = $('#breadcrumb');
    clear(box);
    const dir = currentDirectory();
    if (!dir) return;
    const parts = state.path ? state.path.split('/') : [];
    const root = el('span', { class: 'crumb' + (parts.length ? '' : ' current'), text: `🏠 ${dir.name}` });
    if (parts.length) root.addEventListener('click', () => navigate(''));
    box.appendChild(root);

    let acc = '';
    parts.forEach((part, index) => {
      acc = acc ? `${acc}/${part}` : part;
      box.appendChild(el('span', { class: 'sep', text: '›' }));
      const isLast = index === parts.length - 1;
      const crumb = el('span', { class: 'crumb' + (isLast ? ' current' : ''), text: part });
      if (!isLast) { const target = acc; crumb.addEventListener('click', () => navigate(target)); }
      box.appendChild(crumb);
    });
  }

  function renderMessage(icon, text) {
    const area = $('#file-area');
    clear(area);
    area.appendChild(el('div', { class: 'empty' }, [
      el('div', { class: 'big', text: icon }),
      el('div', { text }),
    ]));
  }

  function renderFiles(entries) {
    const area = $('#file-area');
    clear(area);
    if (!entries.length) {
      renderMessage('📂', state.searchMode ? 'Ничего не найдено' : 'Папка пуста — перетащите файлы или папку сюда, чтобы загрузить');
      return;
    }
    area.appendChild(state.view === 'grid' ? renderGrid(entries) : renderTable(entries));
  }

  function renderTable(entries) {
    const table = el('table', { class: 'file-table' });
    table.appendChild(el('thead', {}, el('tr', {}, [
      el('th', { text: 'Имя' }),
      el('th', { class: 'right col-size', text: 'Размер' }),
      el('th', { class: 'col-date', text: 'Изменён' }),
      el('th', { class: 'right', text: '' }),
    ])));
    const tbody = el('tbody');
    entries.forEach((entry) => tbody.appendChild(renderRow(entry)));
    table.appendChild(tbody);
    return table;
  }

  // Thumbnail grid: images get a real preview, everything else an icon.
  function renderGrid(entries) {
    const grid = el('div', { class: 'file-grid' });
    entries.forEach((entry) => grid.appendChild(renderTile(entry)));
    return grid;
  }

  function renderTile(entry) {
    const tile = el('div', Object.assign(
      { class: 'tile', title: state.searchMode ? entry.path : entry.name },
      entry.is_dir ? { 'data-drop-path': entry.path } : null,
    ));
    const thumb = el('div', { class: 'thumb' });
    if (!entry.is_dir && entry.kind === 'image') {
      thumb.appendChild(el('img', { src: fileUrl('/api/preview', entry), alt: '', loading: 'lazy' }));
    } else {
      thumb.appendChild(el('span', { class: 'glyph', text: KIND_ICON[entry.kind] || KIND_ICON.file }));
    }
    tile.appendChild(thumb);
    tile.appendChild(el('div', { class: 'cap' }, [
      el('div', { class: 'nm', text: entry.name }),
      el('div', { class: 'sub', text: entry.is_dir ? (state.searchMode ? entry.path : 'папка') : fmtSize(entry.size) }),
    ]));
    tile.addEventListener('click', () => openEntry(entry));
    return tile;
  }

  function renderViewButton() {
    const btn = $('#view-btn');
    if (!btn) return;
    btn.textContent = state.view === 'grid' ? '☰' : '▦';
    btn.title = state.view === 'grid' ? 'Показать списком' : 'Показать плиткой';
  }

  function toggleView() {
    state.view = state.view === 'grid' ? 'list' : 'grid';
    rememberView(state.view);
    renderViewButton();
    renderFiles(state.entries);
  }

  // ------------------------------------------------------------------ file row
  function renderRow(entry) {
    const icon = KIND_ICON[entry.kind] || KIND_ICON.file;
    const nameEl = el('span', { class: 'nm link', text: entry.name, title: entry.path });
    nameEl.addEventListener('click', () => openEntry(entry));
    const nameWrap = el('div', { class: 'file-name' }, [el('span', { class: 'ico', text: icon }), nameEl]);

    // Every directory is fully read/write, so every action is always offered.
    const actions = el('div', { class: 'row-actions' }, [
      !entry.is_dir ? iconButton('⬇', 'Скачать', () => triggerDownload(fileUrl('/api/download', entry))) : null,
      !entry.is_dir && FSViewer.isPreviewable(entry.kind) ? iconButton('👁', 'Открыть', () => openPreview(entry)) : null,
      iconButton('🔗', 'Создать публичную ссылку', () => openShareDialog(entry)),
      iconButton('↗', 'Переместить', () => openTransferDialog(entry, 'move')),
      iconButton('⧉', 'Копировать', () => openTransferDialog(entry, 'copy')),
      iconButton('✏', 'Переименовать', () => renameEntry(entry)),
      iconButton('🗑', 'Удалить', () => deleteEntry(entry)),
    ]);

    // One compact meta line, shown always for search hits and only on narrow
    // screens otherwise (the table hides its size/date columns there).
    const metaBits = [];
    if (state.searchMode) metaBits.push(entry.path);
    metaBits.push(entry.is_dir ? 'папка' : fmtSize(entry.size));
    metaBits.push(fmtDate(entry.mtime));
    const meta = el('div', {
      class: 'row-meta' + (state.searchMode ? '' : ' only-mobile'),
      text: metaBits.join(' · '),
    });

    return el('tr', entry.is_dir ? { 'data-drop-path': entry.path } : null, [
      el('td', {}, [nameWrap, meta]),
      el('td', { class: 'right muted col-size', text: entry.is_dir ? '—' : fmtSize(entry.size) }),
      el('td', { class: 'muted col-date', text: fmtDate(entry.mtime) }),
      el('td', { class: 'right' }, actions),
    ]);
  }

  function openEntry(entry) {
    if (entry.is_dir) return navigate(entry.path);
    if (state.searchMode) return navigate(entry.path.split('/').slice(0, -1).join('/'));
    if (FSViewer.isPreviewable(entry.kind)) return openPreview(entry);
    return triggerDownload(fileUrl('/api/download', entry));
  }

  function triggerDownload(url) {
    const link = el('a', { href: url });
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function openPreview(entry) {
    FSViewer.open({
      items: state.entries,
      item: entry,
      urlFor: (item) => fileUrl('/api/preview', item),
      downloadUrlFor: (item) => fileUrl('/api/download', item),
    });
  }

  // --------------------------------------------------------- share-link dialog
  function openShareDialog(entry) {
    const expiry = el('select', { class: 'input' });
    [['1', '1 час'], ['24', '24 часа'], ['168', '7 дней'], ['720', '30 дней'], ['8760', '365 дней']]
      .forEach((pair) => expiry.appendChild(el('option', { value: pair[0], text: pair[1], selected: pair[0] === '24' })));
    const password = el('input', { class: 'input', type: 'password', placeholder: 'оставьте пустым — без пароля', autocomplete: 'new-password' });
    const limit = el('input', { class: 'input', type: 'number', min: '0', value: '0' });
    const result = el('div');
    const createBtn = el('button', { class: 'btn primary', type: 'button', text: 'Создать ссылку' });

    const body = el('div', {}, [
      el('div', { class: 'hint', text: `${entry.is_dir ? '📁 Папка' : '📄 Файл'}: ${entry.name}` }),
      el('div', { class: 'field' }, [el('label', { text: 'Срок действия' }), expiry]),
      el('div', { class: 'field' }, [el('label', { text: 'Пароль (необязательно)' }), password]),
      el('div', { class: 'field' }, [el('label', { text: 'Лимит скачиваний (0 — без лимита)' }), limit]),
      result,
    ]);

    const modal = openModal({
      title: 'Публичная ссылка',
      body,
      footer: [
        el('button', { class: 'btn ghost', type: 'button', text: 'Закрыть', onclick: () => modal.close() }),
        createBtn,
      ],
    });

    createBtn.addEventListener('click', async () => {
      createBtn.disabled = true;
      try {
        const data = await api('/api/share/create', {
          method: 'POST',
          json: {
            directory: state.directory,
            path: entry.path,
            expiry_hours: parseInt(expiry.value, 10) || 24,
            password: password.value.trim() || null,
            max_downloads: parseInt(limit.value, 10) || 0,
          },
        });
        clear(result);
        const urlInput = el('input', { value: data.url, readonly: true });
        const copyBtn = el('button', { class: 'btn', type: 'button', text: '📋 Копировать' });
        copyBtn.addEventListener('click', () => copyText(data.url, 'Ссылка в буфере обмена'));
        result.appendChild(el('div', { class: 'link-box' }, [urlInput, copyBtn]));
        if (data.password) {
          const pwdInput = el('input', { value: data.password, readonly: true });
          const pwdCopy = el('button', { class: 'btn', type: 'button', title: 'Копировать пароль', text: '📋' });
          pwdCopy.addEventListener('click', () => copyText(data.password, 'Пароль в буфере обмена'));
          result.appendChild(el('div', { class: 'field', style: 'margin-top:14px;' }, [
            el('label', { text: 'Пароль ссылки (виден здесь и в «Внешних ссылках»)' }),
            el('div', { class: 'link-box' }, [pwdInput, pwdCopy]),
          ]));
        }
        result.appendChild(el('div', {
          class: 'muted',
          text: `Действует до ${fmtDate(data.expires_at)}${data.has_password ? ' · защищено паролем' : ''}`,
        }));
        toast('ok', 'Ссылка создана', data.url);
      } catch (err) {
        toast('err', 'Ошибка', err.message);
      } finally {
        createBtn.disabled = false;
      }
    });
  }

  // ------------------------------------------------------- directory manager
  // Add / edit one exposed directory: the path on the server (with a server-side
  // folder picker) and the name shown in the panel.
  function openDirectoryDialog(directory) {
    const isEdit = Boolean(directory);
    const pathInput = el('input', {
      class: 'input',
      value: directory ? directory.host_path : '/',
      placeholder: '/mnt/media',
      autocomplete: 'off',
      spellcheck: 'false',
    });
    const nameInput = el('input', {
      class: 'input',
      value: directory ? directory.name : '',
      placeholder: 'например: Медиа',
      autocomplete: 'off',
    });
    const createBox = el('input', { type: 'checkbox' });
    const picker = el('div', { class: 'picker hidden' });
    const error = el('div', { class: 'hint', style: 'color:var(--danger); min-height:16px;' });
    const saveBtn = el('button', { class: 'btn primary', type: 'button', text: isEdit ? 'Сохранить' : 'Подключить' });

    const toggleBtn = el('button', { class: 'btn', type: 'button', text: '📁 Выбрать на сервере' });
    const body = el('div', {}, [
      el('div', { class: 'hint', text: 'Путь указывается так, как он выглядит на сервере, например /mnt/media или /srv/photos/2026.' }),
      el('div', { class: 'field' }, [
        el('label', { text: 'Путь на сервере' }),
        el('div', { class: 'row-inline' }, [pathInput, toggleBtn]),
      ]),
      picker,
      el('div', { class: 'field' }, [el('label', { text: 'Имя в панели' }), nameInput]),
      el('label', { class: 'checkline' }, [
        createBox,
        el('span', { text: 'Создать каталог, если его ещё нет' }),
      ]),
      error,
    ]);

    const modal = openModal({
      title: isEdit ? 'Каталог: ' + directory.name : 'Новый каталог',
      size: 'wide',
      body,
      footer: [
        el('button', { class: 'btn ghost', type: 'button', text: 'Отмена', onclick: () => modal.close() }),
        saveBtn,
      ],
    });

    toggleBtn.addEventListener('click', () => {
      const hidden = picker.classList.toggle('hidden');
      toggleBtn.textContent = hidden ? '📁 Выбрать на сервере' : '✕ Скрыть проводник';
      if (!hidden && !picker.firstChild) {
        hostFolderPicker(picker, pathInput.value || '/', (picked) => {
          pathInput.value = picked;
          picker.classList.add('hidden');
          toggleBtn.textContent = '📁 Выбрать на сервере';
          if (!nameInput.value.trim()) nameInput.value = picked.split('/').filter(Boolean).pop() || '';
        });
      }
    });

    saveBtn.addEventListener('click', async () => {
      error.textContent = '';
      saveBtn.disabled = true;
      try {
        const payload = {
          host_path: pathInput.value.trim(),
          name: nameInput.value.trim() || null,
          create: createBox.checked,
        };
        let saved;
        if (isEdit) {
          payload.id = directory.id;
          saved = await api('/api/directories/update', { method: 'POST', json: payload });
        } else {
          saved = await api('/api/directories/add', { method: 'POST', json: payload });
        }
        modal.close();
        toast('ok', isEdit ? 'Каталог обновлён' : 'Каталог подключён', saved.name + ' — ' + saved.host_path);
        if (!isEdit) state.directory = saved.id;
        await loadDirectories();
      } catch (err) {
        error.textContent = err.message;
      } finally {
        saveBtn.disabled = false;
      }
    });

    setTimeout(() => pathInput.focus(), 30);
  }

  // Server-side folder browser: walks the directories that really exist on the
  // host below /api/host/list and reports the chosen server path.
  function hostFolderPicker(container, initialPath, onPick) {
    async function load(path) {
      clear(container);
      container.appendChild(el('div', { class: 'picker-path' }, [
        el('span', { class: 'muted', text: 'Сервер: ' }),
        el('code', { text: path || '/' }),
      ]));

      const bar = el('div', { class: 'picker-bar' });
      ['/', '/mnt', '/media', '/srv', '/storage', '/home', '/opt', '/var/lib'].forEach((shortcut) => {
        const chip = el('button', { class: 'chip', type: 'button', text: shortcut });
        chip.addEventListener('click', () => load(shortcut));
        bar.appendChild(chip);
      });
      container.appendChild(bar);

      const listBox = el('div', { class: 'picker-list' }, el('div', { class: 'muted pad', text: 'Чтение…' }));
      container.appendChild(listBox);
      let data;
      try {
        data = await api('/api/host/list?' + qs({ path: path || '/' }));
      } catch (err) {
        clear(listBox);
        listBox.appendChild(el('div', { class: 'hint', style: 'color:var(--danger);', text: err.message }));
        return;
      }
      clear(listBox);
      if (data.parent) {
        const up = el('div', { class: 'picker-row' }, [
          el('span', { class: 'ico', text: '⬅' }),
          el('span', { class: 'nm', text: 'Наверх: ' + data.parent }),
        ]);
        up.addEventListener('click', () => load(data.parent));
        listBox.appendChild(up);
      }
      (data.dirs || []).forEach((entry) => {
        const row = el('div', { class: 'picker-row' }, [
          el('span', { class: 'ico', text: '📁' }),
          el('span', { class: 'nm', text: entry.name }),
          el('span', { class: 'go', text: '›' }),
        ]);
        row.addEventListener('click', () => load(entry.path));
        listBox.appendChild(row);
      });
      if (!(data.dirs || []).length) {
        listBox.appendChild(el('div', { class: 'muted pad', text: 'Вложенных папок нет' }));
      }

      const choose = el('button', { class: 'btn primary', type: 'button', text: 'Выбрать эту папку' });
      choose.addEventListener('click', () => onPick(data.path));
      container.appendChild(el('div', { class: 'picker-foot' }, [choose]));
    }
    load(initialPath || '/');
  }

  async function removeDirectory(dir) {
    const ok = await confirmDialog(
      'Убрать каталог',
      `«${dir.name}» (${dir.host_path}) исчезнет из панели, а все его внешние ссылки перестанут `
      + 'работать. Файлы на сервере при этом не удаляются.',
      'Убрать',
    );
    if (!ok) return;
    try {
      const data = await api('/api/directories/remove', { method: 'POST', json: { id: dir.id } });
      toast('ok', 'Каталог убран',
            data.revoked_links ? `отозвано ссылок: ${data.revoked_links}` : dir.host_path);
      if (state.directory === dir.id) {
        state.directory = null;
        state.entries = [];
      }
      await loadDirectories();
    } catch (err) {
      toast('err', 'Ошибка', err.message);
    }
  }

  // ------------------------------------------------------------- file actions
  // Copy / move between any two exposed directories (the same one included).
  function openTransferDialog(entry, mode) {
    const isCopy = mode === 'copy';
    const targets = state.directories.filter((d) => d.available);
    if (!targets.length) {
      toast('err', 'Нет доступного каталога', 'Сначала подключите хотя бы один каталог');
      return;
    }
    const select = el('select', { class: 'input' });
    targets.forEach((d) => select.appendChild(el('option', {
      value: d.id,
      text: `${d.name} — ${d.host_path}`,
      selected: d.id === state.directory,
    })));
    const pathInput = el('input', { class: 'input', placeholder: 'папка внутри каталога (пусто — корень)', autocomplete: 'off' });
    const browse = el('div', { class: 'picker small' });
    const error = el('div', { class: 'hint', style: 'color:var(--danger); min-height:16px;' });

    const runBtn = el('button', { class: 'btn primary', type: 'button', text: isCopy ? 'Копировать' : 'Переместить' });
    const body = el('div', {}, [
      el('div', { class: 'hint', text: `${entry.is_dir ? '📁' : '📄'} ${entry.name}` }),
      el('div', { class: 'field' }, [el('label', { text: 'Каталог назначения' }), select]),
      el('div', { class: 'field' }, [el('label', { text: 'Папка назначения' }), pathInput]),
      el('div', { class: 'field' }, [el('label', { text: 'Выбрать папку' }), browse]),
      error,
    ]);

    const modal = openModal({
      title: isCopy ? 'Копировать' : 'Переместить',
      size: 'wide',
      body,
      footer: [
        el('button', { class: 'btn ghost', type: 'button', text: 'Отмена', onclick: () => modal.close() }),
        runBtn,
      ],
    });

    const browser = inlineFolderPicker(browse, select.value, (path) => { pathInput.value = path; });
    select.addEventListener('change', () => browser.reload(select.value));

    runBtn.addEventListener('click', async () => {
      error.textContent = '';
      runBtn.disabled = true;
      try {
        await api(isCopy ? '/api/copy' : '/api/move', {
          method: 'POST',
          json: {
            directory: state.directory,
            path: entry.path,
            dest_directory: select.value,
            dest_path: pathInput.value.trim(),
          },
        });
        toast('ok', isCopy ? 'Скопировано' : 'Перемещено', entry.name);
        modal.close();
        await loadDir();
      } catch (err) {
        error.textContent = err.message;
      } finally {
        runBtn.disabled = false;
      }
    });
  }

  // Folder list of one exposed directory, used to pick a destination folder
  // instead of typing a path by hand.
  function inlineFolderPicker(container, directoryId, onPath) {
    let current = '';
    let currentDirectoryId = directoryId;

    async function load(path) {
      current = path || '';
      onPath(current);
      clear(container);
      if (current) {
        const up = el('button', { class: 'chip', type: 'button', text: '⬅ Наверх' });
        up.addEventListener('click', () => load(current.split('/').slice(0, -1).join('/')));
        container.appendChild(el('div', { class: 'picker-bar' }, [
          up,
          el('span', { class: 'muted', text: current }),
        ]));
      }
      let data;
      try {
        data = await api('/api/list?' + qs({ directory: currentDirectoryId, path: current }));
      } catch (err) {
        container.appendChild(el('div', { class: 'hint', style: 'color:var(--danger);', text: err.message }));
        return;
      }
      const dirs = (data.entries || []).filter((e) => e.is_dir);
      if (!dirs.length) {
        container.appendChild(el('div', { class: 'muted pad', text: 'Вложенных папок нет — выбран корень каталога' }));
        return;
      }
      const listBox = el('div', { class: 'picker-list' });
      dirs.forEach((dir) => {
        const row = el('div', { class: 'picker-row' }, [
          el('span', { class: 'ico', text: '📁' }),
          el('span', { class: 'nm', text: dir.name }),
          el('span', { class: 'go', text: '›' }),
        ]);
        row.addEventListener('click', () => load(current ? `${current}/${dir.name}` : dir.name));
        listBox.appendChild(row);
      });
      container.appendChild(listBox);
    }

    load('');
    return {
      reload: (id) => { currentDirectoryId = id; return load(''); },
    };
  }

  async function renameEntry(entry) {
    const name = await promptDialog('Переименовать', 'Новое имя', entry.name);
    if (!name || name === entry.name) return;
    try {
      await api('/api/rename', { method: 'POST', json: { directory: state.directory, path: entry.path, new_name: name } });
      toast('ok', 'Переименовано', `«${entry.name}» → «${name}»`);
      await loadDir();
    } catch (err) {
      toast('err', 'Ошибка', err.message);
    }
  }

  async function deleteEntry(entry) {
    const ok = await confirmDialog('Удалить', `Удалить «${entry.name}»? Действие необратимо.${entry.is_dir ? ' Папка будет удалена со всем содержимым.' : ''}`);
    if (!ok) return;
    try {
      await api('/api/delete', { method: 'POST', json: { directory: state.directory, path: entry.path } });
      toast('ok', 'Удалено', entry.name);
      await loadDir();
    } catch (err) {
      toast('err', 'Ошибка', err.message);
    }
  }

  async function createFolder() {
    if (!state.directory) { toast('err', 'Каталог не выбран', 'Подключите каталог в панели'); return; }
    const name = await promptDialog('Новая папка', 'Имя папки');
    if (!name) return;
    try {
      await api('/api/mkdir', { method: 'POST', json: { directory: state.directory, path: state.path, name } });
      toast('ok', 'Папка создана', name);
      await loadDir();
    } catch (err) {
      toast('err', 'Ошибка', err.message);
    }
  }

  // -------------------------------------------------------------------- upload
  // A batch is a list of files that keep their path relative to the folder the
  // user dropped or picked ("Фото/2024/кот.jpg"), so whole folder trees are
  // recreated on the server.  One request carries one file; the corner panel
  // shows a row per file, and only a compact header for very large batches.

  const UP_ROWS = 200;          // per-file rows drawn in the panel
  const UP_MAX_FILES = 20000;   // sanity cap: files *kept* from one dropped tree
  const UP_MAX_DEPTH = 32;      // a dropped tree can contain symlink loops

  function joinPath(...parts) {
    return parts
      .filter((part) => part !== null && part !== undefined && part !== '')
      .join('/')
      .replace(/\/{2,}/g, '/');
  }

  const leafName = (rel) => rel.slice(rel.lastIndexOf('/') + 1);
  const dirName = (rel) => (rel.lastIndexOf('/') < 0 ? '' : rel.slice(0, rel.lastIndexOf('/')));

  // "a, b, c и ещё 12" — a folder of 500 files must not fill the screen.
  function joinList(list, max) {
    const limit = max || 4;
    if (list.length <= limit) return list.join(', ');
    return `${list.slice(0, limit).join(', ')} и ещё ${list.length - limit}`;
  }

  // rows: [{file, rel, size, loaded, state}].  Every row owns pct/fill/meta
  // elements, but beyond UP_ROWS they are left detached: the progress is still
  // counted, only the DOM is spared.
  function upPanel(rows) {
    const title = el('div', { class: 'up-title', text: 'Загрузка' });
    const sub = el('div', { class: 'up-sub', text: 'подготовка…' });
    const close = el('button', { class: 'btn ghost icon', type: 'button', title: 'Отменить загрузку', text: '✕' });
    const list = el('div', { class: 'up-list' });
    const now = rows.length > UP_ROWS ? el('div', { class: 'up-now' }) : null;
    rows.forEach((row, index) => {
      // Compact mode: beyond UP_ROWS the file gets no DOM at all — it is still
      // sent and counted, but only the header reports the batch.
      if (index >= UP_ROWS) return;
      row.pct = el('span', { class: 'up-pct', text: '0 %' });
      row.fill = el('span');
      row.meta = el('div', { class: 'up-meta', text: fmtSize(row.size) });
      const dir = dirName(row.rel);
      row.node = el('div', { class: 'up-row waiting' }, [
        el('div', { class: 'up-row-head' }, [
          el('span', { class: 'up-name', title: row.rel, text: leafName(row.rel) }),
          row.pct,
        ]),
        dir ? el('div', { class: 'up-dir', title: dir, text: `${dir}/` }) : null,
        el('div', { class: 'up-bar' }, [row.fill]),
        row.meta,
      ]);
      list.appendChild(row.node);
    });
    if (now) list.appendChild(el('div', { class: 'up-more', text: `… ещё ${rows.length - UP_ROWS} файл(ов)` }));
    const card = el('div', { class: 'up-card' }, [
      el('div', { class: 'up-head' }, [el('div', {}, [title, sub]), close]),
      now,
      list,
    ]);
    const box = $('#uploads') || document.body;
    clear(box);
    box.appendChild(card);
    return { card, title, sub, close, now };
  }

  function upProgress(row, loaded) {
    // row.loaded feeds the batch totals even when the row has no DOM (compact mode).
    row.loaded = loaded;
    if (!row.pct) return;
    const ratio = row.size ? Math.min(1, loaded / row.size) : 0;
    row.pct.textContent = `${Math.round(ratio * 100)} %`;
    row.fill.setAttribute('style', `width:${(ratio * 100).toFixed(1)}%`);
  }

  // state: 'done' | 'error' | 'cancelled' — the row keeps its last numbers.
  function upFinish(row, state, note) {
    row.state = state;
    if (state === 'done') row.loaded = row.size;
    if (row.node) row.node.className = `up-row ${state}`;
    if (!row.pct) return;
    row.pct.textContent = state === 'done' ? '100 %' : state === 'cancelled' ? '—' : 'ошибка';
    if (state === 'done') row.fill.setAttribute('style', 'width:100%');
    row.meta.textContent = note || fmtSize(row.loaded);
  }

  // One file = one request: only its own request reports the progress of *this*
  // file, which is what fills its bar.  `slot` also carries the fixed target
  // folder and the XHR of the running request, so the batch can be cancelled.
  function sendOne(row, slot, onProgress) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append('directory', slot.directory);
      form.append('path', slot.path);
      // The part name carries the path inside the uploaded folder, so the
      // server recreates the sub-folders ("Фото/2024/кот.jpg").
      form.append('files', row.file, row.rel);
      xhr.open('POST', '/api/upload', true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-CSRF-Token', state.csrf || '');
      xhr.upload.addEventListener('progress', (ev) => {
        if (ev.lengthComputable) onProgress(ev.loaded);
      });
      xhr.addEventListener('load', () => {
        let detail = '';
        try { detail = JSON.parse(xhr.responseText || '{}').detail || ''; } catch (err) { /* not json */ }
        resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, detail });
      });
      xhr.addEventListener('error', () => resolve({ ok: false, status: 0, detail: 'Соединение прервано' }));
      xhr.addEventListener('abort', () => resolve({ ok: false, status: 0, detail: 'Отменено' }));
      slot.xhr = xhr;
      xhr.send(form);
    });
  }

  // ------------------------------------------------------------------ sources
  // Files of an <input type=file>.  With webkitdirectory the browser fills
  // webkitRelativePath with the path inside the picked folder, so the folder
  // picker needs no extra walking.
  function collectFromFileList(list) {
    const files = [];
    Array.prototype.forEach.call(list || [], (file) => {
      if (!file) return;
      files.push({ file, rel: file.webkitRelativePath || file.name });
    });
    return { files, dirs: [] };
  }

  function readEntries(reader) {
    return new Promise((resolve, reject) => {
      const all = [];
      // Chrome hands out at most 100 entries per call, so read until empty.
      const step = () => reader.readEntries((batch) => {
        if (!batch.length) { resolve(all); return; }
        all.push.apply(all, batch);
        step();
      }, reject);
      step();
    });
  }

  async function walkEntry(entry, prefix, out, depth) {
    if (out.files.length + out.dirs.length >= UP_MAX_FILES || depth > UP_MAX_DEPTH) return;
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.files.push({ file, rel: `${prefix}${entry.name}` });
      return;
    }
    if (!entry.isDirectory) return;
    const dir = `${prefix}${entry.name}`;
    const children = await readEntries(entry.createReader());
    if (!children.length) { out.dirs.push(dir); return; }     // keep empty folders
    children.sort((a, b) => a.name.localeCompare(b.name));
    for (const child of children) await walkEntry(child, `${dir}/`, out, depth + 1);
  }

  // A dropped folder is only reachable through the entries API (Chrome, Edge,
  // Firefox, Safari); the plain FileList of a folder drop has no structure.
  // The entries must be read *synchronously*: the item list dies with the event.
  async function collectFromDrop(dataTransfer) {
    const items = Array.prototype.slice.call((dataTransfer && dataTransfer.items) || []);
    const roots = [];
    const loose = [];
    for (const item of items) {
      if (item.kind !== 'file') continue;
      let entry = null;
      try { entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null; } catch (err) { entry = null; }
      if (entry) roots.push(entry);
      else {
        const file = item.getAsFile ? item.getAsFile() : null;
        if (file) loose.push({ file, rel: file.name });
      }
    }
    if (!roots.length && !loose.length) return collectFromFileList(dataTransfer && dataTransfer.files);
    const out = { files: loose, dirs: [] };
    for (const entry of roots) await walkEntry(entry, '', out, 0);
    return out;
  }

  // The empty folders of a dropped tree contain no file that could create them,
  // so they are created explicitly — idempotent, "mkdir -p" on the server.
  async function createFolders(dirs, location) {
    const created = [];
    const failed = [];
    for (const dir of dirs) {
      try {
        await api('/api/mkdir', {
          method: 'POST',
          json: { directory: location.directory, path: '', name: joinPath(location.path, dir), parents: true },
        });
        created.push(dir);
      } catch (err) {
        failed.push(`${dir}: ${err.message}`);
      }
    }
    return { created, failed };
  }

  // Sends a batch one file at a time and keeps the corner panel up to date.
  // Sequential on purpose: a home server has a single uplink, so parallel
  // requests would not finish sooner — every bar would just crawl together.
  async function uploadBatch(collection, location) {
    if (!state.directory) { toast('err', 'Каталог не выбран', 'Подключите каталог в панели'); return; }
    const files = collection.files.slice().sort((a, b) => a.rel.localeCompare(b.rel));
    const dirs = collection.dirs.slice().sort();
    const folders = dirs.length ? await createFolders(dirs, location) : { created: [], failed: [] };
    // The whole batch targets the folder that was open when it started, even if
    // the user walks away while a long upload is still running.
    const target = `${location.directory}|${location.path}`;
    if (!files.length) {
      // A dropped tree can hold nothing but empty folders.
      if (folders.created.length) {
        toast('ok', 'Папки созданы', joinList(folders.created));
        if (target === `${state.directory}|${state.path}`) await loadDir();
      } else {
        toast('err', 'Нечего загружать', folders.failed.length ? joinList(folders.failed, 2) : 'Файлы не найдены');
      }
      return;
    }

    const rows = files.map((item) => ({
      file: item.file, rel: item.rel, size: item.file.size, loaded: 0, state: 'waiting',
    }));
    const total = rows.reduce((sum, row) => sum + row.size, 0);
    const panel = upPanel(rows);
    const slot = { xhr: null, cancelled: false, directory: location.directory, path: location.path };
    const uploaded = [];
    const errors = folders.failed.slice();
    let speed = 0;
    let mark = Date.now();
    let marked = 0;
    let running = true;

    // Bytes of every row so far — the file in flight counts with what has
    // already reached the server, so the header moves with its bar.
    const bytesSent = () => rows.reduce((sum, row) => sum + row.loaded, 0);

    const summary = () => {
      // Cancelled rows are finished too: the counter must reach the total.
      const finished = rows.filter((row) => row.state !== 'waiting' && row.state !== 'running').length;
      const live = bytesSent();
      const ratio = total ? live / total : 0;
      panel.title.textContent = `Загрузка: ${finished} из ${rows.length}`;
      panel.sub.textContent = `${Math.round(ratio * 100)} % · ${fmtSize(live)} из ${fmtSize(total)}`
        + (speed ? ` · ${fmtSize(speed)}/с` : '');
    };
    summary();

    const closePanel = () => { if (panel.card.parentNode) panel.card.remove(); };
    panel.close.addEventListener('click', () => {
      if (!running) { closePanel(); return; }
      slot.cancelled = true;
      if (slot.xhr) slot.xhr.abort();
    });

    for (const row of rows) {
      if (slot.cancelled) { upFinish(row, 'cancelled', 'отменено'); continue; }
      row.state = 'running';
      if (row.node) row.node.className = 'up-row';
      if (panel.now) panel.now.textContent = row.rel;
      const result = await sendOne(row, slot, (loaded) => {
        upProgress(row, loaded);
        const now = Date.now();
        const live = bytesSent();
        const span = (now - mark) / 1000;
        if (span >= 0.5) {                       // one smoothed sample per 0.5 s
          const instant = (live - marked) / span;
          speed = speed ? speed * 0.6 + instant * 0.4 : instant;
          mark = now;
          marked = live;
        }
        summary();
      });

      if (result.ok) {
        upFinish(row, 'done');
        uploaded.push(row.rel);
      } else if (slot.cancelled) {
        upFinish(row, 'cancelled', 'отменено');
      } else {
        const note = result.detail || `не удалось загрузить (${result.status || 'нет связи'})`;
        upFinish(row, 'error', note);
        errors.push(`${row.rel}: ${note}`);
        // 401/428 are session problems: stop the batch instead of failing on
        // every remaining file.  The login screen / password dialog is opened
        // here, because this path does not go through api().
        if (result.status === 401) { showLogin('Сессия истекла — войдите заново'); slot.cancelled = true; }
        if (result.status === 428) { state.mustChange = true; openPasswordDialog(true); slot.cancelled = true; }
      }
      summary();
    }

    running = false;
    const failed = rows.filter((row) => row.state === 'error').length;
    const stopped = rows.filter((row) => row.state === 'cancelled').length;
    panel.close.setAttribute('title', 'Закрыть');
    const folderNote = folders.created.length ? ` · папок: ${folders.created.length}` : '';
    if (failed) {
      panel.title.textContent = 'Загрузка завершена с ошибками';
      panel.sub.textContent = `${uploaded.length} из ${rows.length} файлов загружено${folderNote}`;
    } else if (stopped) {
      panel.title.textContent = stopped === rows.length ? 'Загрузка отменена' : 'Загрузка отменена частично';
      panel.sub.textContent = `${uploaded.length} из ${rows.length} файлов загружено`;
    } else {
      panel.title.textContent = 'Загрузка завершена';
      panel.sub.textContent = `${rows.length} файл(ов) · ${fmtSize(total)}${folderNote}`;
    }

    if (errors.length) toast('err', 'Ошибка загрузки', joinList(errors, 2));
    if (uploaded.length) {
      toast('ok', 'Загружено', joinList(uploaded));
      // Only refresh when the upload landed where the user still is.
      if (target === `${state.directory}|${state.path}`) await loadDir();
    }
    // Nothing to read when everything went through; a failure stays on screen.
    if (!failed) setTimeout(closePanel, stopped ? 4000 : 6000);
  }

  // Files chosen through the "Загрузить" / "Папку" dialogs.
  function uploadFiles(fileList) {
    const collection = collectFromFileList(fileList);
    if (!collection.files.length) return;
    return uploadBatch(collection, { directory: state.directory, path: state.path });
  }

  // A drop can be files, folders or a mix.  `dropPath` is the folder under the
  // cursor when it was one of the listed folders, otherwise the open one.
  async function uploadDrop(dataTransfer, dropPath) {
    if (!state.directory) { toast('err', 'Каталог не выбран', 'Подключите каталог в панели'); return; }
    const location = { directory: state.directory, path: dropPath === undefined ? state.path : dropPath };
    let collection;
    try {
      collection = await collectFromDrop(dataTransfer);
    } catch (err) {
      toast('err', 'Не удалось прочитать перетаскиваемое', err && err.message);
      return;
    }
    if (!collection.files.length && !collection.dirs.length) {
      toast('info', 'Ничего не перетащено', 'Перетащите файлы или папки из проводника');
      return;
    }
    await uploadBatch(collection, location);
  }

  // -------------------------------------------------------------------- search
  let searchTimer = null;
  function onSearch() {
    clearTimeout(searchTimer);
    const value = $('#search-input').value.trim();
    searchTimer = setTimeout(async () => {
      if (!state.directory) return;
      if (value.length < 2) {
        if (state.searchMode) { state.searchMode = false; await loadDir(); }
        return;
      }
      try {
        const data = await api(`/api/search?${qs({ directory: state.directory, q: value })}`);
        state.entries = data.entries || [];
        state.searchMode = true;
        renderFiles(state.entries);
      } catch (err) {
        toast('err', 'Ошибка поиска', err.message);
      }
    }, 260);
  }

  // ------------------------------------------------------------- links manager
  async function openLinksManager() {
    const container = el('div', { class: 'links-wrap' }, el('div', { class: 'muted pad', text: 'Загрузка…' }));
    const refresh = el('button', { class: 'btn ghost', type: 'button', text: '⟳ Обновить' });
    const revokeAll = el('button', { class: 'btn danger', type: 'button', text: '⛔ Отозвать все' });
    const modal = openModal({
      title: 'Внешние ссылки',
      size: 'links',
      body: container,
      footer: [
        el('span', { class: 'muted foot-note', text: 'Пароли видны только здесь — страница доступна из локальной сети.' }),
        revokeAll,
        refresh,
        el('button', { class: 'btn ghost', type: 'button', text: 'Закрыть', onclick: () => modal.close() }),
      ],
    });
    refresh.addEventListener('click', () => renderLinks(container));
    revokeAll.addEventListener('click', async () => {
      const ok = await confirmDialog(
        'Отозвать все ссылки',
        'Все активные внешние ссылки перестанут работать. Отменить это действие нельзя.',
        'Отозвать все',
      );
      if (!ok) return;
      try {
        const data = await api('/api/share/revoke-all', { method: 'POST' });
        toast('ok', 'Ссылки отозваны', `отозвано: ${data.revoked}`);
        await renderLinks(container);
      } catch (err) {
        toast('err', 'Ошибка', err.message);
      }
    });
    await renderLinks(container);
  }

  async function renderLinks(container) {
    let data;
    try {
      data = await api('/api/share/list');
    } catch (err) {
      clear(container);
      container.appendChild(el('div', { class: 'muted pad', text: err.message }));
      return;
    }
    const links = data.links || [];
    const summary = data.summary || {};
    clear(container);

    const filter = el('input', {
      class: 'input',
      placeholder: 'Фильтр: объект, путь, ссылка…',
      autocomplete: 'off',
    });
    container.appendChild(el('div', { class: 'links-bar' }, [
      el('span', { class: 'pill', text: `всего: ${summary.total || 0}` }),
      el('span', { class: 'pill ok', text: `активных: ${summary.active || 0}` }),
      el('span', { class: 'pill', text: `скачиваний: ${summary.downloads || 0}` }),
      el('span', { class: 'links-base muted', text: summary.base_url || '' }),
      filter,
    ]));

    if (!links.length) {
      container.appendChild(el('div', { class: 'muted pad', text: 'Ссылок пока нет. Создайте её кнопкой 🔗 рядом с файлом или папкой.' }));
      return;
    }

    const table = el('table', { class: 'file-table links-table' });
    table.appendChild(el('thead', {}, el('tr', {}, [
      el('th', { text: 'Объект' }),
      el('th', { class: 'col-link', text: 'Ссылка' }),
      el('th', { text: 'Статус' }),
      el('th', { text: 'Пароль' }),
      el('th', { class: 'right col-downloads', text: 'Скачиваний' }),
      el('th', { class: 'col-date', text: 'Истекает' }),
      el('th', { class: 'right', text: '' }),
    ])));
    const tbody = el('tbody');
    table.appendChild(tbody);
    container.appendChild(table);

    const render = (needle) => {
      clear(tbody);
      const text = (needle || '').trim().toLowerCase();
      const rows = links.filter((link) => !text || [
        link.name, link.path, link.directory_name, link.host_path, link.url,
      ].filter(Boolean).join(' ').toLowerCase().indexOf(text) !== -1);
      if (!rows.length) {
        tbody.appendChild(el('tr', {}, el('td', { colspan: '7' }, el('div', { class: 'muted pad', text: 'Ничего не найдено' }))));
        return;
      }
      rows.forEach((link) => tbody.appendChild(linkRow(link, () => renderLinks(container))));
    };
    filter.addEventListener('input', () => render(filter.value));
    render('');
  }

  // One row of the links table.  `refresh` re-renders the whole list.
  function linkRow(link, refresh) {
    const where = link.host_path
      ? `${link.host_path}${link.path ? '/' + link.path : ''}`
      : link.directory_name;

    const nameCell = el('td', {}, [
      el('div', { class: 'file-name' }, [
        el('span', { class: 'ico', text: link.is_dir ? '📁' : '📄' }),
        el('span', { class: 'nm', text: link.name, title: where }),
      ]),
      el('div', { class: 'row-meta', text: `${link.directory_name} · ${where}` }),
    ]);

    const linkCell = el('td', { class: 'col-link' });
    if (link.url) {
      const token = link.url.split('/s/')[1] || '';
      linkCell.appendChild(el('div', { class: 'link-cell' }, [
        el('code', { class: 'short', text: '/s/' + token.slice(0, 12) + '…', title: link.url }),
        iconButton('📋', 'Копировать ссылку', () => copyText(link.url, 'Ссылка в буфере обмена')),
        iconButton('↗', 'Открыть в новой вкладке', () => window.open(link.url, '_blank', 'noopener')),
      ]));
    } else {
      linkCell.appendChild(el('span', { class: 'muted', text: 'недоступна' }));
    }

    let statusText = 'активна';
    let statusClass = 'ok';
    if (!link.active) {
      statusClass = 'off';
      if (link.max_downloads && link.downloads >= link.max_downloads) statusText = 'лимит исчерпан';
      else if (link.expires_at && link.expires_at * 1000 < Date.now()) statusText = 'истекла';
      else statusText = 'отозвана';
    }
    const statusCell = el('td', {}, [
      el('span', { class: 'pill ' + statusClass, text: statusText }),
      link.has_password ? el('span', { class: 'pill', text: '🔒 пароль' }) : null,
      el('div', { class: 'row-meta only-mobile', text: `${where} · ${link.url || ''}` }),
    ]);

    const passwordCell = el('td');
    if (link.password) {
      const value = el('code', { class: 'pwd', text: '••••••' });
      const reveal = iconButton('👁', 'Показать пароль', () => {
        const hiddenNow = value.textContent.indexOf('•') === 0;
        value.textContent = hiddenNow ? link.password : '••••••';
        reveal.textContent = hiddenNow ? '🙈' : '👁';
        reveal.title = hiddenNow ? 'Скрыть пароль' : 'Показать пароль';
      });
      passwordCell.appendChild(el('div', { class: 'pwd-box' }, [
        value,
        reveal,
        iconButton('📋', 'Копировать пароль', () => copyText(link.password, 'Пароль в буфере обмена')),
      ]));
    } else {
      passwordCell.appendChild(el('span', {
        class: 'muted',
        text: link.has_password ? 'недоступен (ссылка создана ранее)' : '—',
      }));
    }

    const actions = el('div', { class: 'row-actions' }, [
      link.active ? iconButton('⛔', 'Отозвать', async () => {
        const ok = await confirmDialog('Отозвать ссылку', `Ссылка на «${link.name}» перестанет работать.`, 'Отозвать');
        if (!ok) return;
        try {
          await api('/api/share/revoke', { method: 'POST', json: { id: link.id } });
          toast('ok', 'Ссылка отозвана', link.name);
          await refresh();
        } catch (err) {
          toast('err', 'Ошибка', err.message);
        }
      }) : null,
      iconButton('📋', 'Копировать ссылку', () => copyText(link.url, 'Ссылка в буфере обмена')),
    ]);

    const downloads = link.max_downloads
      ? `${link.downloads} / ${link.max_downloads}`
      : String(link.downloads);

    return el('tr', {}, [
      nameCell,
      linkCell,
      statusCell,
      passwordCell,
      el('td', { class: 'right muted col-downloads', text: downloads }),
      el('td', { class: 'col-date' }, [
        el('div', { text: fmtDate(link.expires_at) }),
        el('div', { class: 'muted', text: fmtAgo(link.expires_at) }),
      ]),
      el('td', { class: 'right' }, actions),
    ]);
  }

  // ------------------------------------------------------------ password form
  // `force` is used right after the first login with the shipped credentials:
  // the dialog cannot be dismissed until a new password is stored.
  function openPasswordDialog(force) {
    const current = el('input', { class: 'input', type: 'password', autocomplete: 'current-password', value: force ? 'admin' : '' });
    const fresh = el('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
    const repeat = el('input', { class: 'input', type: 'password', autocomplete: 'new-password' });
    const error = el('div', { class: 'hint', style: 'color:var(--danger); min-height:16px;' });
    const submit = el('button', { class: 'btn primary', type: 'button', text: force ? 'Сохранить и продолжить' : 'Сменить пароль' });

    const body = el('div', {}, [
      force ? el('div', { class: 'hint', text: 'Это первый вход с паролем по умолчанию. Задайте свой пароль — до этого панель закрыта.' }) : null,
      el('div', { class: 'field' }, [el('label', { text: 'Текущий пароль' }), current]),
      el('div', { class: 'field' }, [el('label', { text: 'Новый пароль (минимум 4 символа)' }), fresh]),
      el('div', { class: 'field' }, [el('label', { text: 'Повтор нового пароля' }), repeat]),
      error,
    ]);

    const footer = force ? [submit] : [
      el('button', { class: 'btn ghost', type: 'button', text: 'Отмена', onclick: () => modal.close() }),
      submit,
    ];
    const modal = openModal({
      title: force ? 'Смена пароля по умолчанию' : 'Смена пароля',
      size: 'narrow',
      dismissible: !force,
      body,
      footer,
    });

    const save = async () => {
      error.textContent = '';
      if (fresh.value !== repeat.value) { error.textContent = 'Пароли не совпадают'; return; }
      submit.disabled = true;
      try {
        await api('/api/password', {
          method: 'POST',
          json: { current_password: current.value, new_password: fresh.value },
        });
        state.mustChange = false;
        modal.close();
        toast('ok', 'Пароль изменён', 'Используйте новый пароль при следующем входе');
        if (force) {
          showApp();
          await loadDirectories();
        }
      } catch (err) {
        error.textContent = err.message;
      } finally {
        submit.disabled = false;
      }
    };
    submit.addEventListener('click', save);
    body.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') save(); });
    setTimeout(() => current.focus(), 40);
  }

  // -------------------------------------------------------------------- wiring
  function wire() {
    $('#login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const button = $('#login-submit');
      button.disabled = true;
      $('#login-error').textContent = '';
      try {
        const data = await api('/api/login', {
          method: 'POST',
          json: { username: $('#username').value, password: $('#password').value },
        });
        state.user = data.user;
        state.csrf = data.csrf;
        state.mustChange = Boolean(data.must_change_password);
        $('#password').value = '';
        showApp();
        if (state.mustChange) {
          openPasswordDialog(true);
          return;
        }
        await loadDirectories();
      } catch (err) {
        $('#login-error').textContent = err.message;
      } finally {
        button.disabled = false;
      }
    });

    $('#logout-btn').addEventListener('click', async () => {
      try { await api('/api/logout', { method: 'POST' }); } catch (err) { /* ignore */ }
      showLogin();
    });

    $('#add-dir-btn').addEventListener('click', () => { setNav(false); openDirectoryDialog(null); });
    $('#nav-links').addEventListener('click', () => { setNav(false); openLinksManager(); });
    $('#nav-password').addEventListener('click', () => { setNav(false); openPasswordDialog(false); });
    $('#refresh-btn').addEventListener('click', () => loadDir());
    $('#mkdir-btn').addEventListener('click', () => createFolder());
    $('#search-input').addEventListener('input', onSearch);
    $('#view-btn').addEventListener('click', toggleView);
    $('#menu-btn').addEventListener('click', () => {
      setNav(!$('#main-view').classList.contains('nav-open'));
    });
    $('#nav-backdrop').addEventListener('click', () => setNav(false));
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && $('#main-view').classList.contains('nav-open')) setNav(false);
    });

    const filesInput = $('#file-input');
    $('#upload-btn').addEventListener('click', () => filesInput.click());
    filesInput.addEventListener('change', () => { uploadFiles(filesInput.files); filesInput.value = ''; });

    // The OS folder picker: the input is a directory input, so each File already
    // carries its path inside the chosen folder (webkitRelativePath).
    const folderInput = $('#folder-input');
    $('#upload-folder-btn').addEventListener('click', () => folderInput.click());
    folderInput.addEventListener('change', () => { uploadFiles(folderInput.files); folderInput.value = ''; });

    wireDrop();
  }

  // Dropping works anywhere in the window — a folder is dropped on the window,
  // not on a specific element — and it lands in the folder under the cursor
  // when that folder is one of the listed ones (see data-drop-path below).
  function wireDrop() {
    const zone = $('#dropzone');
    let over = null;
    const hasFiles = (ev) => Boolean(ev.dataTransfer)
      && Array.prototype.indexOf.call(ev.dataTransfer.types || [], 'Files') >= 0;
    const folderUnder = (ev) => (ev.target && ev.target.closest ? ev.target.closest('[data-drop-path]') : null);
    const markRow = (node) => {
      if (over === node) return;
      if (over) over.classList.remove('drop-target');
      over = node;
      if (over) over.classList.add('drop-target');
    };
    const clear = () => { markRow(null); zone.classList.remove('dragover'); };

    document.addEventListener('dragover', (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
      const row = folderUnder(ev);
      markRow(row);
      zone.classList.toggle('dragover', !row);
    });
    // Leaving the window (no related target) clears every highlight.
    document.addEventListener('dragleave', (ev) => { if (!ev.relatedTarget) clear(); });
    document.addEventListener('dragend', clear);
    document.addEventListener('drop', (ev) => {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      const row = folderUnder(ev);
      const path = row ? row.getAttribute('data-drop-path') : state.path;
      clear();
      uploadDrop(ev.dataTransfer, path);
    });
  }

  // -------------------------------------------------------------------- boot
  async function boot() {
    state.view = storedView();
    wire();
    try {
      const me = await api('/api/me');
      if (!me.authenticated) {
        showLogin();
        return;
      }
      state.user = me.user;
      state.csrf = me.csrf;
      state.mustChange = Boolean(me.must_change_password);
      showApp();
      if (state.mustChange) {
        openPasswordDialog(true);
        return;
      }
      await loadDirectories();
    } catch (err) {
      showLogin(err.status === 403 ? 'Доступ разрешён только из локальной сети' : '');
    }
  }

  boot();











})();
