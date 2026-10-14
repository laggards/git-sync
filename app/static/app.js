(() => {
  const token = document.querySelector('meta[name="page-token"]')?.content || '';
  const projectList = document.querySelector('#project-list');
  const syncLog = document.querySelector('#sync-log');
  const clearLogsButton = document.querySelector('#clear-logs');
  let polling = false;
  let logsPolling = false;
  let lastSnapshot = '';

  clearLogsButton?.addEventListener('click', clearLogs);

  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };


  function formatTimestamp(value) {
    if (!value) return '\u5c1a\u65e0\u8bb0\u5f55';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).format(date);
  }

  function renderProject(project, globallyBusy) {
    const row = element('tr');
    row.dataset.projectId = project.id;
    row.append(element('th', project.name));
    row.lastElementChild.scope = 'row';
    row.append(
      element('td', project.repository),
      renderBranchSelector(project, globallyBusy),
      element('td', project.workspace_relative),
      element('td', project.workspace_status || '未知'),
      element('td', project.last_job?.status === 'running'
        ? '正在同步'
        : formatTimestamp(project.last_job?.finished_at)),
      element('td', project.last_job?.status === 'running'
        ? '\u6b63\u5728\u540c\u6b65'
        : (project.last_job?.result || '\u6682\u65e0\u8bb0\u5f55')),
    );

    const actions = element('td');
    actions.className = 'project-actions';
    const button = element('button', '拉取代码');
    button.className = 'link-button';
    button.type = 'button';
    button.disabled = Boolean(globallyBusy);
    button.addEventListener('click', () => startUpdate(project.id));
    actions.append(button);

    if (project.url) {
      const link = element('a', '访问项目');
      link.className = 'project-link';
      link.href = project.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      actions.append(link);
    }

    row.append(actions);
    return row;
  }

  function renderBranchSelector(project, globallyBusy) {
    const cell = element('td');
    const select = element('select');
    select.className = 'branch-select';
    select.setAttribute('aria-label', `${project.name} \u5206\u652f`);
    select.dataset.currentBranch = project.branch;
    select.disabled = true;
    const current = element('option', project.branch);
    current.value = project.branch;
    select.append(current);
    select.addEventListener('change', () => saveBranch(project.id, select));
    const loadButton = element('button', '\u8bfb\u53d6\u5206\u652f');
    loadButton.className = 'load-branches link-button';
    loadButton.type = 'button';
    loadButton.disabled = Boolean(globallyBusy);
    loadButton.addEventListener('click', () => loadBranches(project.id, select, loadButton));
    cell.append(select, loadButton);
    return cell;
  }

  // Keep the server-rendered controls usable if the initial projects API call fails.
  projectList?.querySelectorAll('.load-branches').forEach(loadButton => {
    const row = loadButton.closest('tr');
    const select = row?.querySelector('.branch-select');
    if (!select) return;
    select.dataset.currentBranch = select.value;
    loadButton.addEventListener('click', () => {
      loadBranches(loadButton.dataset.projectId, select, loadButton);
    });
  });

  async function loadBranches(projectId, select, loadButton) {
    if (select.dataset.loaded === 'true' || select.dataset.loading === 'true') return;
    select.dataset.loading = 'true';
    loadButton.disabled = true;
    loadButton.textContent = '\u8bfb\u53d6\u4e2d';
    select.disabled = true;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/branches`, {
        headers: { 'X-Page-Token': token, Accept: 'application/json' },
        cache: 'no-store',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '\u8bfb\u53d6\u8fdc\u7aef\u5206\u652f\u5931\u8d25');

      const currentBranch = select.dataset.currentBranch;
      const options = data.branches.map(branch => {
        const option = element('option', branch);
        option.value = branch;
        return option;
      });
      if (!data.branches.includes(currentBranch)) {
        const missing = element('option', `${currentBranch} (\u8fdc\u7aef\u4e0d\u5b58\u5728)`);
        missing.value = currentBranch;
        missing.disabled = true;
        options.unshift(missing);
      }
      select.replaceChildren(...options);
      select.value = currentBranch;
      select.dataset.loaded = 'true';
      loadButton.hidden = true;
    } catch (error) {
      select.title = error.message || '\u8bfb\u53d6\u8fdc\u7aef\u5206\u652f\u5931\u8d25';
      window.alert(select.title);
    } finally {
      delete select.dataset.loading;
      select.disabled = select.dataset.loaded !== 'true';
      loadButton.disabled = false;
      if (select.dataset.loaded !== 'true') loadButton.textContent = '\u91cd\u8bd5';
    }
  }

  async function saveBranch(projectId, select) {
    const previousBranch = select.dataset.currentBranch;
    const branch = select.value;
    if (!branch || branch === previousBranch) return;
    select.disabled = true;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/branch`, {
        method: 'POST',
        headers: {
          'X-Page-Token': token,
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ branch }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '\u4fdd\u5b58\u5206\u652f\u5931\u8d25');
      select.dataset.currentBranch = data.branch;
      await refresh();
    } catch (error) {
      select.value = previousBranch;
      window.alert(error.message || '\u4fdd\u5b58\u5206\u652f\u5931\u8d25');
    } finally {
      select.disabled = false;
    }
  }

  async function refreshLogs() {
    if (logsPolling) return;
    logsPolling = true;
    try {
      const response = await fetch('/api/jobs', {
        headers: { Accept: 'application/json' },
        cache: 'no-cache',
      });
      if (response.status === 304 || !response.ok) return;
      const data = await response.json();
      const separator = `\n\n${'-'.repeat(72)}\n\n`;
      const combined = data.jobs.map(job => {
        const project = job.project_name || job.project_id;
        const started = formatTimestamp(job.started_at);
        const finished = job.finished_at
          ? `\n\u7ed3\u675f\u65f6\u95f4：${formatTimestamp(job.finished_at)}`
          : '';
        const title = `[${started}] ${project} (${job.branch}) - ${job.status}${finished}`;
        const log = job.log || job.error || job.result || '\u6682\u65e0\u540c\u6b65\u65e5\u5fd7';
        return `${title}\n${log}`;
      }).join(separator) || '\u6682\u65e0\u540c\u6b65\u65e5\u5fd7';
      const wasAtNewest = syncLog.scrollTop === 0;
      if (syncLog.value !== combined) {
        syncLog.value = combined;
        if (wasAtNewest) syncLog.scrollTop = 0;
      }
    } catch (_) {
      // Keep the current logs visible while the API is temporarily unavailable.
    } finally {
      logsPolling = false;
    }
  }

  async function clearLogs() {
    if (!window.confirm('\u786e\u5b9a\u6e05\u7a7a\u6240\u6709\u4efb\u52a1\u8bb0\u5f55\u548c\u540c\u6b65\u65e5\u5fd7\u5417\uff1f')) return;
    clearLogsButton.disabled = true;
    try {
      const response = await fetch('/api/jobs/clear', {
        method: 'POST',
        headers: { 'X-Page-Token': token, Accept: 'application/json' },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '\u6e05\u7a7a\u65e5\u5fd7\u5931\u8d25');
      syncLog.value = '\u6682\u65e0\u540c\u6b65\u65e5\u5fd7';
      lastSnapshot = '';
      await refresh();
    } catch (error) {
      window.alert(error.message || '\u6e05\u7a7a\u65e5\u5fd7\u5931\u8d25');
    } finally {
      clearLogsButton.disabled = false;
    }
  }

  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch('/api/projects', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error('读取项目状态失败');
      const data = await response.json();
      const snapshot = JSON.stringify(data);
      if (snapshot !== lastSnapshot) {
        if (data.projects.length) {
          projectList.replaceChildren(...data.projects.map(item => renderProject(item, data.busy)));
        }
        lastSnapshot = snapshot;
      }
      refreshLogs();
    } catch (_) {
      // Preserve the current table while the API is temporarily unavailable.
    } finally {
      polling = false;
    }
  }

  async function startUpdate(projectId) {
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/update`, {
        method: 'POST',
        headers: { 'X-Page-Token': token, Accept: 'application/json' },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法启动同步');
      await refresh();
    } catch (error) {
      window.alert(error.message || '同步请求失败');
      await refresh();
    }
  }

  refresh();
  window.setInterval(refresh, 1500);
})();
