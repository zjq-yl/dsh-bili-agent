const $ = id => document.getElementById(id)
// Keep the access token out of requests' URLs and browser history.
const fragment = location.hash.slice(1)
if (/^[a-f0-9]{64}$/.test(fragment)) {
  sessionStorage.setItem('bili-panel-token', fragment)
  history.replaceState(null, '', location.pathname)
}
const token = sessionStorage.getItem('bili-panel-token') || ''
const pages = {
  overview: ['工作台', '工作台', '看看小黎在忙什么，以及今天发现了什么。'],
  automation: ['浏览与推荐', '浏览与推荐', '安排视频 Agent 的浏览时间与主动推荐。'],
  videos: ['视频历史', '视频历史', '浏览看过的内容，点击视频查看总结与评价。'],
  messages: ['私信会话', '私信会话', '查看回复、处理状态与好友点播。'],
  'friends-section': ['好友', '好友与推荐', '管理兴趣档案和推送偏好。'],
  persona: ['人设与记忆', '人设与记忆', '编辑性格、内容偏好和表达边界。'],
  actions: ['公开互动', '公开互动', '设置公开评论与视频互动，核对执行结果。'],
  logs: ['运行记录', '运行记录', '查看后台事件，检查账号与模型连接。'],
}
function selectPanel(id) {
  const tab = document.querySelector(`[data-panel="${id}"]`)
  if (!tab) return
  for (const item of tab.parentElement.querySelectorAll('[data-panel]')) {
    const selected = item === tab
    item.setAttribute('aria-selected', String(selected)); item.tabIndex = selected ? 0 : -1
    $(item.dataset.panel).hidden = !selected
  }
}
for (const list of document.querySelectorAll('.page-tabs')) {
  list.addEventListener('click', event => {
    const tab = event.target.closest('[data-panel]')
    if (tab) selectPanel(tab.dataset.panel)
  })
  list.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    const tabs = [...list.querySelectorAll('[data-panel]')], index = tabs.indexOf(document.activeElement)
    if (index < 0) return
    event.preventDefault()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    selectPanel(tabs[next].dataset.panel); tabs[next].focus()
  })
}
function navigate(view) {
  if (!pages[view]) view = 'overview'
  $('notice').hidden = true
  for (const section of document.querySelectorAll('main > section')) section.hidden = section.id !== view
  for (const link of document.querySelectorAll('nav a')) {
    const active = link.getAttribute('href') === `#${view}`
    link.classList.toggle('active', active)
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current')
  }
  ;[$('page-label').textContent, $('page-title').textContent, $('page-description').textContent] = pages[view]
  history.replaceState(null, '', `${location.pathname}#${view}`)
  window.scrollTo({ top: 0 })
  if (view === 'videos') void loadLibrary()
}
for (const link of document.querySelectorAll('nav a, .inline-route, .welcome-actions a')) link.addEventListener('click', event => {
  event.preventDefault()
  navigate(link.getAttribute('href').slice(1))
  const focusId = link.dataset.focus
  if (focusId === 'video-input') selectPanel('video-tools')
  else if (link.getAttribute('href') === '#videos') selectPanel('video-history')
  if (link.getAttribute('href') === '#automation') selectPanel('browse-plan')
  if (focusId) requestAnimationFrame(() => $(focusId)?.focus())
})
let current = null
let refreshing = false
let modelVerified = false
let accountName = ''
let automationLoaded = false
let liveConnected = false
const names = { PAUSED: '已暂停', IDLE: '等待任务', ANALYZE: '分析中', REFLECT: '正在汇总', DISCOVER: '正在找视频', BLOCKED: '需要处理', STOPPED: '已停止', RUNNING: '监听中', queued: '排队中', analyzing: '分析中', reflecting: '正在汇总', done: '已完成', failed: '失败', draft: '草稿', sent: '已发送', uncertain: '待确认', sending: '发送中', cancelled: '已取消' }
const errors = { PANEL_AUTH_REQUIRED: '面板访问凭据失效，请重新运行打开面板脚本。', NETWORK_ACCESS_DENIED: '当前运行环境拒绝网络访问。', MODEL_NOT_CONFIGURED: '请先在 DSH 配置默认模型。', HOST_CALL_FAILED: '模型调用失败，请查看 DSH 模型连接状态。', INVALID_SUMMARY_EVIDENCE: '总结格式或证据校验失败，可以重试任务。', NO_SUBTITLE_OR_LOGIN_REQUIRED: '此视频没有可用字幕，或需要重新登录。', DM_SEND_DISABLED: '私信发送尚未开启，请在 DSH 插件设置中启用。', CONFIGURE_DM_SEND_SCOPE_FIRST: '请先启用私信发送并填写允许回复的账号 UID。', STOP_DM_BEFORE_CHANGING_MODE: '请先停止监听，再选择另一种回复模式。', VERIFY_ACCOUNT_FIRST: '请先点击“验证 B 站账号”。', DM_SEND_LIMIT: '发送间隔未到，请稍后再试；聊天不设每日上限。', DATA_DIR_IN_USE: '数据目录已被另一个 worker 占用。', LOGIN_REQUIRED: '登录已失效，请更新本地账号凭据。', PLATFORM_CHALLENGE: 'B 站要求账号验证，请先在 B 站处理。', PAUSE_BEFORE_CHANGING_TOPIC: '请先暂停分析，再切换主题。', VIDEO_NOT_FAILED: '该任务当前不是失败状态，请刷新查看。', MODEL_DAILY_LIMIT: '旧版本的本地次数限制，新版已取消，请重新加载插件。' }
function explain(code) { return errors[code] || code || '操作失败，请刷新后重试。' }
Object.assign(errors, { RECOMMENDATIONS_PAUSED: '主动推荐已暂停', RECOMMENDATION_DAILY_LIMIT: '已达到该好友今日主动推荐上限', RECOMMENDATION_QUIET_HOURS: '当前不在允许推送时段', INVALID_PUSH_TIME: '请输入有效的推送时间', INVALID_RECOMMENDATION_PREFERENCES: '请检查推荐偏好、数量和时长范围', RECOMMENDATION_NOT_DELIVERED: '未找到这条视频向该对象成功发送的记录', VIDEO_NOT_ANALYZED: '这条视频还没有完成分析', VIDEO_AUTHOR_UNKNOWN: '缺少已核实的UP主信息，可在档案中手动填写屏蔽UID', VIDEO_EXCLUDED_BY_PREFERENCES: '按接收者偏好跳过该视频' })
Object.assign(errors, { INVALID_AUTOMATION_CONFIG: '自动运行设置超出允许范围。', AUTOMATION_WINDOW_MUST_BE_SAME_DAY: '结束时间需晚于开始时间，目前支持同一天的时段。', INVALID_VIDEO_DECISION: '视频评价格式不合规，未执行动作。', DISLIKE_REQUIRES_APP_TOKEN: '缺少 App 凭据：仅在本地记为不喜欢。', FAVORITE_FOLDER_REQUIRED: '没有找到默认收藏夹，请在公开互动页选择收藏夹。', CARD_REASON_NOT_SENT: '请先发送该卡片对应的推荐理由。', RECOMMENDATION_REQUEST_LIMIT: '推荐检索请求已达到频率限制。' })
function validationDetail(detail) {
  if (!detail) return ''
  const reasons = { invalid_json: '不是有效的 JSON', object_required: '需要 JSON 对象', string_required: '需要文本', array_required: '需要列表', length: '字符数不符合限制', count: '条数不符合限制', integer_ids_required: '证据 ID 必须是整数', unknown_ids: '引用了本轮不存在的证据 ID', invalid_reflection: '视频感受的分类、长度、评分或证据不合规' }
  const parts = (detail.stage || '').split(':')
  const stage = parts[0] === 'segment' ? `第 ${parts[1]} 段` : parts[0] === 'rollup' ? `第 ${parts[1]} 层第 ${parts[2]} 组合并` : '最终汇总'
  return `${stage} · ${detail.field || ''}：${reasons[detail.reason] || detail.reason || ''}${Number.isInteger(detail.actual) ? `（实际 ${detail.actual}，允许 1–${detail.maximum}）` : ''} · 已尝试 ${detail.attempts || 1} 次`
}
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = `notice${error ? ' error' : ''}`; $('notice').hidden = false }
async function api(path, body) {
  const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) })
  const data = await response.json()
  if (!response.ok || data.error) throw new Error(explain(data.error))
  return data
}
const call = (method, params = {}) => api('/api/action', { method, params })
function badge(state) { const el = document.createElement('span'); el.className = `badge ${['failed','BLOCKED','uncertain'].includes(state) ? 'danger' : ['done','sent','RUNNING'].includes(state) ? '' : 'neutral'}`; el.textContent = names[state] || state; return el }
function text(tag, value, className = '') { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; return el }
function button(label, action) { const el = text('button', label, 'secondary'); el.type = 'button'; el.onclick = () => perform(el, action); return el }
async function perform(el, action) {
  const label = el.textContent
  el.disabled = true; el.textContent = '处理中…'
  try { await action(); await refresh() }
  catch (error) { notice(error.message, true) }
  finally { el.textContent = label; el.disabled = false; updateAutoButton() }
}
function updateAutoButton() {
  const dm = current?.messaging
  const active = dm?.state === 'RUNNING' && dm.auto_send
  $('listen-auto').disabled = !current?.ready || !dm?.send_enabled || !dm?.allowed_peers?.length || active
  $('listen-auto').textContent = active ? '自动回复已开启' : '开启自动回复'
  $('listen-stop').disabled = !current?.ready || dm?.state === 'STOPPED'
}

