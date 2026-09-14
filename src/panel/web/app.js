// Панель отбора сценариев. Текст из сценариев вставляется только через textContent/value —
// посты с форумов не могут подсунуть в страницу разметку или скрипты.
(function () {
  "use strict";

  const TYPE_LABEL = { reddit_story: "История", imessage_drama: "Переписка", video_clip: "Клип" };
  const STATUS_LABEL = { pending: "ждёт", running: "идёт", done: "готово", failed: "ошибка", cancelled: "отменена" };
  const WORD = /[\p{L}]+(?:-[\p{L}]+)*|\d+/gu;

  let tab = "pending";
  let timing = null;
  let lastSummary = "";

  const $ = (sel) => document.querySelector(sel);
  const content = $("#content");

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c) node.append(c);
    return node;
  }

  async function api(path, options = {}) {
    const opts = { ...options, headers: { ...(options.headers || {}) } };
    if (opts.method && opts.method !== "GET") {
      opts.headers["X-Panel"] = "1";
      if (opts.body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(opts.body);
      }
    }
    const resp = await fetch(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    return data;
  }

  function toast(text, isError = false) {
    const t = $("#toast");
    t.textContent = text;
    t.className = isError ? "error" : "";
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.hidden = true), 3500);
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function estimate(texts) {
    // та же оценка, что scripting.estimate_duration_s
    const words = texts.reduce((n, t) => n + (t.match(WORD) || []).length, 0);
    const lines = texts.filter((t) => t.trim()).length;
    const s = words / timing.wps + Math.max(0, lines - 1) * timing.pause_s + lines * timing.line_overhead_s;
    return { words, seconds: Math.round(s) };
  }

  function durationBadge(seconds) {
    const ok = seconds >= timing.min_s && seconds <= timing.max_s;
    return el("span", { class: "badge " + (ok ? "ok" : "warn"), text: `≈ ${seconds} с` });
  }

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + 2 + "px";
  }

  // --- сводка ------------------------------------------------------------------

  async function refreshSummary() {
    try {
      const s = await api("/api/summary");
      timing = s.timing;
      $("#n-pending").textContent = s.review.pending || "";
      $("#n-rejected").textContent = s.review.rejected || "";
      const active = (s.queue.pending || 0) + (s.queue.running || 0);
      $("#n-queue").textContent = active || "";
      const state = $("#worker-state");
      state.className = "dot" + (s.worker_running ? " on" : "");
      state.textContent = s.worker_running
        ? (s.queue.running ? "обработка идёт" : "обработчик ждёт задач")
        : "обработчик остановлен";
      $("#worker-start").hidden = s.worker_running;
      const key = JSON.stringify([s.review, s.queue]);
      if (key !== lastSummary) {
        const first = lastSummary === "";
        lastSummary = key;
        if (!first && tab !== "pending") render();   // на вкладке проверки не сбиваем правки
        if (!first && tab === "pending" && !document.querySelector(".card.dirty")) render();
      }
    } catch (e) {
      $("#worker-state").textContent = "нет связи с сервером";
    }
  }

  // --- на проверке -------------------------------------------------------------

  function sceneCard(scene) {
    const texts = () => [...card.querySelectorAll("textarea")].map((t) => t.value);
    let badge = durationBadge(estimate(scene.script.map((l) => l.text)).seconds);
    const words = el("span", { text: `${scene.words} слов` });
    const onEdit = () => {
      card.classList.add("dirty");
      const est = estimate(texts());
      const fresh = durationBadge(est.seconds);
      badge.replaceWith(fresh);
      badge = fresh;
      words.textContent = `${est.words} слов`;
    };

    const meta = el("div", { class: "meta" },
      el("span", { class: "badge", text: TYPE_LABEL[scene.type] || scene.type }),
      badge, words,
      el("span", { text: fmtTime(scene.created_at) }));
    if (scene.source && scene.source.url) {
      meta.append(el("a", { href: scene.source.url, target: "_blank", rel: "noopener noreferrer",
        text: `источник: ${scene.source.source || "ссылка"}` }));
    }
    if (scene.topic) meta.append(el("span", { text: `тема: ${scene.topic}` }));

    let body;
    if (scene.type === "imessage_drama") {
      body = el("div", { class: "card-body chat" }, scene.script.map((line) => {
        const right = line.speaker === "B";
        const ta = el("textarea", { rows: 1, oninput: (e) => { autosize(e.target); onEdit(); } });
        ta.value = line.text;
        requestAnimationFrame(() => autosize(ta));
        const who = (scene.contacts && scene.contacts[line.speaker]) || line.speaker;
        return el("div", { class: "msg " + (right ? "right" : "left") }, el("span", { class: "who", text: who }), ta);
      }));
    } else {
      body = el("div", { class: "card-body" }, scene.script.map((line) => {
        const ta = el("textarea", { class: "para", oninput: (e) => { autosize(e.target); onEdit(); } });
        ta.value = line.text;
        requestAnimationFrame(() => autosize(ta));
        return ta;
      }));
    }

    const approveBtn = el("button", { text: "Одобрить", onclick: async () => {
      approveBtn.disabled = rejectBtn.disabled = true;
      try {
        const r = await api(`/api/scenes/${scene.scene_id}/approve`, { method: "POST", body: { texts: texts() } });
        toast(`В работе: задачи ${r.jobs.map((j) => "#" + j).join(", ")}${r.worker_started ? " — обработчик запущен" : ""}`);
        card.remove();
        refreshSummary();
      } catch (e) { toast(e.message, true); approveBtn.disabled = rejectBtn.disabled = false; }
    } });
    const rejectBtn = el("button", { class: "danger", text: "Отклонить", onclick: async () => {
      approveBtn.disabled = rejectBtn.disabled = true;
      try {
        await api(`/api/scenes/${scene.scene_id}/reject`, { method: "POST" });
        toast("Отклонён");
        card.remove();
        refreshSummary();
      } catch (e) { toast(e.message, true); approveBtn.disabled = rejectBtn.disabled = false; }
    } });

    const card = el("article", { class: "card" },
      el("div", { class: "card-head" }, el("div", { class: "card-title", text: scene.title || "Без названия" }), meta),
      body,
      el("div", { class: "card-foot" },
        el("span", { class: "hint", text: "правки сохраняются при одобрении; пустой абзац — удаляется" }),
        rejectBtn, approveBtn));
    return card;
  }

  async function renderPending() {
    const scenes = await api("/api/scenes?status=pending");
    content.replaceChildren(...(scenes.length ? scenes.map(sceneCard)
      : [el("div", { class: "empty", text: "Нет сценариев на проверке. Сгенерируйте истории или переписки выше." })]));
  }

  async function renderRejected() {
    const scenes = await api("/api/scenes?status=rejected");
    content.replaceChildren(...(scenes.length ? scenes.map((scene) => {
      const btn = el("button", { class: "ghost", text: "Вернуть на проверку", onclick: async () => {
        try { await api(`/api/scenes/${scene.scene_id}/restore`, { method: "POST" }); card.remove(); refreshSummary(); }
        catch (e) { toast(e.message, true); }
      } });
      const card = el("article", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title", text: scene.title || "Без названия" }),
          el("div", { class: "meta" }, el("span", { class: "badge", text: TYPE_LABEL[scene.type] || scene.type }),
            el("span", { text: (scene.script[0] || { text: "" }).text.slice(0, 140) + "…" }))),
        el("div", { class: "card-foot" }, btn));
      return card;
    }) : [el("div", { class: "empty", text: "Отклонённых нет." })]));
  }

  // --- готовые и очередь ----------------------------------------------------------

  async function renderVideos() {
    const videos = await api("/api/videos");
    $("#n-videos").textContent = videos.length || "";
    if (!videos.length) {
      content.replaceChildren(el("div", { class: "empty", text: "Готовых роликов пока нет." }));
      return;
    }
    content.replaceChildren(el("div", { class: "videos" }, videos.map((v) => el("div", { class: "video" },
      v.url ? el("video", { src: v.url, controls: true, preload: "metadata", playsinline: true })
        : el("div", { class: "empty", text: "файл удалён" }),
      el("div", { class: "info" },
        el("b", { text: v.title || TYPE_LABEL[v.type] || v.type }),
        el("div", { class: "meta" },
          el("span", { class: "badge", text: TYPE_LABEL[v.type] || v.type }),
          el("span", { text: `${v.duration_s} с` }),
          v.size_mb ? el("span", { text: `${v.size_mb} МБ` }) : null,
          el("span", { text: fmtTime(v.rendered_at) })),
        v.url ? el("a", { href: v.url, download: "", text: "Скачать" }) : null)))));
  }

  async function renderQueue() {
    const jobs = await api("/api/jobs");
    if (!jobs.length) {
      content.replaceChildren(el("div", { class: "empty", text: "Очередь пуста." }));
      return;
    }
    const rows = jobs.map((j) => el("tr", {},
      el("td", { text: "#" + j.id }),
      el("td", { text: j.kind }),
      el("td", { class: "st-" + j.status, text: STATUS_LABEL[j.status] || j.status }),
      el("td", { text: fmtTime(j.finished_at || j.created_at) }),
      el("td", { class: "err", text: j.error || "" })));
    content.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "jobs" },
      el("thead", {}, el("tr", {}, ["Задача", "Этап", "Статус", "Время", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, rows))));
  }

  async function render() {
    try {
      if (!timing) await refreshSummary();
      await ({ pending: renderPending, rejected: renderRejected, videos: renderVideos, queue: renderQueue })[tab]();
    } catch (e) {
      content.replaceChildren(el("div", { class: "empty", text: "Ошибка: " + e.message }));
    }
  }

  // --- события ---------------------------------------------------------------------

  document.querySelectorAll(".tabs button").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    tab = btn.dataset.tab;
    render();
  }));

  document.querySelectorAll("form.gen").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button");
    btn.disabled = true;
    try {
      const body = { kind: form.dataset.kind, count: Number(form.count.value) || 1 };
      if (form.topic) body.topic = form.topic.value;
      const r = await api("/api/generate", { method: "POST", body });
      toast(`Поставлено: ${r.jobs.length} задач${r.worker_started ? " — обработчик запущен" : ""}. Сценарии появятся на проверке.`);
      refreshSummary();
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  }));

  $("#worker-start").addEventListener("click", async () => {
    try { await api("/api/worker/start", { method: "POST" }); toast("Обработчик запущен"); refreshSummary(); }
    catch (e) { toast(e.message, true); }
  });

  // вкладки закреплены под шапкой, а её высота меняется (телефон, перенос статуса обработчика)
  const header = $(".top");
  const syncHeader = () => document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  new ResizeObserver(syncHeader).observe(header);
  syncHeader();

  refreshSummary().then(render);
  setInterval(refreshSummary, 8000);
})();
