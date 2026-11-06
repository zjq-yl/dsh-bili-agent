import { spawn } from 'node:child_process'
import { mkdtemp, readFile, writeFile, rm, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import assert from 'node:assert/strict'
import { startPanel } from '../../dist/panel.js'

// Standalone browser acceptance check. All records and covers below are synthetic.
const directory = await mkdtemp(join(tmpdir(), 'bili-ui-'))
const state = {
  ready: true, version: 'test',
  agent: { state: 'ANALYZE', queue: [], activity: { message: '正在总结第 2 / 5 段', title: '测试视频：如何学习 Python', started: Date.now() / 1000 - 12 } },
  messaging: { state: 'RUNNING', account_uid: '100', send_enabled: true, allowed_peers: ['200'], auto_send: true, outbox: [], delivery_window: { used: 4, limit: 20, reason: 'DM_SEND_INTERVAL', next_at: Date.now() / 1000 + 45 } },
  social: { friends: [], recommendations: [] },
  automation: { config: { discovery_source: 'popular_first', max_video_seconds: 600, comments_enabled: true, comment_daily_limit: 3, share_cards: true, actions_enabled: true, dm_auto_start: true, request_enabled: true, favorite_folder: '', scheduled: true, proactive_enabled: true, start_time: '18:00', end_time: '21:00', rounds: 3, per_round: 5 }, slots: ['18:00', '19:00', '20:00'], assessments: [], actions: [], jobs: [{ id: 1, kind: 'request', peer: '200', keyword: 'Python 入门', state: 'waiting', wanted: 2, round_index: 1, rounds: 3, videos: [{ bvid: 'BV0000000001', state: 'analyzing', checkpoint: 2 }], deliveries: [] }], model_budget: { used: 45, limit: 120, reset_at: Date.now() / 1000 + 3600 } },
  events: [{ id: 1, created: Date.now() / 1000, category: 'analysis', reference: 'BV0000000001', message: '正在总结第 2 / 5 段' }],
}

state.agents = { video: { state: 'RUNNING' }, interaction: { state: 'RUNNING', comments: [] } }
state.automation.config.mentions_enabled = true
state.agents.interaction.mentions = { items: [
  { id: '501', peer: '200', bvid: 'BV0000000001', created: Date.now() / 1000, state: 'waiting_video' },
  { id: '502', peer: '200', bvid: 'BV0000000002', created: Date.now() / 1000 - 30, state: 'failed', error: 'MENTION_CONTEXT_CHANGED' },
] }
state.automation.jobs[0].created = Date.now() / 1000 - 60
state.automation.jobs.push(
  { id: 9, kind: 'scheduled', keyword: '热门视频与科技动态', state: 'complete', created: Date.now() / 1000 - 3600, round_index: 3, rounds: 3, videos: [], deliveries: [] },
  { id: 2, kind: 'request', peer: '201', keyword: '合成测试：较长的学习主题与推荐需求，用于检查记录标题截断和窄屏布局', state: 'failed', error: 'MODEL_NOT_CONFIGURED', created: Date.now() / 1000 - 120, round_index: 1, rounds: 3, videos: [], deliveries: [] },
)
const titles = ['用 Python 整理日常工作', '从零理解机器学习', '周末去山里走一走', '天文入门：认识星空', '一个人的简单晚餐', '给房间添一点绿色', '摄影里的光与影', '数学也可以很有趣', '从想法到一个小项目', '十分钟认识一座城市', '关于阅读的几个习惯', '做一张喜欢的书桌', '用代码记录生活', '大自然的声音', '轻松学一点新知识', '最后一页的测试视频']
const items = titles.map((title, i) => ({ id: i + 1, title, bvid: `BV${String(i + 1).padStart(10, '0')}`, page: 1, author: '演示 UP 主', state: 'done', duration: 180 + i * 23, cover: `https://i0.hdslb.com/bfs/archive/panel-test-${i}.svg`, summary: '这是只在详情中展示的合成总结。', assessment: { rating: i % 3 ? '可以看' : '值得看', reason: '这是测试评价理由。' } }))
state.automation.assessments = items.map(item => ({ assessment: item.assessment }))
const actions = []
const contacts = {
  mutual: [{ peer: '200', name: '合成互关好友', kind: 'mutual' }],
  followers: [{ peer: '201', name: '合成粉丝', kind: 'followers' }],
  following: [{ peer: '202', name: '合成关注 UP 主', kind: 'following' }],
}
const panel = await startPanel({ async call(method, params) {
  if (method === 'friends.directory') return { items: params.page === 1 ? contacts[params.kind] : [],
    page: params.page, source_total: 1, has_more: params.kind === 'following' && params.page === 1 }
  if (method === 'friends.lookup') return { peer: params.peer_uid, kind: 'mutual' }
  if (method === 'friends.contact') contacts.followers[0].connection = { state: params.action === 'decline' ? 'declined' : 'pending' }
  if (method === 'friends.message') { actions.push({ method, params }); return { state: 'sent' } }
  if (method === 'mentions.retry') state.agents.interaction.mentions.items.find(x => x.id === params.id).state = 'queued'
  if (method === 'library.list') {
    const found = items.filter(item => item.title.includes(params.query || '') && (!params.rating || item.assessment.rating === params.rating))
    return { items: found.slice(params.offset, params.offset + params.limit), total: found.length, counts: { all: items.length, '值得看': 6, '可以看': 10, '没必要看': 0, unrated: 0 } }
  }
  if (method === 'agent.result') {
    const item = items.find(item => item.id === params.video_id)
    return { state: item.state, assessment: item.assessment, result: { ...item, facts: [], evidence: [], analysis: { question: '示例问题', conclusion: '示例结论', reasoning: '根据测试字幕得出的示例说明', caveats: '这是合成数据，仅用于验证界面。', takeaways: '检查阅读体验' } } }
  }
  if (method === 'memory.profile') return { name: '测试角色', style: '简洁', persona: '测试人设' }
  if (method === 'friends.connect') {
    state.social.friends = [{ ...params.profile, peer: params.profile.peer_uid,
      delivery: { used: 0, reason: 'FRIEND_NOT_MUTUAL' }, connection: { enabled: 1, state: 'waiting_mutual', following: 1, mutual: 0, checked: Date.now() / 1000 } }]
  }
  if (method === 'friends.connection' && params.action === 'pause') state.social.friends[0].connection.enabled = 0
  if (method === 'automation.configure') Object.assign(state.automation.config, params.config)
  if (!['worker.health', 'library.refresh'].includes(method)) actions.push({ method, params })
  return method === 'platform.enqueue' ? { id: 17 } : state
} }, Promise.resolve(''), () => ({ configured: true, provider: 'test', model: 'test' }), directory)
const browser = spawn(process.env.BILI_TEST_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', ['--headless', '--disable-gpu', '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-background-networking', `--user-data-dir=${directory}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
let socket
try {
  let port
  for (let i = 0; i < 100; i++) {
    try { port = (await readFile(join(directory, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break } catch {}
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.ok(port, 'headless browser started')
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(5000) })).json()
  socket = new WebSocket(version.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Browser debugger connection timed out')), 5000)
    socket.addEventListener('open', () => { clearTimeout(timer); resolve() }, { once: true })
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Browser debugger connection failed')) }, { once: true })
  })
  let id = 0
  const pending = new Map(), errors = []
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data)
    if (message.method === 'Fetch.requestPaused') {
      const index = Number(message.params.request.url.match(/panel-test-(\d+)/)?.[1] || 0)
      const palettes = [['#d3edf4','#295970'],['#e8e1fa','#615080'],['#e3efdf','#4b7058'],['#20364c','#b9dbf5'],['#f9e9d9','#91654b']]
      const [bg, ink] = palettes[index % palettes.length]
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400"><rect width="640" height="400" fill="${bg}"/><circle cx="540" cy="65" r="150" fill="${ink}" opacity=".08"/><path d="M0 340 Q170 150 340 310 T640 200 V400 H0" fill="${ink}" opacity=".13"/><text x="36" y="70" fill="${ink}" font-size="19" font-family="sans-serif">DEMO / ${String(index + 1).padStart(2,'0')}</text><text x="36" y="208" fill="${ink}" font-size="34" font-weight="600" font-family="Microsoft YaHei,sans-serif">${titles[index].slice(0,10)}</text><text x="36" y="255" fill="${ink}" font-size="18" font-family="Microsoft YaHei,sans-serif">合成封面 · 界面验收</text></svg>`
      void call('Fetch.fulfillRequest', { requestId: message.params.requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'image/svg+xml' }], body: Buffer.from(svg).toString('base64') }, message.sessionId)
    }
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text)
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
  })
  const call = async (method, params = {}, sessionId) => {
    const key = ++id
    let timer
    const response = new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 10000)
      pending.set(key, resolve)
    })
    socket.send(JSON.stringify({ id: key, method, params, ...(sessionId ? { sessionId } : {}) }))
    const result = await response
    clearTimeout(timer)
    if (result.error) throw new Error(JSON.stringify(result.error))
    return result.result
  }
  const target = await call('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await call('Target.attachToTarget', { targetId: target.targetId, flatten: true })
  await call('Runtime.enable', {}, sessionId)
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false }, sessionId)

  await call('Fetch.enable', { patterns: [{ urlPattern: 'https://i0.hdslb.com/bfs/archive/panel-test-*', requestStage: 'Request' }] }, sessionId)
  await call('Page.navigate', { url: panel.url }, sessionId)
  const evaluate = async expression => {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
    return result.result.value
  }
  const until = async expression => {
    for (let i = 0; i < 80; i++) {
      if (await evaluate(expression)) return
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error(`UI timed out: ${expression}`)
  }
  const screenshotDir = resolve('data/ui-checks')
  await mkdir(screenshotDir, { recursive: true })
  const screenshot = async name => {
    await evaluate('window.scrollTo(0,0)')
    await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    const result = await call('Page.captureScreenshot', { format: 'png' }, sessionId)
    await writeFile(join(screenshotDir, `${name}.png`), Buffer.from(result.data, 'base64'))
  }
  const navigate = async id => evaluate(`document.querySelector('nav a[href="#${id}"]').click()`)
  const noOverflow = async label => assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, label)
  await until("document.getElementById('updated').textContent.includes('每秒同步')")
  // Visible test badge is deliberately injected by this harness, never by production code.
  await evaluate("document.querySelector('.header-tools').prepend(Object.assign(document.createElement('span'), { className: 'badge warning', textContent: '合成数据 · 界面验收' }))")
  assert.deepEqual(await evaluate("[...document.querySelectorAll('main > section')].map(x=>x.id)"), ['overview', 'automation', 'videos', 'messages', 'friends-section', 'persona', 'actions', 'logs'])
  assert.equal(await evaluate("new Set([...document.querySelectorAll('[id]')].map(x=>x.id)).size === document.querySelectorAll('[id]').length"), true, 'no duplicate IDs')
  await screenshot('workbench-desktop')
  state.agent.activity.message = '正在合并笔记并生成最终总结'
  await until("document.getElementById('video-agent-task').textContent.includes('最终总结')")
  for (const id of ['videos', 'automation', 'messages', 'friends-section', 'actions', 'persona', 'logs', 'overview']) {
    await navigate(id)
    assert.equal(await evaluate(`document.getElementById('${id}').hidden`), false)
    await noOverflow(`desktop ${id}`)
  }
  await navigate('videos')
  await until("document.querySelectorAll('.video-card').length === 15")
  await until("[...document.querySelectorAll('.video-cover img')].every(x=>x.complete && x.naturalWidth > 0)")
  assert.equal(await evaluate("getComputedStyle(document.getElementById('video-assessments')).gridTemplateColumns.split(' ').length"), 5)
  assert.equal(await evaluate("document.querySelectorAll('.video-excerpt,.video-reason').length"), 0)
  assert.equal(await evaluate("document.getElementById('video-assessments').textContent.includes('合成总结')"), false)
  await screenshot('videos-desktop')
  await evaluate("document.querySelector('.video-cover').click()")
  await until("document.getElementById('result-dialog').open")
  assert.match(await evaluate("document.getElementById('result-body').textContent"), /只在详情中展示/)
  await screenshot('video-summary')
  await evaluate("document.getElementById('close-result').click()")
  await evaluate("document.getElementById('library-next').click()")
  await until("document.querySelectorAll('.video-card').length === 1")
  assert.match(await evaluate("document.getElementById('library-page').textContent"), /第 2 \/ 2 页/)
  await evaluate("document.getElementById('library-prev').click()")
  await until("document.querySelectorAll('.video-card').length === 15")
  await evaluate("document.querySelector('[data-rating=\"没必要看\"]').click()")
  await until("document.getElementById('video-assessments').textContent.includes('还没有匹配')")
  await evaluate("document.querySelector('[data-rating=\"\"]').click()")
  await until("document.querySelectorAll('.video-card').length === 15")
  await evaluate("document.getElementById('library-query').value='Python'; document.getElementById('library-search').requestSubmit()")
  await until("document.querySelectorAll('.video-card').length === 1")
  await evaluate("document.getElementById('library-query').value=''; document.getElementById('library-search').requestSubmit()")
  await until("document.querySelectorAll('.video-card').length === 15")
  await evaluate("document.getElementById('add-video-shortcut').click()")
  assert.equal(await evaluate("document.getElementById('video-tools').hidden"), false)
  assert.equal(await evaluate('document.activeElement.id'), 'video-input')
  await evaluate("document.getElementById('video-input').value='BV0000000017'; document.getElementById('video-form').requestSubmit()")
  await until("document.getElementById('notice').textContent.includes('视频已入队')")
  assert.ok(actions.some(action => action.method === 'platform.enqueue' && action.params.bvid === 'BV0000000017'))
  await evaluate("document.getElementById('tab-video-tools').focus()")
  await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowLeft', code: 'ArrowLeft' }, sessionId)
  assert.equal(await evaluate("document.getElementById('video-history').hidden"), false, 'keyboard tabs')
  await navigate('automation')
  await screenshot('schedule-desktop')
  await evaluate("document.querySelector('[data-start=\"09:00\"]').click()")
  assert.equal(await evaluate("document.getElementById('auto-start').value"), '09:00')
  assert.match(await evaluate("document.getElementById('schedule-slots').textContent"), /09:0010:0011:00/)
  await new Promise(resolve => setTimeout(resolve, 1100))
  assert.equal(await evaluate("document.getElementById('auto-start').value"), '09:00', 'live sync must not overwrite unsaved settings')
  await evaluate("document.getElementById('auto-end').value='08:00'; document.getElementById('auto-end').dispatchEvent(new Event('input'))")
  assert.equal(await evaluate("document.getElementById('automation-form').checkValidity()"), false)
  await evaluate("document.querySelector('[data-start=\"14:00\"]').click(); document.getElementById('automation-form').requestSubmit()")
  await until("document.getElementById('notice').textContent === '设置已保存。'")
  const saved = actions.find(action => action.method === 'automation.configure')
  assert.equal(saved?.params.config.start_time, '14:00'); assert.equal(saved?.params.config.end_time, '17:00')
  assert.equal(saved?.params.config.rounds, 3); assert.equal(saved?.params.config.per_round, 5)
  await evaluate("document.getElementById('auto-scheduled').click()")
  assert.match(await evaluate("document.getElementById('schedule-description').textContent"), /定时浏览已关闭/)
  await evaluate("document.getElementById('auto-scheduled').click()")
  await evaluate("document.getElementById('tab-browse-history').click()")
  assert.equal(await evaluate("document.getElementById('browse-history').hidden"), false)
  assert.deepEqual(await evaluate("[...document.querySelectorAll('#browse-jobs > details')].map(x=>x.dataset.jobId)"), ['1', '2', '9'], 'creation time determines descending order, not ID')
  assert.equal(await evaluate("document.querySelectorAll('#browse-jobs details[open]').length"), 0, 'history starts collapsed')
  assert.ok(await evaluate("document.querySelector('#browse-jobs time').dateTime"), 'real creation time is shown')
  await screenshot('browse-history-collapsed-desktop')
  await evaluate("document.querySelector('#browse-jobs summary').focus()")
  await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' }, sessionId)
  await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 }, sessionId)
  await until("document.querySelector('#browse-jobs details').open")
  state.automation.jobs[0].videos[0].checkpoint = 3
  await until("document.querySelector('#browse-jobs .job-detail').textContent.includes('已总结 3 段')")
  assert.equal(await evaluate("document.querySelector('#browse-jobs details').open"), true, 'live refresh preserves expansion')
  assert.equal(await evaluate("document.activeElement === document.querySelector('#browse-jobs summary')"), true, 'live refresh preserves keyboard focus')
  assert.equal(await evaluate("document.querySelectorAll('#browse-jobs details[open]').length"), 1)
  await screenshot('browse-history-expanded-desktop')
  await evaluate("document.querySelector('#browse-jobs details[data-job-id=\"2\"] summary').click()")
  assert.ok(await evaluate("document.querySelector('#browse-jobs details[data-job-id=\"2\"] .error').textContent"), 'error remains available in expanded detail')
  await evaluate("document.getElementById('tab-browse-plan').click()")
  await navigate('friends-section')
  await until("document.getElementById('contact-list').textContent.includes('合成互关好友')")
  assert.equal(actions.some(x => ['friends.contact', 'friends.message', 'friends.connect'].includes(x.method)), false, 'listing never follows or sends')
  await screenshot('friends-mutual-desktop')
  await evaluate("[...document.querySelectorAll('#contact-list button')].find(x=>x.textContent==='发私信').click(); document.getElementById('contact-message-text').value='测试选择发送'; document.getElementById('contact-message-form').requestSubmit()")
  await until("document.getElementById('contact-message-status').textContent.includes('已确认发送')")
  assert.ok(actions.some(x => x.method === 'friends.message' && x.params.peer_uid === '200' && x.params.text === '测试选择发送' && x.params.command_id.startsWith('panel-note-')))
  await evaluate("document.getElementById('close-contact').click(); document.querySelector('[data-contacts=followers]').click()")
  await until("document.getElementById('contact-list').textContent.includes('合成粉丝')")
  await evaluate("[...document.querySelectorAll('#contact-list button')].find(x=>x.textContent==='不回关').click()")
  await until("document.getElementById('contact-list').textContent.includes('已选择不回关')")
  await screenshot('friends-followers-desktop')
  await evaluate("[...document.querySelectorAll('#contact-list button')].find(x=>x.textContent==='改为回关').click()")
  await until("document.getElementById('notice').textContent.includes('已安排回关')")
  assert.ok(actions.some(x => x.method === 'friends.contact' && x.params.action === 'follow_back'))
  await evaluate("document.querySelector('[data-contacts=following]').click()")
  await until("document.getElementById('contact-list').textContent.includes('合成关注 UP 主')")
  assert.equal(await evaluate("[...document.querySelectorAll('#contact-list button')].some(x=>x.textContent==='发私信')"), false)
  await evaluate("document.getElementById('contact-next').click()")
  await until("document.getElementById('contact-page').textContent.includes('第 2 页')")
  await evaluate("document.getElementById('contact-uid').value='200'; document.getElementById('contact-search').requestSubmit()")
  await until("document.getElementById('contact-lookup').textContent.includes('UID 200')")
  await evaluate("document.querySelector('[data-contacts=mutual]').click()")
  await evaluate("document.getElementById('new-friend').click()")
  assert.equal(await evaluate('document.activeElement.id'), 'friend-uid')
  assert.equal(await evaluate("document.getElementById('friend-connect').checked"), true)
  await evaluate("document.getElementById('friend-uid').value='222'; document.getElementById('friend-name').value='测试好友'; document.getElementById('friend-interests').value='Python'; document.getElementById('friend-greeting').value='你好，测试招呼'; document.getElementById('friend-form').requestSubmit()")
  await until("document.getElementById('friends').textContent.includes('等待回关')")
  assert.ok(actions.some(action => action.method === 'friends.connect' && action.params.profile.peer_uid === '222' && action.params.greeting === '你好，测试招呼'))
  await screenshot('friends-follow-desktop')
  state.social.friends[0].connection.state = 'ready'; state.social.friends[0].connection.mutual = 1
  await until("document.getElementById('friends').textContent.includes('招呼已发送')")
  await evaluate("[...document.querySelectorAll('#friends button')].find(x=>x.textContent==='暂停好友流程').click()")
  await until("document.getElementById('friends').textContent.includes('已暂停好友流程')")
  await evaluate("[...document.querySelectorAll('#friends button')].find(x=>x.textContent==='编辑档案').click()")
  assert.equal(await evaluate("document.getElementById('friend-connect').checked"), false, 'editing must not follow again')
  await navigate('actions')
  assert.equal(await evaluate("document.getElementById('auto-mentions').checked"), true)
  await until("document.getElementById('mention-list').textContent.includes('等待视频分析')")
  await evaluate("document.querySelector('#mention-list details[data-id=\"502\"] summary').click()")
  await screenshot('mentions-desktop')
  await evaluate("document.querySelector('#mention-list details[data-id=\"502\"] button').click()")
  await until("document.querySelector('#mention-list details[data-id=\"502\"]').textContent.includes('等待核实与理解')")
  assert.ok(actions.some(x => x.method === 'mentions.retry' && x.params.id === '502'))
  for (const width of [390, 360]) {
    await call('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false }, sessionId)
    for (const id of ['overview', 'videos', 'automation', 'messages', 'friends-section', 'actions', 'persona', 'logs']) {
      await navigate(id); await noOverflow(`narrow ${width} ${id}`)
      if (width === 390 && ['videos', 'automation'].includes(id)) await screenshot(`${id}-mobile`)
    }
    await navigate('videos')
    await evaluate("document.getElementById('tab-video-tools').click()")
    await noOverflow(`narrow ${width} video tools`)
    await evaluate("document.getElementById('tab-video-history').click()")
    await navigate('automation')
    await evaluate("document.getElementById('tab-browse-history').click()")
    await noOverflow(`narrow ${width} expandable history`)
    if (width === 390) await screenshot('browse-history-mobile')
    await evaluate("document.getElementById('tab-browse-plan').click()")
    await navigate('friends-section')
    await evaluate("document.querySelector('[data-contacts=mutual]').click()")
    await until("document.getElementById('contact-list').textContent.includes('合成互关好友')")
    await noOverflow(`narrow ${width} mutual directory`)
    if (width === 390) await screenshot('friends-mutual-mobile')
  }
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false }, sessionId)
  await navigate('overview')
  const savedJobs = state.automation.jobs
  state.automation.jobs = []
  await until("document.querySelector('#browse-jobs .empty') !== null")
  state.automation.jobs = savedJobs
  await until("document.querySelectorAll('#browse-jobs > details').length === 3")
  await call('Network.enable', {}, sessionId)
  await call('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 }, sessionId)
  await evaluate("document.getElementById('refresh').click()")
  await until("!document.getElementById('connection-alert').hidden")
  assert.equal(await evaluate("document.getElementById('notice').textContent.includes('Failed to fetch')"), false)
  await call('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }, sessionId)
  await evaluate("document.getElementById('retry-connection').click()")
  await until("document.getElementById('connection-alert').hidden")
  assert.deepEqual(errors, [], 'no uncaught JS exceptions')
  console.log('PASS: 8 routes, 5-column library, video detail/filter/search, schedule validation/save, mutual/fans/following directories, UID lookup, direct DM, decline/follow-back, contact pagination, follow lifecycle, pause/edit, live updates, reconnect, 390/360px overflow; screenshots in data/ui-checks (synthetic data).')
  await call('Browser.close')
} finally {
  socket?.close()
  browser.kill()
  await panel.close()
  await new Promise(resolve => setTimeout(resolve, 1000))
  if (!resolve(directory).startsWith(resolve(tmpdir()) + '\\')) throw new Error('Unexpected temp directory')
  await rm(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 })
}