async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    renderStatus(await api('/api/status'))
  } catch (error) { disconnected(error) }
  finally { refreshing = false; updateAutoButton() }
}
function disconnected(error) {
  liveConnected = false; current = null
  $('connection').textContent = '连接中断 · 正在重连'; $('connection').className = 'badge danger'
  $('updated').textContent = '当前显示的是旧数据，连接恢复后自动更新'
  $('connection-alert').hidden = false
  $('connection-alert').title = error.message
  $('live-indicator').textContent = '正在重连'; $('live-indicator').classList.remove('connected')
  updateAutoButton()
}
function renderStatus(data) {
    current = data
    $('connection').textContent = current.ready ? '● 插件已连接' : '插件未就绪'
    $('connection').className = `badge${current.ready ? '' : ' danger'}`
    if (!current.ready) { $('updated').textContent = '后台未就绪，当前显示的是上次状态'; notice(explain(current.error), true); updateAutoButton(); return }
    $('connection-alert').hidden = true
    $('live-indicator').textContent = liveConnected ? '实时同步' : '正在连接'
    $('live-indicator').classList.toggle('connected', liveConnected)
    const { messaging: dm, agent, model_selection: model } = current
    $('account-state').textContent = accountName || (dm.account_uid ? '已绑定' : '未验证')
    $('account-detail').textContent = dm.account_uid ? `UID ${dm.account_uid}` : '点击下方按钮验证账号'
    $('model-state').textContent = modelVerified ? '连接正常' : model.configured ? '已配置' : '未配置'
    $('model-detail').textContent = model.configured ? `${model.provider} / ${model.model}` : '在 DSH 中选择默认模型'
    $('agent-state').textContent = names[agent.state] || agent.state
    $('agent-detail').textContent = `${agent.queue.length} 个近期任务 · ${agent.queue.filter(v => v.state === 'done').length} 个已完成`
    $('dm-state').textContent = names[dm.state] || dm.state
    $('dm-detail').textContent = dm.error ? explain(dm.error) : dm.state === 'RUNNING' ? (dm.auto_send ? '新消息自动回复' : '新消息只生成草稿') : '启动后约每10秒检查新消息'
    $('send-badge').textContent = dm.send_enabled ? '发送已启用' : '发送未启用'
    $('dm-scope').textContent = dm.allowed_peers.length ? `允许回复的 UID：${dm.allowed_peers.join('、')}` : '尚未配置允许回复的账号。请在 DSH 插件设置中填写 dmAllowedPeers，并按需开启 dmSendEnabled。'
    renderTasks(agent.queue); renderOutbox(dm.outbox, dm); renderFriends(current.social); renderAutomation(current.automation); renderDmProgress(dm)
    renderLive(current)
    $('updated').textContent = `更新于 ${new Date().toLocaleTimeString()} · ${liveConnected ? '实时连接 · 每秒同步' : '正在连接实时推送'}`
    $('version').textContent = `v${current.version}`
    updateAutoButton()
    if (!$('videos').hidden && !libraryLoading && Date.now() - libraryUpdated > 5000) void loadLibrary()
}
async function liveUpdates() {
  for (;;) {
    const controller = new AbortController()
    let timeout = setTimeout(() => controller.abort(), 15000)
    try {
      const response = await fetch('/api/events', { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      if (!response.ok) throw new Error(explain((await response.json()).error))
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) throw new Error('实时连接已断开，正在重连。')
        clearTimeout(timeout); timeout = setTimeout(() => controller.abort(), 15000)
        buffer += decoder.decode(value, { stream: true })
        let end
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2)
          const line = frame.split('\n').find(line => line.startsWith('data: '))
          if (line) { liveConnected = true; renderStatus(JSON.parse(line.slice(6))) }
        }
      }
    } catch (error) { disconnected(error) }
    finally { clearTimeout(timeout); controller.abort() }
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
}
const libraryPageSize = 15
let libraryOffset = 0, libraryQuery = '', libraryLoading = 0, libraryRequest = 0, libraryUpdated = 0
let libraryVersion = ''
let coverQueue = [], coverBusy = false
const coverAttempted = new Set()
async function fillCovers() {
  if (coverBusy || $('videos').hidden || !coverQueue.length) return
  coverBusy = true
  let failed = false
  try {
    while (coverQueue.length && !$('videos').hidden) {
      const id = coverQueue.shift()
      if (coverAttempted.has(id)) continue
      coverAttempted.add(id)
      $('cover-status').textContent = '正在补全 B 站原始封面…'
      try {
        const result = await call('library.refresh', { video_id: id })
        if (result.has_cover) await loadLibrary()
        else failed = true
      } catch { failed = true }
    }
  } finally {
    coverBusy = false
    $('cover-status').textContent = failed ? '部分封面暂时无法获取，可在视频的“更多”中重试。' : ''
  }
}
function videoLink(bvid, page = 1, label = '在 B 站观看') {
  const link = text('a', label)
  if (/^BV[0-9A-Za-z]{10}$/.test(bvid)) link.href = `https://www.bilibili.com/video/${bvid}/?p=${Number(page) || 1}`
  link.target = '_blank'; link.rel = 'noopener noreferrer'; return link
}
async function loadLibrary() {
  const request = ++libraryRequest
  libraryLoading++; libraryUpdated = Date.now()
  try {
    const data = await call('library.list', { query: libraryQuery, rating: $('rating-filter').value, offset: libraryOffset, limit: libraryPageSize })
    if (request !== libraryRequest) return
    if (libraryOffset && libraryOffset >= data.total) { libraryOffset = Math.max(0, Math.floor((data.total - 1) / libraryPageSize) * libraryPageSize); void loadLibrary(); return }
    $('library-total').textContent = `已保存 ${data.counts.all} 个视频 · 点击封面或标题查看详情`
    for (const node of document.querySelectorAll('[data-count]')) node.textContent = data.counts[node.dataset.count] ?? 0
    for (const node of document.querySelectorAll('[data-rating]')) node.setAttribute('aria-pressed', String(node.dataset.rating === $('rating-filter').value))
    coverQueue = data.items.filter(item => !item.cover && !coverAttempted.has(item.id)).map(item => item.id)
    void fillCovers()
    $('library-page').textContent = data.total ? `第 ${libraryOffset / libraryPageSize + 1} / ${Math.ceil(data.total / libraryPageSize)} 页 · 当前分类 ${data.total} 个` : '暂无匹配视频'
    $('library-prev').disabled = libraryOffset === 0; $('library-next').disabled = libraryOffset + libraryPageSize >= data.total
    const version = JSON.stringify(data)
    if (version === libraryVersion) return
    libraryVersion = version
    $('video-assessments').replaceChildren(...data.items.map(item => {
      const card = document.createElement('article'); card.className = 'video-card'
      const cover = document.createElement('button'); cover.type = 'button'; cover.className = 'video-cover'; cover.setAttribute('aria-label', `查看视频详情：${item.title || item.bvid}`)
      cover.onclick = () => void openVideo(item)
      cover.append(text('span', '▷', 'cover-play'), text('small', item.cover ? '查看详情' : '封面暂未获取', 'cover-fallback'))
      if (item.cover) {
        const url = new URL(item.cover)
        if (url.protocol === 'https:' && url.hostname.endsWith('.hdslb.com') && !url.username && !url.password && (!url.port || url.port === '443')) {
          const img = document.createElement('img'); img.src = url.href; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.onerror = () => { img.remove(); cover.querySelector('.cover-fallback').textContent = '封面暂时不可用' }; cover.append(img)
        }
      }
      const seconds = Math.max(0, Math.floor(item.duration || 0))
      cover.append(text('span', `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`, 'video-duration'))
      const content = document.createElement('div'); content.className = 'video-card-content'
      content.append(item.assessment?.rating ? ratingBadge(item.assessment.rating)
        : badge(item.assessment?.retry_requested ? '等待重新评价' : item.assessment?.error ? '评价失败' : item.state === 'done' ? '总结完成 · 待评价' : item.state))
      const heading = document.createElement('h3'), title = text('button', item.title || item.bvid, 'video-title')
      title.type = 'button'; title.onclick = () => void openVideo(item); heading.append(title); content.append(heading)
      content.append(text('p', `${item.author || 'UP 主未记录'} · P${item.page || 1}`, 'subtle'))
      const actions = document.createElement('div'); actions.className = 'video-card-actions'
      actions.append(button(item.summary ? '查看总结' : '查看进度', () => openVideo(item)), videoLink(item.bvid, item.page, '去 B 站 ↗'))
      const more = document.createElement('details'); more.className = 'video-more'; more.append(text('summary', '更多'))
      if (item.assessment?.error === 'INVALID_VIDEO_DECISION') {
        const retry = button('重试评价', async () => {
          await call('automation.retry_review', { video_id: item.id }); await loadLibrary()
          notice('已排队重新评价，将复用现有总结；通过后按当前设置处理推荐和互动。')
        })
        retry.disabled = !!item.assessment.retry_requested; more.append(retry)
      }
      if (!item.cover) more.append(button('重试封面', async () => {
        const result = await call('library.refresh', { video_id: item.id }); await loadLibrary()
        notice(result.has_cover ? '视频封面与信息已更新。' : '信息已更新，平台未提供可用封面。')
      }))
      if (['done', 'failed'].includes(item.state)) more.append(button('重新总结', async () => {
        await call('agent.reanalyze', { video_id: item.id }); await call('agent.start')
        notice('已用保存的字幕重新分析，将调用模型；完成前保留上次总结。'); await loadLibrary()
      }))
      if (more.children.length > 1) actions.append(more)
      content.append(actions); card.append(cover, content); return card
    }))
    if (!data.items.length) $('video-assessments').append(text('p', '还没有匹配的视频。试试其他关键词，或前往「添加与队列」加入视频。', 'empty'))
  } catch (error) { if (request === libraryRequest) notice(error.message, true) }
  finally { libraryLoading-- }
}
function renderTasks(queue) {
  const rows = queue.map(item => {
    const row = document.createElement('tr')
    row.append(text('td', `#${item.id} · ${item.bvid}`))
    const state = document.createElement('td'); state.append(badge(item.state)); if (item.error) state.append(text('small', explain(item.error))); if (item.error_detail) state.append(text('small', validationDetail(item.error_detail))); row.append(state)
    if (item.assessment?.rating) state.append(ratingBadge(item.assessment.rating))
    if (item.retry_at) state.append(text('small', `临时故障，最早 ${new Date(item.retry_at * 1000).toLocaleTimeString()} 自动重试已接单请求`))
    row.append(text('td', `${item.checkpoint} / ${item.total_segments ?? '?'} 段`))
    const actions = document.createElement('td')
    actions.append(button('查看总结', () => showResult(item.id)))
    if (item.state === 'failed') actions.append(button('重试', async () => { await call('agent.retry', { video_id: item.id }); await call('agent.start'); notice(`任务 ${item.id} 已重新排队并继续分析。`) }))
    row.append(actions); return row
  })
  $('tasks').replaceChildren(...rows)
  if (!rows.length) { const row = document.createElement('tr'); const cell = text('td', '暂无视频。添加链接后开始分析。', 'empty'); cell.colSpan = 4; row.append(cell); $('tasks').append(row) }
}
function renderOutbox(outbox, dm) {
  const version = JSON.stringify([outbox, dm.send_enabled, dm.allowed_peers])
  if ($('outbox').dataset.version === version) return
  $('outbox').dataset.version = version
  $('outbox').replaceChildren(...outbox.map(item => {
    const card = document.createElement('article'); card.className = 'draft'
    const top = document.createElement('div'); top.className = 'draft-top'; top.append(text('span', `发送给 UID ${item.peer}`), badge(item.state)); card.append(top, text('p', item.text || '正文已清除'))
    if (item.error) card.append(text('p', explain(item.error), 'subtle'))
    if (item.state === 'uncertain') card.append(text('p', '请到 B 站确认是否已收到；此消息不会自动重发。', 'subtle'))
    const bvid = item.text?.match(/BV[0-9A-Za-z]{10}/)?.[0]
    if (item.state === 'sent' && bvid) card.append(button('反馈这条推荐', async () => {
      $('feedback-peer').value = item.peer; $('feedback-bvid').value = bvid
      navigate('friends-section'); $('feedback-editor').open = true; $('feedback-form').scrollIntoView({ block: 'center' })
    }))
    if (item.state === 'draft') {
      const send = button('发送这条草稿', async () => { const result = await call('dm.send', { draft_id: item.id }); notice(result.state === 'sent' ? '私信已发送。' : `发送状态：${names[result.state] || result.state}`, result.state !== 'sent') })
      send.disabled = !dm.send_enabled || !dm.allowed_peers.includes(item.peer); card.append(send)
    }
    return card
  }))
  if (!outbox.length) $('outbox').append(text('p', '还没有回复记录。启动监听后，从接收账号发一条新私信试试。', 'empty'))
}
function renderDmProgress(dm) {
  const now = Date.now() / 1000
  const stamp = value => value ? new Date(value * 1000).toLocaleString() : '尚无记录'
  const remaining = value => `${Math.max(0, Math.ceil(value - now))} 秒`
  const active = dm.activities || []
  const window = dm.delivery_window
  $('dm-last-check').textContent = dm.last_sync ? new Date(dm.last_sync * 1000).toLocaleTimeString() : '尚未检查'
  $('dm-check-result').textContent = dm.last_check ? `${dm.last_check.peers} 个会话，新增 ${dm.last_check.new_messages} 条收发记录` : '等待第一次检查完成'
  $('dm-next-check').textContent = dm.state === 'BLOCKED' ? '已暂停' : dm.state !== 'RUNNING' ? '监听未启动'
    : dm.next_poll_at ? `${remaining(dm.next_poll_at)}后` : '正在处理本轮'
  const priority = { send: 0, reply: 1, sync: 2, memory: 3 }
  const currentStep = [...active].sort((a, b) => (priority[a.stage] ?? 4) - (priority[b.stage] ?? 4))[0]
  $('dm-current-step').textContent = currentStep?.message || (dm.state === 'BLOCKED' ? '等待处理错误' : dm.state === 'RUNNING' ? '等待下一轮' : '已停止')
  $('dm-step-detail').textContent = currentStep ? `已等待 ${Math.max(0, Math.floor(now - currentStep.started))} 秒${active.length > 1 ? ` · 共 ${active.length} 项处理中` : ''}` : (dm.error ? explain(dm.error) : '')
  $('dm-send-window').textContent = !dm.send_enabled ? '发送开关未开启' : window?.reason ? `${remaining(window.next_at)}后可尝试` : '当前可尝试发送'
  $('dm-send-usage').textContent = window ? `今日发送 ${window.used} 次 · 聊天不设每日上限${window.reason ? ` · 最早 ${stamp(window.next_at)}` : ''}` : ''
  $('dm-reply-expectation').textContent = dm.state === 'BLOCKED'
    ? `当前不会自动回复：${explain(dm.error)}。处理原因后点击“开启自动回复”。`
    : dm.state !== 'RUNNING' ? '监听未启动。启动后才会处理新收到的消息。'
    : !dm.auto_send ? '当前只生成草稿。点击“开启自动回复”可直接切换，已接收的回复草稿会按发送范围继续处理。'
    : dm.reply_dispatch === 'message_event'
      ? '收到私信 → 消息事件触发回复 → 草稿事件触发发送 → B 站返回回执。接收、生成和发送独立运行；底层仍定时检查 B 站，模型和发送冷却可能需要等待。'
      : '当前仍运行旧版私信流程，重载插件后启用消息事件处理。'
  const incoming = dm.incoming || []
  const incomingVersion = JSON.stringify(incoming)
  if ($('dm-incoming').dataset.version !== incomingVersion) {
    $('dm-incoming').dataset.version = incomingVersion
    $('dm-incoming').replaceChildren(...incoming.map(item => {
      const row = document.createElement('article'); row.className = 'dm-received-item'
      let state = item.reply_state ? (names[item.reply_state] || item.reply_state)
        : item.state === 'silent' ? '模型选择不回复'
        : item.state === 'coalesced' ? '已合并到后续消息回复'
        : item.before_listener ? '启动前的消息，不自动补回'
        : dm.auto_send && !item.peer_allowed ? '未在允许自动回复的范围内' : '等待处理'
      if (item.reply_state === 'draft') state = dm.auto_send ? '草稿已生成，等待发送' : '草稿已生成，需手动发送'
      row.append(text('span', `UID ${item.peer} · 消息 ${item.msg_key}`, 'subtle'), text('strong', state), text('small', stamp(item.timestamp), 'subtle'))
      if (item.reply_error) row.append(text('small', explain(item.reply_error), 'error'))
      return row
    }))
    if (!incoming.length) $('dm-incoming').append(text('p', '尚未记录收到的消息。启动监听后，从允许的账号发一条新私信测试。', 'empty'))
  }
  const logs = dm.logs || []
  const logVersion = String(logs[0]?.id || 0)
  if ($('dm-log').dataset.version !== logVersion) {
    $('dm-log').dataset.version = logVersion
    $('dm-log').replaceChildren(...logs.map(event => {
      const row = document.createElement('article'); row.className = `dm-log-entry${event.error ? ' has-error' : ''}`
      row.append(text('time', stamp(event.created)), text('span', `${event.message}${event.error ? `：${explain(event.error)}` : ''}`))
      if (event.reference) row.append(text('small', `编号 ${event.reference}`, 'subtle'))
      return row
    }))
    if (!logs.length) $('dm-log').append(text('p', '暂无私信日志。新版开始检查消息后会记录过程。', 'empty'))
  }
}
function renderFriends(social) {
  const version = JSON.stringify([social?.friends?.map(f => ({ ...f, delivery: { ...f.delivery, next_at: 0 } })), social?.recommendations, social?.feedback, social?.error])
  if ($('friends').dataset.version === version) return
  $('friends').dataset.version = version
  $('friends').replaceChildren(...(social?.friends || []).map(friend => {
    const card = document.createElement('article'); card.className = 'draft'
    const top = document.createElement('div'); top.className = 'draft-top'
    top.append(text('strong', `${friend.name} · ${friend.category}`), text('span', `UID ${friend.peer}`)); card.append(top)
    const tags = document.createElement('div'); tags.className = 'friend-interests'
    for (const interest of friend.interests) tags.append(badge(interest)); card.append(tags)
    card.append(text('p', friend.style || '尚未填写表达偏好', 'subtle'))
    card.append(text('p', friend.recommend ? (friend.auto_send ? '匹配后自动发送推荐' : '匹配后生成推荐草稿') : '已关闭视频推荐', 'subtle'))
    const prefs = friend.preferences || {}
    card.append(text('p', `${prefs.mode === 'leisure' ? '休闲' : '学习'}模式 · ${prefs.level || '水平未设置'} · ${prefs.goal || '目标未设置'}`, 'subtle'))
    card.append(text('p', `今日主动推荐 ${friend.delivery?.used || 0} / ${prefs.daily_limit ?? 5} 个视频 · 推送时段 ${prefs.push_start || '09:00'}—${prefs.push_end || '22:00'}${friend.delivery?.reason ? ` · ${explain(friend.delivery.reason)}` : ''}`, 'subtle'))
    card.append(button('今天先别发', async () => {
      await call('friends.preferences', { peer_uid: friend.peer, pause_today: true })
      notice('已暂停今天的主动推荐，点播请求仍会处理。')
    }), button('恢复主动推荐', async () => {
      await call('friends.preferences', { peer_uid: friend.peer, preferences: { paused_until: 0 } }); notice('已取消暂停，仍按该好友的推送时间和权限发送。')
    }))
    card.append(button('编辑档案', async () => {
      $('friend-editor').open = true
      $('friend-uid').value = friend.peer; $('friend-name').value = friend.name; $('friend-category').value = friend.category
      $('friend-interests').value = friend.interests.join('，'); $('friend-style').value = friend.style
      $('friend-recommend').checked = friend.recommend; $('friend-auto').checked = friend.auto_send; $('friend-uid').focus()
      fillPreferences(prefs)
    })); return card
  }))
  if (!social?.friends?.length) $('friends').append(text('p', '先验证机器人账号，再添加好友的数字 UID 和兴趣。', 'empty'))
  $('recommendation-status').textContent = social?.error ? `推荐处理：${explain(social.error)}` : `最近 ${social?.recommendations?.length || 0} 条推荐记录，可在“最近回复”查看草稿和发送状态。`
  const feedbackNames = { like: '喜欢', too_basic: '太基础', too_hard: '太难', too_long: '太长', not_interested: '不感兴趣', seen: '看过了', block_author: '不再推荐这个UP' }
  $('feedback-history').replaceChildren(...(social?.feedback || []).map(item => text('p', `UID ${item.peer} · ${item.bvid} · ${feedbackNames[item.kind] || item.kind} · ${new Date(item.created * 1000).toLocaleString()}`, 'subtle')))
}

