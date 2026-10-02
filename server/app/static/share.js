/* Sunduk public share view.
   Renders a single shared folder (browsable) or a single shared file.
   Everything is built with createElement/textContent — no HTML injection. */
(function () {
  'use strict';

  const KIND_ICON = {
    dir: '📁', image: '🖼️', video: '🎬', audio: '🎵', archive: '🗜️',
    pdf: '📕', doc: '📄', text: '📝', file: '📦',
  };

  const token = decodeURIComponent(location.pathname.replace(/^\/s\/?/, '').replace(/\/+$/, ''));
  const base = `/api/share/${encodeURIComponent(token)}`;

  const state = { info: null, path: '', entries: [], view: 'list' };

  const $ = (sel) => document.querySelector(sel);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach((key) => {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
        else if (value === true) node.setAttribute(key, '');
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
    return new Date(ts * 1000).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function toast(kind, title, msg) {
    const node = el('div', { class: `toast ${kind || 'info'}` }, [
      el('div', {}, [el('div', { class: 't-title', text: title }), msg ? el('div', { class: 't-msg', text: msg }) : null]),
    ]);
    $('#toasts').appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  function stage(node) {
    const box = $('#stage');
    clear(box);
    box.appendChild(node);
    return box;
  }

  function downloadUrl(path) {
    return `${base}/download?path=${encodeURIComponent(path || '')}`;
  }

  function rawUrl(path) {
    return `${base}/raw?path=${encodeURIComponent(path || '')}`;
  }

  // ------------------------------------------------------------------ rendering
  function renderError(message, detail) {
    $('#subtitle').textContent = 'Недоступно';
    stage(el('div', { class: 'card' }, [
      el('div', { class: 'empty' }, [
        el('div', { class: 'big', text: '🚫' }),
        el('div', { text: message }),
        detail ? el('div', { class: 'share-sub', text: detail }) : null,
      ]),
    ]));
  }

  function renderPassword() {
    $('#title').textContent = state.info.name;
    $('#subtitle').textContent = 'Ссылка защищена паролем';
    const input = el('input', { class: 'input', type: 'password', placeholder: 'Пароль', autocomplete: 'current-password' });
    const button = el('button', { class: 'btn primary', type: 'submit', text: 'Открыть' });
    const form = el('form', {}, [
      el('div', { class: 'field' }, [el('label', { text: 'Пароль' }), input]),
      el('div', {}, [button]),
    ]);
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      button.disabled = true;
      try {
        const res = await fetch(`${base}/unlock`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: input.value }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail || 'Неверный пароль');
        }
        await load('');
      } catch (err) {
        toast('err', 'Ошибка', err.message);
        input.value = '';
        input.focus();
      } finally {
        button.disabled = false;
      }
    });
    stage(el('div', { class: 'card' }, [el('div', { class: 'hint', text: 'Введите пароль, чтобы получить доступ к материалам.' }), form]));
    setTimeout(() => input.focus(), 60);
  }

  function renderMeta(expiresAt) {
    const box = $('#meta');
    clear(box);
    if (!expiresAt) return;
    box.appendChild(el('span', { class: 'pill', text: `⏳ до ${fmtDate(expiresAt)}` }));
  }

  function renderFile(file) {
    $('#title').textContent = state.info.name;
    $('#subtitle').textContent = `${fmtSize(file.size)} · изменён ${fmtDate(file.mtime)}`;
    const card = el('div', { class: 'card' });
    const item = { name: state.info.name, path: '', kind: file.kind, size: file.size, mtime: file.mtime };

    const openBtn = el('button', { class: 'btn', type: 'button', title: 'Открыть во весь экран', text: '⛶ Во весь экран' });
    openBtn.addEventListener('click', () => {
      FSViewer.open({ items: [item], item, urlFor: () => rawUrl(''), downloadUrlFor: () => downloadUrl('') });
    });

    if (file.kind === 'image') {
      card.appendChild(el('img', { src: rawUrl(''), alt: file.name, class: 'share-media' }));
    } else if (file.kind === 'video') {
      card.appendChild(el('video', {
        src: rawUrl(''), controls: true, playsinline: true, preload: 'metadata', class: 'share-media',
      }));
    } else if (file.kind === 'audio') {
      card.appendChild(el('audio', { src: rawUrl(''), controls: true, class: 'share-audio' }));
    } else if (file.kind === 'pdf') {
      card.appendChild(el('iframe', { src: rawUrl(''), title: file.name, class: 'share-pdf' }));
      card.appendChild(el('div', {
        class: 'viewer-note',
        text: 'Если PDF не отобразился (частая ситуация в мобильных браузерах) — скачайте его.',
      }));
    } else if (file.kind === 'text') {
      const pre = el('pre', { class: 'share-text', text: 'Загрузка…' });
      card.appendChild(pre);
      fetch(rawUrl(''), { credentials: 'same-origin' })
        .then((res) => (res.ok ? res.text() : Promise.reject(new Error(`Не удалось открыть файл (${res.status})`))))
        .then((text) => { pre.textContent = text.slice(0, 200000); })
        .catch((err) => { pre.textContent = err.message || 'Не удалось прочитать файл'; });
    }

    card.appendChild(el('div', { class: 'row-between' }, [
      el('div', { class: 'file-name' }, [
        el('span', { class: 'ico', text: KIND_ICON[file.kind] || KIND_ICON.file }),
        el('span', { class: 'nm', text: file.name }),
      ]),
      el('div', { class: 'row-actions', style: 'opacity:1;' }, [
        FSViewer.isPreviewable(file.kind) ? openBtn : null,
        el('a', { class: 'btn primary', href: downloadUrl(''), text: '⬇ Скачать' }),
      ]),
    ]));
    stage(card);
  }

  function breadcrumb() {
    const box = el('div', { class: 'breadcrumb', style: 'padding:0 0 12px;' });
    const parts = state.path ? state.path.split('/') : [];
    const root = el('span', { class: 'crumb' + (parts.length ? '' : ' current'), text: `📁 ${state.info.name}` });
    if (parts.length) root.addEventListener('click', () => load(''));
    box.appendChild(root);
    let acc = '';
    parts.forEach((part, index) => {
      acc = acc ? `${acc}/${part}` : part;
      box.appendChild(el('span', { class: 'sep', text: '›' }));
      const isLast = index === parts.length - 1;
      const crumb = el('span', { class: 'crumb' + (isLast ? ' current' : ''), text: part });
      if (!isLast) { const target = acc; crumb.addEventListener('click', () => load(target)); }
      box.appendChild(crumb);
    });
    return box;
  }

  function triggerDownload(url) {
    const link = el('a', { href: url });
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function renderDir(entries) {
    state.entries = entries;
    $('#title').textContent = state.info.name;
    $('#subtitle').textContent = entries.length ? `${entries.length} объект(ов) в общей папке` : 'Общая папка';
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'dir-bar' }, [breadcrumb(), viewToggle()]));
    if (!entries.length) {
      card.appendChild(el('div', { class: 'empty' }, [
        el('div', { class: 'big', text: '📂' }),
        el('div', { text: 'В этой папке пока пусто' }),
      ]));
      stage(card);
      return;
    }
    card.appendChild(state.view === 'grid' ? renderGrid(entries) : renderTable(entries));
    stage(card);
  }

  function viewToggle() {
    const grid = state.view === 'grid';
    const btn = el('button', {
      class: 'btn icon', type: 'button',
      title: grid ? 'Показать списком' : 'Показать плиткой',
      text: grid ? '☰' : '▦',
    });
    btn.addEventListener('click', () => {
      state.view = grid ? 'list' : 'grid';
      renderDir(state.entries);
    });
    return btn;
  }

  function openItem(entry) {
    if (entry.is_dir) return load(entry.path);
    if (FSViewer.isPreviewable(entry.kind)) return openViewer(entry);
    return triggerDownload(downloadUrl(entry.path));
  }

  function openViewer(entry) {
    FSViewer.open({
      items: state.entries,
      item: entry,
      urlFor: (it) => rawUrl(it.path),
      downloadUrlFor: (it) => downloadUrl(it.path),
    });
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
    entries.forEach((entry) => {
      const nameEl = el('span', { class: 'nm link', text: entry.name, title: entry.path });
      nameEl.addEventListener('click', () => openItem(entry));
      const actions = el('div', { class: 'row-actions' });
      if (!entry.is_dir) {
        if (FSViewer.isPreviewable(entry.kind)) {
          actions.appendChild(el('button', {
            class: 'btn ghost icon', type: 'button', title: 'Открыть', text: '👁',
            onclick: (ev) => { ev.stopPropagation(); openViewer(entry); },
          }));
        }
        actions.appendChild(el('a', { class: 'btn ghost icon', href: downloadUrl(entry.path), title: 'Скачать', text: '⬇' }));
      }
      tbody.appendChild(el('tr', {}, [
        el('td', {}, [
          el('div', { class: 'file-name' }, [
            el('span', { class: 'ico', text: KIND_ICON[entry.kind] || KIND_ICON.file }),
            nameEl,
          ]),
          el('div', { class: 'row-meta only-mobile', text: `${entry.is_dir ? 'папка' : fmtSize(entry.size)} · ${fmtDate(entry.mtime)}` }),
        ]),
        el('td', { class: 'right muted col-size', text: entry.is_dir ? '—' : fmtSize(entry.size) }),
        el('td', { class: 'muted col-date', text: fmtDate(entry.mtime) }),
        el('td', { class: 'right' }, actions),
      ]));
    });
    table.appendChild(tbody);
    return table;
  }

  function renderGrid(entries) {
    const box = el('div', { class: 'file-grid' });
    entries.forEach((entry) => {
      const tile = el('div', { class: 'tile', title: entry.path });
      const thumb = el('div', { class: 'thumb' });
      if (entry.is_dir) {
        thumb.appendChild(el('span', { class: 'glyph', text: '📁' }));
      } else if (entry.kind === 'image') {
        thumb.appendChild(el('img', { src: rawUrl(entry.path), alt: '', loading: 'lazy' }));
      } else {
        thumb.appendChild(el('span', { class: 'glyph', text: KIND_ICON[entry.kind] || KIND_ICON.file }));
      }
      tile.appendChild(thumb);
      tile.appendChild(el('div', { class: 'cap' }, [
        el('div', { class: 'nm', text: entry.name }),
        el('div', { class: 'sub', text: entry.is_dir ? 'папка' : fmtSize(entry.size) }),
      ]));
      tile.addEventListener('click', () => openItem(entry));
      box.appendChild(tile);
    });
    return box;
  }

  // ----------------------------------------------------------------------- load
  async function load(path) {
    state.path = path || '';
    let res;
    try {
      res = await fetch(`${base}?path=${encodeURIComponent(state.path)}`, { credentials: 'same-origin' });
    } catch (err) {
      renderError('Не удалось связаться с сервером');
      return;
    }
    if (res.status === 404) {
      renderError('Ссылка не найдена', 'Возможно, истёк срок действия или доступ отозван.');
      return;
    }
    if (res.status === 429) {
      renderError('Слишком много запросов', 'Подождите минуту и обновите страницу.');
      return;
    }
    if (!res.ok) {
      renderError('Не удалось открыть ссылку', `Код ответа: ${res.status}`);
      return;
    }
    const info = await res.json();
    state.info = info;
    if (info.requires_password) { renderPassword(); return; }
    renderMeta(info.expires_at);
    if (info.is_dir) renderDir(info.entries || []);
    else renderFile(info.file || {});
  }

  if (!token) {
    renderError('Некорректная ссылка', 'В адресе отсутствует токен доступа.');
  } else {
    load('');
  }
})();

