'use strict';

(() => {
  const workspace = document.getElementById('review-workspace');
  const find = selector => workspace.querySelector(selector);
  const userId = document.body.dataset.userId;
  const surface = find('#drawing-surface');
  const stage = surface.closest('.stage');
  const overlay = find('#overlay');
  const form = find('#frame-form');
  const note = find('#frame-note');
  const fields = find('#region-fields');
  const toggle = find('#draw-toggle');
  const help = find('#draw-help');
  const zoom = find('#zoom-image');
  const approve = form?.querySelector('[value="approve"]');
  const draftStatus = find('#draft-status');
  const status = find('#review-save-status');
  const errorBox = find('#image-error');
  const loadingLabel = find('#frame-loading');
  const strip = find('.frame-strip');
  const stripPrevious = find('#frame-strip-previous');
  const stripNext = find('#frame-strip-next');
  const previous = find('.viewer-controls .previous');
  const next = find('.viewer-controls .next');
  const jump = find('.jump-form');
  const states = JSON.parse(find('#review-frame-states').textContent);
  const stateLabels = {pending: '待审', changes: '有问题', approved: '已通过'};
  const groupSize = 32;
  const version = Number(workspace.dataset.version);
  const filter = workspace.dataset.filter;
  const urlFor = (kind, frame) => workspace.dataset[kind + 'Url'].replace(/0$/, String(frame));
  let picture = surface.querySelector('img');
  let frameNumber = Number(workspace.dataset.frame);
  let revision = Number(workspace.dataset.revision);
  let regions = JSON.parse(find('#region-data').dataset.regions);
  let initialRegions = JSON.stringify(regions);
  let initialNote = note?.value;
  let initialRange = form?.elements.range_end.value;
  let drawing = false;
  let drag;
  let storageWorks = true;
  let imageReady = false;
  let saving = false;
  let leaving = false;
  let switching = false;
  let requested = frameNumber;
  let navigation;
  let retry;
  let reloadRequired = false;
  let warmTimer;
  let groupStart = 0;
  let visible = [];
  const images = new Map();

  function pageUrl(number) {
    const url = new URL(workspace.dataset.reviewUrl, location.href);
    url.search = new URLSearchParams({v: version, frame: number, frames: filter});
    return url;
  }

  function dirty() {
    return form && (initialNote !== note.value || initialRegions !== JSON.stringify(regions)
                    || initialRange !== form.elements.range_end.value);
  }

  function draftKey(number) {
    return `movie-review-draft:${userId}:${urlFor('edit', number)}`;
  }

  function storeDraft() {
    if (!storageWorks || !form) return;
    try {
      if (!dirty()) {
        sessionStorage.removeItem(draftKey(frameNumber));
        draftStatus.hidden = true;
        return;
      }
      sessionStorage.setItem(draftKey(frameNumber), JSON.stringify({note: note.value, regions,
        rangeEnd: form.elements.range_end.value, revision}));
      draftStatus.textContent = '草稿已保存在当前标签页。';
      draftStatus.hidden = false;
    } catch (_) { storageWorks = false; }
  }

  function restoreDraft() {
    if (!form) return;
    draftStatus.hidden = true;
    if (!storageWorks) return;
    try {
      const key = draftKey(frameNumber);
      const raw = sessionStorage.getItem(key);
      if (raw === null) return;
      const draft = JSON.parse(raw);
      if (draft?.revision !== revision || !Array.isArray(draft.regions)) {
        sessionStorage.removeItem(key);
        return;
      }
      note.value = draft.note;
      form.elements.range_end.value = draft.rangeEnd;
      regions = draft.regions;
      draftStatus.textContent = '已恢复当前标签页的草稿。';
      draftStatus.hidden = false;
    } catch (_) { storageWorks = false; }
  }

  function setDrawing(enabled) {
    drawing = enabled;
    surface.classList.toggle('drawing', enabled);
    toggle?.setAttribute('aria-pressed', String(enabled));
    if (toggle) toggle.textContent = enabled ? '取消圈选' : '＋ 添加圈选';
    if (help) help.hidden = !enabled;
  }

  function updateControls() {
    const locked = saving || switching || reloadRequired;
    if (form) {
      for (const control of form.querySelectorAll('button, textarea, input:not([type="hidden"])')) {
        control.disabled = locked || ((control === approve || control === toggle) && !imageReady);
      }
      for (const submit of workspace.querySelectorAll('.return-form button')) {
        submit.disabled = locked || (submit.id === 'review-return-button' && !states.includes('changes'));
      }
    }
    zoom.disabled = !imageReady || locked;
    stage.setAttribute('aria-busy', String(switching || (!imageReady && errorBox.hidden)));
  }

  function hideError() {
    errorBox.hidden = true;
    retry = undefined;
  }

  function showError(message, action, label = '重试') {
    find('#review-error-text').textContent = message;
    find('#review-retry').textContent = label;
    find('#review-retry').hidden = !action;
    retry = action;
    errorBox.hidden = false;
    updateControls();
  }

  function requireRefresh(message, url = pageUrl(frameNumber)) {
    reloadRequired = true;
    showError(message, () => location.assign(url), '刷新');
  }

  function decoded(number, priority = 'high') {
    const existing = images.get(number);
    if (existing) {
      if (priority === 'high') existing.image.fetchPriority = 'high';
      return existing.ready;
    }
    const image = new Image();
    image.decoding = 'async';
    image.fetchPriority = priority;
    image.draggable = false;
    const entry = {image};
    images.set(number, entry);
    image.src = urlFor('image', number);
    entry.ready = image.decode().then(() => image).catch(error => {
      if (images.get(number) === entry) images.delete(number);
      throw error;
    });
    return entry.ready;
  }

  function keepImages(numbers) {
    for (const [number, entry] of images) {
      if (numbers.includes(number)) continue;
      images.delete(number);
      if (entry.image !== picture) entry.image.removeAttribute('src');
    }
  }

  function neighbours(number) {
    const before = visible.filter(value => value < number).slice(-2).reverse();
    const after = visible.filter(value => value > number).slice(0, 2);
    return [after[0], before[0], after[1], before[1]].filter(Boolean);
  }

  function warmNearby(number) {
    clearTimeout(warmTimer);
    const nearby = navigator.connection?.saveData ? [] : neighbours(number);
    keepImages([number, ...nearby]);
    warmTimer = setTimeout(async () => {
      for (const candidate of nearby) {
        if (frameNumber !== number || switching || saving || reloadRequired || document.hidden) return;
        try { await decoded(candidate, 'low'); } catch (_) { /* A direct visit retries the image. */ }
      }
    }, 90);
  }

  function setPicture(image, frame) {
    image.alt = `第 ${frame.number} 帧，上方原片，下方待审画面`;
    image.width = frame.width;
    image.height = frame.height;
    surface.style.setProperty('--image-ratio', `${frame.width} / ${frame.height}`);
    surface.style.setProperty('--image-width', frame.width);
    surface.style.setProperty('--image-height', frame.height);
    if (image !== picture) picture.replaceWith(image);
    picture = image;
    imageReady = true;
  }

  function ring(box, index) {
    const element = document.createElement('div');
    element.className = index === undefined ? 'ring preview' : 'ring';
    for (const [name, value] of Object.entries({left: box.x, top: box.y, width: box.w, height: box.h})) {
      element.style[name] = `${value * 100}%`;
    }
    if (index !== undefined) {
      const badge = document.createElement('span');
      badge.textContent = index + 1;
      element.append(badge);
    }
    return element;
  }

  function updateFields(focusLast = false) {
    overlay.replaceChildren(...regions.map((region, index) => ring(region, index)));
    if (!fields) return;
    fields.replaceChildren();
    regions.forEach((region, index) => {
      const row = document.createElement('div');
      row.className = 'region-row';
      const input = document.createElement('input');
      input.type = 'text';
      input.maxLength = 500;
      input.placeholder = '填写圈选意见';
      input.setAttribute('aria-label', `圈选 ${index + 1} 的意见`);
      input.value = region.note;
      input.addEventListener('input', () => { region.note = input.value; storeDraft(); });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'text-button';
      remove.textContent = '移除';
      remove.setAttribute('aria-label', `移除圈选 ${index + 1}`);
      remove.addEventListener('click', () => {
        regions.splice(index, 1);
        storeDraft();
        updateFields();
      });
      row.append(String(index + 1), input, remove);
      fields.append(row);
      if (focusLast && index === regions.length - 1) input.focus();
    });
  }

  function paragraph(value, className = 'preserve-lines') {
    const element = document.createElement('p');
    element.className = className;
    element.textContent = value;
    return element;
  }

  function renderPrior(prior) {
    const container = find('#review-prior');
    container.replaceChildren();
    if (!prior) return;
    const detail = document.createElement('details');
    detail.className = 'prior';
    detail.open = true;
    const summary = document.createElement('summary');
    summary.textContent = '上轮返修意见';
    detail.append(summary);
    if (prior.note) detail.append(paragraph(prior.note));
    for (const [index, region] of prior.regions.entries()) {
      detail.append(paragraph(`圈选 ${index + 1} · ${region.note}`));
    }
    if (prior.regions.length) {
      const link = document.createElement('a');
      link.href = prior.marked_url;
      link.className = 'button small';
      link.textContent = '查看上轮圈选图 ↗';
      link.target = '_blank';
      link.rel = 'noopener';
      detail.append(link);
    }
    container.append(detail);
  }

  function renderFrame({frame, prior}, image) {
    const focusFromStrip = strip.contains(document.activeElement);
    frameNumber = frame.number;
    setPicture(image, frame);
    find('#review-frame-counter').textContent = `${String(frameNumber).padStart(4,'0')} / ${String(states.length).padStart(4,'0')}`;
    find('#review-frame-time').textContent = frame.time_ms === null ? `第 ${frameNumber} 帧` : `${(frame.time_ms / 1000).toFixed(3)} s`;
    find('#review-frame-size').textContent = `${frame.width} × ${frame.height}`;
    const badge = find('#review-frame-status');
    const dot = document.createElement('i');
    dot.className = 'status-dot ' + frame.state;
    badge.className = 'badge ' + frame.state;
    badge.replaceChildren(dot, stateLabels[frame.state]);
    const inherited = find('#review-inherited');
    inherited.hidden = !frame.origin || frame.origin === version;
    inherited.textContent = inherited.hidden ? '' : `沿用 V${frame.origin} 的人工通过`;
    renderPrior(prior);
    regions = frame.regions;
    if (form) {
      form.setAttribute('action', urlFor('edit', frameNumber));
      note.value = frame.note;
      const range = form.elements.range_end;
      range.value = '';
      range.min = frameNumber;
      range.placeholder = frameNumber;
      range.closest('details').open = false;
      form.querySelector('[value="previous"]').hidden = frameNumber <= 1;
      form.querySelector('[value="pending"]').hidden = frame.state === 'pending';
      initialNote = note.value;
      initialRange = range.value;
      initialRegions = JSON.stringify(regions);
      restoreDraft();
    } else {
      const readonly = find('#review-readonly-notes');
      readonly.replaceChildren();
      if (frame.note) readonly.append(paragraph(frame.note));
      for (const [index, region] of regions.entries()) {
        readonly.append(paragraph(`圈选 ${index + 1} · ${region.note}`));
      }
      if (!readonly.childNodes.length) readonly.append(paragraph('此帧无修改意见。', 'field-help'));
    }
    updateFields();
    renderStrip(true);
    if (focusFromStrip) strip.querySelector('[aria-current="true"]')?.focus({preventScroll: true});
  }

  function renderStrip(center = false) {
    visible = states.flatMap((state,index) => filter === 'all' || state === filter ? [index + 1] : []);
    if (center) {
      const at = visible.findIndex(number => number >= frameNumber);
      groupStart = Math.floor((at < 0 ? Math.max(0,visible.length - 1) : at) / groupSize) * groupSize;
    }
    const group = visible.slice(groupStart,groupStart + groupSize);
    strip.replaceChildren(...group.map(number => {
      const link = document.createElement('a');
      const state = states[number - 1];
      link.className = 'frame-chip ' + state + (number === frameNumber ? ' current' : '');
      link.href = pageUrl(number);
      link.dataset.frame = number;
      if (number === frameNumber) link.setAttribute('aria-current','true');
      link.setAttribute('aria-label', `第 ${number} 帧 ${stateLabels[state]}`);
      const label = document.createElement('span');
      label.textContent = String(number).padStart(2,'0');
      link.append(label,document.createElement('i'));
      return link;
    }));
    if (!group.length) strip.append(paragraph('没有符合此筛选的帧。','field-help'));
    const current = strip.querySelector('.current');
    strip.scrollLeft = current ? current.offsetLeft - (strip.clientWidth - current.offsetWidth) / 2 : 0;
    stripPrevious.disabled = groupStart === 0;
    stripNext.disabled = groupStart + groupSize >= visible.length;
    find('.frame-strip-navigation').hidden = visible.length <= groupSize;
    find('#frame-strip-range').textContent = group.length ? `${group[0]}–${group.at(-1)} 帧` : '';
    for (const [link, target] of [[previous, visible.findLast(n => n < frameNumber)], [next, visible.find(n => n > frameNumber)]]) {
      link.href = target ? pageUrl(target) : '#';
      link.dataset.frame = target || '';
      link.setAttribute('aria-disabled', String(!target));
    }
  }

  function renderProgress() {
    const counts = {approved:0,changes:0,pending:0};
    for (const state of states) counts[state] += 1;
    find('#review-progress-count').textContent = `${counts.approved} / ${states.length}`;
    find('.full-progress').value = counts.approved;
    for (const element of workspace.querySelectorAll('[data-review-count]')) {
      element.textContent = counts[element.dataset.reviewCount];
    }
  }

  async function showFrame(number, historyMode = 'push') {
    if (saving || reloadRequired || !Number.isInteger(number) || number < 1 || number > states.length) return;
    if (number === frameNumber && imageReady && !switching && historyMode !== 'replace') return;
    if (dirty()) {
      storeDraft();
      if (!storageWorks && !confirm('当前草稿无法保存在此标签页，是否继续切换？')) {
        if (historyMode === 'pop') history.replaceState(null,'',pageUrl(frameNumber));
        return;
      }
    }
    requested = number;
    status.hidden = true;
    loadingLabel.hidden = true;
    clearTimeout(warmTimer);
    navigation?.abort();
    const signal = new AbortController();
    navigation = signal;
    switching = true;
    hideError();
    setDrawing(false);
    drag = undefined;
    overlay.querySelector('.preview')?.remove();
    keepImages([frameNumber,number,...neighbours(number)]);
    updateControls();
    const pendingLabel = setTimeout(() => {
      if (navigation !== signal) return;
      loadingLabel.textContent = `正在加载第 ${number} 帧…`;
      loadingLabel.hidden = false;
    }, 130);
    const details = new URL(urlFor('frame', number),location.href);
    details.searchParams.set('revision',revision);
    try {
      const [response,image] = await Promise.all([
        fetch(details, {headers:{Accept:'application/json'}, signal:signal.signal}),
        decoded(number).catch(() => null),
      ]);
      if (navigation !== signal) return;
      if (response.status === 409) {
        requireRefresh('审核状态已更新，请刷新后继续。',pageUrl(number));
        return;
      }
      if (response.redirected || response.status === 403 || response.status === 404) {
        leaving = true;
        location.assign(response.redirected ? response.url : pageUrl(number));
        return;
      }
      if (!response.ok) throw new Error('frame request');
      const payload = await response.json();
      if (navigation !== signal) return;
      if (String(payload.user_id) !== userId || payload.writable !== Boolean(form) || payload.revision !== revision) {
        leaving = true;
        location.assign(pageUrl(number));
        return;
      }
      if (!image) throw new Error('image decode');
      states[number - 1] = payload.frame.state;
      renderFrame(payload,image);
      if (historyMode === 'push') history.pushState(null,'',pageUrl(number));
      if (historyMode === 'replace') history.replaceState(null,'',pageUrl(number));
    } catch (error) {
      if (navigation === signal && error.name !== 'AbortError') {
        showError(`第 ${number} 帧加载失败。`, () => showFrame(number,historyMode));
      }
    } finally {
      clearTimeout(pendingLabel);
      if (navigation === signal) {
        loadingLabel.hidden = true;
        switching = false;
        requested = frameNumber;
        updateControls();
        if (imageReady && errorBox.hidden) warmNearby(frameNumber);
      }
    }
  }

  function point(event) {
    const bound = surface.getBoundingClientRect();
    const clamp = value => Math.min(1, Math.max(0, value));
    return {x:clamp((event.clientX - bound.left) / bound.width), y:clamp((event.clientY - bound.top) / bound.height)};
  }

  function box(first,last) {
    return {x:Math.min(first.x,last.x), y:Math.min(first.y,last.y),
            w:Math.abs(first.x-last.x), h:Math.abs(first.y-last.y)};
  }

  async function save(event) {
    event.preventDefault();
    if (saving || switching || reloadRequired) return;
    const action = event.submitter?.value;
    if (!action) return;
    status.hidden = true;
    if (action === 'approve' && (!imageReady || dirty())) {
      alert(imageReady ? '请先保存或清除当前帧的未提交草稿。' : '图片尚不可用。');
      return;
    }
    if (action === 'save' || action === 'previous') {
      const missing = regions.findIndex(region => !region.note.trim());
      if (missing >= 0) {
        fields.querySelectorAll('input')[missing].focus();
        alert('请为每个圈选填写意见。');
        return;
      }
      if (action === 'save' && !note.value.trim() && !regions.length) {
        note.focus();
        alert('请先填写意见或添加圈选。');
        return;
      }
    } else if ((note.value.trim() || regions.length) && !confirm('此操作将清除该帧的意见，是否继续？')) return;
    form.elements.regions.value = JSON.stringify(regions);
    const body = new FormData(form);
    body.set('action', action);
    saving = true;
    clearTimeout(warmTimer);
    updateControls();
    let saved;
    try {
      const response = await fetch(form.getAttribute('action'),{method:'POST',headers:{Accept:'application/json'},body});
      if (response.redirected) {
        leaving = true;
        location.assign(response.url);
        return;
      }
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409) requireRefresh(result.message);
        else {
          status.textContent = result.message || '提交失败，请检查后重试。';
          status.hidden = false;
        }
        return;
      }
      try { sessionStorage.removeItem(draftKey(frameNumber)); } catch (_) { storageWorks = false; }
      if (result.reload) {
        leaving = true;
        location.assign(result.location);
        return;
      }
      revision = result.revision;
      form.elements.revision.value = revision;
      for (const element of workspace.querySelectorAll('.return-form [name="revision"]')) element.value = revision;
      for (const number of result.frames) {
        states[number - 1] = result.state;
        try {
          const key = draftKey(number);
          const draft = JSON.parse(sessionStorage.getItem(key));
          if (draft && draft.revision === revision - 1) sessionStorage.removeItem(key);
        } catch (_) { storageWorks = false; }
      }
      // Drafts on untouched frames remain valid after saving another frame in this tab.
      if (storageWorks) {
        try {
          const prefix = draftKey(0).slice(0,-1);
          for (const key of Object.keys(sessionStorage)) {
            if (!key.startsWith(prefix)) continue;
            const draft = JSON.parse(sessionStorage.getItem(key));
            if (draft?.revision === revision - 1) sessionStorage.setItem(key, JSON.stringify({...draft,revision}));
          }
        } catch (_) { storageWorks = false; }
      }
      initialNote = note.value;
      initialRegions = JSON.stringify(regions);
      initialRange = form.elements.range_end.value;
      draftStatus.hidden = true;
      renderProgress();
      renderStrip();
      saved = result;
    } catch (_) {
      requireRefresh('无法确认提交结果，请刷新后查看当前审核状态。');
    } finally {
      saving = false;
      updateControls();
    }
    if (saved) {
      await showFrame(saved.frame,'replace');
      status.textContent = {approved:'已通过此帧',changes:'意见已保存',pending:'已恢复待审'}[saved.state];
      status.hidden = false;
      if (!errorBox.hidden) requireRefresh('审核结果已保存，当前画面更新失败，请刷新后继续。', pageUrl(saved.frame));
    }
  }

  if (form) {
    form.addEventListener('input',storeDraft);
    form.addEventListener('submit',save);
    toggle.addEventListener('click', () => {
      if (regions.length >= 24 && !drawing) {
        alert('一帧最多 24 个圈选。');
        return;
      }
      setDrawing(!drawing);
    });
    surface.addEventListener('pointerdown',event => {
      if (!drawing || event.button !== 0 || drag) return;
      event.preventDefault();
      surface.setPointerCapture(event.pointerId);
      drag = {pointer:event.pointerId, start:point(event)};
    });
    surface.addEventListener('pointermove',event => {
      if (drag?.pointer !== event.pointerId) return;
      overlay.querySelector('.preview')?.remove();
      overlay.append(ring(box(drag.start,point(event))));
    });
    surface.addEventListener('pointerup',event => {
      if (drag?.pointer !== event.pointerId) return;
      const area = box(drag.start,point(event));
      drag = undefined;
      overlay.querySelector('.preview')?.remove();
      if (area.w < .008 || area.h < .008) return;
      regions.push({...area,note:''});
      storeDraft();
      setDrawing(false);
      updateFields(true);
    });
    surface.addEventListener('pointercancel',() => {
      drag = undefined;
      overlay.querySelector('.preview')?.remove();
    });
    window.addEventListener('beforeunload',event => {
      if (!leaving && !storageWorks && dirty()) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
    for (const returnForm of workspace.querySelectorAll('.return-form[data-confirm]')) {
      returnForm.addEventListener('submit',event => {
        if (dirty()) {
          event.preventDefault();
          alert('请先保存当前帧的修改。');
        } else if (confirm(returnForm.dataset.confirm)) leaving = true;
        else event.preventDefault();
      });
    }
  }

  zoom.addEventListener('click', () => {
    const enabled = stage.classList.toggle('zoomed');
    zoom.setAttribute('aria-pressed',String(enabled));
    zoom.textContent = enabled ? '适配窗口' : '放大';
  });
  find('#review-retry').addEventListener('click', () => retry?.());
  for (const select of [document.getElementById('choose-version'),find('#frame-filter')]) {
    select.addEventListener('change', () => location.assign(select.value));
  }
  workspace.addEventListener('click',event => {
    const link = event.target.closest('a[data-frame]');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const target = link === previous ? visible.findLast(number => number < requested)
      : link === next ? visible.find(number => number > requested) : Number(link.dataset.frame);
    if (target) showFrame(target);
  });
  jump.addEventListener('submit',event => {
    event.preventDefault();
    status.hidden = true;
    showFrame(Number(jump.elements.frame.value));
  });
  stripPrevious.addEventListener('click', () => { groupStart -= groupSize; renderStrip(); });
  stripNext.addEventListener('click', () => { groupStart += groupSize; renderStrip(); });
  document.addEventListener('keydown', event => {
    if (event.target.closest('input,textarea,select,button,a,[contenteditable]') || drawing || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.key === 'ArrowLeft' ? visible.findLast(number => number < requested)
      : event.key === 'ArrowRight' ? visible.find(number => number > requested) : undefined;
    if (!target) return;
    event.preventDefault();
    showFrame(target);
  });
  window.addEventListener('popstate', () => {
    const params = new URLSearchParams(location.search);
    if (Number(params.get('v')) === version && (params.get('frames') || 'all') === filter && params.has('frame') && !saving) {
      showFrame(Number(params.get('frame')),'pop');
    } else location.reload();
  });

  history.replaceState(null,'',pageUrl(frameNumber) + location.hash);
  restoreDraft();
  updateFields();
  renderStrip(true);
  updateControls();
  const initial = picture;
  const initialFrame = frameNumber;
  const initialEntry = {image:initial};
  initialEntry.ready = initial.decode().then(() => initial);
  images.set(initialFrame,initialEntry);
  initialEntry.ready.then(() => {
    if (frameNumber !== initialFrame || switching) return;
    imageReady = true;
    updateControls();
    warmNearby(initialFrame);
  }).catch(() => {
    if (images.get(initialFrame) === initialEntry) images.delete(initialFrame);
    if (frameNumber === initialFrame && !switching) {
      showError('当前帧图片加载失败。', () => showFrame(initialFrame,'replace'));
    }
  });
})();
