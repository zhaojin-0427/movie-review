'use strict';

const find = (selector) => document.querySelector(selector);
const surface = find('#drawing-surface');
const overlay = find('#overlay');
const fields = find('#region-fields');
const form = find('#frame-form');
const toggle = find('#draw-toggle');
const help = find('#draw-help');
const hiddenRegions = find('#regions-input');
const note = find('#frame-note');
const data = find('#region-data');
const regions = data ? JSON.parse(data.dataset.regions) : [];
const initialRegions = JSON.stringify(regions);
const initialNote = note?.value;
let drawing = false;
let drag;
let submitting = false;
const userId = document.body.dataset.userId;
const initialRange = form?.elements.range_end.value;
const draftStatus = find('#draft-status');
const draftPrefix = 'movie-review-draft:';
const draftKey = form && `${draftPrefix}${userId}:${new URL(form.getAttribute('action'), location.href).pathname}`;
let storageWorks = true;
let imageReady = false;

function dirty() {
  return form && (initialNote !== note.value || initialRegions !== JSON.stringify(regions)
                  || initialRange !== form.elements.range_end.value);
}

function clearDraftsForOtherUsers() {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(draftPrefix) && (!userId || !key.startsWith(`${draftPrefix}${userId}:`))) {
        sessionStorage.removeItem(key);
      }
    }
  } catch (_) { storageWorks = false; }
}

function storeDraft() {
  if (!storageWorks || !draftKey) return;
  try {
    if (!dirty()) {
      sessionStorage.removeItem(draftKey);
      draftStatus.hidden = true;
      return;
    }
    sessionStorage.setItem(draftKey, JSON.stringify({note: note.value, regions,
      rangeEnd: form.elements.range_end.value, revision: form.dataset.revision}));
    draftStatus.textContent = '草稿已保存在当前标签页。';
    draftStatus.hidden = false;
  } catch (_) { storageWorks = false; }
}

clearDraftsForOtherUsers();
if (draftKey && storageWorks) {
  try {
    const draft = JSON.parse(sessionStorage.getItem(draftKey));
    if (draft && draft.revision === form.dataset.revision) {
      note.value = draft.note;
      form.elements.range_end.value = draft.rangeEnd;
      regions.splice(0, regions.length, ...draft.regions);
      draftStatus.textContent = '已恢复当前标签页的草稿。';
      draftStatus.hidden = false;
    } else if (draft) {
      sessionStorage.removeItem(draftKey);
    }
  } catch (_) { storageWorks = false; }
}

