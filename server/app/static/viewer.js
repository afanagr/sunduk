/* Sunduk media viewer — shared by the admin SPA (app.js) and the public
   share page (share.js).

   Opens a full-screen lightbox for every popular format the browser can show:
   images (jpg/png/gif/webp/avif/bmp...), video (mp4/m4v/mov/webm/mkv/ogv...),
   audio (mp3/m4a/flac/ogg/opus/wav...), PDF (built-in viewer) and plain text.
   Video/audio are streamed by the backend with HTTP range support, so seeking
   works without downloading the whole file.

   Everything is built with createElement/textContent (no HTML injection), so it
   is safe with the strict CSP used by both applications. */
window.FSViewer = (function () {
  'use strict';

  const PREVIEW_KINDS = ['image', 'video', 'audio', 'pdf', 'text'];
  const TEXT_LIMIT = 200000;      // characters rendered for a text preview

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
    return new Date(ts * 1000).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function isPreviewable(kind) { return PREVIEW_KINDS.indexOf(kind) !== -1; }

  /**
   * open({
   *   items:    [{name, path, kind, size, mtime}, ...]   // current listing
   *   item:     entry that was clicked (or the first previewable one)
   *   urlFor:   (item) => inline URL  (Content-Disposition: inline)
   *   downloadUrlFor: (item) => attachment URL (optional, defaults to urlFor)
   * })
   */
  function open(opts) {
    const options = opts || {};
    const all = options.items || [];
    const items = all.filter((it) => it && !it.is_dir && isPreviewable(it.kind));
    if (!items.length) return null;
    let index = items.indexOf(options.item);
    if (index < 0) index = 0;

    const urlFor = options.urlFor;
    const downloadUrlFor = options.downloadUrlFor || urlFor;

    const title = el('div', { class: 'nm' });
    const subtitle = el('div', { class: 'sub' });
    const stage = el('div', { class: 'viewer-stage' });
    const counter = el('span', { class: 'viewer-count' });
    const downloadLink = el('a', { class: 'btn', title: 'Скачать файл', text: '⬇ Скачать' });
    const openLink = el('a', { class: 'btn ghost', target: '_blank', rel: 'noopener',
                               title: 'Открыть в новой вкладке', text: '↗ Открыть' });

    const prevBtn = el('button', { class: 'viewer-nav prev', type: 'button', title: 'Предыдущий (←)', text: '‹' });
    const nextBtn = el('button', { class: 'viewer-nav next', type: 'button', title: 'Следующий (→)', text: '›' });
    prevBtn.addEventListener('click', (ev) => { ev.stopPropagation(); step(-1); });
    nextBtn.addEventListener('click', (ev) => { ev.stopPropagation(); step(1); });

    const closeBtn = el('button', { class: 'btn ghost icon', type: 'button', title: 'Закрыть (Esc)', text: '✕' });
    const head = el('div', { class: 'viewer-head' }, [
      el('div', { class: 'viewer-title' }, [title, subtitle]),
      closeBtn,
    ]);
    const foot = el('div', { class: 'viewer-foot' }, [
      counter,
      el('div', { class: 'spacer' }),
      downloadLink,
      openLink,
    ]);
    const root = el('div', { class: 'viewer' }, [
      head,
      el('div', { class: 'viewer-body' }, [stage, prevBtn, nextBtn]),
      foot,
    ]);

    function render() {
      const item = items[index];
      title.textContent = item.name;
      const bits = [];
      if (item.size) bits.push(fmtSize(item.size));
      if (item.mtime) bits.push('изменён ' + fmtDate(item.mtime));
      subtitle.textContent = bits.join(' · ');
      counter.textContent = items.length > 1 ? `${index + 1} / ${items.length}` : '';
      const many = items.length > 1;
      prevBtn.disabled = !many;
      nextBtn.disabled = !many;
      prevBtn.classList.toggle('hidden', !many);
      nextBtn.classList.toggle('hidden', !many);
      const url = urlFor(item);
      downloadLink.setAttribute('href', downloadUrlFor(item));
      openLink.setAttribute('href', url);
      clear(stage);

      const fail = (message) => {
        clear(stage);
        stage.appendChild(el('div', { class: 'viewer-msg' }, [
          el('div', { class: 'big', text: '🎞️' }),
          el('div', { text: message || 'Браузер не может показать этот файл' }),
          el('div', { class: 'viewer-actions' }, [
            el('a', { class: 'btn primary', href: downloadUrlFor(item), text: '⬇ Скачать' }),
            el('a', { class: 'btn', href: url, target: '_blank', rel: 'noopener', text: '↗ Открыть в новой вкладке' }),
          ]),
        ]));
      };

      if (item.kind === 'image') {
        const img = el('img', {
          src: url, alt: item.name, title: 'Нажмите, чтобы увеличить',
          onerror: () => fail('Формат изображения не поддерживается браузером'),
        });
        img.addEventListener('click', () => img.classList.toggle('zoom'));
        stage.appendChild(img);
      } else if (item.kind === 'video') {
        stage.appendChild(el('video', {
          src: url, controls: true, autoplay: true, playsinline: true, preload: 'metadata',
          onerror: () => fail('Видео не воспроизводится в этом браузере — скачайте файл'),
        }));
      } else if (item.kind === 'audio') {
        stage.appendChild(el('div', { class: 'audio-stage' }, [
          el('div', { class: 'big', text: '🎵' }),
          el('audio', { src: url, controls: true, autoplay: true, preload: 'metadata',
                        onerror: () => fail('Аудио не воспроизводится в этом браузере — скачайте файл') }),
        ]));
      } else if (item.kind === 'pdf') {
        stage.appendChild(el('iframe', { src: url, title: item.name }));
        stage.appendChild(el('div', {
          class: 'viewer-note',
          text: 'Если PDF не отобразился (частая ситуация в мобильных браузерах) — скачайте его.',
        }));
      } else {
        stage.appendChild(el('div', { class: 'viewer-msg', text: 'Загрузка…' }));
        fetch(url, { credentials: 'same-origin' })
          .then((res) => {
            if (!res.ok) {
              throw new Error(res.status === 413
                ? 'Файл слишком большой для предпросмотра'
                : `Не удалось открыть файл (${res.status})`);
            }
            return res.text();
          })
          .then((text) => {
            clear(stage);
            const truncated = text.length > TEXT_LIMIT;
            stage.appendChild(el('pre', { text: truncated ? text.slice(0, TEXT_LIMIT) : text }));
            if (truncated) {
              stage.appendChild(el('div', {
                class: 'viewer-note',
                text: 'Показаны первые 200 000 символов — скачайте файл, чтобы увидеть остальное.',
              }));
            }
          })
          .catch((err) => {
            clear(stage);
            stage.appendChild(el('div', { class: 'viewer-msg' }, [
              el('div', { text: err.message || 'Не удалось прочитать файл' }),
              el('div', { class: 'viewer-actions' }, [
                el('a', { class: 'btn primary', href: downloadUrlFor(item), text: '⬇ Скачать' }),
              ]),
            ]));
          });
      }
    }

    function step(delta) {
      if (items.length < 2) return;
      index = (index + delta + items.length) % items.length;
      render();
    }

    function close() {
      document.removeEventListener('keydown', onKey);
      root.remove();
      if (options.onClose) options.onClose();
    }

    function onKey(ev) {
      if (ev.key === 'Escape') close();
      else if (ev.key === 'ArrowLeft') step(-1);
      else if (ev.key === 'ArrowRight') step(1);
    }

    closeBtn.addEventListener('click', close);
    stage.addEventListener('click', (ev) => { if (ev.target === stage) close(); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(root);
    render();
    return { close, next: () => step(1), prev: () => step(-1), current: () => items[index] };
  }

  return { open, isPreviewable, fmtSize, fmtDate };
})();
