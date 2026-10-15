'use strict';
(() => {
  const control = document.getElementById('capture-toggle');
  const label = document.getElementById('capture-label');
  let busy = false;
  const render = result => {
    control.dataset.ready = String(Boolean(result.ready));
    label.textContent = result.label;
    control.title = result.label + ' · 点击开启或找回悬浮窗口';
  };
  async function check() {
    if (busy || document.hidden) return;
    try { render(await api('capture/status')); }
    catch { render({label: '连接中断，点击重试'}); }
  }
  control.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    control.disabled = true;
    label.textContent = '正在开启';
    try {
      render(await api('capture/start', {}));
      toast('已请求显示悬浮窗口。回到原文中划选词语即可取词。');
    } catch (error) {
      render({label: '未能开启，点击重试'});
      toast(error.message);
    } finally {
      busy = false;
      control.disabled = false;
    }
  });
  check();
  setInterval(check, 3000);
  document.addEventListener('visibilitychange', check);
})();
