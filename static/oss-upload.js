'use strict';

window.movieReviewOssUpload = async (form, file, workId, report) => {
  const endpoint = form.dataset.ossUrl;
  const csrf = form.elements.csrf.value;
  const post = async (url, values = {}) => {
    const body = new URLSearchParams({csrf, ...values});
    const response = await fetch(url, {method: 'POST', body, redirect: 'error'});
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.message || `平台上传请求失败（${response.status}）`);
    }
    return response.json();
  };
  const uploadPart = (url, data, current, offset) => new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', url);
    request.timeout = 10 * 60 * 1000;
    request.upload.onprogress = event => {
      const uploaded = Math.min(file.size, offset + event.loaded);
      report(`${file.name} · 第 ${current} 片 · ${Math.floor(100 * uploaded / file.size)}%`);
    };
    request.onload = () => request.status >= 200 && request.status < 300
      ? resolve() : reject(new Error(`OSS 上传失败（${request.status}）`));
    request.onerror = request.ontimeout = () => reject(new Error('连接 OSS 失败，请检查网络及 OSS 跨域设置。'));
    request.send(data);
  });
  const options = workId ? {work_id: workId} :
    {review_mode: form.elements.review_mode.value, export_quality: form.elements.export_quality.value};
  const {id, part_size_bytes: size} = await post(endpoint,
    {filename: file.name, size_bytes: file.size, ...options});
  const item = `${endpoint}/${id}`;
  let number = 1;
  for (let start = 0; start < file.size; start += size, number++) {
    for (let retry = 0; ; retry++) {
      const {url} = await post(`${item}/parts/${number}`);
      try {
        await uploadPart(url, file.slice(start, start + size, ''), number, start);
        break;
      } catch (error) {
        if (retry === 2) throw error;
      }
    }
  }
  await post(`${item}/complete`);
  report(`${file.name} · 已传到 OSS，正在校验`);
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 3000));
    const response = await fetch(item, {redirect: 'error'});
    if (!response.ok) throw new Error('暂时无法读取上传状态，请查看工单列表。');
    const result = await response.json();
    if (result.state === 'rejected') throw new Error(result.error || '上传校验失败。');
    if (result.state === 'done') return result.result;
  }
};

for (const form of document.querySelectorAll('[data-oss-manual]')) {
  const button = form.querySelector('button[type="submit"]');
  const report = message => { form.querySelector('[role="status"]').textContent = message; };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const file = form.elements.submission.files[0];
    if (!file || button.disabled) return;
    button.disabled = true;
    try {
      await window.movieReviewOssUpload(form, file, form.dataset.workId, report);
      location.reload();
    } catch (error) {
      report(error.message);
    } finally {
      button.disabled = false;
    }
  });
}
