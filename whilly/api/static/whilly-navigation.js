/* Focus-only navigation. Capture prevents hint letters invoking operator actions. */
(function () {
  'use strict';
  if (window.whillyNavigation) return;
  window.whillyNavigation = true;
  const alphabet = 'asdfghjklqwertyuiopzxcvbnm';
  let hints = [], prefix = '', help = null, previous = null;
  const editable = target => target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]');
  function clear() {
    hints.forEach(({node, element}) => { node.remove(); delete element.dataset.keyboardTarget; });
    hints = []; prefix = '';
  }
  function closeHelp() {
    if (!help) return;
    help.close(); help.remove(); help = null;
    if (previous?.isConnected) previous.focus();
  }
  function showHelp() {
    clear(); previous = document.activeElement;
    help = document.createElement('dialog');
    help.dataset.keyboardHelp = ''; help.className = 'keyboard-help';
    help.setAttribute('aria-label', 'Горячие клавиши');
    const title = document.createElement('h2'); title.textContent = '[ Клавиатура ]'; help.append(title);
    const text = document.createElement('p');
    text.textContent = 'Tab / Shift+Tab — переход · Enter / Space — действие кнопки · F — метки для фокуса (без нажатия) · Esc — закрыть · ? — справка. В полях ввода горячие клавиши отключены.';
    help.append(text);
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '[ Закрыть · Esc ]';
    close.addEventListener('click', closeHelp); help.append(close);
    help.addEventListener('cancel', event => { event.preventDefault(); closeHelp(); });
    document.body.append(help); help.showModal(); close.focus();
  }
  function showHints() {
    clear();
    const scope = document.querySelector('dialog[open]') || document;
    const elements = [...scope.querySelectorAll('a[href],button,input:not([type="hidden"]),select,textarea,summary,[tabindex]')].filter(element => {
      const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      return !element.matches(':disabled,[aria-disabled="true"]') && element.tabIndex >= 0 &&
        !element.closest('[hidden],[inert],[aria-hidden="true"]') && style.visibility === 'visible' &&
        rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
    });
    const width = Math.max(1, Math.ceil(Math.log(Math.max(1, elements.length)) / Math.log(alphabet.length)));
    hints = elements.map((element, index) => {
      let value = index, label = '';
      for (let i = 0; i < width; i++) { label = alphabet[value % alphabet.length] + label; value = Math.floor(value / alphabet.length); }
      const node = document.createElement('span'), rect = element.getBoundingClientRect();
      node.className = 'keyboard-hint'; node.textContent = label.toUpperCase();
      node.setAttribute('aria-hidden', 'true'); node.dataset.targetId = String(index);
      element.dataset.keyboardTarget = String(index);
      node.style.left = `${Math.max(0, Math.min(rect.left, innerWidth - 40))}px`;
      node.style.top = `${Math.max(0, rect.top)}px`;
      (scope === document ? document.body : scope).append(node);
      return {node, element, label};
    });
  }
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (help) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeHelp(); }
      // Keep native dialog Tab/activation while blocking dashboard shortcuts.
      else event.stopImmediatePropagation();
      return;
    }
    if (hints.length) {
      if (event.key === 'Tab') {
        // Let the browser perform native Tab/Shift+Tab navigation. Hints are
        // only an overlay and must not turn the focus ring into a dead end.
        clear(); return;
      }
      if (['Enter', ' '].includes(event.key)) {
        clear(); event.stopImmediatePropagation(); return;
      }
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.key === 'Escape') { clear(); return; }
      if (event.repeat || event.key === 'Shift') return;
      const letter = event.key.toLowerCase();
      if (letter.length !== 1 || !alphabet.includes(letter)) { clear(); return; }
      prefix += letter;
      const candidates = hints.filter(hint => hint.label.startsWith(prefix));
      if (!candidates.length) { clear(); return; }
      if (candidates[0].label === prefix) {
        const target = candidates[0].element; clear(); target.focus(); return;
      }
      hints.forEach(hint => { hint.node.hidden = !hint.label.startsWith(prefix); });
      return;
    }
    if (editable(event.target) || event.repeat) return;
    if (event.key.toLowerCase() === 'f' || event.key === '?') {
      event.preventDefault(); event.stopImmediatePropagation();
      if (event.key === '?') showHelp(); else showHints();
    }
  }, true);
  document.addEventListener('click', event => {
    if (event.target.closest?.('[data-keyboard-help-open]')) showHelp();
    else clear();
  });
  document.addEventListener('htmx:beforeSwap', () => { clear(); closeHelp(); });
  window.addEventListener('resize', clear);
  document.addEventListener('scroll', clear, true);
})();
