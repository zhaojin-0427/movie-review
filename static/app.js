'use strict';

const find = (selector) => document.querySelector(selector);
const userId = document.body.dataset.userId;
const draftPrefix = 'movie-review-draft:';
try {
  for (const key of Object.keys(sessionStorage)) {
    if (key.startsWith(draftPrefix) && (!userId || !key.startsWith(`${draftPrefix}${userId}:`))) {
      sessionStorage.removeItem(key);
    }
  }
} catch (_) { /* Storage can be disabled. */ }

async function checkIdentity() {
  if (!userId) return;
  try {
    const response = await fetch(document.body.dataset.identityUrl, {cache: 'no-store'});
    if (response.status === 401 || (response.ok && String((await response.json()).user_id) !== userId)) {
      document.body.hidden = true;
      location.reload();
    }
  } catch (_) { /* The server checks authorization for every request. */ }
}
window.addEventListener('pageshow', (event) => { if (event.persisted) checkIdentity(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkIdentity(); });

for (const submitted of document.querySelectorAll('form[data-confirm]:not(.return-form)')) {
  submitted.addEventListener('submit', (event) => {
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
  const dialogs = [...document.querySelectorAll('.execution-dialog')];
  function selectDetailTab(dialog, selected) {
    for (const tab of dialog.querySelectorAll('[role="tab"]')) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    }
  }
  function openExecutionDialog(dialog) {
    if (dialog.open) return;
    selectDetailTab(dialog, dialog.querySelector('[role="tab"]'));
    dialog.showModal();
  }
  for (const dialog of dialogs) {
    const section = dialog.closest('[data-execution-section]');
    const opener = dialog.previousElementSibling;
    opener.addEventListener('click', () => openExecutionDialog(dialog));
    for (const button of dialog.querySelectorAll('[data-close-execution-dialog]')) {
      button.addEventListener('click', () => dialog.close());
    }
    dialog.addEventListener('close', () => {
      if (location.hash === '#' + dialog.id) history.replaceState(null, '', '#' + section.id);
      opener.focus({preventScroll: true});
    });
    const tabs = [...dialog.querySelectorAll('[role="tab"]')];
    for (const [index, tab] of tabs.entries()) {
      tab.addEventListener('click', () => selectDetailTab(dialog, tab));
      tab.addEventListener('keydown', event => {
        const next = {
          ArrowLeft: (index - 1 + tabs.length) % tabs.length,
          ArrowRight: (index + 1) % tabs.length,
          Home: 0,
          End: tabs.length - 1,
        }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        selectDetailTab(dialog, tabs[next]);
        tabs[next].focus();
      });
    }
  }
  const showSection = () => {
    const target = document.getElementById(location.hash.slice(1));
    const section = target?.closest('[data-execution-section]') || panels[0];
    for (const panel of panels) panel.hidden = panel !== section;
    for (const link of links) {
      if (link.hash === `#${section.id}`) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
    if (dialogs.includes(target)) {
      const notice = find('.execution-page > .notice[role="status"]');
      if (notice) {
        target.querySelector('[data-execution-dialog-notice]').replaceChildren(notice.cloneNode(true));
        notice.remove();
      }
      target.closest('[data-list-item]').scrollIntoView({block: 'nearest'});
      openExecutionDialog(target);
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
