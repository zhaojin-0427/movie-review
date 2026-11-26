'use strict';

async function ossDigest(blob, report) {
  const hash = window.sha256.create();
  const step = 8 * 1024 * 1024;
  for (let offset = 0; offset < blob.size; offset += step) {
    hash.update(new Uint8Array(await blob.slice(offset, offset + step).arrayBuffer()));
    if (report) report(Math.min(100, Math.floor(100 * (offset + step) / blob.size)));
  }
  return hash.hex();
}

function putOssObject(url, data, headers, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.timeout = 10 * 60 * 1000;
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = event => progress?.(event.loaded);
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300
      ? resolve(xhr.getResponseHeader('x-oss-version-id')) : reject(new Error(`OSS 上传失败（${xhr.status}）`));
    xhr.onerror = xhr.ontimeout = () => reject(new Error('连接 OSS 失败，请检查网络及 OSS 跨域设置。'));
    xhr.send(data);
  });
}

async function retryOss(send) {
  for (let count = 0; ; count++) {
    try {
      return await send();
    } catch (error) {
      if (count === 2) throw error;
    }
  }
}

async function ossMultipart(file, session, item, post, report) {
  const {part_size_bytes: size} = session;
  let number = 1;
  for (let start = 0; start < file.size; start += size, number++) {
    await retryOss(async () => {
      const {url} = await post(`${item}/parts/${number}`);
      return putOssObject(url, file.slice(start, start + size, ''), {}, loaded => {
        const uploaded = Math.min(file.size, start + loaded);
        report(`${file.name} · 第 ${number} 片 · ${Math.floor(100 * uploaded / file.size)}%`);
      });
    });
  }
  await post(`${item}/complete`);
}

async function directReviewZip(entries, item, post, report) {
  const files = entries.filter(entry => !entry.directory);
  const allowed = /^(pairs\/[0-9]{6}\.jpg|review-index\.json|version\.json|timeline\.json|meta\.jsonl|production\.json)$/;
  const maximum = 16 * 1024 * 1024;
  if (files.length > 2405 || new Set(files.map(entry => entry.filename)).size !== files.length
      || !files.some(entry => entry.filename === 'review-index.json')
      || files.some(entry => !allowed.test(entry.filename) || entry.encrypted || entry.uncompressedSize < 1
        || entry.uncompressedSize > (entry.filename.startsWith('pairs/') ? 14 * 1024 * 1024 : maximum))) {
    throw new Error('此 ZIP 需要新版接口的审核清单与原始 JPG，且文件必须符合大小限制。');
  }
  const objects = {};
  for (let offset = 0; offset < files.length; offset += 6) {
    const batch = await Promise.all(files.slice(offset, offset + 6).map(async entry => {
      const blob = await entry.getData(new window.zip.BlobWriter(), {checkSignature: true});
      return {name: entry.filename, blob, item: {size_bytes: blob.size, sha256: await ossDigest(blob)}};
    }));
    const payload = Object.fromEntries(batch.map(file => [file.name, file.item]));
    const signed = await post(`${item}/objects/sign`, {objects: JSON.stringify(payload)});
    await Promise.all(batch.map(async file => {
      const {url, headers} = signed[file.name];
      const version = await retryOss(() => putOssObject(url, file.blob, headers));
      if (!version || version === 'null') throw new Error('审核桶须启用版本控制，且 OSS CORS 须暴露 x-oss-version-id。');
      objects[file.name] = {...file.item, version_id: version};
      report(`正在上传审核图和清单 · ${Object.keys(objects).length} / ${files.length}`);
    }));
  }
  await post(`${item}/objects/complete`, {objects: JSON.stringify(objects)});
}

window.movieReviewOssUpload = async (form, file, workId, report) => {
  const endpoint = form.dataset.ossUrl;
  const protocol = form.dataset.ossProtocol;
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
  const options = workId ? {work_id: workId} :
    {review_mode: form.elements.review_mode.value, export_quality: form.elements.export_quality.value};
  const direct = protocol === 'objects_v1';
  let reader;
  let item;
  try {
    if (direct && workId) {
      window.zip.configure({useWebWorkers: false, useCompressionStream: true});
      reader = new window.zip.ZipReader(new window.zip.BlobReader(file));
      const entries = await reader.getEntries();
      if (form.dataset.workKind === 'finalize') {
        const proofs = entries.filter(entry => !entry.directory && entry.filename === 'delivery-proof.json');
        if (proofs.length !== 1 || proofs[0].uncompressedSize > 64 * 1024 || proofs[0].encrypted) {
          throw new Error('成片 ZIP 缺少可读取的 delivery-proof.json。');
        }
        const proof = JSON.parse(await proofs[0].getData(new window.zip.TextWriter(), {checkSignature: true}));
        options.final_verification = JSON.stringify(proof.final_verification);
        options.input_manifest_sha256 = proof.input_manifest_sha256;
      }
      if (form.dataset.workKind !== 'finalize') {
        const session = await post(endpoint, {filename: file.name, size_bytes: file.size, ...options});
        item = `${endpoint}/${session.id}`;
        await directReviewZip(entries, item, post, report);
      }
    }
    if (!item) {
      if (direct) options.sha256 = await ossDigest(file, percent => report(`${file.name} · 计算摘要 ${percent}%`));
      const session = await post(endpoint, {filename: file.name, size_bytes: file.size, ...options});
      item = `${endpoint}/${session.id}`;
      await ossMultipart(file, session, item, post, report);
    }
  } finally {
    if (reader) await reader.close();
  }
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
