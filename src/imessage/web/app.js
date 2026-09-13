// Детерминированный рендер переписки: состояние экрана — чистая функция времени t (мс).
// Playwright вызывает init(data) один раз, затем renderAt(t) на каждый кадр и снимает скриншот.
// renderAt возвращает ключ состояния: одинаковый ключ — одинаковый кадр, повторно не снимается.

(function () {
  "use strict";

  const POP_MS = 180;          // появление пузыря
  const RECEIPT_DELAY_MS = 350;
  const DOT_STEP_MS = 110;     // шаг анимации точек «печатает…»

  let D = null;
  let rows = [];
  let natural = [];
  let listEl, viewportEl, receiptEl, typingEl, typedEl, caretEl, placeholderEl, sendEl, micEl, dots;

  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  const ease = (p) => 1 - Math.pow(1 - p, 3);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function initials(name) {
    const parts = name.trim().split(/\s+/).filter(Boolean);
    const letters = parts.slice(0, 2).map((p) => p[0]).join("");
    return (letters || "?").toUpperCase();
  }

  window.init = function (data) {
    D = data;
    document.getElementById("phone").className = data.theme;
    document.getElementById("name").textContent = data.contact;
    document.getElementById("avatar").textContent = initials(data.contact);
    document.getElementById("clock").textContent = data.clock;
    const badge = document.getElementById("badge");
    badge.textContent = data.badge;
    badge.style.display = data.badge ? "" : "none";

    listEl = document.getElementById("list");
    viewportEl = document.getElementById("viewport");
    typedEl = document.getElementById("typed");
    caretEl = document.getElementById("caret");
    placeholderEl = document.getElementById("placeholder");
    sendEl = document.getElementById("send");
    micEl = document.getElementById("mic");

    const stamp = el("div", "stamp");
    stamp.appendChild(el("b", "", "iMessage"));
    stamp.appendChild(el("br"));
    stamp.appendChild(document.createTextNode(data.stamp));
    listEl.appendChild(stamp);

    data.messages.forEach((m, i) => {
      const prev = data.messages[i - 1];
      const row = el("div", "row " + m.side);
      row.style.paddingTop = (!prev ? 2 : prev.side === m.side ? 2 : 9) + "px";
      row.appendChild(el("div", "bubble", m.text));
      listEl.appendChild(row);
      rows.push(row);
    });

    receiptEl = el("div", "receipt", data.receipt);
    listEl.appendChild(receiptEl);

    typingEl = el("div", "row left last");
    typingEl.style.paddingTop = "9px";
    const typingBubble = el("div", "bubble typing");
    for (let k = 0; k < 3; k++) typingBubble.appendChild(el("i"));
    typingEl.appendChild(typingBubble);
    listEl.appendChild(typingEl);
    dots = Array.from(typingBubble.children);

    // Естественная высота каждой строки — для плавного «вырастания» при появлении
    rows.forEach((row) => natural.push(row.offsetHeight));
    return { messages: rows.length };
  };

  window.renderAt = function (t) {
    const ms = D.messages;
    const key = [];

    let lastVisible = -1;
    for (let i = 0; i < ms.length; i++) if (t >= ms[i].appear_ms) lastVisible = i;

    for (let i = 0; i < ms.length; i++) {
      const row = rows[i];
      const bubble = row.firstChild;
      if (i > lastVisible) {
        row.style.display = "none";
        continue;
      }
      row.style.display = "";
      const next = i < lastVisible ? ms[i + 1] : null;
      const isLast = !next || next.side !== ms[i].side;
      row.classList.toggle("last", isLast);
      key.push(isLast ? "L" : "n");

      const p = clamp((t - ms[i].appear_ms) / POP_MS, 0, 1);
      if (p < 1) {
        const e = ease(p);
        row.style.height = (natural[i] * e).toFixed(2) + "px";
        row.style.overflow = "hidden";
        bubble.style.transform = "scale(" + (0.86 + 0.14 * e).toFixed(4) + ")";
        bubble.style.opacity = Math.min(1, e * 1.4).toFixed(3);
        key.push(i + ":" + Math.round(p * 60));
      } else {
        row.style.height = "";
        row.style.overflow = "";
        bubble.style.transform = "";
        bubble.style.opacity = "";
      }
    }
    key.push("v" + lastVisible);

    // Следующее сообщение: собеседник «печатает» (точки) или владелец набирает текст в поле
    const upcoming = ms[lastVisible + 1];
    let typing = false;
    let typed = "";
    if (upcoming && t >= upcoming.typing_start_ms) {
      if (upcoming.side === "left") {
        typing = true;
      } else {
        const span = Math.max(1, upcoming.appear_ms - upcoming.typing_start_ms);
        const n = Math.floor(upcoming.text.length * clamp((t - upcoming.typing_start_ms) / span, 0, 1));
        typed = upcoming.text.slice(0, n);
      }
    }

    typingEl.style.display = typing ? "" : "none";
    if (typing) {
      const phase = Math.floor(t / DOT_STEP_MS) % 9;
      dots.forEach((d, k) => {
        const x = (phase - k * 2 + 9) % 9;
        d.style.opacity = x < 3 ? [1, 0.75, 0.5][x] : 0.3;
      });
      key.push("t" + phase);
    }

    const last = ms[lastVisible];
    const receipt = !!last && last.side === "right" && t - last.appear_ms > RECEIPT_DELAY_MS && !typed && !typing;
    if (receipt) {
      rows[lastVisible].after(receiptEl);
      receiptEl.style.display = "";
    } else {
      receiptEl.style.display = "none";
    }
    key.push(receipt ? "r" : "-");

    typedEl.textContent = typed;
    const hasText = typed.length > 0;
    placeholderEl.style.display = hasText ? "none" : "";
    caretEl.style.display = hasText ? "" : "none";
    sendEl.style.display = hasText ? "" : "none";
    micEl.style.display = hasText ? "none" : "";
    key.push("i" + typed.length);

    // Прокрутка: новые сообщения снизу, старые уходят вверх
    const offset = Math.max(0, listEl.offsetHeight - viewportEl.clientHeight);
    listEl.style.transform = "translateY(" + (-offset).toFixed(2) + "px)";
    key.push("o" + offset.toFixed(1));

    return key.join("|");
  };
})();