async function checkIdentity() {
  if (!userId) return;
  try {
    const response = await fetch(document.body.dataset.identityUrl, {cache: 'no-store'});
    if (response.status === 401 || (response.ok && String((await response.json()).user_id) !== userId)) {
      document.body.hidden = true;
      submitting = true;
      location.reload();
    }
  } catch (_) { /* The server still checks authorization on every submission. */ }
}
window.addEventListener('pageshow', (event) => { if (event.persisted) checkIdentity(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkIdentity(); });

const picture = surface?.querySelector('img');
const approve = form?.querySelector('[value="approve"]');
const zoom = find('#zoom-image');
if (picture) {
  if (zoom) zoom.disabled = true;
  picture.decode().then(() => {
    imageReady = picture.naturalWidth > 0;
    if (approve) approve.disabled = !imageReady;
    if (toggle) toggle.disabled = !imageReady;
    if (zoom) zoom.disabled = !imageReady;
  }).catch(() => { find('#image-error').hidden = false; });
}
zoom?.addEventListener('click', () => {
  const enabled = surface.closest('.stage').classList.toggle('zoomed');
  zoom.setAttribute('aria-pressed', String(enabled));
  zoom.textContent = enabled ? '适配窗口' : '放大';
});

function setDrawing(value) {
  drawing = value;
  surface.classList.toggle('drawing', value);
  toggle.setAttribute('aria-pressed', String(value));
  toggle.textContent = value ? '取消圈选' : '＋ 添加圈选';
  help.hidden = !value;
}

function ring(box, index) {
  const node = document.createElement('div');
  node.className = index === undefined ? 'ring preview' : 'ring';
  for (const [name, value] of Object.entries({left: box.x, top: box.y, width: box.w, height: box.h})) {
    node.style[name] = `${value * 100}%`;
  }
  if (index !== undefined) {
    const badge = document.createElement('span');
    badge.textContent = index + 1;
    node.append(badge);
  }
  return node;
}

function updateFields(focusLast = false) {
  if (!overlay) return;
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

function point(event) {
  const bound = surface.getBoundingClientRect();
  const clamp = (n) => Math.min(1, Math.max(0, n));
  return {x: clamp((event.clientX - bound.left) / bound.width), y: clamp((event.clientY - bound.top) / bound.height)};
}

function box(first, last) {
  return {x: Math.min(first.x, last.x), y: Math.min(first.y, last.y),
          w: Math.abs(first.x - last.x), h: Math.abs(first.y - last.y)};
}

if (form) {
  form.addEventListener('input', storeDraft);
  toggle.addEventListener('click', () => {
    if (regions.length >= 24 && !drawing) {
      alert('一帧最多 24 个圈选。');
      return;
    }
    setDrawing(!drawing);
  });
  surface.addEventListener('pointerdown', (event) => {
    if (!drawing || event.button !== 0 || drag) return;
    event.preventDefault();
    surface.setPointerCapture(event.pointerId);
    drag = {pointer: event.pointerId, start: point(event)};
  });
  surface.addEventListener('pointermove', (event) => {
    if (drag?.pointer !== event.pointerId) return;
    overlay.querySelector('.preview')?.remove();
    overlay.append(ring(box(drag.start, point(event))));
  });
  surface.addEventListener('pointerup', (event) => {
    if (drag?.pointer !== event.pointerId) return;
    const area = box(drag.start, point(event));
    drag = undefined;
    overlay.querySelector('.preview')?.remove();
    if (area.w < .008 || area.h < .008) return;
    regions.push({...area, note: ''});
    storeDraft();
    setDrawing(false);
    updateFields(true);
  });
  surface.addEventListener('pointercancel', () => {
    drag = undefined;
    overlay.querySelector('.preview')?.remove();
  });
  form.addEventListener('submit', (event) => {
    const action = event.submitter?.value;
    if (action === 'approve' && (!imageReady || dirty())) {
      event.preventDefault();
      alert(imageReady ? '请先保存或清除当前帧的未提交草稿。' : '图片尚不可用。');
      return;
    }
    if (action === 'save' || action === 'previous') {
      const missing = regions.findIndex((r) => !r.note.trim());
      if (missing >= 0) {
        event.preventDefault();
        fields.querySelectorAll('input')[missing].focus();
        alert('请为每个圈选填写意见。');
        return;
      }
      if (action === 'save' && !note.value.trim() && !regions.length) {
        event.preventDefault();
        note.focus();
        alert('请先填写意见或添加圈选。');
        return;
      }
    } else if ((note.value.trim() || regions.length) && !confirm('此操作将清除该帧的意见，是否继续？')) {
      event.preventDefault();
      return;
    }
    hiddenRegions.value = JSON.stringify(regions);
    submitting = true;
  });
  window.addEventListener('beforeunload', (event) => {
    if (!submitting && !storageWorks && dirty()) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
}
updateFields();

const current = find('.frame-chip.current');
if (current && current.offsetLeft > current.parentElement.offsetWidth / 2) {
  current.parentElement.scrollLeft = current.offsetLeft - current.parentElement.offsetLeft - current.parentElement.clientWidth / 2;
}
for (const id of ['#choose-version', '#frame-filter']) {
  find(id)?.addEventListener('change', (event) => { window.location.assign(event.target.value); });
}
if (surface) {
  document.addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea, select, button, a, [contenteditable]') || drawing) return;
    const selector = {ArrowLeft: '.viewer-controls .previous', ArrowRight: '.viewer-controls .next'}[event.key];
    const link = selector && find(selector);
    if (link && link.getAttribute('aria-disabled') !== 'true') {
      event.preventDefault();
      link.click();
    }
  });
}
for (const submitted of document.querySelectorAll('form[data-confirm]')) {
  submitted.addEventListener('submit', (event) => {
    if (submitted.classList.contains('return-form') && dirty()) {
      event.preventDefault();
      alert('请先保存当前帧的修改。');
      return;
    }
    if (!confirm(submitted.dataset.confirm)) event.preventDefault();
  });
}
const importJob = find('#import-job');
if (importJob) {
  const title = find('#import-title');
  const duplicate = find('#allow-duplicate');
  const updateTitle = () => {
    title.hidden = duplicate.hidden = Boolean(importJob.value);
    title.querySelector('input').required = !importJob.value;
  };
  importJob.addEventListener('change', updateTitle);
  updateTitle();
  find('#upload-form').addEventListener('submit', (event) => {
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    button.textContent = '正在上传并校验…';
  });
}

const batchUploadForm = document.querySelector('[data-batch-upload]');
if (batchUploadForm) {
  const sourceInput = batchUploadForm.querySelector('input[type="file"]');
  const dropzone = batchUploadForm.querySelector('[data-upload-dropzone]');
  const fileList = batchUploadForm.querySelector('[data-upload-files]');
  const uploadStatus = batchUploadForm.querySelector('[data-upload-status]');
  const submitButton = batchUploadForm.querySelector('button[type="submit"]');
  let selectedFiles = [];
  let dragDepth = 0;

  const fileKey = (file) => `${file.name}\n${file.size}\n${file.lastModified}`;
  const syncInput = () => {
    try {
      const transfer = new DataTransfer();
      selectedFiles.forEach((file) => transfer.items.add(file));
      sourceInput.files = transfer.files;
    } catch (_) {
      // Browsers that do not allow assigning a FileList still submit the last selection.
    }
  };
  const updateFileList = () => {
    fileList.replaceChildren();
    fileList.hidden = selectedFiles.length === 0;
    selectedFiles.forEach((file) => {
      const row = document.createElement('div');
      row.className = 'execution-upload-file';
      const name = document.createElement('span');
      name.textContent = file.name;
      const size = document.createElement('small');
      size.textContent = `${(file.size / 1024 / 1024).toFixed(1)} MiB`;
      row.append(name, size);
      fileList.append(row);
    });
    uploadStatus.textContent = selectedFiles.length
      ? `已选择 ${selectedFiles.length} 个视频，每个视频会分别创建一条工单。`
      : '可一次选择多个视频，每个视频会分别创建一条工单。';
  };
  const addFiles = (files) => {
    const known = new Set(selectedFiles.map(fileKey));
    for (const file of Array.from(files || [])) {
      if (!known.has(fileKey(file))) {
        selectedFiles.push(file);
        known.add(fileKey(file));
      }
    }
    syncInput();
    updateFileList();
  };

  sourceInput.addEventListener('change', () => addFiles(sourceInput.files));
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      sourceInput.click();
    }
  });
  dropzone.addEventListener('dragenter', (event) => {
    event.preventDefault();
    dragDepth += 1;
    dropzone.classList.add('is-dragging');
  });
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  });
  dropzone.addEventListener('dragleave', (event) => {
    event.preventDefault();
    dragDepth -= 1;
    if (dragDepth <= 0) {
      dragDepth = 0;
      dropzone.classList.remove('is-dragging');
    }
  });
  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove('is-dragging');
    addFiles(event.dataTransfer.files);
  });
  batchUploadForm.addEventListener('submit', (event) => {
    if (!selectedFiles.length) {
      event.preventDefault();
      uploadStatus.textContent = '请先选择至少一个视频文件。';
      sourceInput.focus();
      return;
    }
    submitButton.disabled = true;
    submitButton.textContent = `正在上传 ${selectedFiles.length} 个视频…`;
    uploadStatus.textContent = '正在上传并创建工单，请稍候。';
  });
  updateFileList();
}

