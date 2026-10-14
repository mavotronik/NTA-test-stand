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
  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    const pre = evt.detail.target;
    if (!pre?.classList?.contains('job-log-pre')) return;
    pre.dataset.logScrollTop = String(pre.scrollTop);
  });
  document.body.addEventListener('htmx:afterSwap', (evt) => {
    const pre = evt.detail.target;
    if (!pre?.classList?.contains('job-log-pre')) return;
    if (pre.dataset.logScrollTop) {
      pre.scrollTop = Number(pre.dataset.logScrollTop);
    }
  });
})();
