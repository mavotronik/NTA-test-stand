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

  function jobLogShell(el) {
    return el?.closest('.job-log-shell');
  }

  function jobLogAutoscrollEnabled(pre) {
    const cb = jobLogShell(pre)?.querySelector('.job-log-autoscroll');
    return !cb || cb.checked;
  }

  function scrollJobLogToBottom(pre) {
    pre.scrollTop = pre.scrollHeight;
  }

  function initJobLogAutoscrollToggle(cb) {
    if (cb.dataset.autoscrollBound) return;
    cb.dataset.autoscrollBound = '1';
    const saved = localStorage.getItem(LOG_AUTOSCROLL_KEY);
    if (saved !== null) cb.checked = saved === 'true';
    cb.addEventListener('change', () => {
      localStorage.setItem(LOG_AUTOSCROLL_KEY, String(cb.checked));
      const pre = jobLogShell(cb)?.querySelector('.job-log-pre');
      if (cb.checked && pre) scrollJobLogToBottom(pre);
    });
  }

  document.querySelectorAll('.job-log-autoscroll').forEach(initJobLogAutoscrollToggle);
  document.querySelectorAll('.job-log-pre').forEach((pre) => {
    if (jobLogAutoscrollEnabled(pre)) scrollJobLogToBottom(pre);
  });

  function redirectToLoginFromHtmx(evt) {
    const xhr = evt.detail.xhr;
    if (!xhr) return false;
    if (window.location.pathname === '/login') return false;
    let responsePath = '';
    try {
      responsePath = new URL(xhr.responseURL || '', window.location.origin).pathname;
    } catch (_) {
      responsePath = '';
    }
    if (xhr.status === 401 || responsePath === '/login') {
      evt.detail.shouldSwap = false;
      window.location.href = '/login';
      return true;
    }
    return false;
  }

  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (redirectToLoginFromHtmx(evt)) return;
    const target = evt.detail.target;
    if (target?.id === 'instance-job-status') {
      const root = instanceJobWatchRoot();
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
    const overlay = root.closest('.modal-overlay');
    if (overlay && !overlay.classList.contains('visible')) return;
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

  function instanceJobWatchRoot() {
    return document.getElementById('instance-job-watch');
  }

  function initInstanceJobWatch(root) {
    if (!root) return;
    const initialHeader = root.querySelector('[data-job-status]');
    if (initialHeader) {
      root.dataset.lastJobStatus = initialHeader.dataset.jobStatus || '';
    }
  }

  initInstanceJobWatch(instanceJobWatchRoot());

  window.jobLogPollVals = function jobLogPollVals(target) {
    const pre = target?.classList?.contains('job-log-pre')
      ? target
      : document.querySelector('.modal-overlay.visible .job-log-pre') || document.querySelector('.job-log-pre');
    if (!pre) return { after_id: 0 };
    let afterId = 0;
    pre.querySelectorAll('.job-log-line[data-log-id]').forEach((el) => {
      const id = parseInt(el.dataset.logId, 10);
      if (!Number.isNaN(id)) afterId = Math.max(afterId, id);
    });
    return { after_id: afterId };
  };

  let containerLogsAbort = null;

  function stopContainerLogsStream() {
    if (containerLogsAbort) {
      containerLogsAbort.abort();
      containerLogsAbort = null;
    }
  }

  function disableJobLogPolling(pre) {
    if (!pre || pre.dataset.jobLogPollingDisabled === '1') return;
    pre.dataset.jobLogPollingDisabled = '1';
    pre.removeAttribute('hx-get');
    pre.removeAttribute('hx-trigger');
    pre.removeAttribute('hx-swap');
    pre.removeAttribute('hx-vals');
    if (typeof htmx !== 'undefined') htmx.process(pre);
  }

  async function startContainerLogsStream(pre, url) {
    if (!pre || !url || pre.dataset.containerLogsActive === '1') return;
    pre.dataset.containerLogsActive = '1';
    stopContainerLogsStream();
    const ac = new AbortController();
    containerLogsAbort = ac;
    const decoder = new TextDecoder();
    try {
      const resp = await fetch(url, { credentials: 'same-origin', signal: ac.signal });
      if (!resp.ok || !resp.body) return;
      const reader = resp.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        pre.appendChild(document.createTextNode(decoder.decode(value, { stream: true })));
        if (jobLogAutoscrollEnabled(pre)) scrollJobLogToBottom(pre);
      }
    } catch (err) {
      if (err?.name !== 'AbortError') {
        pre.appendChild(document.createTextNode(`\n[ошибка потока логов: ${err?.message || err}]\n`));
      }
    } finally {
      if (containerLogsAbort === ac) containerLogsAbort = null;
    }
  }

  function maybeStartBuildRunContainerLogs(root) {
    if (!root || root.dataset.jobType !== 'build_run') return;
    const url = root.dataset.containerLogsUrl;
    if (!url) return;
    const header = root.querySelector('[data-job-status]');
    if (!header || header.dataset.jobStatus !== 'done') return;
    const pre = root.querySelector('.job-log-pre');
    if (!pre) return;
    disableJobLogPolling(pre);
    if (pre.dataset.containerLogsActive === '1') return;
    pre.appendChild(document.createTextNode('\n--- логи контейнера ---\n'));
    startContainerLogsStream(pre, url);
  }

  function initContainerLogsForModal(inner) {
    if (!inner) return;
    const directPre = inner.querySelector('.job-log-pre[data-container-logs-url]');
    if (directPre?.dataset.containerLogsUrl) {
      startContainerLogsStream(directPre, directPre.dataset.containerLogsUrl);
      return;
    }
    maybeStartBuildRunContainerLogs(inner.querySelector('#instance-job-watch') || instanceJobWatchRoot());
  }

  document.body.addEventListener('htmx:responseError', (evt) => {
    if (redirectToLoginFromHtmx(evt)) return;
    if (evt.detail.target?.id === 'instance-current-task' && evt.detail.xhr?.status === 404) {
      window.location.href = '/instances?message=instance+deleted';
    }
  });

  const modalHtmlCache = new Map();

  function modalHxLink(el) {
    return el?.closest?.(
      'a[hx-get][hx-target="#formModalInner"], a[hx-get][hx-target="#logModalInner"]',
    );
  }

  function modalOverlayForTarget(targetId) {
    if (targetId === 'formModalInner') return document.getElementById('formModal');
    if (targetId === 'logModalInner') return document.getElementById('logModal');
    return null;
  }

  function showModalLoadingPlaceholder(inner) {
    if (!inner || inner.querySelector('.js-form-modal-dialog, .js-log-modal-dialog')) return;
    inner.innerHTML =
      '<div class="modal modal-loading-placeholder" aria-busy="true" aria-label="Загрузка"><span class="mdi mdi-infinity modal-loading-spinner" aria-hidden="true"></span></div>';
  }

  function initFormModalInner(inner) {
    if (!inner) return;
    openOverlay(document.getElementById('formModal'));
    initInstancePortInput(inner.querySelector('#instance-port'));
    initInstanceSearchableSelects(inner);
    initInstanceSourceMode(inner);
    if (inner.querySelector('[data-host-metric-modal]')) {
      initHostMetricCharts(inner);
    }
    inner.querySelector('input:not([disabled]), select, textarea')?.focus();
  }

  function initLogModalInner(inner) {
    if (!inner) return;
    openOverlay(document.getElementById('logModal'));
    initInstanceJobWatch(instanceJobWatchRoot());
    inner.querySelectorAll('.job-log-autoscroll').forEach(initJobLogAutoscrollToggle);
    inner.querySelectorAll('.instance-auto-open-ui').forEach(initInstanceAutoOpenUi);
    inner.querySelectorAll('.job-log-pre').forEach((pre) => {
      if (jobLogAutoscrollEnabled(pre)) scrollJobLogToBottom(pre);
    });
    initContainerLogsForModal(inner);
  }

  function prefetchModalHtml(url) {
    if (!url || modalHtmlCache.has(url)) return;
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'text/html' } })
      .then((resp) => (resp.ok ? resp.text() : ''))
      .then((html) => {
        if (html) modalHtmlCache.set(url, html);
      })
      .catch(() => {});
  }

  document.body.addEventListener(
    'pointerenter',
    (e) => {
      const link = modalHxLink(e.target);
      if (!link) return;
      prefetchModalHtml(link.getAttribute('hx-get') || '');
    },
    { passive: true },
  );

  document.body.addEventListener(
    'click',
    (e) => {
      const link = modalHxLink(e.target);
      if (!link) return;
      const targetId = link.getAttribute('hx-target')?.replace(/^#/, '');
      const inner = targetId ? document.getElementById(targetId) : null;
      const overlay = modalOverlayForTarget(targetId);
      if (!inner || !overlay) return;
      openOverlay(overlay);
      showModalLoadingPlaceholder(inner);
    },
    true,
  );

  function openOverlay(overlay) {
    if (!overlay) return;
    overlay.classList.add('visible');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
  }

  function closeOverlay(overlay) {
    if (!overlay) return;
    if (overlay.id === 'logModal') stopContainerLogsStream();
    overlay.classList.remove('visible');
    overlay.setAttribute('aria-hidden', 'true');
    if (overlay.id && location.hash === '#' + overlay.id) {
      history.replaceState(null, '', location.pathname + location.search);
    }
    if (!document.querySelector('.modal-overlay.visible')) {
      document.body.classList.remove('modal-open');
    }
    if (overlay.classList.contains('page-modal') && overlay.dataset.closeHref) {
      window.location.href = overlay.dataset.closeHref;
    }
  }

  const SELF_UPDATE_RELOAD_KEY = 'tsm-self-update-reload';

  function dismissAllModals() {
    stopContainerLogsStream();
    document.querySelectorAll('.modal-overlay').forEach((overlay) => {
      overlay.classList.remove('visible');
      overlay.setAttribute('aria-hidden', 'true');
    });
    document.body.classList.remove('modal-open');
    const formInner = document.getElementById('formModalInner');
    const logInner = document.getElementById('logModalInner');
    if (formInner) formInner.innerHTML = '';
    if (logInner) logInner.innerHTML = '';
  }

  function hardReloadAfterSelfUpdate() {
    sessionStorage.setItem(SELF_UPDATE_RELOAD_KEY, '1');
    dismissAllModals();
    const url = new URL(window.location.href);
    url.searchParams.set('_', String(Date.now()));
    url.hash = '';
    window.location.replace(url.toString());
  }

  if (sessionStorage.getItem(SELF_UPDATE_RELOAD_KEY) === '1') {
    sessionStorage.removeItem(SELF_UPDATE_RELOAD_KEY);
    dismissAllModals();
    const cleanUrl = new URL(window.location.href);
    if (cleanUrl.searchParams.has('_')) {
      cleanUrl.searchParams.delete('_');
      history.replaceState(null, '', cleanUrl.toString());
    }
  }

  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    dismissAllModals();
    if (sessionStorage.getItem(SELF_UPDATE_RELOAD_KEY) === '1') {
      hardReloadAfterSelfUpdate();
    }
  });

  if (document.querySelector('.modal-overlay.visible, .modal-overlay:target')) {
    document.body.classList.add('modal-open');
  }

  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-open-modal]');
    if (opener) {
      const overlay = document.getElementById(opener.getAttribute('data-open-modal'));
      if (overlay) {
        e.preventDefault();
        openOverlay(overlay);
        return;
      }
    }
    const closer = e.target.closest('[data-close-modal]');
    if (closer) {
      const overlay = closer.closest('.modal-overlay');
      if (overlay) {
        e.preventDefault();
        if (overlay.id === 'confirmModal') closeConfirm();
        else closeOverlay(overlay);
      }
      return;
    }
    if (e.target.classList.contains('modal-overlay') && e.target.classList.contains('visible')) {
      if (e.target.id === 'confirmModal') closeConfirm();
      else closeOverlay(e.target);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (confirmOverlay?.classList.contains('visible')) {
      e.preventDefault();
      closeConfirm();
      return;
    }
    const open = [...document.querySelectorAll('.modal-overlay.visible')].at(-1);
    if (open) {
      e.preventDefault();
      closeOverlay(open);
    }
  });

  function initSearchableSelect(select) {
    if (!select || select.dataset.searchableBound) return;
    if (select.closest('.searchable-select')) return;
    select.dataset.searchableBound = '1';
    select.classList.remove('select');
    select.classList.add('searchable-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    const wrap = document.createElement('div');
    wrap.className = 'searchable-select';
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    const allowEmpty = select.id === 'instance-git-tag';
    const emptyLabel = allowEmpty
      ? [...select.options].find((o) => o.value === '')?.textContent || '— не выбран —'
      : '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'input searchable-select-input';
    input.id = select.id + '-input';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.autocomplete = 'off';
    input.placeholder = allowEmpty ? emptyLabel : 'Поиск…';
    const list = document.createElement('ul');
    list.className = 'searchable-select-dropdown';
    list.id = select.id + '-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    document.body.appendChild(list);
    input.setAttribute('aria-controls', list.id);
    wrap.appendChild(input);
    let open = false;
    let activeIndex = -1;
    let userFiltering = false;
    function syncInputFromSelect() {
      const opt = select.options[select.selectedIndex];
      if (allowEmpty && !select.value) {
        input.value = '';
        input.placeholder = emptyLabel;
      } else if (opt) {
        input.value = opt.textContent;
        input.placeholder = allowEmpty ? emptyLabel : 'Поиск…';
      } else {
        input.value = '';
      }
    }
    function positionList() {
      const r = input.getBoundingClientRect();
      list.style.top = Math.round(r.bottom + 4) + 'px';
      list.style.left = Math.round(r.left) + 'px';
      list.style.width = Math.round(r.width) + 'px';
    }
    function visibleOptions() {
      const q = userFiltering ? input.value.trim().toLowerCase() : '';
      return [...select.options].filter((o) => {
        if (!allowEmpty && o.value === '') return false;
        if (!q) return true;
        return o.textContent.toLowerCase().includes(q);
      });
    }
    function optionNodes() {
      return [...list.querySelectorAll('.searchable-select-option')];
    }
    function setActive(idx) {
      const nodes = optionNodes();
      nodes.forEach((n, i) => n.classList.toggle('is-active', i === idx));
      activeIndex = idx;
      if (idx >= 0 && nodes[idx]) nodes[idx].scrollIntoView({ block: 'nearest' });
    }
    function renderList() {
      list.replaceChildren();
      const opts = visibleOptions();
      if (!opts.length) {
        const li = document.createElement('li');
        li.className = 'searchable-select-empty';
        li.textContent = 'Ничего не найдено';
        list.appendChild(li);
        activeIndex = -1;
        return;
      }
      opts.forEach((o) => {
        const li = document.createElement('li');
        li.className = 'searchable-select-option';
        li.setAttribute('role', 'option');
        li.dataset.value = o.value;
        li.textContent = o.textContent;
        if (o.value === select.value) li.setAttribute('aria-selected', 'true');
        li.addEventListener('mousedown', (e) => {
          e.preventDefault();
          pick(o.value);
        });
        list.appendChild(li);
      });
      activeIndex = -1;
    }
    function openList() {
      open = true;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      renderList();
      positionList();
    }
    function closeList() {
      open = false;
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      activeIndex = -1;
      userFiltering = false;
      syncInputFromSelect();
    }
    function pick(value) {
      select.value = value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      closeList();
    }
    function onScrollOrResize() {
      if (open) positionList();
    }
    input.addEventListener('focus', () => {
      userFiltering = false;
      openList();
    });
    input.addEventListener('input', () => {
      userFiltering = true;
      if (!open) openList();
      else renderList();
      positionList();
    });
    input.addEventListener('blur', () => {
      setTimeout(closeList, 150);
    });
    input.addEventListener('keydown', (e) => {
      if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        e.preventDefault();
        openList();
        return;
      }
      const nodes = optionNodes();
      if (e.key === 'Escape') {
        e.preventDefault();
        closeList();
        return;
      }
      if (e.key === 'ArrowDown' && nodes.length) {
        e.preventDefault();
        setActive(Math.min(activeIndex + 1, nodes.length - 1));
        return;
      }
      if (e.key === 'ArrowUp' && nodes.length) {
        e.preventDefault();
        setActive(Math.max(activeIndex - 1, 0));
        return;
      }
      if (e.key === 'Enter' && open && activeIndex >= 0 && nodes[activeIndex]) {
        e.preventDefault();
        pick(nodes[activeIndex].dataset.value);
      }
    });
    select.addEventListener('change', syncInputFromSelect);
    window.addEventListener('scroll', onScrollOrResize, true);
    window.addEventListener('resize', onScrollOrResize);
    select._searchableSync = syncInputFromSelect;
    select._searchableInput = input;
    syncInputFromSelect();
  }
  function initInstanceSearchableSelects(root) {
    const scope = root && root.querySelector ? root : document;
    const form = scope.querySelector('#instance-form') || (scope.id === 'instance-form' ? scope : null);
    const selects = form
      ? [form.querySelector('#instance-git-tag'), form.querySelector('#instance-git-branch')]
      : [scope.querySelector('#instance-git-tag'), scope.querySelector('#instance-git-branch')];
    selects.filter(Boolean).forEach(initSearchableSelect);
  }
  function initInstancePortInput(input) {
    const hint = input?.form?.querySelector('#instance-port-error') || document.getElementById('instance-port-error');
    if (!input || !hint || input.dataset.portBound) return;
    input.dataset.portBound = '1';
    let occupied = [];
    try {
      occupied = JSON.parse(input.dataset.occupied || '[]');
    } catch (err) {
      occupied = [];
    }
    const byPort = new Map(occupied.map((row) => [Number(row.port), row]));
    function conflictMessage() {
      const raw = input.value.trim();
      if (!raw) return '';
      const value = Number(raw);
      if (!Number.isInteger(value)) return '';
      const hit = byPort.get(value);
      if (!hit) return '';
      return 'Порт ' + value + ' уже занят инстансом «' + hit.name + '»';
    }
    function showConflict() {
      const message = conflictMessage();
      input.classList.toggle('input-invalid', Boolean(message));
      if (message) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
      hint.hidden = !message;
      hint.textContent = message;
      return !message;
    }
    input.addEventListener('input', showConflict);
    if (input.form) {
      input.form.addEventListener('submit', (event) => {
        if (!showConflict()) event.preventDefault();
      });
    }
    showConflict();
  }
  function initInstanceSourceMode(root) {
    const scope = root && root.querySelector ? root : document;
    const form = scope.querySelector('#instance-form');
    if (!form || form.dataset.sourceModeBound) return;
    form.dataset.sourceModeBound = '1';
    const repoPanel = form.querySelector('#instance-source-repo');
    const pathPanel = form.querySelector('#instance-source-path');
    const branchCommitPanel = form.querySelector('#instance-source-branch-commit');
    const branchSelect = form.querySelector('#instance-git-branch');
    const branchInput = form.querySelector('#instance-git-branch-input');
    const tagSelect = form.querySelector('#instance-git-tag');
    const pathInput = form.querySelector('#instance-source-path-input');
    function syncTagCommit() {
      const mode = form.querySelector('input[name="source_mode"]:checked')?.value || 'repo';
      const tagChosen = !!(tagSelect && tagSelect.value);
      if (branchCommitPanel) branchCommitPanel.hidden = tagChosen;
      if (branchSelect && tagChosen) {
        const mainOpt = [...branchSelect.options].find((o) => o.value === 'main');
        if (mainOpt) branchSelect.value = 'main';
        if (branchSelect._searchableSync) branchSelect._searchableSync();
      }
      if (branchSelect) branchSelect.removeAttribute('required');
      if (branchInput) {
        if (mode === 'path' || tagChosen) branchInput.removeAttribute('required');
        else branchInput.setAttribute('required', '');
      }
    }
    function sync() {
      const mode = form.querySelector('input[name="source_mode"]:checked')?.value || 'repo';
      if (repoPanel) repoPanel.hidden = mode === 'path';
      if (pathPanel) pathPanel.hidden = mode !== 'path';
      if (pathInput) {
        if (mode === 'path') pathInput.setAttribute('required', '');
        else pathInput.removeAttribute('required');
      }
      syncTagCommit();
    }
    form.querySelectorAll('input[name="source_mode"]').forEach((el) => {
      el.addEventListener('change', sync);
    });
    if (tagSelect) tagSelect.addEventListener('change', syncTagCommit);
    sync();
  }
  function initImageBuildSourceMode(root) {
    const scope = root && root.querySelector ? root : document;
    const form = scope.querySelector('#image-build-form');
    if (!form || form.dataset.sourceModeBound) return;
    form.dataset.sourceModeBound = '1';
    const repoPanel = form.querySelector('#image-source-repo');
    const pathPanel = form.querySelector('#image-source-path');
    const branchCommitPanel = form.querySelector('#image-source-branch-commit');
    const branchSelect = form.querySelector('#image-git-branch');
    const tagSelect = form.querySelector('#image-git-tag');
    const pathInput = form.querySelector('#image-source-path-input');
    function syncTagCommit() {
      const tagChosen = !!(tagSelect && tagSelect.value);
      if (branchCommitPanel) branchCommitPanel.hidden = tagChosen;
      if (branchSelect && tagChosen) {
        const mainOpt = [...branchSelect.options].find((o) => o.value === 'main');
        if (mainOpt) branchSelect.value = 'main';
      }
    }
    function sync() {
      const mode = form.querySelector('input[name="source_mode"]:checked')?.value || 'repo';
      if (repoPanel) repoPanel.hidden = mode === 'path';
      if (pathPanel) pathPanel.hidden = mode !== 'path';
      if (pathInput) {
        if (mode === 'path') pathInput.setAttribute('required', '');
        else pathInput.removeAttribute('required');
      }
      syncTagCommit();
    }
    form.querySelectorAll('input[name="source_mode"]').forEach((el) => {
      el.addEventListener('change', sync);
    });
    if (tagSelect) tagSelect.addEventListener('change', syncTagCommit);
    sync();
  }

  initInstancePortInput(document.getElementById('instance-port'));
  initInstanceSearchableSelects(document);
  initInstanceSourceMode(document);
  initImageBuildSourceMode(document);

  const confirmOverlay = document.getElementById('confirmModal');
  const confirmDialog = confirmOverlay?.querySelector('.modal');
  const confirmTitle = document.getElementById('confirmModalTitle');
  const confirmBody = document.getElementById('confirmModalBody');
  const confirmIcon = document.getElementById('confirmModalIcon');
  const confirmOk = document.getElementById('confirmModalOk');
  const confirmCancel = document.getElementById('confirmModalCancel');
  const confirmClose = document.getElementById('confirmModalClose');
  let confirmOnOk = null;
  let confirmLastFocus = null;

  function getFocusable(root) {
    return [...root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(
      (el) => !el.disabled && el.getAttribute('aria-hidden') !== 'true',
    );
  }

  function setConfirmButtonLabel(btn, label, icon, expandExtra) {
    if (!btn) return;
    btn.replaceChildren();
    btn.classList.toggle('confirm-ok-expand', Boolean(expandExtra));
    if (expandExtra) btn.setAttribute('aria-label', label + expandExtra);
    else btn.removeAttribute('aria-label');
    if (icon) {
      const span = document.createElement('span');
      span.className = 'mdi ' + icon;
      btn.appendChild(span);
    }
    const text = document.createElement('span');
    text.className = 'confirm-ok-label';
    text.appendChild(document.createTextNode(label));
    if (expandExtra) {
      const extra = document.createElement('span');
      extra.className = 'confirm-ok-extra';
      extra.setAttribute('aria-hidden', 'true');
      const extraText = document.createElement('span');
      extraText.textContent = expandExtra;
      extra.appendChild(extraText);
      text.appendChild(extra);
    }
    btn.appendChild(text);
  }

  function setModalTone(tone) {
    const t = tone || 'danger';
    if (confirmDialog) confirmDialog.dataset.tone = t;
    if (!confirmOk) return;
    confirmOk.classList.remove('success', 'primary', 'danger');
    confirmOk.classList.add('button', t === 'success' ? 'success' : t === 'primary' ? 'primary' : 'danger');
  }

  function fillConfirmBody(message, items) {
    if (!confirmBody) return;
    confirmBody.replaceChildren();
    if (message) {
      const p = document.createElement('p');
      p.className = 'modal-text';
      p.textContent = message;
      confirmBody.appendChild(p);
    }
    if (!items?.length) return;
    const ul = document.createElement('ul');
    ul.className = 'modal-item-list';
    const max = 8;
    items.slice(0, max).forEach((name) => {
      const li = document.createElement('li');
      li.textContent = name;
      ul.appendChild(li);
    });
    if (items.length > max) {
      const li = document.createElement('li');
      li.className = 'modal-item-more';
      li.textContent = 'и ещё ' + (items.length - max);
      ul.appendChild(li);
    }
    confirmBody.appendChild(ul);
  }

  function closeConfirm() {
    if (!confirmOverlay?.classList.contains('visible')) return;
    confirmOnOk = null;
    document.removeEventListener('keydown', onConfirmKeydown);
    closeOverlay(confirmOverlay);
    confirmLastFocus?.focus?.();
  }

  function onConfirmKeydown(e) {
    if (e.key !== 'Tab' || !confirmDialog) return;
    const nodes = getFocusable(confirmDialog);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function openConfirm(opts) {
    const title = opts.title || 'Подтверждение';
    const message = opts.message || '';
    const onConfirm = opts.onConfirm;
    if (!confirmOverlay || !confirmDialog) {
      if (window.confirm(message || title)) onConfirm?.();
      return;
    }
    confirmLastFocus = document.activeElement;
    confirmTitle.textContent = title;
    setModalTone(opts.tone);
    setConfirmButtonLabel(confirmCancel, opts.cancel || 'Отмена');
    const okLabel = opts.ok || 'Подтвердить';
    let expandExtra = '';
    if (opts.okHover && opts.okHover !== okLabel) {
      expandExtra = opts.okHover.startsWith(okLabel) ? opts.okHover.slice(okLabel.length) : opts.okHover;
    }
    setConfirmButtonLabel(confirmOk, okLabel, expandExtra ? null : (opts.okIcon || opts.icon), expandExtra);
    if (confirmIcon) confirmIcon.className = 'mdi ' + (opts.icon || 'mdi-alert-outline');
    fillConfirmBody(message, opts.items);
    confirmOnOk = onConfirm;
    openOverlay(confirmOverlay);
    document.addEventListener('keydown', onConfirmKeydown);
    const focusBtn = opts.tone === 'danger' || opts.tone === 'warning' ? confirmCancel : confirmOk;
    (focusBtn || confirmCancel)?.focus();
  }

  function submitConfirmedForm(form) {
    const clone = document.createElement('form');
    clone.method = form.method || 'post';
    clone.action = form.action;
    clone.style.display = 'none';
    if (form.enctype) clone.enctype = form.enctype;
    form.querySelectorAll('input, select, textarea').forEach((el) => {
      if (!el.name || el.disabled) return;
      if (el instanceof HTMLInputElement && (el.type === 'submit' || el.type === 'button')) return;
      const field = document.createElement('input');
      field.type = 'hidden';
      field.name = el.name;
      if (el instanceof HTMLSelectElement) {
        field.value = el.options[el.selectedIndex]?.value ?? '';
      } else if (el instanceof HTMLTextAreaElement) {
        field.value = el.value;
      } else {
        field.value = el.value;
      }
      clone.appendChild(field);
    });
    document.body.appendChild(clone);
    clone.submit();
  }

  confirmOk?.addEventListener('click', () => {
    const fn = confirmOnOk;
    closeConfirm();
    fn?.();
  });

  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-confirm')) return;
    e.preventDefault();
    openConfirm({
      title: form.dataset.confirmTitle || 'Подтверждение',
      message: form.dataset.confirmMessage || '',
      cancel: form.dataset.confirmCancel || 'Отмена',
      ok: form.dataset.confirmOk || 'Подтвердить',
      okHover: form.dataset.confirmOkHover || '',
      tone: form.dataset.confirmTone || 'danger',
      icon: form.dataset.confirmIcon || 'mdi-alert-outline',
      onConfirm: () => submitConfirmedForm(form),
    });
  });

  window.instancesRowRevs = function instancesRowRevs() {
    const rows = document.querySelectorAll('#instances-rows tr[data-instance-id]');
    const revs = {};
    rows.forEach((tr) => {
      revs[tr.dataset.instanceId] = tr.dataset.instanceRev || '';
    });
    return JSON.stringify(revs);
  };

  window.imagesRowRevs = function imagesRowRevs() {
    const rows = document.querySelectorAll('#images-rows tr[data-image-id]');
    const revs = {};
    rows.forEach((tr) => {
      revs[tr.dataset.imageId] = tr.dataset.imageRev || '';
    });
    return JSON.stringify(revs);
  };

  const BULK_COPY = {
    start: {
      title: 'Запустить инстансы?',
      message: (n) => `Поставить в очередь запуск выбранных инстансов (${n})? Контейнеры стартуют с текущим образом.`,
      ok: 'Запустить',
      tone: 'success',
      icon: 'mdi-play',
    },
    stop: {
      title: 'Остановить инстансы?',
      message: (n) => `Поставить в очередь остановку выбранных инстансов (${n})? Контейнеры будут остановлены.`,
      ok: 'Остановить',
      tone: 'danger',
      icon: 'mdi-stop',
    },
    rebuild: {
      title: 'Пересобрать инстансы?',
      message: (n) => `Поставить в очередь пересборку выбранных инстансов (${n})? Для каждого будет сборка образа и запуск.`,
      ok: 'Пересобрать',
      tone: 'primary',
      icon: 'mdi-sync',
    },
  };

  const bulkForm = document.getElementById('instances-bulk-form');
  const instancesRowsBody = document.getElementById('instances-rows');
  const bulkSelectedIds = new Set();
  let restoreBulkChecks = () => {};
  if (bulkForm) {
    const actionInput = document.getElementById('instances-bulk-action');
    const selectAll = document.getElementById('instances-select-all');
    const rowChecks = () => document.querySelectorAll('.instances-row-check');
    const bulkButtons = bulkForm.querySelectorAll('[data-instances-bulk]');

    function syncBulkUi() {
      const checks = rowChecks();
      const checked = [...checks].filter((c) => c.checked);
      bulkButtons.forEach((btn) => {
        btn.disabled = checked.length === 0;
      });
      if (selectAll) {
        selectAll.indeterminate = checked.length > 0 && checked.length < checks.length;
        selectAll.checked = checks.length > 0 && checked.length === checks.length;
      }
    }

    restoreBulkChecks = function restoreBulkChecksImpl() {
      rowChecks().forEach((cb) => {
        cb.checked = bulkSelectedIds.has(cb.value);
      });
      syncBulkUi();
    };

    if (instancesRowsBody) {
      instancesRowsBody.addEventListener('change', (evt) => {
        const target = evt.target;
        if (!(target instanceof HTMLInputElement) || !target.classList.contains('instances-row-check')) return;
        if (target.checked) bulkSelectedIds.add(target.value);
        else bulkSelectedIds.delete(target.value);
        syncBulkUi();
      });
    }

    if (selectAll) {
      selectAll.addEventListener('change', () => {
        rowChecks().forEach((cb) => {
          cb.checked = selectAll.checked;
          if (selectAll.checked) bulkSelectedIds.add(cb.value);
          else bulkSelectedIds.delete(cb.value);
        });
        syncBulkUi();
      });
    }
    bulkButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.instancesBulk;
        const copy = BULK_COPY[action];
        if (!action || !actionInput || !copy) return;
        const selected = [...rowChecks()].filter((c) => c.checked);
        if (!selected.length) return;
        const names = selected.map((c) => c.dataset.instanceName || c.value);
        openConfirm({
          title: copy.title,
          message: copy.message(selected.length),
          items: names,
          ok: copy.ok,
          tone: copy.tone,
          icon: copy.icon,
          onConfirm: () => {
            actionInput.value = action;
            bulkForm.requestSubmit();
          },
        });
      });
    });
    restoreBulkChecks();
  }

  document.body.addEventListener('htmx:beforeRequest', (evt) => {
    const target = evt.detail.target;
    const verb = (evt.detail.requestConfig?.verb || 'get').toLowerCase();
    if (target?.id === 'formModalInner') {
      openOverlay(document.getElementById('formModal'));
      if (verb === 'get') showModalLoadingPlaceholder(target);
    }
    if (target?.id === 'logModalInner' && verb === 'get') {
      stopContainerLogsStream();
      openOverlay(document.getElementById('logModal'));
      showModalLoadingPlaceholder(target);
    }
    const url = evt.detail.pathInfo?.requestPath;
    if (
      url
      && target
      && (target.id === 'formModalInner' || target.id === 'logModalInner')
      && verb === 'get'
      && modalHtmlCache.has(url)
    ) {
      evt.preventDefault();
      target.innerHTML = modalHtmlCache.get(url);
      if (typeof htmx !== 'undefined') htmx.process(target);
      if (target.id === 'formModalInner') initFormModalInner(target);
      else initLogModalInner(target);
    }
  });

  const IMAGES_BULK_COPY = {
    unlink: {
      title: 'Отвязать образы?',
      message: (n) => `Снять привязку с инстансов для выбранных образов (${n})? Контейнеры и файлы на хосте не удаляются. Занятые сейчас образы будут пропущены.`,
      ok: 'Отвязать',
      tone: 'warning',
      icon: 'mdi-link-variant-off',
    },
    delete: {
      title: 'Удалить образы?',
      message: (n) => `Удалить с хоста выбранные образы (${n})? Используемые образы будут пропущены.`,
      ok: 'Удалить',
      tone: 'danger',
      icon: 'mdi-delete',
    },
  };

  const imagesBulkForm = document.getElementById('images-bulk-form');
  const imagesRowsBody = document.getElementById('images-rows');
  const imagesBulkSelectedIds = new Set();
  let restoreImagesBulkChecks = () => {};
  if (imagesBulkForm) {
    const actionInput = document.getElementById('images-bulk-action');
    const selectAll = document.getElementById('images-select-all');
    const rowChecks = () => document.querySelectorAll('.images-row-check');
    const bulkButtons = imagesBulkForm.querySelectorAll('[data-images-bulk]');

    function syncImagesBulkUi() {
      const checks = rowChecks();
      const checked = [...checks].filter((c) => c.checked);
      bulkButtons.forEach((btn) => {
        btn.disabled = checked.length === 0;
      });
      if (selectAll) {
        selectAll.indeterminate = checked.length > 0 && checked.length < checks.length;
        selectAll.checked = checks.length > 0 && checked.length === checks.length;
      }
    }

    restoreImagesBulkChecks = function restoreImagesBulkChecksImpl() {
      rowChecks().forEach((cb) => {
        cb.checked = imagesBulkSelectedIds.has(cb.value);
      });
      syncImagesBulkUi();
    };

    if (imagesRowsBody) {
      imagesRowsBody.addEventListener('change', (evt) => {
        const target = evt.target;
        if (!(target instanceof HTMLInputElement) || !target.classList.contains('images-row-check')) return;
        if (target.checked) imagesBulkSelectedIds.add(target.value);
        else imagesBulkSelectedIds.delete(target.value);
        syncImagesBulkUi();
      });
    }

    if (selectAll) {
      selectAll.addEventListener('change', () => {
        rowChecks().forEach((cb) => {
          cb.checked = selectAll.checked;
          if (selectAll.checked) imagesBulkSelectedIds.add(cb.value);
          else imagesBulkSelectedIds.delete(cb.value);
        });
        syncImagesBulkUi();
      });
    }

    bulkButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.imagesBulk;
        const copy = IMAGES_BULK_COPY[action];
        if (!action || !actionInput || !copy) return;
        const selected = [...rowChecks()].filter((c) => c.checked);
        if (!selected.length) return;
        const names = selected.map((c) => c.dataset.imageLabel || c.value);
        openConfirm({
          title: copy.title,
          message: copy.message(selected.length),
          items: names,
          ok: copy.ok,
          tone: copy.tone,
          icon: copy.icon,
          onConfirm: () => {
            actionInput.value = action;
            imagesBulkForm.requestSubmit();
          },
        });
      });
    });
    restoreImagesBulkChecks();
  }

  document.body.addEventListener('htmx:afterSwap', (evt) => {
    const target = evt.detail.target;
    if (target?.id === 'instances-rows') {
      restoreBulkChecks();
    }
    if (target?.id === 'images-rows') {
      restoreImagesBulkChecks();
    }
    if (target?.id === 'formModalInner') {
      const url = evt.detail.pathInfo?.requestPath;
      if (url && target.innerHTML) modalHtmlCache.set(url, target.innerHTML);
      initFormModalInner(target);
      return;
    }
    if (target?.id === 'logModalInner') {
      const url = evt.detail.pathInfo?.requestPath;
      if (url && target.innerHTML) modalHtmlCache.set(url, target.innerHTML);
      initLogModalInner(target);
      return;
    }
    if (target?.id !== 'instance-job-status') return;
    target.querySelectorAll('.job-log-autoscroll').forEach(initJobLogAutoscrollToggle);
    target.querySelectorAll('.instance-auto-open-ui').forEach(initInstanceAutoOpenUi);

    const root = instanceJobWatchRoot();
    const prevStatus = root?.dataset.pendingPrevJobStatus || root?.dataset.lastJobStatus || '';
    if (root) delete root.dataset.pendingPrevJobStatus;
    const header = root?.querySelector('[data-job-status]');
    if (root && header) root.dataset.lastJobStatus = header.dataset.jobStatus || '';

    scheduleAutoOpenInstanceUi(root, prevStatus);
    maybeStartBuildRunContainerLogs(root);
    if (target?.id === 'self-update-status') {
      handleSelfUpdateStatus(target);
    }
    if (target?.querySelector?.('.js-self-update-progress') || target?.classList?.contains('js-self-update-progress')) {
      initFormModalInner(target.id === 'formModalInner' ? target : target.querySelector('#formModalInner') || target);
    }
  });

  const selfUpdateState = {
    toastShown: false,
    reloadStarted: false,
  };

  function showSelfUpdateToast(message) {
    const container = document.getElementById('toastContainer');
    if (!container || !message) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = '<span class="mdi mdi-update"></span><span></span>';
    el.querySelector('span:last-child').textContent = message;
    container.appendChild(el);
    window.setTimeout(() => el.remove(), 12000);
  }

  function pollHealthAndReload() {
    if (selfUpdateState.reloadStarted) return;
    selfUpdateState.reloadStarted = true;
    let attempts = 0;
    const tick = () => {
      attempts += 1;
      fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' })
        .then((r) => {
          if (r.ok) {
            hardReloadAfterSelfUpdate();
            return;
          }
          throw new Error('not ready');
        })
        .catch(() => {
          if (attempts < 120) window.setTimeout(tick, 1000);
        });
    };
    window.setTimeout(tick, 1500);
  }

  function handleSelfUpdateStatus(el) {
    if (!el?.dataset) return;
    const phase = el.dataset.phase || '';
    const restart = el.dataset.restartPending === '1';
    const by = (el.dataset.initiatedBy || '').trim();
    if ((phase === 'pulling' || phase === 'restarting' || restart) && !selfUpdateState.toastShown) {
      selfUpdateState.toastShown = true;
      const who = by ? ` ${by}` : '';
      showSelfUpdateToast(`Систему обновляет${who}. Страница перезагрузится.`);
    }
    if (phase === 'restarting' || restart) {
      pollHealthAndReload();
    }
  }

  document.getElementById('self-update-status')?.dataset &&
    handleSelfUpdateStatus(document.getElementById('self-update-status'));

  const hostMetricPalette = { green: '#22c55e', orange: '#f97316', red: '#ef4444' };

  function readHostMetricPalette() {
    const root = document.documentElement;
    const styles = getComputedStyle(root);
    return {
      green: styles.getPropertyValue('--success').trim() || hostMetricPalette.green,
      orange: hostMetricPalette.orange,
      red: styles.getPropertyValue('--danger').trim() || hostMetricPalette.red,
    };
  }

  function parseHexColor(hex) {
    const h = hex.replace('#', '').trim();
    if (h.length === 3) {
      return [
        parseInt(h[0] + h[0], 16),
        parseInt(h[1] + h[1], 16),
        parseInt(h[2] + h[2], 16),
      ];
    }
    if (h.length >= 6) {
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    }
    return [34, 197, 94];
  }

  function mixColors(c1, c2, t) {
    const a = parseHexColor(c1);
    const b = parseHexColor(c2);
    const r = Math.round(a[0] + (b[0] - a[0]) * t);
    const g = Math.round(a[1] + (b[1] - a[1]) * t);
    const bl = Math.round(a[2] + (b[2] - a[2]) * t);
    return `rgb(${r},${g},${bl})`;
  }

  function metricColorForValue(pct, palette) {
    const p = Math.max(0, Math.min(100, pct));
    if (p <= 50) return mixColors(palette.green, palette.orange, p / 50);
    if (p <= 95) return mixColors(palette.orange, palette.red, (p - 50) / 45);
    return palette.red;
  }

  function colorValueForMetric(metric, value, cpuCount) {
    if (metric === 'load') {
      const cpus = Math.max(1, Number(cpuCount) || 1);
      return Math.min(100, (Number(value) / cpus) * 100);
    }
    return Number(value);
  }

  function fetchHostMetricHistory(metric, hours) {
    const q = new URLSearchParams({ metric, hours: String(hours) });
    return fetch(`/api/host-metrics/history?${q}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    }).then((r) => (r.ok ? r.json() : null));
  }

  const hostMetricChartState = new WeakMap();
  const hostMetricBaseBitmap = new WeakMap();

  function hostMetricPayloadSignature(payload) {
    const pts = payload?.points;
    if (!pts?.length) return '0';
    const last = pts[pts.length - 1];
    return `${pts.length}:${last.ts}:${last.value}`;
  }

  function saveHostMetricChartBase(canvas) {
    if (!canvas.width || !canvas.height) return;
    const base = document.createElement('canvas');
    base.width = canvas.width;
    base.height = canvas.height;
    base.getContext('2d').drawImage(canvas, 0, 0);
    hostMetricBaseBitmap.set(canvas, base);
  }

  function restoreHostMetricChartBase(canvas) {
    const base = hostMetricBaseBitmap.get(canvas);
    if (!base?.width || canvas.width !== base.width || canvas.height !== base.height) return false;
    const dpr = window.devicePixelRatio || 1;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(base, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }

  function formatHostMetricValue(value, valueKind, unit) {
    const n = Number(value);
    if (Number.isNaN(n)) return '—';
    if (valueKind === 'load') return n.toFixed(2);
    return `${n.toFixed(1)}${unit || '%'}`;
  }

  function formatHostMetricTime(ts) {
    return new Date(ts * 1000).toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function setupHostMetricCanvasSize(canvas) {
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function buildHostMetricLayout(canvas, payload, w, h) {
    const metric = canvas.dataset.metric || payload?.metric || 'cpu';
    const cpuCount = canvas.dataset.cpuCount || payload?.cpu_count || 1;
    const valueKind = payload?.value_kind || (metric === 'load' ? 'load' : 'percent');
    const unit = payload?.unit ?? (valueKind === 'load' ? '' : '%');
    const points = payload?.points || [];
    const isCardBg = canvas.dataset.chartRole === 'card-bg';
    const pad = isCardBg
      ? { l: 0, r: 0, t: 8, b: 8 }
      : { l: 48, r: 12, t: 14, b: 32 };
    const innerW = w - pad.l - pad.r;
    const innerH = h - pad.t - pad.b;
    if (!points.length) {
      return { metric, cpuCount, valueKind, unit, points, values: [], pad, innerW, innerH, isCardBg, empty: true };
    }

    const values = points.map((p) => Number(p.value));
    let yMin = 0;
    let yMax = 100;
    if (valueKind === 'load') {
      yMin = Math.min(...values);
      yMax = Math.max(...values);
      if (yMin === yMax) {
        yMin = Math.max(0, yMin - 0.5);
        yMax = yMax + 0.5;
      } else {
        const padY = (yMax - yMin) * 0.08;
        yMin = Math.max(0, yMin - padY);
        yMax = yMax + padY;
      }
    }

    const tMin = points[0].ts;
    const tMax = points[points.length - 1].ts;
    const tSpan = tMax - tMin || 1;

    function xAtIndex(i) {
      return pad.l + ((points[i].ts - tMin) / tSpan) * innerW;
    }
    function xAtTs(ts) {
      return pad.l + ((ts - tMin) / tSpan) * innerW;
    }
    function yAt(val) {
      const ratio = (val - yMin) / (yMax - yMin || 1);
      return pad.t + innerH - ratio * innerH;
    }

    return {
      metric,
      cpuCount,
      valueKind,
      unit,
      points,
      values,
      pad,
      innerW,
      innerH,
      isCardBg,
      yMin,
      yMax,
      tMin,
      tMax,
      tSpan,
      xAtIndex,
      xAtTs,
      yAt,
      empty: false,
    };
  }

  function drawHostMetricAxes(ctx, layout) {
    const secondary =
      getComputedStyle(document.documentElement).getPropertyValue('--text-secondary').trim() || '#6b7280';
    const border = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#e1e4e8';
    const { pad, innerW, innerH, yMin, yMax, valueKind, unit, tMin, tMax, xAtTs } = layout;
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.l, pad.t);
    ctx.lineTo(pad.l, pad.t + innerH);
    ctx.lineTo(pad.l + innerW, pad.t + innerH);
    ctx.stroke();

    ctx.fillStyle = secondary;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const yTicks = valueKind === 'load' ? 4 : 5;
    for (let i = 0; i <= yTicks; i += 1) {
      const ratio = i / yTicks;
      const val = yMax - ratio * (yMax - yMin);
      const y = pad.t + ratio * innerH;
      ctx.fillText(formatHostMetricValue(val, valueKind, unit), pad.l - 8, y);
    }

    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const xLabels = [
      { ts: tMin, align: 'left' },
      { ts: tMin + (tMax - tMin) / 2, align: 'center' },
      { ts: tMax, align: 'right' },
    ];
    xLabels.forEach(({ ts, align }) => {
      const x = xAtTs(ts);
      ctx.textAlign = align;
      ctx.fillText(formatHostMetricTime(ts), x, pad.t + innerH + 8);
    });
  }

  function drawHostMetricChart(canvas, payload) {
    const { ctx, w, h } = setupHostMetricCanvasSize(canvas);
    ctx.clearRect(0, 0, w, h);
    const palette = readHostMetricPalette();
    const layout = buildHostMetricLayout(canvas, payload, w, h);
    hostMetricChartState.set(canvas, { layout, payload: payload || {} });

    if (layout.empty) {
      if (!layout.isCardBg) {
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-secondary').trim() || '#6b7280';
        ctx.font = '12px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Накапливаем данные…', w / 2, h / 2 + 4);
      }
      return;
    }

    if (!layout.isCardBg) drawHostMetricAxes(ctx, layout);

    const { points, values, metric, cpuCount, xAtIndex, yAt } = layout;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    for (let i = 1; i < points.length; i += 1) {
      const v0 = values[i - 1];
      const v1 = values[i];
      const x0 = xAtIndex(i - 1);
      const y0 = yAt(v0);
      const x1 = xAtIndex(i);
      const y1 = yAt(v1);
      const steps = 12;
      for (let s = 0; s < steps; s += 1) {
        const t0 = s / steps;
        const t1 = (s + 1) / steps;
        const vm = v0 + (v1 - v0) * ((t0 + t1) / 2);
        const colorVal = colorValueForMetric(metric, vm, cpuCount);
        ctx.strokeStyle = metricColorForValue(colorVal, palette);
        ctx.beginPath();
        ctx.moveTo(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0);
        ctx.lineTo(x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1);
        ctx.stroke();
      }
    }

    if (!layout.isCardBg) {
      const last = points.length - 1;
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-secondary').trim() || '#6b7280';
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'bottom';
      const unitHint = layout.valueKind === 'load' ? 'load' : layout.unit || '%';
      ctx.fillText(unitHint, w - 8, h - 6);
    }
    saveHostMetricChartBase(canvas);
  }

  function nearestHostMetricPointIndex(layout, mouseX) {
    const { points, pad, innerW, tMin, tSpan } = layout;
    if (!points.length || mouseX < pad.l || mouseX > pad.l + innerW) return -1;
    const rel = (mouseX - pad.l) / innerW;
    const targetTs = tMin + rel * tSpan;
    let best = 0;
    let bestDist = Math.abs(points[0].ts - targetTs);
    for (let i = 1; i < points.length; i += 1) {
      const dist = Math.abs(points[i].ts - targetTs);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    return best;
  }

  function drawHostMetricHover(canvas, index) {
    const stored = hostMetricChartState.get(canvas);
    if (!stored?.layout || stored.layout.empty || index < 0) return;
    if (!restoreHostMetricChartBase(canvas)) drawHostMetricChart(canvas, stored.payload);
    const { layout } = stored;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const point = layout.points[index];
    const x = layout.xAtIndex(index);
    const y = layout.yAt(index.value);
    const border = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#e1e4e8';
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, layout.pad.t);
    ctx.lineTo(x, layout.pad.t + layout.innerH);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--primary').trim() || '#3b82f6';
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    return { x, y, point };
  }

  function bindHostMetricModalChart(canvas) {
    if (canvas.dataset.chartTooltipBound === '1') return;
    const wrap = canvas.closest('.host-metric-chart-interactive');
    const tooltip = wrap?.querySelector('.host-metric-chart-tooltip');
    if (!wrap || !tooltip) return;
    canvas.dataset.chartTooltipBound = '1';

    function hideTooltip() {
      tooltip.hidden = true;
      if (!restoreHostMetricChartBase(canvas)) {
        const stored = hostMetricChartState.get(canvas);
        if (stored?.payload) drawHostMetricChart(canvas, stored.payload);
      }
    }

    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const stored = hostMetricChartState.get(canvas);
      if (!stored?.layout || stored.layout.empty || stored.layout.isCardBg) return;
      const index = nearestHostMetricPointIndex(stored.layout, mouseX);
      if (index < 0) {
        hideTooltip();
        return;
      }
      const hover = drawHostMetricHover(canvas, index);
      if (!hover) return;
      const { point } = hover;
      const { valueKind, unit } = stored.layout;
      tooltip.innerHTML = `<strong>${formatHostMetricTime(point.ts)}</strong>${formatHostMetricValue(point.value, valueKind, unit)}`;
      tooltip.hidden = false;
      let left = hover.x;
      let top = hover.y;
      const pad = 12;
      left = Math.max(pad, Math.min(rect.width - pad, left));
      top = Math.max(pad + 20, top);
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
    });

    canvas.addEventListener('mouseleave', hideTooltip);
  }

  function applyHostMetricPayload(canvas, payload) {
    if (!payload) return;
    const sig = hostMetricPayloadSignature(payload);
    if (canvas.dataset.chartSig === sig) return;
    canvas.dataset.chartSig = sig;
    if (payload.cpu_count != null) canvas.dataset.cpuCount = String(payload.cpu_count);
    drawHostMetricChart(canvas, payload);
    canvas.dataset.chartReady = '1';
  }

  function initHostMetricCanvas(canvas) {
    if (!canvas || canvas.dataset.chartLoading === '1') return;
    if (canvas.dataset.chartRole === 'card-bg' && canvas.dataset.chartReady === '1') return;
    const metric = canvas.dataset.metric;
    const hours = canvas.dataset.historyHours || '24';
    if (!metric) return;
    canvas.dataset.chartLoading = '1';
    fetchHostMetricHistory(metric, hours)
      .then((payload) => {
        applyHostMetricPayload(canvas, payload);
        if (!canvas.dataset.chartRole) bindHostMetricModalChart(canvas);
      })
      .catch(() => {})
      .finally(() => {
        delete canvas.dataset.chartLoading;
      });
  }

  function refreshHostMetricCardCharts() {
    document
      .querySelectorAll('#host-metrics-panel canvas.host-metric-chart-canvas[data-chart-role="card-bg"]')
      .forEach((canvas) => {
        if (canvas.dataset.chartLoading === '1') return;
        const metric = canvas.dataset.metric;
        const hours = canvas.dataset.historyHours || '24';
        if (!metric) return;
        fetchHostMetricHistory(metric, hours)
          .then((payload) => applyHostMetricPayload(canvas, payload))
          .catch(() => {});
      });
  }

  function initHostMetricCharts(root) {
    const scope = root || document;
    scope.querySelectorAll('canvas.host-metric-chart-canvas').forEach(initHostMetricCanvas);
  }

  initHostMetricCharts(document.getElementById('host-metrics-panel'));

  const hostMetricsLive = document.getElementById('host-metrics-live');
  const hostMetricsChartSec = Math.max(1, parseFloat(hostMetricsLive?.dataset.chartRefreshSec || '5'));
  if (hostMetricsLive) {
    setInterval(refreshHostMetricCardCharts, hostMetricsChartSec * 1000);
  }

  document.body.addEventListener('htmx:afterSwap', (evt) => {
    const target = evt.detail.target;
    if (target?.id === 'formModalInner') {
      const progress = target.querySelector('.js-self-update-progress');
      if (progress) openOverlay(document.getElementById('formModal'));
    }
  });
})();
