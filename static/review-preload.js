'use strict';

window.createReviewImageCache = function createReviewImageCache(workspace, total) {
  const panel = workspace.querySelector('#review-preload');
  const status = panel.querySelector('#review-preload-status');
  const progress = panel.querySelector('#review-preload-progress');
  const retry = panel.querySelector('#review-preload-retry');
  const bodies = new Map();
  const pending = new Map();
  const batches = new Map();
  const failed = new Set();
  const chunkSize = 32;
  const concurrent = navigator.connection?.effectiveType === '3g' ? 2 : 4;
  const limitedMemory = navigator.deviceMemory && navigator.deviceMemory <= 2;
  const limit = (limitedMemory ? 64 : 256) * 1024 * 1024;
  let bytes = 0;
  let cursor = 1;
  let preferred = [];
  let started = false;
  let paused = Boolean(navigator.connection?.saveData);
  let atLimit = false;
  let suspended = false;
  let stopped = false;
  let repaint;

  function updateProgress() {
    const count = bodies.size;
    progress.value = count;
    retry.hidden = stopped || atLimit || !(paused || (failed.size > 0 && pending.size === 0));
    retry.textContent = paused ? '继续加载' : '重试未加载图片';
    let label = `已加载 ${count} / ${total} 帧`;
    if (count === total) label = `全片图片已加载 · ${total} 帧`;
    else if (atLimit) label += ' · 已达缓存上限，其余帧按需加载';
    else if (paused) label += ' · 后台加载已暂停';
    else if (failed.size && !pending.size) label += ' · 部分图片加载失败';
    status.textContent = label;
  }

  function repaintProgress() {
    if (repaint) return;
    repaint = setTimeout(() => {
      repaint = undefined;
      updateProgress();
    }, 150);
  }

  async function sourceFor(number) {
    const start = Math.floor((number - 1) / chunkSize) * chunkSize + 1;
    let batch = batches.get(start);
    if (!batch || Date.now() - batch.time > 10 * 60 * 1000) {
      const url = new URL(workspace.dataset.imageSourcesUrl, location.href);
      url.searchParams.set('start', start);
      const request = fetch(url, {headers: {Accept: 'application/json'}, cache: 'no-store',
        redirect: 'error', signal: AbortSignal.timeout?.(15000)}).then(async response => {
        if (!response.ok) throw new Error('image sources');
        return (await response.json()).sources;
      });
      batch = {time: Date.now(), request};
      batches.set(start, batch);
      request.catch(() => { if (batches.get(start) === batch) batches.delete(start); });
    }
    const source = (await batch.request)[number];
    if (!source) throw new Error('image source missing');
    return source;
  }

  async function download(number) {
    try {
      const source = await sourceFor(number);
      const response = await fetch(source, {cache: 'no-store', credentials: 'same-origin',
        referrerPolicy: 'no-referrer', priority: 'low', signal: AbortSignal.timeout?.(45000)});
      if (!response.ok || !/^image\/(?:jpeg|png)(?:;|$)/i.test(response.headers.get('Content-Type') || '')) {
        throw new Error('image request');
      }
      const blob = await response.blob();
      if (!blob.size) throw new Error('empty image');
      if (stopped) return;
      if (bytes + blob.size > limit) {
        atLimit = true;
        return;
      }
      bodies.set(number, blob);
      bytes += blob.size;
    } catch (error) {
      failed.add(number);
      // A browser-level network or CORS error can affect the entire batch.
      if (error instanceof TypeError || error.name === 'TimeoutError') paused = true;
    } finally {
      pending.delete(number);
      schedule();
    }
  }

  function schedule() {
    repaintProgress();
    if (!started || paused || suspended || atLimit || stopped || document.hidden) return;
    const needsImage = number => !bodies.has(number) && !pending.has(number) && !failed.has(number);
    while (pending.size < concurrent) {
      let number = preferred.find(needsImage);
      if (!number) {
        while (cursor <= total && !needsImage(cursor)) cursor += 1;
        if (cursor > total) break;
        number = cursor++;
      }
      pending.set(number, download(number));
    }
  }

  retry.addEventListener('click', () => {
    failed.clear();
    batches.clear();
    cursor = 1;
    paused = false;
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  window.addEventListener('pagehide', () => { suspended = true; });
  window.addEventListener('pageshow', () => {
    suspended = false;
    schedule();
  });

  return {
    warm(numbers) {
      if (stopped) return;
      preferred = numbers;
      started = true;
      panel.hidden = false;
      schedule();
    },
    async image(number) {
      await pending.get(number);
      return bodies.get(number);
    },
    discard(number) {
      const blob = bodies.get(number);
      if (!blob) return;
      bodies.delete(number);
      bytes -= blob.size;
      failed.add(number);
      schedule();
    },
    stop() {
      stopped = true;
      bodies.clear();
      batches.clear();
      panel.hidden = true;
    },
  };
};