for (const time of document.querySelectorAll('time[data-local-time]')) {
  time.textContent = new Date(time.dateTime).toLocaleString('zh-CN');
}
for (const form of document.querySelectorAll('form[data-credential-submit]')) {
  const button = form.querySelector('button[type="submit"]');
  const label = button.textContent;
  form.addEventListener('submit', (event) => {
    if (button.disabled) {
      event.preventDefault();
      return;
    }
    button.disabled = true;
    button.textContent = '正在签发…';
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
      button.disabled = false;
      button.textContent = label;
    }
  });
}
const copyCredential = find('[data-copy-field="token"]');
copyCredential?.addEventListener('click', async () => {
  const input = copyCredential.closest('section').querySelector('input[aria-describedby="copy-credential-status"]');
  const status = find('#copy-credential-status');
  try {
    await navigator.clipboard.writeText(input.value);
    status.textContent = '已复制';
  } catch (_) {
    input.select();
    status.textContent = '已选中，请手动复制';
  }
});
for (const button of document.querySelectorAll('[data-submit-confirm]')) {
  button.addEventListener('click', (event) => {
    if (!confirm(button.dataset.submitConfirm)) event.preventDefault();
  });
}

const sectionNav = find('[data-section-nav]');
if (sectionNav) {
  const panels = [...document.querySelectorAll('[data-execution-section]')];
  const links = [...sectionNav.querySelectorAll('a')];
  const showSection = () => {
    const target = document.getElementById(location.hash.slice(1));
    const section = target?.closest('[data-execution-section]') || panels[0];
    for (const panel of panels) panel.hidden = panel !== section;
    for (const link of links) {
      if (link.hash === `#${section.id}`) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
    if (target instanceof HTMLDetailsElement) {
      target.open = true;
      target.scrollIntoView({block: 'nearest'});
    } else if (target && sectionNav.getBoundingClientRect().top < 92) {
      sectionNav.scrollIntoView({block: 'start'});
    }
  };
  window.addEventListener('hashchange', showSection);
  showSection();
}

const accountsPage = find('.accounts-page');
if (accountsPage) {
  const createAccount = find('#create-account');
  const resetAccount = find('#reset-account');
  const confirmAccount = find('#confirm-account');
  const confirmTitle = find('#confirm-account-heading');
  const confirmDescription = find('#account-confirm-description');
  const confirmButton = confirmAccount.querySelector('button[name="action"]');
  const stickyHeader = find('.topbar');
  const openMenu = () => accountsPage.querySelector('.account-menu[open]');

  const syncCreateDialog = () => {
    if (location.hash === '#create-account' && !createAccount.open) createAccount.showModal();
  };
  window.addEventListener('hashchange', syncCreateDialog);
  syncCreateDialog();
  createAccount.addEventListener('close', () => {
    if (location.hash === '#create-account') history.replaceState(null, '', location.pathname + location.search);
  });

  for (const dialog of accountsPage.querySelectorAll('.account-dialog')) {
    dialog.querySelector('[data-account-cancel]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => dialog.querySelector('form').reset());
  }

  for (const menu of accountsPage.querySelectorAll('.account-menu')) {
    menu.addEventListener('toggle', () => {
      if (!menu.open) return;
      const trigger = menu.getBoundingClientRect();
      const popupHeight = menu.querySelector('.account-menu-body').offsetHeight + 4;
      const spaceAbove = trigger.top - (stickyHeader?.getBoundingClientRect().bottom || 0);
      const spaceBelow = window.innerHeight - trigger.bottom;
      menu.classList.toggle('open-up', spaceBelow < popupHeight && spaceAbove > spaceBelow);
    });
  }
  document.addEventListener('click', (event) => {
    const menu = openMenu();
    if (menu && !menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener('focusin', (event) => {
    const menu = openMenu();
    if (menu && !menu.contains(event.target)) menu.open = false;
  });
  accountsPage.addEventListener('keydown', (event) => {
    const menu = openMenu();
    if (event.key === 'Escape' && menu) {
      event.preventDefault();
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  accountsPage.addEventListener('click', (event) => {
    if (event.target.closest('[data-create-account]')) {
      createAccount.showModal();
      return;
    }
    const button = event.target.closest('[data-account-action]');
    if (!button) return;
    const action = button.dataset.accountAction;
    const entry = button.closest('.account-entry');
    const menu = button.closest('.account-menu');
    const dialog = action === 'reset' ? resetAccount : confirmAccount;
    const name = entry.querySelector('.account-name strong').textContent;
    const username = entry.querySelector('.account-name .mono').textContent;
    dialog.querySelector('[data-account-target]').textContent = `${name}（${username}）`;
    dialog.querySelector('[name="user_id"]').value = entry.dataset.accountId;
    if (dialog === confirmAccount) {
      const messages = {
        enable: '启用后，该账号可以重新登录。',
        disable: '停用会注销该账号的登录会话，并暂停其登录权限。',
        delete: '删除会注销该账号的登录会话。历史操作和作业归属会保留。',
      };
      confirmTitle.textContent = button.textContent;
      confirmDescription.textContent = messages[action];
      if (action === 'delete' && Number(entry.dataset.owned) > 0) {
        confirmDescription.textContent += '该账号名下仍在审核的作业会进入等待状态。';
      }
      confirmButton.value = action;
      confirmButton.textContent = button.textContent;
      confirmButton.classList.toggle('account-danger', action === 'delete');
    }
    menu.open = false;
    menu.querySelector('summary').focus();
    dialog.showModal();
  });
}

for (const list of document.querySelectorAll('[data-filter-list]')) {
  const toolbar = list.querySelector('.list-filters');
  if (!toolbar) continue;
  const fields = [...toolbar.querySelectorAll('[data-filter]')];
  const items = [...list.querySelectorAll('[data-list-item]')];
  const count = list.querySelector('[data-match-count]');
  const empty = list.querySelector('[data-no-matches]');
  const update = () => {
    const filters = fields.map(field => [field.dataset.filter, field.value.trim()]);
    let visible = 0;
    for (const item of items) {
      const matches = filters.every(([field, value]) => !value || (field === 'search'
        ? item.dataset.search.toLowerCase().includes(value.toLowerCase())
        : item.dataset[field] === value));
      item.hidden = !matches;
      if (matches) visible += 1;
    }
    count.textContent = String(visible);
    empty.hidden = visible > 0;
  };
  for (const field of fields) field.addEventListener(field.tagName === 'SELECT' ? 'change' : 'input', update);
  toolbar.hidden = false;
  update();
}
