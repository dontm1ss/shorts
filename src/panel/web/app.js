// Панель отбора сценариев. Текст из сценариев вставляется только через textContent/value —
// посты с форумов не могут подсунуть в страницу разметку или скрипты.
(function () {
  "use strict";

  const TYPE_LABEL = { reddit_story: "История", imessage_drama: "Переписка", video_clip: "Клип" };
  const STATUS_LABEL = { pending: "ждёт", running: "идёт", done: "готово", failed: "ошибка", cancelled: "отменена" };
  const WORD = /[\p{L}]+(?:-[\p{L}]+)*|\d+/gu;

  let tab = "pending";
  let timing = null;
  let emotions = [];   // [{id, label}] — интонации озвучки (core/emotions.py)
  let autoApprove = false;
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

  // Время очереди — локальное время ПК без часового пояса: сегодня — ЧЧ:ММ:СС, иначе с датой
  function fmtClock(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return d.toDateString() === new Date().toDateString()
      ? time : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " + time;
  }

  function fmtSeconds(s) {
    return s < 60 ? `${s} с` : s < 3600 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${Math.floor(s / 3600)} ч ${Math.floor(s / 60) % 60} мин`;
  }

  // Длительность: завершённая — от старта до конца; идущая — от старта до сейчас
  function duration(job) {
    if (!job.started_at || job.status === "pending") return "—";
    const end = job.status === "running" ? new Date() : new Date(job.finished_at);
    const s = Math.max(0, Math.round((end - new Date(job.started_at)) / 1000));
    return isNaN(s) ? "—" : fmtSeconds(s);
  }

  // Панель над списком: подпись + кнопки массовых действий
  function toolbar(label, ...buttons) {
    return el("div", { class: "toolbar" }, el("span", { class: "toolbar-label", text: label }), ...buttons);
  }

  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
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

  // Петля: конец переписки переходит в начало, ролик крутится по кругу
  function loopBadge() {
    return el("span", { class: "badge loop", text: "↻ петля",
      title: "конец переходит в начало — ролик крутится по кругу, и повтор не сразу заметен" });
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
      emotions = s.emotions || [];
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
      $("#auto-row").hidden = !s.review_enabled;
      const auto = $("#auto-approve");
      if (!auto.disabled) auto.checked = s.auto_approve;   // не перебивать переключение, пока оно сохраняется
      autoApprove = s.auto_approve;
      const key = JSON.stringify([s.review, s.queue]);
      if (tab === "queue" && lastSummary !== "") renderQueue();   // время «Длилась» у идущей задачи тикает
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

  // Интонация реплики: по ней озвучка берёт запись голоса с этой эмоцией
  function emotionSelect(line, onEdit) {
    const sel = el("select", { class: "emo", title: "интонация озвучки", onchange: () => {
      sel.dataset.emotion = sel.value;
      onEdit();
    } });
    for (const e of emotions) sel.append(el("option", { value: e.id, text: e.label }));
    sel.value = line.emotion || "neutral";
    sel.dataset.emotion = sel.value;
    return sel;
  }

  function sceneCard(scene) {
    const texts = () => [...card.querySelectorAll("textarea")].map((t) => t.value);
    const lineEmotions = () => [...card.querySelectorAll("select.emo")].map((s) => s.value);
    let badge = durationBadge(estimate(scene.script.map((l) => l.text)).seconds);
    const words = el("span", { text: `${scene.words} слов` });
    let syncSeam = null;
    const onEdit = () => {
      card.classList.add("dirty");
      const est = estimate(texts());
      const fresh = durationBadge(est.seconds);
      badge.replaceWith(fresh);
      badge = fresh;
      words.textContent = `${est.words} слов`;
      if (syncSeam) syncSeam();
    };

    const meta = el("div", { class: "meta" },
      el("span", { class: "badge", text: TYPE_LABEL[scene.type] || scene.type }),
      scene.loop ? loopBadge() : null,
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
        return el("div", { class: "msg " + (right ? "right" : "left") },
          el("div", { class: "msg-head" }, el("span", { class: "who", text: who }), emotionSelect(line, onEdit)), ta);
      }));
      if (scene.loop) {
        // Стык петли: после последнего сообщения — снова первое, как его увидит зритель на следующем круге
        const ghostText = el("div", { class: "ghost-bubble" });
        const ghost = el("div", { class: "msg ghost" }, ghostText);
        const show = (all) => {
          const i = Math.max(0, all.findIndex((t) => t.trim()));   // пустые сообщения удаляются
          ghost.className = "msg ghost " + (scene.script[i].speaker === "B" ? "right" : "left");
          ghostText.textContent = all[i];
        };
        show(scene.script.map((l) => l.text));
        syncSeam = () => show(texts());
        body.append(el("div", { class: "seam" },
          el("div", { class: "seam-label", text: "↻ дальше по кругу — снова первое сообщение" }),
          ghost,
          scene.loop_idea ? el("div", { class: "seam-idea", text: "Стык: " + scene.loop_idea }) : null));
      }
    } else {
      body = el("div", { class: "card-body" }, scene.script.map((line) => {
        const ta = el("textarea", { class: "para", oninput: (e) => { autosize(e.target); onEdit(); } });
        ta.value = line.text;
        requestAnimationFrame(() => autosize(ta));
        return el("div", { class: "part" }, emotionSelect(line, onEdit), ta);
      }));
    }

    // одобрение — и своей кнопкой, и «Одобрить все»: правки текста и интонаций уходят вместе с ним
    const doApprove = async () => {
      approveBtn.disabled = rejectBtn.disabled = true;
      try {
        const r = await api(`/api/scenes/${scene.scene_id}/approve`,
          { method: "POST", body: { texts: texts(), emotions: lineEmotions() } });
        card.remove();
        return r;
      } catch (e) {
        approveBtn.disabled = rejectBtn.disabled = false;
        throw e;
      }
    };
    const approveBtn = el("button", { text: "Одобрить", onclick: async () => {
      try {
        const r = await doApprove();
        toast(`В работе: задачи ${r.jobs.map((j) => "#" + j).join(", ")}${r.worker_started ? " — обработчик запущен" : ""}`);
        refreshSummary();
      } catch (e) { toast(e.message, true); }
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
        el("span", { class: "hint", text: "правки и интонации сохраняются при одобрении; пустой абзац — удаляется" }),
        rejectBtn, approveBtn));
    card.dataset.scene = scene.scene_id;
    card.approve = doApprove;
    return card;
  }

  async function renderPending() {
    const scenes = await api("/api/scenes?status=pending");
    const empty = autoApprove
      ? "Автоодобрение включено — новые сценарии сразу уходят в озвучку. Готовые ролики — на вкладке «Готовые»."
      : "Нет сценариев на проверке. Сгенерируйте истории или переписки выше.";
    if (!scenes.length) {
      content.replaceChildren(el("div", { class: "empty", text: empty }));
      return;
    }
    const cards = scenes.map(sceneCard);
    const count = (n) => `${n} ${plural(n, "сценарий", "сценария", "сценариев")}`;

    const approveAll = el("button", { text: "Одобрить все", onclick: async () => {
      const list = cards.filter((c) => c.isConnected);   // только те, что на странице
      if (!list.length || !confirm(`Одобрить ${count(list.length)}? Они уйдут в озвучку и рендер.`)) return;
      approveAll.disabled = deleteAll.disabled = true;
      let done = 0;
      for (const card of list) {
        try {
          await card.approve();
          approveAll.textContent = `Одобрено ${++done} из ${list.length}…`;
        } catch (e) {
          toast(`Одобрено ${done} из ${list.length}, дальше ошибка: ${e.message}`, true);
          break;
        }
      }
      if (done === list.length) toast(`Одобрено: ${count(done)} — все ушли в работу`);
      refreshSummary();
      render();
    } });

    const deleteAll = el("button", { class: "danger", text: "Удалить все", onclick: async () => {
      const ids = cards.filter((c) => c.isConnected).map((c) => c.dataset.scene);
      if (!ids.length || !confirm(`Удалить ${count(ids.length)} на проверке? Удаление необратимо.`)) return;
      approveAll.disabled = deleteAll.disabled = true;
      try {
        const r = await api("/api/scenes?" + new URLSearchParams([["status", "pending"], ...ids.map((i) => ["ids", i])]),
          { method: "DELETE" });
        toast(`Удалено: ${count(r.deleted)}`);
      } catch (e) { toast(e.message, true); }
      refreshSummary();
      render();
    } });

    content.replaceChildren(toolbar(`${count(scenes.length)} на проверке`, deleteAll, approveAll), ...cards);
  }

  async function renderRejected() {
    const scenes = await api("/api/scenes?status=rejected");
    const deleteAll = el("button", { class: "danger", text: "Удалить все", onclick: async () => {
      const n = scenes.length;
      if (!confirm(`Удалить все отклонённые (${n})? Удаление необратимо.`)) return;
      deleteAll.disabled = true;
      try {
        const r = await api("/api/scenes?" + new URLSearchParams([["status", "rejected"],
          ...scenes.map((s) => ["ids", s.scene_id])]), { method: "DELETE" });
        toast(`Удалено отклонённых: ${r.deleted}`);
      } catch (e) { toast(e.message, true); }
      refreshSummary();
      render();
    } });
    content.replaceChildren(...(scenes.length ? [toolbar(`Отклонённых: ${scenes.length}`, deleteAll)] : []),
      ...(scenes.length ? scenes.map((scene) => {
      const btn = el("button", { class: "ghost", text: "Вернуть на проверку", onclick: async () => {
        try { await api(`/api/scenes/${scene.scene_id}/restore`, { method: "POST" }); card.remove(); refreshSummary(); }
        catch (e) { toast(e.message, true); }
      } });
      const card = el("article", { class: "card" },
        el("div", { class: "card-head" },
          el("div", { class: "card-title", text: scene.title || "Без названия" }),
          el("div", { class: "meta" }, el("span", { class: "badge", text: TYPE_LABEL[scene.type] || scene.type }),
            scene.loop ? loopBadge() : null,
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
    const deleteAll = el("button", { class: "danger", text: "Удалить все", onclick: async () => {
      const cards = [...content.querySelectorAll(".video")];
      if (!cards.length || !confirm(`Удалить все ролики (${cards.length})? Файлы удалятся насовсем.`)) return;
      deleteAll.disabled = true;
      for (const player of content.querySelectorAll("video")) {   // отпустить файлы (Windows)
        player.pause();
        player.removeAttribute("src");
        player.load();
      }
      try {
        const ids = cards.map((c) => c.dataset.scene);   // только видимые: свежие ролики не трогаем
        const r = await api("/api/videos?" + new URLSearchParams(ids.map((i) => ["ids", i])), { method: "DELETE" });
        toast(`Удалено роликов: ${r.deleted}` + (r.busy ? `, ${r.busy} заняты — попробуйте ещё раз` : ""), r.busy > 0);
      } catch (e) { toast(e.message, true); }
      render();
    } });
    content.replaceChildren(toolbar(videosLabel(videos.length), deleteAll),
      el("div", { class: "videos" }, videos.map(videoCard)));
  }

  function videosLabel(n) {
    return `${n} ${plural(n, "ролик", "ролика", "роликов")}`;
  }

  // Сколько делался ролик: от одобрения сценария до готового файла (с ожиданием в очереди)
  // и чистая работа — озвучка, субтитры или запись интерфейса, рендер
  function videoTiming(v) {
    if (v.elapsed_s == null && v.work_s == null) return null;
    const parts = [];
    if (v.elapsed_s != null) parts.push(`${fmtSeconds(Math.max(0, v.elapsed_s))} после одобрения`);
    if (v.work_s != null) parts.push(`работа ${fmtSeconds(v.work_s)}`);
    return el("div", { class: "timing", text: "⏱ " + parts.join(" · "),
      title: "от одобрения сценария до готового ролика, включая ожидание в очереди; " +
             "работа — озвучка, субтитры или запись переписки и рендер" });
  }

  function videoCard(v) {
    const title = v.title || TYPE_LABEL[v.type] || v.type;
    // петлю плеер крутит по кругу — стык виден так же, как на площадке
    const player = v.url ? el("video", { src: v.url, controls: true, preload: "metadata", playsinline: true,
                                         loop: v.loop }) : null;
    const del = el("button", { class: "danger", text: "Удалить", onclick: async () => {
      if (!confirm(`Удалить ролик «${title}»? Файл удалится насовсем.`)) return;
      del.disabled = true;
      if (player) {   // отпустить файл: Windows не даст удалить то, что ещё читает плеер
        player.pause();
        player.removeAttribute("src");
        player.load();
      }
      try {
        await api(`/api/videos/${encodeURIComponent(v.scene_id)}`, { method: "DELETE" });
        card.remove();
        const left = document.querySelectorAll(".video").length;
        $("#n-videos").textContent = left || "";
        const label = content.querySelector(".toolbar-label");
        if (label) label.textContent = videosLabel(left);
        if (!left) content.replaceChildren(el("div", { class: "empty", text: "Готовых роликов пока нет." }));
        toast("Ролик удалён");
      } catch (e) {
        toast(e.message, true);
        del.disabled = false;
        if (player) player.src = v.url;
      }
    } });
    const card = el("div", { class: "video" },
      player || el("div", { class: "empty", text: "файл удалён" }),
      el("div", { class: "info" },
        el("b", { text: title }),
        el("div", { class: "meta" },
          el("span", { class: "badge", text: TYPE_LABEL[v.type] || v.type }),
          v.loop ? loopBadge() : null,
          el("span", { text: `${v.duration_s} с` }),
          v.size_mb ? el("span", { text: `${v.size_mb} МБ` }) : null,
          el("span", { text: fmtTime(v.rendered_at) })),
        videoTiming(v),
        el("div", { class: "actions" },
          v.url ? el("a", { href: v.url, download: "", text: "Скачать" }) : el("span"),
          del)));
    card.dataset.scene = v.scene_id;
    return card;
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
      el("td", { class: "time", text: fmtClock(j.created_at) }),
      el("td", { class: "time", text: j.status === "pending" ? "—" : fmtClock(j.started_at) }),
      el("td", { class: "time", text: j.status === "running" || j.status === "pending" ? "—" : fmtClock(j.finished_at) }),
      el("td", { class: "time", text: duration(j) }),
      el("td", { class: "err", text: j.error || "" })));
    content.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "jobs" },
      el("thead", {}, el("tr", {}, ["Задача", "Этап", "Статус", "В очереди", "Начата", "Завершена", "Длилась", ""]
        .map((h) => el("th", { text: h })))),
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
      if (form.loop && form.loop.checked) body.loop = true;
      const r = await api("/api/generate", { method: "POST", body });
      toast(`Поставлено: ${r.jobs.length} задач${body.loop ? " (петли)" : ""}${r.worker_started ? " — обработчик запущен" : ""}. Сценарии появятся на проверке.`);
      refreshSummary();
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  }));

  $("#auto-approve").addEventListener("change", async (e) => {
    const box = e.target;
    const wanted = box.checked;
    box.disabled = true;
    try {
      const r = await api("/api/settings", { method: "PUT", body: { auto_approve: wanted } });
      autoApprove = r.auto_approve;
      const pending = Number($("#n-pending").textContent) || 0;
      toast(r.auto_approve
        ? "Автоодобрение включено: новые сценарии сразу идут в озвучку" +
          (pending ? `. Уже ждущие проверки (${pending}) — одобрите вручную` : "")
        : "Автоодобрение выключено: новые сценарии ждут проверки");
      if (tab === "pending") render();
    } catch (err) {
      box.checked = !wanted;
      toast(err.message, true);
    }
    box.disabled = false;
  });

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