const preferenceFields = { mode: 'friend-mode', level: 'friend-level', goal: 'friend-goal', max_seconds: 'friend-max-seconds', daily_limit: 'friend-daily-limit', push_start: 'friend-push-start', push_end: 'friend-push-end', blocked_keywords: 'friend-blocked-keywords', blocked_authors: 'friend-blocked-authors' }
function fillPreferences(prefs = {}) {
  const defaults = { mode: 'learning', level: '', goal: '', max_seconds: 0, daily_limit: 5, push_start: '09:00', push_end: '22:00', blocked_keywords: [], blocked_authors: [] }
  for (const [key, id] of Object.entries(preferenceFields)) { const value = prefs[key] ?? defaults[key]; $(id).value = Array.isArray(value) ? value.join('，') : value }
}
function readPreferences() {
  return Object.fromEntries(Object.entries(preferenceFields).map(([key, id]) => [key, key.startsWith('blocked_') ? keywords(id) : $(id).type === 'number' ? Number($(id).value) : $(id).value.trim()]))
}
async function showResult(id) {
  const data = await call('agent.result', { video_id: id })
  if (!data.result) {
    $('result-title').textContent = `任务 #${id} · ${names[data.state] || data.state}`
    $('result-body').replaceChildren(text('p', data.error ? explain(data.error) : '总结尚未生成。可在「添加与队列」查看处理进度。', 'hint'))
    if (!$('result-dialog').open) $('result-dialog').showModal()
    return
  }
  const result = data.result
  $('result-title').textContent = result.title || '视频总结'
  const body = $('result-body'); body.replaceChildren(text('p', result.summary))
  body.prepend(videoLink(result.bvid, result.page, '▶ 在 B 站观看完整视频'))
  if (data.state !== 'done') body.prepend(text('p', '新版分析尚未完成，以下为上次保存的总结。', 'hint'))
  if (data.assessment) body.prepend(ratingBadge(data.assessment.rating), text('p', data.assessment.reason))
  body.append(text('h3', '内容要点')); const list = document.createElement('ul')
  for (const fact of result.facts || []) {
    const entry = text('li', fact.text)
    for (const id of (fact.evidence || []).slice(0, 3)) {
      const evidence = (result.evidence || []).find(point => point.id === id)
      if (evidence) {
        const seconds = Math.max(0, Math.floor(evidence.from))
        const link = videoLink(result.bvid, result.page, ` ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`)
        link.href += `&t=${seconds}`; entry.append(link)
      }
    }
    list.append(entry)
  }
  body.append(list)
  if (result.analysis) {
    for (const [key, heading] of [['question', '核心问题'], ['conclusion', '核心结论'], ['reasoning', '论证过程'], ['caveats', '局限与未证实之处'], ['takeaways', '适合谁与实际收获']]) {
      body.append(text('h3', heading), text('p', result.analysis[key]))
    }
  } else body.append(text('p', '这是旧版摘要，可在视频卡片点击“重新深度总结”补充分析。', 'hint'))
  if (result.reflection) body.append(text('h3', '我的感受'), text('p', result.reflection.impression))
  body.append(text('h3', '回到原视频'))
  const bvid = result.bvid
  if (/^BV[0-9A-Za-z]{10}$/.test(bvid)) for (const evidence of (result.evidence || []).slice(0, 10)) {
    const seconds = Math.max(0, Math.floor(Number(evidence.from) || 0)); const link = text('a', `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}  查看原文`)
    link.href = `https://www.bilibili.com/video/${bvid}/?p=${Number(result.page) || 1}&t=${seconds}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; body.append(link)
  }
  body.append(text('p', (result.limitations || []).join('；'), 'result-limit'))
  if (!$('result-dialog').open) $('result-dialog').showModal()
}
async function openVideo(item) {
  try {
    await showResult(item.id)
    if (item.assessment?.error) $('result-body').prepend(text('p', `评价未完成：${explain(item.assessment.error)}`, 'hint'))
    if (item.assessment?.error_detail) $('result-body').append(text('p', `${item.assessment.error_detail.field}：${item.assessment.error_detail.reason}`, 'hint'))
  } catch (error) { notice(error.message, true) }
}
function bind(id, action) { $(id).onclick = () => perform($(id), action) }
function form(id, action) { $(id).onsubmit = event => { event.preventDefault(); void perform($(id).querySelector('button[type="submit"]'), action) } }
bind('refresh', refresh)
bind('retry-connection', refresh)
bind('verify-account', async () => { const result = await call('dm.account'); accountName = result.name; notice(`账号验证成功：${result.name}（UID ${result.uid}）。`) })
bind('verify-model', async () => { await call('model.check'); modelVerified = true; notice('模型连接正常，已完成一次真实模型调用。') })
bind('resume', async () => { await call('agent.start'); notice('分析已继续，等待队列任务。') })
bind('pause', async () => { await call('agent.pause'); notice('分析已暂停，已有进度保留。') })
bind('listen-preview', async () => { await call('dm.start', { auto_send: false }); notice('开始监听新私信，只生成草稿。') })
bind('listen-auto', async () => { await call('dm.start', { auto_send: true }); notice('自动回复已开启。现在可从允许的账号发送一条新私信。') })
bind('listen-stop', async () => { await call('dm.stop'); notice('私信监听已停止。') })
$('close-result').onclick = () => $('result-dialog').close()
form('video-form', async () => { const bvid = $('video-input').value.match(/BV[0-9A-Za-z]{10}/)?.[0]; if (!bvid) throw new Error('请输入有效的 BV 号或 B 站视频链接。'); const task = await call('platform.enqueue', { bvid, page: Number($('video-page').value) }); notice(`视频已入队，任务 ${task.id}。点击“开始 / 继续分析”生成总结。`) })
form('discover-form', async () => { await call('agent.start', { keyword: $('keyword').value.trim() }); notice('主题发现已开始，最多进行 3 轮搜索。') })
const keywords = id => $(id).value.split(/[,，\n]/).map(word => word.trim()).filter(Boolean)
form('profile-form', async () => {
  await call('memory.configure', { profile: { name: $('profile-name').value.trim(), style: $('profile-style').value.trim(),
    persona: $('profile-persona').value.trim(), preferred_keywords: keywords('profile-preferred'), avoided_keywords: keywords('profile-avoided') } })
  notice('人设和选片偏好已保存，下一次生成时生效。已有草稿和发送权限不变。')
})
form('friend-form', async () => {
  await call('friends.save', { profile: { peer_uid: $('friend-uid').value.trim(), name: $('friend-name').value.trim(),
    category: $('friend-category').value.trim(), interests: $('friend-interests').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    style: $('friend-style').value.trim(), recommend: $('friend-recommend').checked, auto_send: $('friend-auto').checked,
    preferences: readPreferences() } })
  notice('好友档案已保存，将用于之后的私信回复和新视频推荐。')
})
form('feedback-form', async () => {
  await call('friends.feedback', { peer_uid: $('feedback-peer').value.trim(), bvid: $('feedback-bvid').value.trim(), kind: $('feedback-kind').value })
  notice('反馈已保存，后续匹配会参考这条记录。')
})
form('memory-form', async () => { const items = await call('memory.recall', { query: $('memory-query').value.trim() }); $('memories').replaceChildren(...items.map(item => button(item.result.title || item.result.summary, () => showResult(item.video_id)))); if (!items.length) $('memories').append(text('p', '没有找到相关记忆。', 'subtle')) })
async function init() {
  await refresh()
  if (current?.ready) try {
    const profile = await call('memory.profile')
    $('profile-name').value = profile.name; $('profile-style').value = profile.style
    $('profile-persona').value = profile.persona || ''
    $('profile-preferred').value = (profile.preferred_keywords || []).join('，')
    $('profile-avoided').value = (profile.avoided_keywords || []).join('，')
  } catch (error) { notice(error.message, true) }
  void liveUpdates()
}
void init()

function ratingBadge(rating) {
  const element = badge(rating || '评价待确认')
  element.className = `badge rating ${rating === '值得看' ? 'worth' : rating === '可以看' ? 'okay' : rating === '没必要看' ? 'skip' : 'neutral'}`
  return element
}
const autoFields = { discovery_source: 'auto-source', scheduled: 'auto-scheduled', dm_auto_start: 'auto-dm', request_enabled: 'auto-request',
  proactive_enabled: 'auto-proactive', share_cards: 'auto-share', actions_enabled: 'auto-actions',
  start_time: 'auto-start', end_time: 'auto-end', rounds: 'auto-rounds', per_round: 'auto-per-round',
  favorite_folder: 'auto-folder', max_video_seconds: 'auto-duration', comments_enabled: 'auto-comments', comment_daily_limit: 'auto-comment-limit' }
// Preview only: the worker still validates and schedules the saved plan.
function previewSchedule() {
  const start = $('auto-start'), end = $('auto-end'), rounds = $('auto-rounds'), count = $('auto-per-round')
  const minutes = input => input.value ? Number(input.value.slice(0, 2)) * 60 + Number(input.value.slice(3)) : NaN
  const from = minutes(start), to = minutes(end)
  end.setCustomValidity(Number.isFinite(from) && Number.isFinite(to) && to <= from ? '结束时间需晚于开始时间，请选择同一天的时段。' : '')
  const valid = [start, end, rounds, count].every(input => input.validity.valid)
  $('schedule-slots').replaceChildren()
  for (const preset of document.querySelectorAll('[data-start]')) preset.setAttribute('aria-pressed', String(preset.dataset.start === start.value && preset.dataset.end === end.value))
  if (!valid) { $('schedule-description').textContent = '请填写有效时段、1–6 轮和每轮 1–5 个视频。'; return }
  if (!$('auto-scheduled').checked) { $('schedule-description').textContent = '定时浏览已关闭；保存后仍可手动添加视频。'; return }
  $('schedule-description').textContent = `共 ${rounds.value} 轮，目标最多 ${Number(rounds.value) * Number(count.value)} 个视频；实际数量取决于可用内容。`
  for (let i = 0; i < Number(rounds.value); i++) {
    const at = from + Math.floor((to - from) * i / Number(rounds.value))
    $('schedule-slots').append(text('span', `${String(Math.floor(at / 60)).padStart(2, '0')}:${String(at % 60).padStart(2, '0')}`, 'slot'))
  }
}
for (const id of ['auto-start', 'auto-end', 'auto-rounds', 'auto-per-round', 'auto-scheduled']) $(id).addEventListener('input', previewSchedule)
for (const preset of document.querySelectorAll('[data-start]')) preset.onclick = () => {
  $('auto-start').value = preset.dataset.start; $('auto-end').value = preset.dataset.end; previewSchedule()
}
function renderAutomation(auto) {
  if (!auto) return
  if (!automationLoaded) {
    for (const [key, id] of Object.entries(autoFields)) {
      if ($(id).type === 'checkbox') $(id).checked = auto.config[key]; else $(id).value = auto.config[key]
    }
    automationLoaded = true
    previewSchedule()
  }
  $('daily-window').textContent = `${auto.config.start_time} — ${auto.config.end_time}`
  $('daily-detail').textContent = auto.config.scheduled ? `${auto.config.discovery_source === 'popular_first' ? '热门优先' : '兴趣搜索'} · ${auto.config.rounds} 轮 · 每轮最多 ${auto.config.per_round} 个视频` : '定时浏览已关闭'
  const slotsVersion = JSON.stringify(auto.slots)
  if ($('daily-slots').dataset.version !== slotsVersion) {
    $('daily-slots').dataset.version = slotsVersion
    $('daily-slots').replaceChildren(...auto.slots.map((slot, index) => text('span', `${slot} · 第 ${index + 1} 轮`, 'slot')))
  }
  $('daily-slots').hidden = !auto.config.scheduled
  $('automation-health').textContent = auto.error ? `自动任务：${explain(auto.error)}` : '运行设置已同步。DSH 完全退出时，自动任务也会停止。'
  const reviews = auto.assessments || []
  for (const [id, rating] of [['count-worth', '值得看'], ['count-okay', '可以看'], ['count-skip', '没必要看']]) $(id).textContent = reviews.filter(item => item.assessment.rating === rating).length
  const states = { queued: '等待探索', browsing: '正在选片与分析', waiting: '等待分析与评价', result_ready: '视频 Agent 已交接结果', delivering: '互动 Agent 正在投递', complete: '处理完成', expired: '时段已过', failed: '需要处理' }
  const jobsVersion = JSON.stringify(auto.jobs)
  if ($('browse-jobs').dataset.version !== jobsVersion) {
    $('browse-jobs').dataset.version = jobsVersion
    $('browse-jobs').replaceChildren(...auto.jobs.map(job => {
    const card = document.createElement('article'); card.className = 'job-card'
    const top = document.createElement('div'); top.className = 'draft-top'; top.append(text('strong', job.kind === 'request' ? `好友请求 · UID ${job.peer}` : '定时浏览'), badge(states[job.state] || job.state))
    const analyzed = job.videos.filter(video => video.state === 'done').length
    card.append(top, text('h3', job.kind === 'scheduled' && job.requirements?.discovery_source === 'popular_first' ? `热门优先 · 补充主题：${job.keyword}` : job.keyword), text('p', `${job.round_index} / ${job.rounds} 轮 · 已分析 ${analyzed} 个 · 跳过 ${job.videos.filter(video => video.error).length} 个`, 'subtle'))
    if (job.blocked_reason || job.error) card.append(text('p', explain(job.blocked_reason || job.error), 'error'))
    for (const video of job.videos) {
      card.append(text('p', `${video.bvid} · ${names[video.state] || (video.error ? '已跳过' : '待获取')} · 已总结 ${video.checkpoint || 0} 段${video.error ? ` · ${explain(video.error)}` : ''}`, 'subtle'))
      if (video.retry_at) card.append(text('p', `保留进度，最早 ${new Date(video.retry_at * 1000).toLocaleTimeString()} 重试`, 'subtle'))
    }
    if (job.kind === 'request') {
      const prefs = job.requirements || {}
      card.append(text('p', `本次需求：${prefs.mode === 'leisure' ? '休闲' : '学习'} · ${prefs.level || '沿用已有水平'} · ${prefs.goal || '按本次主题推荐'}`, 'subtle'))
      const results = (job.deliveries || []).filter(item => item.role === 'result')
      card.append(text('p', results.length ? `推荐回复：${results.filter(item => item.state === 'sent').length} / ${results.length} 条已确认发送` : '推荐回复尚未生成；分析耗时取决于字幕长度与模型速度。', 'subtle'))
      if (!(current?.social?.friends || []).some(friend => friend.peer === job.peer)) card.append(button('设为主动推荐好友', async () => {
        $('friend-uid').value = job.peer; $('friend-name').value = `好友 ${job.peer}`
        $('friend-interests').value = job.keyword; $('friend-recommend').checked = true
        fillPreferences(job.requirements || {})
        $('friend-auto').checked = !!current?.messaging?.send_enabled && current.messaging.allowed_peers.includes(job.peer)
        navigate('friends-section'); $('friend-editor').open = true; $('friend-uid').focus(); notice('已填入请求对象和兴趣，确认后点击“保存好友”启用后续主动推荐。')
      }))
    }
      return card
    }))
    if (!auto.jobs.length) $('browse-jobs').append(text('p', '等待每日探索时段，或等待好友发来推荐请求。', 'empty'))
  }
  const actionNames = { like: '点赞', favorite: '收藏', dislike: '不喜欢' }
  const actionStates = { planned: '等待执行', sending: '执行中', sent: '平台已接受', local_only: '仅本地记录', uncertain: '结果待确认', blocked: '需要配置' }
  const actionsVersion = JSON.stringify(auto.actions)
  if ($('action-list').dataset.version !== actionsVersion) {
    $('action-list').dataset.version = actionsVersion
    $('action-list').replaceChildren(...auto.actions.map(item => {
      const card = document.createElement('article'); card.className = 'activity-item'
      const top = document.createElement('div'); top.className = 'draft-top'
      top.append(text('strong', `${actionNames[item.action] || item.action} · ${item.bvid}`), badge(actionStates[item.state] || item.state))
      card.append(top, text('p', item.reason)); if (item.error) card.append(text('small', explain(item.error), 'subtle'))
      return card
    }))
    if (!auto.actions.length) $('action-list').append(text('p', '暂无互动记录。', 'empty'))
  }
}
$('rating-filter').onchange = () => { libraryOffset = 0; void loadLibrary() }
form('library-search', async () => { libraryQuery = $('library-query').value.trim(); libraryOffset = 0; await loadLibrary() })
$('library-prev').onclick = () => { libraryOffset = Math.max(0, libraryOffset - libraryPageSize); void loadLibrary() }
$('library-next').onclick = () => { libraryOffset += libraryPageSize; void loadLibrary() }
for (const [formId, keys] of Object.entries({
  'automation-form': ['discovery_source', 'scheduled', 'start_time', 'end_time', 'rounds', 'per_round', 'max_video_seconds', 'proactive_enabled', 'share_cards'],
  'chat-settings-form': ['dm_auto_start', 'request_enabled'],
  'interaction-settings-form': ['actions_enabled', 'favorite_folder', 'comments_enabled', 'comment_daily_limit'],
})) form(formId, async () => {
  const config = Object.fromEntries(keys.map(key => {
    const input = $(autoFields[key])
    return [key, input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value.trim()]
  }))
  await call('automation.configure', { config }); notice('设置已保存。')
})
for (const node of document.querySelectorAll('[data-rating]')) node.onclick = () => {
  $('rating-filter').value = node.dataset.rating; $('rating-filter').onchange()
}
$('add-video-shortcut').onclick = event => {
  event.preventDefault(); selectPanel('video-tools'); $('video-input').focus()
}
bind('load-folders', async () => {
  const result = await call('automation.folders')
  $('folder-options').replaceChildren(...result.folders.map(folder => button(`${folder.title} · ${folder.id}`, async () => { $('auto-folder').value = folder.id; notice('已选择收藏夹，保存设置后生效。') })))
  if (!result.folders.length) $('folder-options').textContent = '没有找到收藏夹，请先在 B 站创建。'
})
navigate(pages[fragment] ? fragment : 'overview')

Object.assign(errors, { VIDEO_NOT_READY_FOR_REANALYSIS: '视频正在处理中，请完成或暂停后再查看状态', VIDEO_PART_CHANGED: '分P信息已变化，请重新添加正确的视频链接', RECOMMENDATION_CARD_EXPIRED: '附加卡片已跨天，未继续发送', DM_SEND_INTERVAL: '等待私信发送冷却', DM_DAILY_LIMIT: '旧版本的聊天日上限已取消，请重载插件', VIDEO_PAUSED: '视频处理已暂停，点击开始 / 继续分析恢复', RECIPIENT_NOT_ENABLED: '接收者不在允许发送范围', DM_SEND_UNCERTAIN: '发送结果待确认，请到 B 站核对；不会自动重复发送', VIDEO_EXCEEDS_AUTOMATION_DURATION: '超过自动选片时长上限', NO_SUBTITLE_URL: '没有可用字幕地址', INVALID_RECOMMENDATION_SELECTION: '推荐选择输出不合规，任务保留等待处理' })
function renderLive(data) {
  const agents = data.agents || {}
  const config = data.automation?.config || {}, messaging = data.messaging
  const videoState = agents.video?.state === 'RUNNING' ? '运行中' : (names[agents.video?.state] || '等待读取')
  const chatState = messaging.state === 'RUNNING' ? (messaging.auto_send ? '自动回复中' : '草稿模式') : (names[messaging.state] || '等待读取')
  $('video-agent-badge').textContent = videoState
  $('chat-agent-badge').textContent = chatState
  $('video-agent-badge').className = `badge${agents.video?.state === 'RUNNING' ? '' : ' neutral'}`
  $('chat-agent-badge').className = `badge${messaging.state === 'RUNNING' ? '' : messaging.state === 'BLOCKED' ? ' danger' : ' neutral'}`
  $('video-agent-summary').textContent = config.scheduled ? `${config.start_time}—${config.end_time} · ${config.rounds} 轮 × ${config.per_round} 个视频${config.proactive_enabled ? ' · 主动推荐已开' : ' · 主动推荐已关'}` : '定时浏览已关闭，可前往浏览计划调整。'
  $('chat-agent-summary').textContent = messaging.error ? explain(messaging.error) : messaging.state === 'RUNNING' ? '持续接收私信，好友点播时协作查找视频。' : '监听尚未运行，可前往私信会话开启。'
  $('video-agent-task').textContent = data.agent?.activity?.message || data.automation?.activity?.message || (data.agent?.error ? explain(data.agent.error) : '暂无进行中的视频任务')
  $('chat-agent-task').textContent = agents.interaction?.activity?.message || messaging.activities?.find(item => item.stage !== 'sync')?.message || (messaging.state === 'RUNNING' ? '等待新消息' : '私信监听未启动')
  $('workspace-date').textContent = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long', timeZone: 'Asia/Shanghai' }).format(new Date())
  const commentStates = { queued: '等待判断', analyzing: '判断是否评论', planned: '等待发表', skipped: '决定不评论', sent: '已发表', uncertain: '待核对，不重发', failed: '生成失败', cancelled: '已取消' }
  const comments = agents.interaction?.comments || []
  const commentsVersion = JSON.stringify(comments)
  if ($('public-comments').dataset.version !== commentsVersion) {
    $('public-comments').dataset.version = commentsVersion
    $('public-comments').replaceChildren(...comments.map(item => {
      const card = document.createElement('article'); card.className = 'job-card'
      card.append(videoLink(item.bvid), badge(commentStates[item.state] || item.state), text('p', item.text || item.reason || '等待处理'))
      if (item.error) card.append(text('p', explain(item.error), 'error'))
      return card
    }))
  }
  const auto = data.automation, dm = data.messaging
  const active = [data.agent?.activity, auto?.activity, agents.interaction?.activity].filter(Boolean)
  const modelNames = { segment_summary: '分段总结', notes_summary: '合并分段笔记', video_summary: '最终总结', video_decision: '视频评价与好友匹配', requested_recommendations: '挑选请求结果', dm_reply: '生成私信回复', dm_summary: '整理对话记忆', check: '模型连接检查' }
  for (const call of data.recent_model_calls || []) if (call.state === 'running') active.push({ message: `模型正在处理：${modelNames[call.purpose] || call.purpose}`, started: call.started })
  const activityVersion = JSON.stringify([active.map(item => [item.message, item.title, item.bvid, item.reference, item.started]), data.agent?.error])
  if ($('live-activity').dataset.version !== activityVersion) {
    $('live-activity').dataset.version = activityVersion
    $('live-activity').replaceChildren(...active.map(item => {
      const card = document.createElement('article'); card.className = 'activity-item'
      card.append(text('strong', item.message), text('p', item.title || item.bvid || (item.reference ? `任务 / 视频 ${item.reference}` : '')),
        text('small', '', 'subtle activity-elapsed'))
      card.lastElementChild.dataset.started = String(item.started)
      return card
    }))
    if (!active.length) $('live-activity').append(text('p', data.agent?.error ? `分析等待处理：${explain(data.agent.error)}` : '暂无进行中的任务。', 'subtle'))
  }
  for (const elapsed of $('live-activity').querySelectorAll('.activity-elapsed')) {
    elapsed.textContent = `当前步骤已等待 ${Math.max(0, Math.floor(Date.now() / 1000 - Number(elapsed.dataset.started)))} 秒`
  }
  const window = dm.delivery_window
  $('send-progress').textContent = window ? `今日发送 ${window.used} 次 · 聊天不设每日上限。${window.reason ? `${explain(window.reason)}，最早 ${new Date(window.next_at * 1000).toLocaleTimeString()} 可再次发送。` : '当前可发送。'}主动推荐每位好友每天最多 5 个视频，点播单独处理。` : ''
  const budget = auto?.model_budget
  $('model-progress').textContent = budget ? `今日模型已调用 ${budget.used} 次 · 不设本地次数上限。${auto.delivery_error ? `发送等待：${explain(auto.delivery_error)}` : ''}` : ''
  const friends = data.social?.friends || []
  const eligible = friends.filter(friend => friend.recommend && friend.auto_send && dm.allowed_peers.includes(friend.peer))
  $('recommendation-readiness').textContent = !auto?.config.proactive_enabled ? '主动推荐已关闭，可在浏览与推荐中启用。'
    : !dm.send_enabled ? '主动推荐无法发送：插件尚未开启私信发送。'
    : !eligible.length ? '主动推荐尚无可发送对象：请添加好友兴趣，并启用“生成推荐”和“自动发送推荐”。已有点播请求仍单独处理。'
    : `${eligible.length} 位接收好友 · 当前 ${eligible.filter(friend => !friend.delivery?.reason).length} 位允许推送。只推荐值得看且匹配兴趣的视频。`
  const events = data.events || []
  const recent = events.slice(0, 4), recentVersion = JSON.stringify(recent)
  if ($('recent-activity').dataset.version !== recentVersion) {
    $('recent-activity').dataset.version = recentVersion
    $('recent-activity').replaceChildren(...recent.map(event => {
      const row = document.createElement('article'); row.className = 'activity-item'
      row.append(text('time', new Date(event.created * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })), text('p', `${event.message}${event.error ? `：${explain(event.error)}` : ''}`))
      return row
    }))
    if (!recent.length) $('recent-activity').append(text('p', '暂无活动，任务执行后会显示在这里。', 'empty'))
  }
  const log = $('runtime-log')
  const version = String(events[0]?.id || 0)
  if (log.dataset.version !== version) {
    log.dataset.version = version
    log.replaceChildren(...events.map(event => {
      const row = document.createElement('article'); row.className = 'activity-item'
      row.append(text('small', `${new Date(event.created * 1000).toLocaleString()} · ${event.category}${event.reference ? ` · ${event.reference}` : ''}`, 'subtle'),
        text('p', `${event.message}${event.error ? `：${explain(event.error)}` : ''}`))
      return row
    }))
    if (!events.length) log.append(text('p', '暂无运行记录。', 'subtle'))
  }
  const requests = (auto?.jobs || []).filter(job => job.kind === 'request' && !['complete', 'expired'].includes(job.state)).slice(0, 2)
  const requestsVersion = JSON.stringify(requests)
  if ($('live-requests').dataset.version !== requestsVersion) {
    $('live-requests').dataset.version = requestsVersion
    $('live-requests').replaceChildren(...requests.map(job => {
    const card = document.createElement('article'); card.className = 'activity-item'
    const deliveries = (job.deliveries || []).filter(item => item.role === 'result')
    card.append(text('strong', `请求 #${job.id} · ${job.keyword}`), text('p', `已总结 ${job.videos.filter(video => video.state === 'done').length} 个 · ${deliveries.length ? `推荐回复 ${deliveries.filter(item => item.state === 'sent').length} / ${deliveries.length} 条已确认发送` : '推荐回复尚未生成'}`))
    if (job.blocked_reason || job.error) card.append(text('p', explain(job.blocked_reason || job.error), 'error'))
    card.append(button('查看完整处理过程', async () => { navigate('automation'); selectPanel('browse-history') }))
      return card
    }))
  }
}
