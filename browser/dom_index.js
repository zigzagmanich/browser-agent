// Инжектится в каждый фрейм. Обходит DOM (включая shadow DOM), находит
// видимые интерактивные элементы, помечает их data-agent-ref и возвращает
// линеаризованный поток узлов: текст + элементы в порядке чтения.
//
// Никаких знаний о конкретных сайтах здесь нет — только общие правила
// «что такое интерактивный элемент» и «что такое видимый элемент».

(startIndex) => {
  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea', 'summary', 'option', 'audio', 'video'
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'tab', 'switch', 'textbox', 'combobox', 'searchbox',
    'option', 'slider', 'spinbutton', 'treeitem', 'listbox'
  ]);

  // Составные элементы списка: строка таблицы, пункт списка, карточка.
  // Индексируются целиком, одним рефом — иначе одна запись списка
  // разваливается на пять-шесть бесполезных ячеек.
  const ITEM_ROLES = new Set(['row', 'listitem', 'article', 'option', 'treeitem']);

  // Что остаётся видимым внутри составного элемента: только настоящие
  // органы управления. Ячейки с текстом схлопываются в имя самого элемента.
  const STRONG_TAGS = new Set(['a', 'button', 'input', 'select', 'textarea']);
  const STRONG_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'switch', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'textbox', 'combobox', 'searchbox'
  ]);

  const SKIP_TAGS = new Set(['script', 'style', 'noscript', 'svg', 'path', 'head', 'meta', 'link']);

  const MAX_NAME = 120;
  const MAX_ITEM_NAME = 160;

  // Что считается органом управления при взгляде «снаружи» элемента.
  const CONTROL_SELECTOR =
    'a[href],button,input,select,textarea,[role="button"],[role="link"],[onclick]';

  // Кликабельная карточка — это заголовок-ссылка плюс несколько действий и
  // ссылок на автора/раздел. Больше — уже контейнер списка, схлопывать нельзя.
  const MAX_ITEM_CONTROLS = 12;

  let idx = startIndex;
  const nodes = [];
  let itemSeen = null; // ключ контрола карточки -> его узел — против дублей
  let occluded = 0;    // сколько контролов на экране закрыто другим слоем
  let cellSigs = [];   // стек: сигнатуры ячеек открытых сеток (календарь и т. п.)

  // Убираем метки предыдущего снапшота — рефы живут ровно один снапшот.
  for (const el of document.querySelectorAll('[data-agent-ref]')) {
    el.removeAttribute('data-agent-ref');
  }

  // Невидимые символы: пробелы нулевой ширины, мягкий перенос, пустой символ
  // Брайля и т. п. Рассылки забивают ими превью, чтобы клиент не подтянул
  // следующий текст. Смысла ноль, а в подписи на 160 символов они занимали до
  // половины места — платно и в ущерб настоящему тексту.
  const INVISIBLE = /[\u00AD\u034F\u115F\u1160\u180E\u200B-\u200F\u2060-\u2064\u2800\u3164\uFEFF]/g;
  const clean = (s) => (s || '').replace(INVISIBLE, '').replace(/\s+/g, ' ').trim();
  const trunc = (s, n) => (s.length > n ? s.slice(0, n) + '…' : s);

  // Три состояния, а не два. «Нет бокса» — не то же самое, что «не видно»:
  // у display:contents и у портальной обёртки нулевого размера собственного
  // прямоугольника нет, а потомки с position:fixed прекрасно видны. Модалки
  // часто рендерятся именно так — и раньше вырезались из индекса целиком.
  //   'hidden'  — не видно ни элемента, ни потомков: пропускаем поддерево;
  //   'boxless' — самого элемента нет, потомков обходим;
  //   'visible' — обычный случай.
  // Прозрачный нативный контрол поверх нарисованного: настоящий
  // <input type=checkbox> с opacity:0 лежит над стилизованным квадратиком, и
  // клики получает именно он. Так делают чекбоксы, радио, загрузку файлов.
  // Раньше такой контрол выпадал как «невидимый» — агент не видел чекбоксов
  // выбора писем. Прозрачность не делает контрол недоступным, пока он
  // принимает клики. Только элементы формы: кнопки, появляющиеся при
  // наведении, по-прежнему не индексируются — иначе шум в каждой строке.
  function transparentControl(el, style) {
    if (style.pointerEvents === 'none') return false;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    return tag === 'input' || tag === 'select' || tag === 'textarea' ||
      role === 'checkbox' || role === 'radio' || role === 'switch';
  }

  function visibility(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return 'hidden';
    if (parseFloat(style.opacity) === 0 && !transparentControl(el, style)) return 'hidden';
    const r = el.getBoundingClientRect();
    if (r.width >= 1 && r.height >= 1) return 'visible';
    if (style.display === 'contents') return 'boxless';
    // Нулевой бокс, который обрезает содержимое, — это свёрнутый блок
    // (аккордеон, выпадашка на height:0): его потомков действительно не видно.
    const clips = /hidden|clip|scroll|auto/.test(style.overflowX + ' ' + style.overflowY);
    return clips ? 'hidden' : 'boxless';
  }

  // Для текстового узла нет getBoundingClientRect — меряем через Range.
  // Текст ниже экрана в снапшот не идёт: кликнуть по нему нельзя, а прочитать
  // можно после скролла.
  function textInViewport(node) {
    try {
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width < 1 && r.height < 1) return false;
      return r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
    } catch (e) {
      return true;
    }
  }

  // Текст под модалкой или липкой шапкой не читается глазами. Перекрытием
  // считаем только слой с position fixed/sticky: elementFromPoint не видит
  // элементов с pointer-events:none, и подпись поверх картинки иначе сочлась
  // бы перекрытой самой картинкой.
  function textOccluded(node) {
    try {
      const host = node.parentElement;
      if (!host) return false;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      const x = Math.min(Math.max(r.left + r.width / 2, 1), window.innerWidth - 1);
      const y = Math.min(Math.max(r.top + r.height / 2, 1), window.innerHeight - 1);
      const top = document.elementFromPoint(x, y);
      if (!top || host.contains(top) || top.contains(host)) return false;
      for (let e = top, i = 0; e && i < 8; e = e.parentElement, i++) {
        const pos = window.getComputedStyle(e).position;
        if (pos === 'fixed' || pos === 'sticky') return true;
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
  }

  // Элемент в пределах окна, но обрезан прокручиваемым контейнером: дни
  // календаря ниже его края, пункты длинного выпадающего списка. Это не
  // перекрытие, а «нужен скролл»: раньше такие элементы шли как перекрытые и
  // выпадали из снапшота целиком — агент не знал, что их можно докрутить.
  // Только настоящие контейнеры прокрутки (auto/scroll): карусель с
  // overflow:hidden листается кнопками, а не скроллом.
  function clippedByScroller(el) {  // -> обрезающий блок или null
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    for (let a = el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
      if (a.scrollHeight <= a.clientHeight + 1 && a.scrollWidth <= a.clientWidth + 1) continue;
      const cs = window.getComputedStyle(a);
      if (!/auto|scroll/.test(cs.overflowX + ' ' + cs.overflowY)) continue;
      const b = a.getBoundingClientRect();
      if (cy < b.top || cy > b.bottom || cx < b.left || cx > b.right) return a;
    }
    return null;
  }

  // Перекрыт ли элемент другим (модалка, оверлей, cookie-баннер).
  // Только для того, что на экране: для уехавшего за вьюпорт elementFromPoint
  // вернёт что-то из шапки, и элемент ошибочно сочтётся перекрытым.
  function isOccluded(el) {
    try {
      if (!inViewport(el)) return false;
      // Обрезанный краем блока проверяем через сам блок: лента категорий под
      // открытым окном блюда тоже недоступна, хоть и «за краем».
      const clip = clippedByScroller(el);
      if (clip) return isOccluded(clip);
      const r = el.getBoundingClientRect();
      const x = Math.min(Math.max(r.left + r.width / 2, 1), window.innerWidth - 1);
      // Несколько точек по высоте: элемент, наполовину ушедший под липкую
      // шапку, кликабелен — по центру он перекрыт, по трети высоты нет.
      for (const k of [0.5, 0.25, 0.75]) {
        const y = Math.min(Math.max(r.top + r.height * k, 1), window.innerHeight - 1);
        const top = document.elementFromPoint(x, y);
        if (!top) continue;
        if (el.contains(top) || top.contains(el)) return false;
      }
      return true;
    } catch (e) {
      return false;
    }
  }

  // Спрятанный контрол — не орган управления. Кастомный выпадающий список
  // держит внутри <input type=hidden> или <select> с display:none, а кликают
  // по обёртке с курсором-рукой. Раньше такой «контрол» внутри делал обёртку
  // некликабельной, а подпись унаследовала курсор и тоже не считалась
  // кнопкой — кнопка «Сортировка» выпадала из индекса целиком.
  function hasControls(el) {
    for (const c of el.querySelectorAll(CONTROL_SELECTOR)) {
      if (c.tagName === 'INPUT' && c.type === 'hidden') continue;
      const shown = c.checkVisibility
        ? c.checkVisibility({ visibilityProperty: true })
        : c.getClientRects().length > 0;
      if (shown) return true;
    }
    return false;
  }

  // cursor — наследуемое свойство. У каждого потомка кликабельной карточки
  // computed cursor === 'pointer', поэтому проверка computed-значения ловит
  // подписи, адреса и рейтинги, а не саму карточку. Носитель курсора — тот,
  // у кого значение отличается от родительского.
  function ownsPointer(el) {
    if (window.getComputedStyle(el).cursor !== 'pointer') return false;
    const parent = el.parentElement;
    if (!parent) return true;
    return window.getComputedStyle(parent).cursor !== 'pointer';
  }

  // Сетка одинаковых ячеек внутри «кнопки»: календарь дат, схема мест, выбор
  // размера. День — голый <div> с курсором-рукой, без роли и tabindex, а весь
  // календарь размечен как один role=button. Индексатор внутрь органа
  // управления не спускался, и месяц склеивался в одну кнопку «ПН ВТ … 1 2 3 …
  // 30» — выбрать дату было нельзя. Признак сетки общий: семь и больше
  // листовых кликабельных ячеек одного тега и одного размера с коротким
  // текстом. У карточки такого нет — заголовок, цена и описание разного размера.
  const cellSig = (el) => {
    const r = el.getBoundingClientRect();
    return el.tagName + '|' + Math.round(r.width) + 'x' + Math.round(r.height);
  };

  function isLeafCell(el) {
    if ([...el.children].some((c) => clean(c.innerText))) return false;
    const t = clean(el.innerText);
    return t.length > 0 && t.length <= 12 && window.getComputedStyle(el).cursor === 'pointer';
  }

  function uniformCells(el) {
    const all = el.getElementsByTagName('*');
    if (all.length <= 20) return null; // обычная кнопка столько не содержит
    const groups = new Map();
    for (const d of all) {
      if (!isLeafCell(d)) continue;
      const s = cellSig(d);
      groups.set(s, (groups.get(s) || 0) + 1);
    }
    const sigs = new Set([...groups].filter(([, n]) => n >= 7).map(([s]) => s));
    return sigs.size ? sigs : null;
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    // Ячейка открытой сетки: курсор у неё унаследован, ownsPointer её не видит.
    const grid = cellSigs[cellSigs.length - 1];
    if (grid && isLeafCell(el) && grid.has(cellSig(el))) return true;
    // <a> без href — не всегда пустышка: кнопку «Сортировка» делают ссылкой
    // без адреса с обработчиком клика в скрипте. Раньше такая ссылка
    // отбрасывалась сразу, до проверки роли, tabindex и курсора. Теперь она
    // проходит общие проверки ниже; якорь <a name> без курсора их не пройдёт.
    const bareAnchor = tag === 'a' && !el.hasAttribute('href') && !el.hasAttribute('onclick');
    if (INTERACTIVE_TAGS.has(tag) && !bareAnchor) {
      if (tag === 'input' && el.type === 'hidden') return false;
      return true;
    }
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.isContentEditable) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && parseInt(ti, 10) >= 0) return true;
    // Кастомные кликабельные div'ы: сам объявил cursor:pointer и внутри нет
    // настоящих контролов (иначе кликать надо по ним, а не по обёртке).
    if (ownsPointer(el) && !hasControls(el)) return true;
    return false;
  }

  // Строка списка/карточка: индексируем целиком, даже если внутри есть кнопки.
  // Два признака, оба общие: явная роль записи — либо собственный
  // cursor:pointer при небольшом числе контролов внутри.
  // Обёртка-одиночка структуру не меняет, а сравнивать сигнатуры соседей мешает:
  // у элемента ровно один ребёнок, и сравнивать не с чем. Спускаемся сквозь неё.
  function contentRoot(el) {
    let node = el;
    for (let i = 0; i < 4 && node.children.length === 1; i++) node = node.children[0];
    return node;
  }

  // Запись не состоит из таких же записей. Если среди соседей три и больше
  // однотипных (тег + набор классов) и каждый из них сам — орган управления или
  // запись, перед нами контейнер списка: карусель, сетка карточек, лента.
  // Схлопывать его нельзя: имя склеится из нескольких записей, а подписи
  // вложенных карточек срежутся как «дубль строки» — в индексе останутся
  // безымянные обрубки.
  //
  // Важно, что дети должны быть контролами сами, а не просто содержать их:
  // ячейки строки и блоки карточки тоже бывают однотипными, но кликают не по
  // ним. Сравниваются сигнатуры соседей между собой — знания о классах
  // конкретного сайта тут нет.
  function looksLikeList(el) {
    const seen = new Map();
    for (const child of contentRoot(el).children) {
      // Карточка бывает завёрнута в слот — у ребёнка тоже снимаем обёртки.
      const core = contentRoot(child);
      const role = (core.getAttribute('role') || '').toLowerCase();
      if (!isStrongControl(core) && !ITEM_ROLES.has(role)) continue;
      // Ключевое отличие вложенной записи от кнопки действия: внутри записи
      // есть свои органы управления, у кнопки «Скрыть» внутри нет ничего.
      // Без этой проверки парой одинаковых кнопок в карточке можно было бы
      // объявить карточку списком.
      const inner = core.querySelectorAll(CONTROL_SELECTOR).length;
      if (inner < 1) continue;
      // Сигнатура по форме, а не по классам: у соседей-карточек классы бывают
      // разными (модификаторы, хеши CSS-модулей), а форма одинаковая.
      const key = core.tagName + '|' + role + '|' + inner;
      const n = (seen.get(key) || 0) + 1;
      // Двух хватает: последняя страница карусели бывает неполной.
      if (n >= 2) return true;
      seen.set(key, n);
    }
    return false;
  }

  // Карточка в сетке часто не имеет ни роли, ни собственного курсора: курсор
  // на самих ссылках. Но все ссылки внутри ведут по одному адресу — картинка,
  // заголовок и бейдж указывают на одну сущность. Это и есть признак записи.
  function wrapsSingleTarget(el) {
    const links = el.querySelectorAll('a[href]');
    if (links.length < 2) return false;
    const first = links[0].getAttribute('href');
    for (const a of links) {
      if (a.getAttribute('href') !== first) return false;
    }
    return true;
  }

  function isItem(el) {
    const role = (el.getAttribute('role') || '').toLowerCase();
    const explicit = ITEM_ROLES.has(role);
    const tag = el.tagName.toLowerCase();
    // <article> — по спеке самодостаточная запись; для неё собственный курсор
    // не обязателен, карточка может быть некликабельной целиком.
    if (!explicit && tag !== 'article' && !ownsPointer(el) && !wrapsSingleTarget(el)) {
      return false;
    }
    if (looksLikeList(el)) return false;
    // Явной роли записи верим: её проставил автор разметки.
    if (explicit || tag === 'tr' || tag === 'li') return true;
    // Запись не бывает выше экрана: то, что выше, — контейнер списка.
    if (el.getBoundingClientRect().height > window.innerHeight) return false;
    const controls = el.querySelectorAll(CONTROL_SELECTOR).length;
    return controls >= 1 && controls <= MAX_ITEM_CONTROLS;
  }

  function isStrongControl(el) {
    const tag = el.tagName.toLowerCase();
    if (STRONG_TAGS.has(tag)) {
      if (tag === 'a' && !el.hasAttribute('href') && !el.hasAttribute('onclick')) return false;
      if (tag === 'input' && el.type === 'hidden') return false;
      return true;
    }
    if (STRONG_ROLES.has((el.getAttribute('role') || '').toLowerCase())) return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // Поле ввода без подписи всё равно полезно — по нему можно печатать.
  function isFormControl(el) {
    const tag = el.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
  }

  // Одна и та же фраза в карточке часто повторена: заголовок, скрытая подпись
  // для скринридера, дубль в ссылке. Модели хватит одного раза, а повтор
  // съедает лимит имени и вытесняет цену: в строке ниже экрана оставалось одно
  // название дважды. Удаляем повтор фразы из трёх и более слов.
  function dropRepeats(text) {
    const words = text.split(' ');
    const out = [];
    let seen = ' ';
    let i = 0;
    while (i < words.length) {
      let skip = 0;
      for (let k = Math.min(12, words.length - i); k >= 3; k--) {
        if (seen.includes(' ' + words.slice(i, i + k).join(' ') + ' ')) {
          skip = k;
          break;
        }
      }
      if (skip) {
        i += skip;
      } else {
        out.push(words[i]);
        seen += words[i] + ' ';
        i++;
      }
    }
    return out.join(' ');
  }

  function accessibleName(el, long) {
    const tag = el.tagName.toLowerCase();
    const LIMIT = long ? MAX_ITEM_NAME : MAX_NAME;
    const byId = (ids) =>
      clean(
        (ids || '')
          .split(/\s+/)
          .map((id) => (document.getElementById(id) || {}).innerText || '')
          .join(' ')
      );

    let name =
      clean(el.getAttribute('aria-label')) ||
      byId(el.getAttribute('aria-labelledby')) ||
      clean(el.getAttribute('alt')) ||
      clean(el.getAttribute('placeholder')) ||
      '';

    if (!name && tag === 'input') {
      const id = el.getAttribute('id');
      if (id) {
        const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (lbl) name = clean(lbl.innerText);
      }
      if (!name && el.closest('label')) name = clean(el.closest('label').innerText);
      if (!name) name = clean(el.getAttribute('name'));
    }
    if (!name) {
      const text = clean(el.innerText);
      // Составной записи — без повторов; её имя длинное, повтор бьёт больнее.
      name = (long ? dropRepeats(text.slice(0, 600)) : text).slice(0, LIMIT);
    }
    if (!name) name = clean(el.getAttribute('title'));
    // Иконочная кнопка: текста нет, но подпись бывает у вложенной картинки или
    // у <title> внутри svg. Это стандартные места, а не разметка конкретного сайта.
    if (!name) {
      const inner = el.querySelector('[aria-label],img[alt],svg title,[title]');
      if (inner) {
        name = clean(
          inner.getAttribute('aria-label') ||
          inner.getAttribute('alt') ||
          inner.getAttribute('title') ||
          inner.textContent
        );
      }
    }
    // value='on' у чекбокса и радио — умолчание HTML, а не подпись.
    const valueIsDefault = tag === 'input' && (el.type === 'checkbox' || el.type === 'radio');
    if (!name && el.value && !valueIsDefault) name = clean(String(el.value));
    return trunc(name, LIMIT);
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const attrs = {};
    const role = el.getAttribute('role');
    if (role) attrs.role = role;
    if (tag === 'input' || tag === 'textarea') {
      if (el.type) attrs.type = el.type;
      // У чекбокса и радио value — служебное (по умолчанию 'on'), важен checked.
      const toggle = el.type === 'checkbox' || el.type === 'radio';
      if (el.value && el.type !== 'password' && !toggle) attrs.value = trunc(clean(String(el.value)), 60);
      if (el.checked !== undefined && (el.type === 'checkbox' || el.type === 'radio')) {
        attrs.checked = String(el.checked);
      }
      if (el.required) attrs.required = 'true';
    }
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      if (href && !href.startsWith('javascript:')) attrs.href = trunc(href, 90);
    }
    if (tag === 'select') {
      attrs.options = Array.from(el.options)
        .slice(0, 25)
        .map((o) => clean(o.text))
        .join(' | ');
      if (el.value) attrs.selected = clean(String(el.value));
    }
    // Нарисованный переключатель (<label role=checkbox>, <div role=switch>)
    // хранит состояние не в checked, а в aria-атрибуте или в спрятанном
    // <input>, к которому привязан. Без этого модель не видит, включён ли
    // фильтр, и нажимает его второй раз — выключая.
    if (!attrs.checked) {
      const aria = el.getAttribute('aria-checked');
      if (aria) attrs.checked = aria;
      else if (tag === 'label' && el.control &&
               (el.control.type === 'checkbox' || el.control.type === 'radio')) {
        attrs.checked = String(el.control.checked);
      }
    }
    const pressed = el.getAttribute('aria-pressed');
    if (pressed) attrs.pressed = pressed;
    const selected = el.getAttribute('aria-selected');
    if (selected && tag !== 'select') attrs.selected = selected;
    const expanded = el.getAttribute('aria-expanded');
    if (expanded) attrs.expanded = expanded;
    if (el.disabled) attrs.disabled = 'true';
    return { tag, attrs };
  }

  // Контроль внутри строки часто повторяет её же текст (чекбокс выбора,
  // ссылка на тему письма). Реф нужен — подпись дублировать незачем.
  // Внутри карточки один и тот же переход часто продублирован: логотип и
  // текстовая ссылка на того же работодателя, заголовок и обёртка. Второй
  // реф агенту ничего не добавляет.
  function dedupKey(el, name) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') {
      const href = el.getAttribute('href');
      if (href) return 'href:' + href;
    }
    if (!name) return null;
    return tag + ':' + (el.getAttribute('role') || '') + ':' + name.toLowerCase();
  }

  // Короткая подпись («Откликнуться», «В корзину») почти всегда входит в
  // текст карточки — текст карточки включает надписи её кнопок. Раньше это
  // считалось повтором, и кнопка отклика шла безымянной ссылкой. Короткая
  // подпись — повтор, только если карточка с неё начинается: это заголовок.
  function repeatsItem(name, itemName) {
    if (!name || !itemName) return false;
    const a = name.toLowerCase();
    const b = itemName.toLowerCase();
    const probe = a.slice(0, 25);
    if (probe.length < 12) return false;
    if (probe.length < 25) return b.startsWith(probe);
    return b.includes(probe) || a.includes(b.slice(0, 25));
  }

  function emit(node, depth, long, itemName) {
    let name = accessibleName(node, long);
    const insideItem = itemName !== null && itemName !== undefined;

    // Подпись, повторяющую строку, срезаем — но элемент остаётся: это как раз
    // ссылка-заголовок записи, главный переход внутрь. Отличать «имени не было
    // вовсе» от «имя срезано как дубль» обязательно, иначе карточка теряет
    // ссылку на саму себя.
    let blanked = false;
    if (insideItem && repeatsItem(name, itemName)) {
      name = '';
      blanked = true;
    }

    // Безымянный элемент агенту бесполезен: сослаться на «ничего» он не может,
    // а индекс замусоривается пустыми ячейками и обёртками, унаследовавшими
    // cursor:pointer. Но настоящий орган управления — кнопка, чекбокс, ссылка —
    // остаётся: иконка без подписи всё равно кликабельна, а без неё нельзя ни
    // изменить количество товара, ни удалить строку.
    if (!name && !blanked && !isStrongControl(node) && !isFormControl(node)) return null;

    // Безымянная ссылка внутри записи — логотип или картинка: основной переход
    // у записи уже есть, а безымянный дубль его только повторяет. Кнопке без
    // подписи замены нет (минус, удалить, в избранное) — её оставляем.
    const linkish =
      node.tagName.toLowerCase() === 'a' ||
      (node.getAttribute('role') || '').toLowerCase() === 'link';
    if (!name && !blanked && insideItem && linkish) return null;

    if (itemSeen) {
      const key = dedupKey(node, name);
      if (key) {
        const first = itemSeen.get(key);
        if (first) {
          // Первой по порядку часто идёт картинка-галерея («Ещё 13 фото»), а
          // заголовок записи — вторым, с тем же адресом. Дубль выбрасываем, но
          // подпись галереи с первого рефа снимаем: иначе основной переход в
          // запись выглядит как «открыть фото». Имя записи и так рядом.
          if (blanked) first.name = '';
          return null;
        }
        itemSeen.set(key, null);
      }
    }

    const ref = String(idx++);
    node.setAttribute('data-agent-ref', ref);
    const clipped = inViewport(node) && clippedByScroller(node) !== null;
    const d = describe(node);
    // Полный абсолютный адрес ссылки — не для модели (в снапшоте href урезан),
    // а для проверки переходов: navigate пускает только по ссылкам со страницы.
    let url;
    if (d.tag === 'a' && node.getAttribute('href')) {
      try { url = new URL(node.getAttribute('href'), document.baseURI).href; } catch (e) {}
    }
    const entry = {
      type: 'element',
      ref,
      tag: d.tag,
      attrs: d.attrs,
      url,
      name,
      viewport: inViewport(node) && !clipped,
      // Обрезан краем блока на экране — докручивается scroll по рефу. Такие
      // идут первыми в разделе «вне видимой области»: они ближе всего.
      clipped: clipped || undefined,
      depth,
      // К какой записи относится контрол. Безымянной иконке это единственный
      // контекст: security-слою иначе не понять, что «кнопка без подписи»
      // лежит в строке корзины, а не в шапке сайта.
      item: insideItem ? trunc(itemName, 80) : undefined
    };
    nodes.push(entry);
    if (itemSeen) {
      const key = dedupKey(node, name);
      if (key) itemSeen.set(key, entry);
    }
    return name;
  }

  // itemName — текст строки списка, внутри которой мы находимся (или null).
  function walk(root, depth, itemName) {
    const insideItem = itemName !== null;
    const children = root.shadowRoot
      ? [...root.shadowRoot.childNodes, ...root.childNodes]
      : root.childNodes;

    for (const node of children) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (insideItem) continue; // текст строки уже вошёл в её имя
        const t = clean(node.textContent);
        if (t) {
          const vp = textInViewport(node);
          // Кнопки под модалкой уже скрыты; если оставить их текст, агент увидит
          // «страницу», по которой нечего нажать, и не поймёт, что поверх окно.
          if (vp && textOccluded(node)) continue;
          nodes.push({ type: 'text', value: trunc(t, 400), depth, viewport: vp });
        }
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;

      const tag = node.tagName.toLowerCase();
      if (SKIP_TAGS.has(tag)) continue;
      // Собственная отладочная подсветка: её номера рефов иначе попадают в
      // снапшот сырыми текстовыми узлами и портят замеры.
      if (node.id === '__agent_overlay__' || node.hasAttribute('data-agent-overlay')) continue;
      if (tag === 'iframe' || tag === 'frame') {
        if (!insideItem) {
          nodes.push({ type: 'text', value: '[iframe — содержимое в отдельном фрейме]', depth });
        }
        continue;
      }
      // inert — по спеке элемент недоступен для взаимодействия целиком: так
      // сайты выключают страницу под открытой модалкой.
      if (node.inert || node.hasAttribute('inert')) continue;
      const vis = visibility(node);
      if (vis === 'hidden') continue;
      if (vis === 'boxless') {
        walk(node, depth, itemName); // самого элемента нет — идём к потомкам
        continue;
      }

      // Составная строка списка: один реф на всю запись.
      if (!insideItem && isItem(node)) {
        if (isOccluded(node)) {
          occluded++;
          continue;
        }
        const name = emit(node, depth, true, null);
        if (name !== null) {
          const outer = itemSeen;
          itemSeen = new Map();
          walk(node, depth + 1, name); // внутри оставим только органы управления
          itemSeen = outer;
          continue;
        }
      }

      // «Кнопка», внутри которой сетка ячеек, — контейнер: сама она не реф,
      // ячейки внутри — рефы.
      if (!insideItem && !STRONG_TAGS.has(tag) && isInteractive(node)) {
        const sigs = uniformCells(node);
        if (sigs) {
          cellSigs.push(sigs);
          walk(node, depth + 1, itemName);
          cellSigs.pop();
          continue;
        }
      }

      const interactive = insideItem ? isStrongControl(node) : isInteractive(node);
      if (interactive) {
        // Перекрытый элемент не эмитим — и внутрь не идём, иначе его текст
        // вывалится сырыми узлами и агент решит, что элемент доступен.
        if (isOccluded(node)) {
          occluded++;
          continue;
        }
        if (emit(node, depth, false, itemName) !== null) {
          // Внутрь не спускаемся: текст уже в name. Исключение — контейнеры.
          const containerish = ['listbox', 'tablist', 'menu', 'grid', 'combobox'];
          if (!containerish.includes((node.getAttribute('role') || '').toLowerCase())) continue;
        } else if (insideItem) {
          continue; // дубль или безымянная обёртка внутри карточки
        }
      }
      walk(node, depth + 1, itemName);
    }
  }

  walk(document.body || document.documentElement, 0, null);

  // Многие приложения скроллят не окно, а внутренний контейнер. Если считать
  // только window.scrollY, агент решит, что страница кончилась, и не пролистает.
  function findScroller() {
    let best = null;
    let bestArea = 0;
    for (const el of document.querySelectorAll('div, main, section, ul, ol, tbody')) {
      const r = el.getBoundingClientRect();
      if (r.height < 200 || r.width < 200) continue;           // дешёвый отсев
      if (el.scrollHeight - el.clientHeight < 50) continue;
      const oy = window.getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll') continue;
      const area = r.width * r.height;
      if (area > bestArea) {
        bestArea = area;
        best = el;
      }
    }
    return best;
  }

  const doc = document.documentElement;
  const windowScrolls = doc.scrollHeight - window.innerHeight > 50;
  let scroll;

  if (windowScrolls) {
    scroll = {
      y: Math.round(window.scrollY),
      height: Math.round(doc.scrollHeight),
      viewport: Math.round(window.innerHeight),
      atBottom: window.scrollY + window.innerHeight >= doc.scrollHeight - 4,
      inner: false
    };
  } else {
    const sc = findScroller();
    if (sc) {
      const r = sc.getBoundingClientRect();
      sc.setAttribute('data-agent-scroll', '1');
      scroll = {
        y: Math.round(sc.scrollTop),
        height: Math.round(sc.scrollHeight),
        viewport: Math.round(sc.clientHeight),
        atBottom: sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4,
        inner: true,
        // Куда навести курсор, чтобы колесо крутило именно этот контейнер.
        cx: Math.round(r.left + r.width / 2),
        cy: Math.round(r.top + r.height / 2)
      };
    } else {
      scroll = {
        y: 0,
        height: Math.round(doc.scrollHeight),
        viewport: Math.round(window.innerHeight),
        atBottom: true,
        inner: false
      };
    }
  }

  // Много перекрытых контролов и мало доступных — почти наверняка поверх
  // страницы открыто окно. Модели это прямой сигнал: сначала разберись с ним.
  scroll.occluded = occluded;
  return { nodes, nextIndex: idx, scroll };
}
