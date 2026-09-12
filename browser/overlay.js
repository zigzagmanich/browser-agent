// Рисует поверх страницы рамки с ref'ами проиндексированных элементов.
// Нужен для двух вещей: убедиться глазами, что индексация не врёт,
// и для демо-видео — видно, что именно «видит» агент.
(on) => {
  const ID = '__agent_overlay__';
  const old = document.getElementById(ID);
  if (old) old.remove();
  if (!on) return 0;

  const layer = document.createElement('div');
  layer.id = ID;
  // Метка для индексатора: свою же отладочную разметку индексировать нельзя.
  layer.setAttribute('data-agent-overlay', '1');
  Object.assign(layer.style, {
    position: 'fixed', inset: '0', zIndex: '2147483647', pointerEvents: 'none',
  });

  const palette = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#008080'];
  let n = 0;

  for (const el of document.querySelectorAll('[data-agent-ref]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.bottom < 0 || r.top > window.innerHeight) continue;

    const ref = el.getAttribute('data-agent-ref');
    const color = palette[(parseInt(ref, 10) || 0) % palette.length];

    const box = document.createElement('div');
    Object.assign(box.style, {
      position: 'fixed',
      left: r.left + 'px', top: r.top + 'px',
      width: r.width + 'px', height: r.height + 'px',
      border: '2px solid ' + color,
      boxSizing: 'border-box',
      background: color + '14',
    });

    const tag = document.createElement('div');
    tag.textContent = ref;
    Object.assign(tag.style, {
      position: 'fixed',
      left: r.left + 'px',
      top: Math.max(0, r.top - 14) + 'px',
      background: color, color: '#fff',
      font: '11px/14px ui-monospace, monospace',
      padding: '0 4px', borderRadius: '2px',
    });

    layer.appendChild(box);
    layer.appendChild(tag);
    n += 1;
  }

  document.body.appendChild(layer);
  return n;
}
