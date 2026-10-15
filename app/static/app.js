(function () {
  const sidebar = document.getElementById('sidebar');
  const menuButton = document.getElementById('menuButton');
  const themeButton = document.getElementById('themeButton');
  if (menuButton && sidebar) {
    menuButton.addEventListener('click', () => {
      if (window.innerWidth <= 700) sidebar.classList.toggle('mobile-open');
      else {
        sidebar.classList.toggle('collapsed');
        localStorage.setItem('sidebar-collapsed', sidebar.classList.contains('collapsed'));
      }
    });
    if (localStorage.getItem('sidebar-collapsed') === 'true') sidebar.classList.add('collapsed');
  }
  if (themeButton) {
    const saved = localStorage.getItem('dashboard-theme');
    if (saved) document.documentElement.dataset.theme = saved;
    themeButton.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      localStorage.setItem('dashboard-theme', next);
    });
  }
  const LOG_AUTOSCROLL_KEY = 'job-log-autoscroll';

  function jobLogAutoscrollEnabled(pre) {
    const cb = pre?.closest('.card')?.querySelector('.job-log-autoscroll');
    return !cb || cb.checked;
  }

  function scrollJobLogToBottom(pre) {
    pre.scrollTop = pre.scrollHeight;
  }

  function initJobLogAutoscrollToggle(cb) {
    const saved = localStorage.getItem(LOG_AUTOSCROLL_KEY);
    if (saved !== null) cb.checked = saved === 'true';
    cb.addEventListener('change', () => {
      localStorage.setItem(LOG_AUTOSCROLL_KEY, String(cb.checked));
      const pre = cb.closest('.card')?.querySelector('.job-log-pre');
      if (cb.checked && pre) scrollJobLogToBottom(pre);
    });
  }

  document.querySelectorAll('.job-log-autoscroll').forEach(initJobLogAutoscrollToggle);
  document.querySelectorAll('.job-log-pre').forEach((pre) => {
    if (jobLogAutoscrollEnabled(pre)) scrollJobLogToBottom(pre);
  });

  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    const target = evt.detail.target;
    if (target?.id === 'instance-job-status') {
      const root = document.getElementById('instance-job-watch');
      const header = target.querySelector('[data-job-status]');
      if (root && header) {
        root.dataset.pendingPrevJobStatus = header.dataset.jobStatus || '';
      }
    }
    const pre = evt.detail.target;
    if (!pre?.classList?.contains('job-log-pre')) return;
    pre.dataset.logScrollTop = String(pre.scrollTop);
  });
  document.body.addEventListener('htmx:afterSwap', (evt) => {
    const pre = evt.detail.target;
    if (!pre?.classList?.contains('job-log-pre')) return;
    if (jobLogAutoscrollEnabled(pre)) scrollJobLogToBottom(pre);
    else if (pre.dataset.logScrollTop) pre.scrollTop = Number(pre.dataset.logScrollTop);
  });

  const AUTO_OPEN_UI_KEY = 'instance-auto-open-ui';
  let lastAutoOpenedJobId = null;

  function initInstanceAutoOpenUi(cb) {
    if (cb.dataset.autoOpenBound) return;
    cb.dataset.autoOpenBound = '1';
    const saved = localStorage.getItem(AUTO_OPEN_UI_KEY);
    if (saved !== null) cb.checked = saved === 'true';
    cb.addEventListener('change', () => {
      localStorage.setItem(AUTO_OPEN_UI_KEY, String(cb.checked));
    });
  }

  const ACTIVE_JOB_STATUSES = new Set(['queued', 'running']);

  function scheduleAutoOpenInstanceUi(root, prevStatus) {
    if (!root || root.dataset.jobType !== 'build_run') return;
    if (!ACTIVE_JOB_STATUSES.has(prevStatus)) return;

    const header = root.querySelector('[data-job-status="done"][data-job-type="build_run"]');
    const actions = root.querySelector('.instance-job-success-actions');
    if (!header || !actions) return;

    const jobId = header.dataset.jobId;
    if (lastAutoOpenedJobId === jobId) return;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const cb = root.querySelector('.instance-auto-open-ui');
        if (!cb?.checked) return;
        const link = root.querySelector('.instance-open-ui-link');
        const url = link?.dataset.uiUrl || link?.href;
        if (!url) return;
        actions.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        window.open(url, '_blank', 'noopener');
        lastAutoOpenedJobId = jobId;
      });
    });
  }

  document.querySelectorAll('.instance-auto-open-ui').forEach(initInstanceAutoOpenUi);

  const instanceJobWatch = document.getElementById('instance-job-watch');
  if (instanceJobWatch) {
    const initialHeader = instanceJobWatch.querySelector('[data-job-status]');
    if (initialHeader) {
      instanceJobWatch.dataset.lastJobStatus = initialHeader.dataset.jobStatus || '';
    }
  }

  document.body.addEventListener('htmx:afterSwap', (evt) => {
    const target = evt.detail.target;
    if (target?.id !== 'instance-job-status') return;
    target.querySelectorAll('.job-log-autoscroll').forEach(initJobLogAutoscrollToggle);
    target.querySelectorAll('.instance-auto-open-ui').forEach(initInstanceAutoOpenUi);

    const root = document.getElementById('instance-job-watch');
    const prevStatus = root?.dataset.pendingPrevJobStatus || root?.dataset.lastJobStatus || '';
    if (root) delete root.dataset.pendingPrevJobStatus;
    const header = root?.querySelector('[data-job-status]');
    if (root && header) root.dataset.lastJobStatus = header.dataset.jobStatus || '';

    scheduleAutoOpenInstanceUi(root, prevStatus);
  });
})();
