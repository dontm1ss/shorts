// Детерминированный рендер переписки: состояние экрана — чистая функция времени t (мс).
// Playwright вызывает init(data) один раз, затем renderAt(t) на каждый кадр и снимает скриншот.
// renderAt возвращает ключ состояния: одинаковый ключ — одинаковый кадр, повторно не снимается.

(function () {
  "use strict";

  const POP_MS = 180;            // появление пузыря
  const RECEIPT_DELAY_MS = 450;  // «Прочитано» — чуть позже своего сообщения
  const DOT_STEP_MS = 110;       // шаг анимации точек «печатает…»
  // Синие пузыри в iOS — градиент по высоте экрана: выше светлее, ниже темнее
  const BLUE_TOP = [99, 176, 252];
  const BLUE_BOTTOM = [47, 118, 245];

  let D = null;
  const rows = [];
  const natural = [];
  let listEl, viewportEl, receiptEl, typingEl, dots;

  const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  const ease = (p) => 1 - Math.pow(1 - p, 3);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function blueAt(y) {
    const k = clamp(y / viewportEl.clientHeight, 0, 1);
    const c = BLUE_TOP.map((v, i) => Math.round(v + (BLUE_BOTTOM[i] - v) * k));
    return "rgb(" + c.join(",") + ")";
  }

  window.init = function (data) {
    D = data;
    document.getElementById("phone").className = data.theme;
    listEl = document.getElementById("list");
    viewportEl = document.getElementById("viewport");

    const stamp = el("div", "stamp");
    stamp.appendChild(el("div", "service", "iMessage"));
    const when = el("div");
    when.appendChild(el("b", "", data.stamp_day));
    when.appendChild(document.createTextNode(" " + data.stamp_time));
    stamp.appendChild(when);
    listEl.appendChild(stamp);

    data.messages.forEach((m, i) => {
      const prev = data.messages[i - 1];
      const row = el("div", "row " + m.side);
      row.style.paddingTop = (!prev ? 2 : prev.side === m.side ? 2 : 10) + "px";
      row.appendChild(el("div", "bubble", m.text));
      listEl.appendChild(row);
      rows.push(row);
    });

    receiptEl = el("div", "receipt");
    receiptEl.appendChild(el("b", "", data.receipt_label));
    receiptEl.appendChild(document.createTextNode(" " + data.receipt_time));
    listEl.appendChild(receiptEl);

    typingEl = el("div", "row left last");
    typingEl.style.paddingTop = "10px";
    const typingBubble = el("div", "bubble typing");
    for (let k = 0; k < 3; k++) typingBubble.appendChild(el("i"));
    typingEl.appendChild(typingBubble);
    listEl.appendChild(typingEl);
    dots = Array.from(typingBubble.children);

    // Естественная высота строк — для плавного «вырастания» пузыря при появлении
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

    // Собеседник «печатает» перед своим сообщением
    const upcoming = ms[lastVisible + 1];
    const typing = !!upcoming && upcoming.side === "left" && t >= upcoming.typing_start_ms;
    typingEl.style.display = typing ? "" : "none";
    if (typing) {
      const phase = Math.floor(t / DOT_STEP_MS) % 9;
      dots.forEach((d, k) => {
        const x = (phase - k * 2 + 9) % 9;
        d.style.opacity = x < 3 ? [1, 0.75, 0.5][x] : 0.3;
      });
      key.push("t" + phase);
    }

    // «Прочитано» — под последним своим сообщением, если оно последнее в переписке
    const last = ms[lastVisible];
    const receipt = !!last && last.side === "right" && t - last.appear_ms > RECEIPT_DELAY_MS;
    if (receipt) {
      rows[lastVisible].after(receiptEl);
      receiptEl.style.display = "";
    } else {
      receiptEl.style.display = "none";
    }
    key.push(receipt ? "r" : "-");

    // Прокрутка: новые сообщения снизу, старые уходят вверх
    const offset = Math.max(0, listEl.offsetHeight - viewportEl.clientHeight);
    listEl.style.transform = "translateY(" + (-offset).toFixed(2) + "px)";
    key.push("o" + offset.toFixed(1));

    // Градиент синих пузырей по их положению на экране
    const top0 = viewportEl.getBoundingClientRect().top;
    for (let i = 0; i <= lastVisible; i++) {
      if (ms[i].side !== "right") continue;
      const bubble = rows[i].firstChild;
      const r = bubble.getBoundingClientRect();
      const bottomColor = blueAt(r.bottom - top0);
      bubble.style.background = "linear-gradient(" + blueAt(r.top - top0) + ", " + bottomColor + ")";
      bubble.style.setProperty("--tail", bottomColor);
    }

    return key.join("|");
  };
})();
