/* 原子旅行 V1 — 主应用逻辑
 * 消费 docs/V1-BUILD.md §4 API 合同；后端不可达时自动降级到 MOCK 数据集。
 */
"use strict";

/* UAT 钩子：JS 运行时错误计数，镜像到 <html data-js-errors> 供无头验收断言。
 * capture=false 只收脚本运行时错误，不收资源加载失败（瓦片/图片 404 不算 JS 错误）。 */
window.__jsErrors = [];
(function () {
  const sync = () => document.documentElement.setAttribute("data-js-errors", String(window.__jsErrors.length));
  sync();
  window.addEventListener("error", (e) => {
    window.__jsErrors.push(String((e && e.message) || e));
    sync();
  });
  window.addEventListener("unhandledrejection", (e) => {
    window.__jsErrors.push("unhandledrejection: " + String(e && e.reason));
    sync();
  });
})();

/* ================= 常量 ================= */

const CAT_META = {
  food:            { zh: "美食",   color: "#f59e0b" },
  market_street:   { zh: "市集",   color: "#fbbf24" },
  culture_history: { zh: "人文",   color: "#a78bfa" },
  art_museum:      { zh: "展馆",   color: "#818cf8" },
  nature_park:     { zh: "自然",   color: "#34d399" },
  viewpoint:       { zh: "观景",   color: "#38bdf8" },
  photo_spot:      { zh: "拍照",   color: "#38bdf8" },
  sightseeing:     { zh: "地标",   color: "#60a5fa" },
  activity:        { zh: "玩乐",   color: "#f472b6" },
  shopping:        { zh: "购物",   color: "#fb923c" },
  nightlife:       { zh: "夜生活", color: "#c084fc" },
  transport_experience: { zh: "交通体验", color: "#94a3b8" },
  day_trip:        { zh: "近郊",   color: "#4ade80" },
  onsen_spa:       { zh: "温泉",   color: "#f9a8d4" },
};
/* 未知分类的确定性兜底色（按名字哈希取色，保证同一分类同色） */
const FALLBACK_COLORS = ["#f87171", "#facc15", "#2dd4bf", "#e879f9", "#a3e635", "#fca5a5"];
function catMetaOf(cat) {
  if (CAT_META[cat]) return CAT_META[cat];
  let h = 0;
  for (const ch of String(cat || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return { zh: cat || "其他", color: FALLBACK_COLORS[h % FALLBACK_COLORS.length] };
}
const SLOT_ZH = { morning: "上午", noon: "中午", afternoon: "下午", sunset: "傍晚", evening: "晚上", night: "夜间" };

const state = {
  mode: "unknown",       // 'api' | 'mock'
  planId: null,
  plan: null,
  intent: null,
  currentDay: 1,
  selected: null,        // {day, idx}
  map: null,
  markerLayer: null,
  routeLayer: null,
  markers: new Map(),    // "day:idx" -> marker
  chooser: null,         // {day, idx, token, loading, confirming, cards} 三候选选择器
  chooserSeq: 0,         // 选择器请求序号：丢弃过期响应
  pendingConfirm: null,  // requires_confirmation 的 proposed 负载
  note: null,            // intent 响应的兜底提示（如目的地未识别）
  destinations: [],      // /api/destinations 缓存（动态渲染数据源：name_zh/tagline_zh）
  // ---- V1.1 P1 探索模式 ----
  exploreOn: false,      // 地图处于探索模式（全原子上图）
  atomCache: {},         // dest -> atoms[]（/api/atoms 响应有 max-age=300，这里再会话级缓存）
  exploreLayer: null,    // 探索模式 canvas 图层
  exploreMarkers: null,  // category -> [circleMarker]
  hiddenCats: new Set(), // 图例里被关掉的分类
  wanted: new Set(),     // 「想要」的 atom_id，下次生成时随 intent 上送 explicit_pois
  // ---- V1.1 P2 生成演出 ----
  showMap: null,
  showSkip: false,
  // ---- V1.1 M1.1b 叙事层/顾问对话 ----
  narratives: {},        // atom_id -> L1 顾问叙事（llm_used:false 时保持空）
  dayThemes: {},         // day -> 每日主题
  chatBusy: false,       // 对话请求进行中
  chatSeq: 0,            // 对话请求序号：丢弃过期响应
};

/* ================= DOM ================= */
const $ = (id) => document.getElementById(id);
const els = {
  landing: $("screen-landing"), loading: $("screen-loading"), plan: $("screen-plan"),
  input: $("intent-input"), go: $("btn-go"), chips: $("chips"),
  mockBadge: $("mock-badge"), restart: $("btn-restart"), llmDot: $("llm-dot"),
  loadingTitle: $("loading-title"), loadingDest: $("loading-dest"),
  skeletonCards: $("skeleton-cards"), stageText: $("stage-text"),
  banner: $("swap-banner"), bannerText: $("swap-banner-text"), bannerClose: $("swap-banner-close"),
  planMeta: $("plan-meta"), dayTabs: $("day-tabs"), daySummary: $("day-summary"), timeline: $("timeline"),
  mapBox: $("map"), mapLegend: $("map-legend"),
  overlay: $("confirm-overlay"), confirmLead: $("confirm-lead"), confirmList: $("confirm-list"),
  confirmOk: $("confirm-ok"), confirmCancel: $("confirm-cancel"),
  // V1.1 P1/P2
  mapMode: $("map-mode"), wantCount: $("want-count"), atomCard: $("atom-card"),
  showScreen: $("screen-show"), showMap: $("show-map"),
  showPhase: $("show-phase"), showReason: $("show-reason"), showSkip: $("show-skip"),
  // V1.1 M1.1b
  advisorBanner: $("advisor-banner"), advisorBannerText: $("advisor-banner-text"),
  advisorBannerClose: $("advisor-banner-close"),
  chatLog: $("chat-log"), chatInput: $("chat-input"), chatSend: $("chat-send"),
};

/* ================= 工具 ================= */
const qs = new URLSearchParams(location.search);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function apiPost(path, body, timeoutMs = 6000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data && data.error) throw Object.assign(new Error(data.error.message_zh || "API error"), { payload: data.error });
    return data;
  } finally {
    clearTimeout(t);
  }
}

function catOf(item) { return catMetaOf(item.category); }
function slotKey(day, idx) { return day + ":" + idx; }

/* 任意形状取名字：字符串 | {name_zh} | {name:{zh}} | {name:"..."} */
function nameOf(x) {
  if (x == null) return "";
  if (typeof x === "string") return x;
  if (typeof x.name_zh === "string") return x.name_zh;
  if (x.name && typeof x.name.zh === "string") return x.name.zh;
  if (typeof x.name === "string") return x.name;
  return "";
}

/* 门户字段：字符串 或 {name_zh} 或 null */
function gwName(g) { return nameOf(g); }

/* 收据字段：字符串 或 {text_zh/text, icon, basis[]} */
function receiptText(r) {
  if (r == null) return "";
  if (typeof r === "string") return r;
  return r.text_zh || r.text || "";
}
function receiptIcon(r) {
  return (r && typeof r === "object" && r.icon) || "✦";
}

/* 后端 item 形状防御性归一化 → 前端 canonical item */
function normItem(raw) {
  const sp = raw.spatial || {};
  const name = raw.name_zh || (raw.name && raw.name.zh) || (typeof raw.name === "string" ? raw.name : "") || "未命名";
  const cats = raw.categories || (raw.category ? [raw.category] : []);
  // 坐标校验：非有限数一律置 null（renderMap 跳过 null marker）
  const rawLat = raw.lat != null ? raw.lat : sp.lat;
  const rawLon = raw.lon != null ? raw.lon : sp.lon;
  const lat = Number.isFinite(+rawLat) ? +rawLat : null;
  const lon = Number.isFinite(+rawLon) ? +rawLon : null;
  return {
    type: raw.type || "atom",
    atom_id: raw.atom_id || raw.id || "",
    name_zh: name,
    category: raw.category || cats[0] || "sightseeing",
    slot: raw.slot || "afternoon",
    start: raw.start || raw.start_time || "",
    duration_min: raw.duration_min || (raw.time_structure && raw.time_structure.typical_duration_min) || 60,
    lat, lon,
    receipt: receiptText(raw.receipt) || "综合评分最高",
    receipt_icon: receiptIcon(raw.receipt),
    pinned: !!raw.pinned,
    pinnedByUser: !!raw.pinnedByUser,
    alts: raw.alts || [],
    // 透传后端/mock 语义字段（锚点/成对安排），避免 swap 后丢失
    anchor: !!raw.anchor,
    paired: !!raw.paired,
    meal_kind: raw.meal_kind,
  };
}

function normPlan(raw) {
  const days = (raw.days || []).map((d) => ({
    day: d.day,
    cluster: d.cluster,
    cluster_zh: d.cluster_zh || d.cluster || "",
    gateway_in: gwName(d.gateway_in),
    gateway_out: gwName(d.gateway_out),
    summary: d.summary || "",
    items: (d.items || []).map(normItem),
  }));
  const dest = raw.destination || "osaka";
  return {
    plan_id: raw.plan_id || state.planId,
    destination: dest,
    destination_zh: raw.destination_zh || destZh(dest),
    title: raw.title || "",
    days,
  };
}

/* 目的地中文名/简介：/api/destinations 动态数据源，未探测到时退回小词典 */
function destZh(key) {
  const d = (state.destinations || []).find((x) => x.id === key);
  if (d && d.name_zh) return d.name_zh;
  return { osaka: "大阪", bangkok: "曼谷", abudhabi: "阿布扎比" }[key] || (key ? String(key) : "");
}
function destTagline(key) {
  const d = (state.destinations || []).find((x) => x.id === key);
  return (d && d.tagline_zh) || "";
}

/* ================= 模式判定 ================= */
async function ensureMode() {
  if (state.mode !== "unknown") return state.mode;
  if (qs.get("forceMock") === "1") return setMode("mock");
  try {
    // 轻量探测：真实后端应服务本页面（同源），用 destinations 端点试连
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2500);
    const res = await fetch("/api/destinations", { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    state.destinations = data.destinations || [];  // 动态渲染数据源（name_zh/tagline_zh）
    return setMode("api");
  } catch {
    return setMode("mock");
  }
}
function setMode(m) {
  state.mode = m;
  if (m === "mock") {
    els.mockBadge.classList.remove("hidden");
  } else {
    // api 模式：探测顾问模式状态，顶栏状态点给出克制提示（不打扰主流程）
    refreshLlmDot();
  }
  return m;
}
async function refreshLlmDot() {
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const h = await res.json();
    els.llmDot.classList.remove("hidden");
    if (h.llm === "on") {
      els.llmDot.classList.add("on");
      els.llmDot.title = "顾问模式在线";
    } else {
      els.llmDot.classList.add("off");
      els.llmDot.title = "顾问模式未启用，基础功能正常";
    }
  } catch { /* health 探测失败不阻塞主流程，状态点保持隐藏 */ }
}

/* ================= 首屏目的地动态化（M1.2：不再写死大阪主导） ================= */
/* 每个目的地一条示例意图（chip 标注目的地，让用户感知多目的地）；
 * 新目的地没配示例时退通用模板。这些是「示例」不是「规则」。 */
const DEST_SAMPLES = {
  osaka:    { emoji: "🌸", text: "清明想和女朋友去大阪，5天，拍照和美食为主，人均1万左右" },
  bangkok:  { emoji: "⚡", text: "特种兵穷游曼谷3天" },
  abudhabi: { emoji: "🏜️", text: "想去阿布扎比看海和清真寺，4天，节奏别太赶" },
};
/* 离线/后端不可达时的静态兜底（与 index.html 内嵌 chips 一致），保证首屏可用 */
const DEST_FALLBACK = [
  { id: "osaka", name_zh: "大阪" },
  { id: "bangkok", name_zh: "曼谷" },
  { id: "abudhabi", name_zh: "阿布扎比" },
];

function sampleFor(d) {
  return DEST_SAMPLES[d.id] || { emoji: "🧭", text: `想去${d.name_zh || d.id}玩几天，帮我安排` };
}

function renderLanding(dests) {
  els.chips.innerHTML = dests.map((d) => {
    const s = sampleFor(d);
    return `<button class="chip" data-text="${esc(s.text)}">${s.emoji} ${esc(s.text)}</button>`;
  }).join("");
  // hero-foot：真实城市列表 + tagline_zh（后端新字段，缺失时只列名）+ 原子总数
  const names = dests.map((d) =>
    d.tagline_zh ? `${esc(d.name_zh)}「${esc(d.tagline_zh)}」` : esc(d.name_zh)).join(" · ");
  const total = dests.reduce((n, d) => n + (d.atom_count || 0), 0);
  const foot = $("hero-foot");
  if (foot) foot.innerHTML = `已支持 ${names}${total ? ` · 共 ${total} 个体验原子，持续增加` : ""}`;
}

async function initLanding() {
  if (qs.get("forceMock") === "1") { renderLanding(DEST_FALLBACK); return; }
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 3000);
    const res = await fetch("/api/destinations", { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const dests = ((await res.json()) || {}).destinations || [];
    renderLanding(dests.length ? dests : DEST_FALLBACK);
  } catch {
    renderLanding(DEST_FALLBACK); // 离线兜底：静态三城，不阻塞首屏
  }
}

/* ================= chat 占位文案（M1.2：开放式 + 每次进站轮换） ================= */
/* 旧文案「下雨了 / 今天太累了 / 想吃蟹道乐」被误读成「只能说这三句」；
 * 每组都强调「随便说」，示例跨场景（懒/买/累/雨/预算），避免又是写死的一组。 */
const CHAT_PLACEHOLDERS = [
  "随便说，比如：第二天想睡懒觉 / 留半天买手办 / 不想去人多的地方…",
  "想到什么说什么：想吃顿好的 / 今天下午想放空 / 帮我避开网红店…",
  "直接说就行：明天下雨怎么办 / 多加一个看夜景的地方 / 预算想省一点…",
  "像跟朋友说话一样：走累了 / 想给爸妈安排轻松点 / 最后一天想买伴手礼…",
];
function rotateChatPlaceholder() {
  if (els.chatInput) {
    els.chatInput.placeholder =
      CHAT_PLACEHOLDERS[Math.floor(Math.random() * CHAT_PLACEHOLDERS.length)];
  }
}

/* ================= 主流程 ================= */
async function startJourney(text) {
  els.go.disabled = true;
  try {
    await ensureMode();
    const useMock = state.mode === "mock";

    // 1) intent（V1.1 P1：探索模式「想要」的 atom_id 随 explicit_pois 上送，后端锚定 pin）
    const intentBody = { text };
    if (state.wanted.size) intentBody.explicit_pois = [...state.wanted];
    const intentRes = useMock
      ? await MOCK.postIntent(text, [...state.wanted])
      : await apiPost("/api/intent", intentBody, 8000);
    state.intent = intentRes.intent_vector;
    state.planId = intentRes.plan_id;
    state.note = intentRes.note || null;

    showScreen("loading");
    const destRaw = intentRes.destination
      || (state.intent && state.intent.geography && state.intent.geography.destination_specified);
    const destKey = Array.isArray(destRaw) ? destRaw[0] : destRaw;
    const destZh = catDestZh(destKey);
    els.loadingDest.textContent = "目的地：" + destZh;
    els.loadingDest.classList.remove("hidden");

    // 2) skeleton → 立即渲染骨架卡
    const sk = useMock
      ? await MOCK.postSkeleton(state.planId)
      : await apiPost(`/api/plan/${state.planId}/skeleton`, {}, 8000);
    renderSkeleton(sk.days || []);

    // 3) refine 与演出并行：演出是附加层，refine 数据就绪后任何时候可跳过直达结果
    startTicker(MOCK.GENERIC_STAGES);
    const refinePromise = useMock
      ? MOCK.postRefine(state.planId, (label) => setStage(label))
      : apiPost(`/api/plan/${state.planId}/refine`, {}, 30000);

    // V1.1 P2：拿到 trace 后进入演出态（首访全量；无 trace 时静默走原骨架流程）
    const shown = await maybeRunShow(refinePromise, useMock, destKey);
    const refineRes = await refinePromise;
    stopTicker();
    if (shown) await finishShow(); // 演出收尾（若用户未跳过，此时动画已播完或正在收尾）

    state.plan = normPlan(refineRes.plan);
    if (state.plan.plan_id) state.planId = state.plan.plan_id;
    state.currentDay = state.plan.days.length ? state.plan.days[0].day : 1;

    showScreen("plan");
    renderPlan();
    // V1.1 P3：行程页渲染后后台调叙事层（骨架先行，叙事流式叠加；失败静默降级）
    fetchNarratives(useMock);
  } catch (err) {
    stopTicker();
    cancelShow();
    showScreen("landing");
    alert("生成失败：" + (err && err.message ? err.message : "未知错误"));
  } finally {
    els.go.disabled = false;
  }
}

function catDestZh(destKey) {
  return destZh(destKey) || "未识别";
}

function showScreen(name) {
  els.landing.classList.toggle("hidden", name !== "landing");
  els.loading.classList.toggle("hidden", name !== "loading");
  els.plan.classList.toggle("hidden", name !== "plan");
  els.showScreen.classList.toggle("hidden", name !== "show");
  els.restart.classList.toggle("hidden", name === "landing");
  if (name === "plan" && state.map) setTimeout(() => state.map.invalidateSize(), 60);
}

/* ---------- 骨架态 ---------- */
function renderSkeleton(days) {
  els.skeletonCards.innerHTML = "";
  days.forEach((d, i) => {
    const card = document.createElement("div");
    card.className = "sk-card";
    card.style.animationDelay = i * 0.12 + "s";
    const gw = gwName(d.gateway_in) ? `✈ 抵达 ${gwName(d.gateway_in)}` : gwName(d.gateway_out) ? `✈ 返程 ${gwName(d.gateway_out)}` : "";
    card.innerHTML =
      `<div class="sk-day">DAY ${d.day}</div>` +
      `<div class="sk-cluster">${esc(d.cluster_zh || d.cluster || "规划中")}</div>` +
      (gw ? `<div class="sk-gateway">${esc(gw)}</div>` : "") +
      `<div class="sk-slots"><div class="sk-slot"></div><div class="sk-slot"></div><div class="sk-slot"></div></div>`;
    els.skeletonCards.appendChild(card);
  });
}

let tickerTimer = null;
function startTicker(fallback) {
  let i = 0;
  setStage(fallback[0]);
  tickerTimer = setInterval(() => {
    i = (i + 1) % fallback.length;
    setStage(fallback[i]);
  }, 900);
}
function stopTicker() {
  if (tickerTimer) { clearInterval(tickerTimer); tickerTimer = null; }
}
function setStage(text) {
  els.stageText.classList.add("fade");
  setTimeout(() => {
    els.stageText.textContent = text;
    els.stageText.classList.remove("fade");
  }, 240);
  els.loadingTitle.textContent = "骨架已就绪，正在逐日求精…";
}

/* ================= 主界面渲染 ================= */
function mapPaneVisible() {
  const pane = $("map-pane");
  return pane && window.getComputedStyle(pane).display !== "none";
}

function renderPlan() {
  rotateChatPlaceholder(); // 每次进站换一组开放式提示，避免「只能说那三句」的错觉
  renderMeta();
  renderDayTabs();
  renderDay();
  // 窄屏下地图面板默认隐藏（行程/地图 tab 切换）：延迟到切到地图时再初始化，
  // 避免 Leaflet 在 0 尺寸容器上初始化
  if (mapPaneVisible()) {
    initMap();
    renderMap();
  }
  renderLegend();
  // 后端目的地兜底提示（如「XX 的原子库还在建设中，先按大阪出草稿」）：
  // sticky 常驻展示（用户手动关闭），不再 6 秒静默消失
  if (state.note) { showBanner(state.note, true); state.note = null; }
}

function renderMeta() {
  const p = state.plan;
  const iv = state.intent || {};
  const party = iv.party && iv.party.composition && iv.party.composition[0];
  const partyZh = { couple: "情侣", elderly_parents: "带父母", solo: "独行", family_kids: "亲子", group_friends: "朋友结伴", business: "差旅" }[party] || "";
  const emoZh = { photo: "拍照", foodie: "美食", romantic: "浪漫", healing: "治愈", night_view: "夜景", iconic: "地标", lively_local: "烟火气", cultural_deep: "文化深度", quiet_escape: "避世", family_fun: "合家欢", adventure: "冒险", bargain: "淘货", hidden_gem: "小众", nostalgic: "怀旧", luxury: "轻奢" };
  const tags = [];
  if (partyZh) tags.push(partyZh);
  (iv.emotion_goals || []).slice(0, 4).forEach((t) => emoZh[t] && tags.push(emoZh[t]));
  if (iv.budget && iv.budget.per_person_cny) tags.push("人均 ¥" + Math.round(iv.budget.per_person_cny / 1000) + "k");
  const total = p.days.reduce((n, d) => n + d.items.length, 0);
  const tagline = destTagline(p.destination);
  els.planMeta.innerHTML =
    `<div class="ptitle">${esc(p.title || p.destination_zh + "行程")}</div>` +
    `<div class="psub">${p.days.length} 天 · ${total} 个安排 · 每个安排都可单独替换` +
    (tagline ? ` · ${esc(tagline)}` : "") + `</div>` +
    `<div class="ptags">${tags.map((t) => `<span class="ptag">${esc(t)}</span>`).join("")}</div>`;
}

function renderDayTabs() {
  els.dayTabs.innerHTML = "";
  state.plan.days.forEach((d) => {
    const b = document.createElement("button");
    b.className = "day-tab" + (d.day === state.currentDay ? " active" : "");
    const theme = state.dayThemes && state.dayThemes[String(d.day)];
    b.innerHTML = `<span class="dt-d">第 ${d.day} 天</span><span class="dt-c">${esc(d.cluster_zh)}</span>` +
      (theme ? `<span class="dt-theme" title="${esc(theme)}">${esc(theme)}</span>` : "");
    b.onclick = () => { state.currentDay = d.day; state.selected = null; state.chooser = null; renderDayTabs(); renderDay(); renderMap(); };
    els.dayTabs.appendChild(b);
  });
}

function renderDay() {
  const d = state.plan.days.find((x) => x.day === state.currentDay);
  if (!d) return;
  const gwBits = [];
  if (d.gateway_in) gwBits.push(`<span class="gw">✈ 抵达 ${esc(d.gateway_in)}</span>`);
  if (d.gateway_out) gwBits.push(`<span class="gw">✈ 返程 ${esc(d.gateway_out)}</span>`);
  els.daySummary.innerHTML = esc(d.summary || "") + (gwBits.length ? "<br>" + gwBits.join(" · ") : "");

  els.timeline.innerHTML = "";
  d.items.forEach((item, idx) => {
    const cat = catOf(item);
    // 门户/休息窗为结构性槽位：不可替换、不可锁定
    const structural = item.type === "gateway" || item.type === "rest";
    const el = document.createElement("div");
    el.className = "tl-item" + (item.pinned ? " pinned" : "") + (state.selected && state.selected.idx === idx ? " selected" : "");
    el.dataset.slotKey = slotKey(d.day, idx);
    el.innerHTML =
      `<div class="tl-time">${esc(item.start || "--:--")}</div>` +
      `<div class="tl-head">` +
        `<span class="tl-cat-dot" style="background:${cat.color};color:${cat.color}"></span>` +
        `<span class="tl-name" title="${esc(item.name_zh)}">${esc(item.name_zh)}</span>` +
        (item.pinned ? `<span class="tl-pinned-badge">已锁定</span>` : "") +
        (structural ? "" : `<button class="tl-pin" title="${item.pinned ? "取消锁定" : "锁定这个安排（重解不触碰）"}">${item.pinned ? "📌" : "📍"}</button>`) +
      `</div>` +
      // L1 顾问叙事（在事实收据上方，斜体分层；无叙事不渲染该行）
      (state.narratives[item.atom_id]
        ? `<div class="tl-advisor"><span class="adv-icon">💬</span><span>${esc(state.narratives[item.atom_id])}</span></div>`
        : "") +
      `<div class="tl-receipt"><span class="rc-icon">${esc(item.receipt_icon || "✦")}</span><span>${esc(item.receipt)}</span></div>` +
      `<div class="tl-actions">` +
        (structural ? "" : `<button class="tl-swap">换一个</button>`) +
        `<span class="tl-dur">${SLOT_ZH[item.slot] || ""} · 约 ${item.duration_min} 分钟</span>` +
      `</div>`;

    const pinBtn = el.querySelector(".tl-pin");
    if (pinBtn) pinBtn.onclick = (e) => { e.stopPropagation(); togglePin(d.day, idx); };
    const swapBtn = el.querySelector(".tl-swap");
    if (swapBtn) swapBtn.onclick = (e) => { e.stopPropagation(); doSwap(d.day, idx, e.currentTarget); };
    el.onclick = () => selectItem(d.day, idx, true);
    // 三候选选择器（展开在该槽位下方）
    if (state.chooser && state.chooser.day === d.day && state.chooser.idx === idx) {
      el.classList.add("choosing");
      el.appendChild(buildChooserEl(d.day, idx));
    }
    els.timeline.appendChild(el);
  });
}

function togglePin(day, idx) {
  const d = state.plan.days.find((x) => x.day === day);
  const item = d.items[idx];
  item.pinned = !item.pinned;
  item.pinnedByUser = item.pinned; // 只有用户手动锁定才阻止再次「换一个」
  renderDay();
}

function selectItem(day, idx, fly) {
  state.selected = { day, idx };
  document.querySelectorAll(".tl-item").forEach((el) => el.classList.toggle("selected", el.dataset.slotKey === slotKey(day, idx)));
  const d = state.plan.days.find((x) => x.day === day);
  const item = d && d.items[idx];
  if (item && state.map && fly && item.lat != null) {
    state.map.flyTo([item.lat, item.lon], 15, { duration: 0.8 });
    const m = state.markers.get(slotKey(day, idx));
    if (m && m.openPopup) setTimeout(() => m.openPopup(), 850);
  }
}

/* ---------- 地图 ---------- */
function initMap() {
  if (state.map || typeof L === "undefined") {
    if (typeof L === "undefined" && !document.querySelector(".map-fallback")) {
      const fb = document.createElement("div");
      fb.className = "map-fallback";
      fb.textContent = "地图组件未加载（离线环境），左侧时间轴功能不受影响";
      $("map-pane").appendChild(fb);
    }
    return;
  }
  const first = state.plan.days[0] && state.plan.days[0].items.find((i) => i.lat != null);
  state.map = L.map("map", { zoomControl: true, attributionControl: true });
  // 免 key 底图：标准 OSM 瓦片（CARTO dark_all 现在要求 API key）
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
  state.markerLayer = L.layerGroup().addTo(state.map);
  state.routeLayer = L.layerGroup().addTo(state.map);
  if (first && first.lat != null) state.map.setView([first.lat, first.lon], 12);
  else state.map.setView([34.6937, 135.5023], 11);
}

function markerIcon(item, dimmed, current) {
  const c = catOf(item).color;
  return L.divIcon({
    className: "",
    html: `<div class="atom-marker ${dimmed ? "dim" : ""} ${current ? "cur" : ""}" style="width:14px;height:14px;background:${c};color:${c}"></div>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
    popupAnchor: [0, -10],
  });
}

function renderMap() {
  if (!state.map || state.exploreOn) return;
  state.markerLayer.clearLayers();
  state.routeLayer.clearLayers();
  state.markers.clear();

  const bounds = [];
  state.plan.days.forEach((d) => {
    const isCur = d.day === state.currentDay;
    const pts = [];
    d.items.forEach((item, idx) => {
      if (item.lat == null || item.lon == null) return;
      const ll = [item.lat, item.lon];
      const m = L.marker(ll, { icon: markerIcon(item, !isCur, isCur) });
      m.bindPopup(
        `<div class="pop-title">${esc(item.name_zh)}</div>` +
        `<div class="pop-rc">${esc(item.receipt_icon || "✦")} ${esc(item.receipt)}</div>`
      );
      m.on("click", () => {
        if (d.day !== state.currentDay) { state.currentDay = d.day; renderDayTabs(); renderDay(); }
        selectItem(d.day, idx, false);
        const el = document.querySelector(`[data-slot-key="${slotKey(d.day, idx)}"]`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      m.addTo(state.markerLayer);
      state.markers.set(slotKey(d.day, idx), m);
      pts.push(ll);
      if (isCur) bounds.push(ll);
    });
    if (isCur && pts.length >= 2) {
      // 浅色底图上的高对比动线：靛蓝实线 + 深色描边
      L.polyline(pts, { color: "#1e293b", weight: 6, opacity: 0.35, lineCap: "round" }).addTo(state.routeLayer);
      L.polyline(pts, { color: "#4f46e5", weight: 3, opacity: 0.9, dashArray: "1 8", lineCap: "round" }).addTo(state.routeLayer);
    }
  });

  if (bounds.length) {
    state.map.flyToBounds(L.latLngBounds(bounds).pad(0.18), { duration: 0.7 });
  }
}

function renderLegend() {
  if (state.exploreOn) return; // 探索模式图例由 renderExploreLegend 管理
  els.mapLegend.classList.remove("interactive");
  const used = new Set();
  state.plan.days.forEach((d) => d.items.forEach((i) => used.add(i.category)));
  els.mapLegend.innerHTML = [...used].map((c) => {
    const m = catMetaOf(c);
    return `<div class="lg-row"><span class="lg-dot" style="background:${m.color}"></span>${m.zh}</div>`;
  }).join("");
}

/* ================= V1.1 P1：探索模式 =================
 * 全部原子上图（L.canvas + circleMarker，449 点无 DOM marker 压力），
 * 分类着色 + 图例开关，点位详情卡 + [想要] → explicit_pois。 */

async function loadAtoms(dest) {
  if (state.atomCache[dest]) return state.atomCache[dest];
  let atoms;
  if (state.mode === "mock") {
    atoms = MOCK.getAtoms(dest);
  } else {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    try {
      const res = await fetch(`/api/atoms?dest=${encodeURIComponent(dest)}`, { signal: ctrl.signal });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      atoms = data.atoms || [];
    } finally { clearTimeout(t); }
  }
  state.atomCache[dest] = atoms;
  return atoms;
}

async function enterExplore() {
  state.exploreOn = true;
  els.atomCard.classList.add("hidden");
  const dest = (state.plan && state.plan.destination) || "osaka";
  let atoms;
  try {
    atoms = await loadAtoms(dest);
  } catch {
    showBanner("原子库加载失败，稍后再试");
    setMapMode("plan");
    return;
  }
  if (!state.exploreOn) return; // 加载期间用户已切回行程
  if (!state.map) { initMap(); }
  state.markerLayer.clearLayers();
  state.routeLayer.clearLayers();
  state.markers.clear();
  if (state.exploreLayer) { state.map.removeLayer(state.exploreLayer); state.exploreLayer = null; }

  // canvas 渲染器：449 个 circleMarker 一次绘制，不走 DOM
  const renderer = L.canvas({ padding: 0.4 });
  const layer = L.layerGroup();
  const byCat = {};
  const bounds = [];
  atoms.forEach((a) => {
    const sp = a.spatial || {};
    if (!Number.isFinite(+sp.lat) || !Number.isFinite(+sp.lon)) return;
    const cat = (a.categories && a.categories[0]) || "sightseeing";
    const m = catMetaOf(cat);
    const wanted = state.wanted.has(a.id);
    const cm = L.circleMarker([+sp.lat, +sp.lon], {
      renderer, radius: wanted ? 8 : 5.5,
      color: wanted ? "#fbbf24" : "rgba(255,255,255,0.85)", weight: wanted ? 2.5 : 1,
      fillColor: m.color, fillOpacity: wanted ? 1 : 0.85,
    });
    cm.on("click", () => openAtomCard(a));
    cm._atom = a;
    cm._cat = cat;
    cm.addTo(layer);
    (byCat[cat] = byCat[cat] || []).push(cm);
    if (!state.hiddenCats.has(cat)) bounds.push([+sp.lat, +sp.lon]);
  });
  // 图例关掉的分类：初始即隐藏
  state.hiddenCats.forEach((cat) => (byCat[cat] || []).forEach((cm) => { if (cm.remove) cm.remove(); }));
  layer.addTo(state.map);
  state.exploreLayer = layer;
  state.exploreMarkers = byCat;
  renderExploreLegend(byCat);
  if (bounds.length) state.map.flyToBounds(L.latLngBounds(bounds).pad(0.1), { duration: 0.6 });
}

function exitExplore() {
  state.exploreOn = false;
  els.atomCard.classList.add("hidden");
  if (state.map && state.exploreLayer) {
    state.map.removeLayer(state.exploreLayer);
    state.exploreLayer = null;
    state.exploreMarkers = null;
  }
  renderMap();
  renderLegend();
}

function setMapMode(mode) {
  document.querySelectorAll(".mm-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  if (mode === "explore") enterExplore();
  else exitExplore();
}

function renderExploreLegend(byCat) {
  const cats = Object.keys(byCat).sort((a, b) => byCat[b].length - byCat[a].length);
  els.mapLegend.classList.add("interactive");
  els.mapLegend.innerHTML =
    `<div class="lg-hint">点击分类显隐</div>` +
    cats.map((c) => {
      const m = catMetaOf(c);
      const off = state.hiddenCats.has(c);
      return `<div class="lg-row${off ? " off" : ""}" data-cat="${esc(c)}">` +
        `<span class="lg-dot" style="background:${m.color}"></span>${m.zh} · ${byCat[c].length}</div>`;
    }).join("");
  els.mapLegend.querySelectorAll(".lg-row").forEach((row) => {
    row.onclick = () => toggleExploreCat(row.dataset.cat, row);
  });
}

function toggleExploreCat(cat, row) {
  const markers = (state.exploreMarkers || {})[cat] || [];
  if (state.hiddenCats.has(cat)) {
    state.hiddenCats.delete(cat);
    markers.forEach((cm) => cm.addTo(state.exploreLayer));
    row && row.classList.remove("off");
  } else {
    state.hiddenCats.add(cat);
    markers.forEach((cm) => state.exploreLayer.removeLayer(cm));
    row && row.classList.add("off");
  }
}

const ENERGY_ZH = (n) => (n != null ? `体力 ${"●".repeat(Math.min(n, 5))}${"○".repeat(Math.max(5 - n, 0))}` : "");
const BEST_TIME_ZH = { morning: "上午最佳", afternoon: "下午最佳", sunset: "傍晚最佳", evening: "晚上最佳", night: "夜间最佳", noon: "中午最佳" };

function openAtomCard(a) {
  const cat = (a.categories && a.categories[0]) || "sightseeing";
  const m = catMetaOf(cat);
  const media = a.media && a.media.image_url ? a.media : null;
  const c = a.constraints || {};
  const bt = (a.time_structure && a.time_structure.best_time_of_day) || [];
  const wanted = state.wanted.has(a.id);
  els.atomCard.innerHTML =
    (media
      ? `<div class="ac-media"><img src="${esc(media.image_url)}" alt="${esc(nameOf({ name: a.name }))}" loading="lazy" onerror="this.parentNode.outerHTML=window.__acPh('${esc(m.zh)}','${m.color}')">` +
        `<span class="ac-license">${esc(media.image_source || "")}${media.image_license ? " · " + esc(media.image_license) : ""}</span></div>`
      : acPlaceholder(m)) +
    `<div class="ac-body">` +
      `<div class="ac-name">${esc((a.name && a.name.zh) || "未命名")}</div>` +
      `<div class="ac-cat">${esc(m.zh)} · ${esc((a.spatial && a.spatial.cluster) || "")}</div>` +
      (a.summary ? `<div class="ac-summary">${esc(a.summary)}</div>` : "") +
      // M1.1b：行家提醒（tips，最多 3 条，带来源标注；无 tips 不渲染该区块）
      (Array.isArray(a.tips) && a.tips.length
        ? `<div class="ac-tips"><div class="ac-tips-head">🧭 行家提醒</div>` +
          a.tips.slice(0, 3).map((t) =>
            `<div class="ac-tip"><span>${esc(t.tip_zh || "")}` +
            `<span class="ac-tip-src">${t.source && t.source.url ? `<a href="${esc(t.source.url)}" target="_blank" rel="noopener">来源：Wikivoyage ↗</a>` : "来源：Wikivoyage"}</span></span></div>`
          ).join("") + `</div>`
        : "") +
      `<div class="ac-meta">` +
        (c.budget_tier != null ? `<span class="ac-chip">预算档 ${c.budget_tier} · ${BUDGET_ZH[c.budget_tier] || ""}</span>` : "") +
        (c.energy_cost != null ? `<span class="ac-chip">${ENERGY_ZH(c.energy_cost)}</span>` : "") +
        bt.slice(0, 2).map((t) => `<span class="ac-chip">${BEST_TIME_ZH[t] || esc(t)}</span>`).join("") +
      `</div>` +
      `<div class="ac-actions">` +
        `<button class="ac-want${wanted ? " wanted" : ""}">${wanted ? "✓ 已加入行程意愿" : "＋ 想要"}</button>` +
        `<button class="ac-close" aria-label="关闭">✕</button>` +
      `</div>` +
    `</div>`;
  els.atomCard.classList.remove("hidden");
  els.atomCard.querySelector(".ac-want").onclick = () => toggleWant(a);
  els.atomCard.querySelector(".ac-close").onclick = () => els.atomCard.classList.add("hidden");
}

/* 无图占位：类别色渐变 + 类别名，不是灰盒子 */
function acPlaceholder(m) {
  return `<div class="ac-placeholder" style="background:linear-gradient(135deg,${m.color}33,${m.color}66),radial-gradient(circle at 70% 20%,${m.color}44,transparent 60%)">` +
    `<span style="border-bottom:3px solid ${m.color}">${esc(m.zh)}</span></div>`;
}
window.__acPh = (zh, color) => acPlaceholder({ zh, color });

function toggleWant(a) {
  if (state.wanted.has(a.id)) state.wanted.delete(a.id);
  else state.wanted.add(a.id);
  updateWantCount();
  openAtomCard(a); // 重渲染按钮态
  // 同步该点位 marker 的金圈
  const markers = (state.exploreMarkers || {})[(a.categories && a.categories[0]) || "sightseeing"] || [];
  markers.forEach((cm) => {
    if (cm._atom && cm._atom.id === a.id) {
      const wanted = state.wanted.has(a.id);
      cm.setStyle({ radius: wanted ? 8 : 5.5, color: wanted ? "#fbbf24" : "rgba(255,255,255,0.85)", weight: wanted ? 2.5 : 1, fillOpacity: wanted ? 1 : 0.85 });
    }
  });
  if (state.wanted.has(a.id)) showBanner(`已加入「${(a.name && a.name.zh) || ""}」——下次生成时会点名安排它`);
}

function updateWantCount() {
  const n = state.wanted.size;
  els.wantCount.textContent = n;
  els.wantCount.classList.toggle("hidden", n === 0);
}

/* ================= V1.1 P2：生成过程演出（点→筛→簇→线） =================
 * 每一帧来自 refine 响应的真实 generation_trace，不是纯动画。
 * 演出与 refine 并行：refine 先好就等演出，演出没播完用户可随时跳过。 */

const SHOW_DAY_COLORS = ["#22d3ee", "#a78bfa", "#f472b6", "#fbbf24", "#34d399", "#60a5fa"];
const showCtl = { running: false, skipped: false, done: null };

async function maybeRunShow(refinePromise, useMock, destKey) {
  // 复访（本会话已看过一次）直接跳过，回到原骨架流程
  if (sessionStorage.getItem("atom_show_seen") === "1") return false;
  let refineRes, atoms;
  try {
    [refineRes, atoms] = await Promise.all([
      refinePromise,
      loadAtoms(destKey).catch(() => null),
    ]);
  } catch { return false; }
  const trace = refineRes && refineRes.generation_trace;
  if (!trace || !trace.candidates || !trace.candidates.length) return false;
  sessionStorage.setItem("atom_show_seen", "1");
  stopTicker();
  // cluster id → 中文名（trace 不含 cluster_zh，从 plan.days 补）
  const clusterZh = {};
  ((refineRes.plan && refineRes.plan.days) || []).forEach((d) => {
    if (d.cluster && (d.cluster_zh || d.cluster)) clusterZh[d.cluster] = d.cluster_zh || d.cluster;
  });
  await runShow(trace, atoms || [], catDestZh(destKey), clusterZh);
  return true;
}

function setShowPhase(text, reason) {
  els.showPhase.classList.add("fade");
  setTimeout(() => { els.showPhase.textContent = text; els.showPhase.classList.remove("fade"); }, 200);
  els.showReason.textContent = reason || "";
}

/* 可中断的等待：跳过标志置位后立即 resolve */
function showSleep(ms) {
  return new Promise((res) => {
    const t0 = Date.now();
    (function tick() {
      if (showCtl.skipped || Date.now() - t0 >= ms) return res();
      setTimeout(tick, 60);
    })();
  });
}

/* 截图钩子：?showFreeze=N 时第 N 阶段播完后停住（长等待直到跳过） */
function showFreezePoint(phase) {
  if (qs.get("showFreeze") !== String(phase)) return Promise.resolve();
  document.title = "SHOWFREEZE " + phase;
  return showSleep(3600000);
}

async function runShow(trace, atoms, destZh, clusterZh) {
  showCtl.running = true;
  showCtl.skipped = false;
  els.showSkip.onclick = () => { showCtl.skipped = true; };
  showScreen("show");

  const map = L.map("show-map", { zoomControl: false, attributionControl: false, zoomSnap: 0.5 });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(map);
  state.showMap = map;
  const renderer = L.canvas({ padding: 0.4 });
  const layer = L.layerGroup().addTo(map);
  const lineLayer = L.layerGroup().addTo(map);

  // atom_id -> 类别色（探索数据补充 candidates 里缺的坐标/类别）
  const atomById = {};
  atoms.forEach((a) => { atomById[a.id] = a; });
  const colorOf = (id) => {
    const a = atomById[id];
    return catMetaOf(a && a.categories && a.categories[0]).color;
  };

  // ① 全点撒出（trace.candidates 全量，真实数据）
  setShowPhase(`${destZh} · ${trace.total_atoms || trace.candidates.length} 个体验原子，全部铺开`);
  const cands = trace.candidates.filter((c) => Number.isFinite(+c.lat) && Number.isFinite(+c.lon));
  const dotById = {};
  const bounds = [];
  // 分批撒点（每批 ~60 个，间隔 90ms，纯节奏；点的集合本身是真实全量）
  const BATCH = 60;
  for (let i = 0; i < cands.length && !showCtl.skipped; i += BATCH) {
    cands.slice(i, i + BATCH).forEach((c) => {
      const cm = L.circleMarker([+c.lat, +c.lon], {
        renderer, radius: 5, color: "rgba(255,255,255,0.7)", weight: 1,
        fillColor: colorOf(c.atom_id), fillOpacity: 0.9,
      });
      cm._id = c.atom_id;
      cm.addTo(layer);
      dotById[c.atom_id] = cm;
      bounds.push([+c.lat, +c.lon]);
    });
    if (i === 0 && bounds.length) map.fitBounds(L.latLngBounds(bounds).pad(0.12));
    await showSleep(90);
  }
  if (bounds.length) map.fitBounds(L.latLngBounds(bounds).pad(0.12), { animate: false });
  await showSleep(1200);
  await showFreezePoint(1);

  // ② 按 trace 逐批熄灭被排除点（kept=false，配真实 reason_zh 字幕）
  const excluded = trace.candidates.filter((c) => c.kept === false);
  if (excluded.length) {
    const byReason = {};
    excluded.forEach((c) => { (byReason[c.reason_zh || "排除：不满足本次条件"] = byReason[c.reason_zh || "排除：不满足本次条件"] || []).push(c); });
    const reasons = Object.keys(byReason);
    for (const r of reasons) {
      if (showCtl.skipped) break;
      const group = byReason[r];
      setShowPhase("按你的条件筛选…", `${r}（${group.length} 个${trace.excluded_sampled ? "，抽样显示" : ""}）`);
      // 分批熄灭
      for (let i = 0; i < group.length; i += 25) {
        group.slice(i, i + 25).forEach((c) => {
          const cm = dotById[c.atom_id];
          if (cm) cm.setStyle({ fillOpacity: 0.06, opacity: 0.08, radius: 3.5 });
        });
        await showSleep(70);
      }
      await showSleep(650);
    }
  } else {
    setShowPhase("按你的条件筛选…", "本次没有触发硬排除，全部候选进入打分");
    await showSleep(1400);
  }
  await showFreezePoint(2);

  // ③ clusters 逐簇高亮 + 圈出（只展示最终入选天使用的簇，其余簇保持微亮）
  const assignments = trace.day_assignments || [];
  const usedClusters = new Set(assignments.map((d) => d.cluster));
  const clusterByName = {};
  (trace.clusters || []).forEach((cl) => { clusterByName[cl.cluster] = cl; });
  // 先把非入选簇压暗
  (trace.clusters || []).forEach((cl) => {
    if (usedClusters.has(cl.cluster)) return;
    (cl.atom_ids || []).forEach((id) => {
      const cm = dotById[id];
      if (cm) cm.setStyle({ fillOpacity: 0.15, opacity: 0.2, radius: 4 });
    });
  });
  let ci = 0;
  for (const d of assignments) {
    if (showCtl.skipped) break;
    const cl = clusterByName[d.cluster];
    if (!cl) continue;
    const color = SHOW_DAY_COLORS[ci % SHOW_DAY_COLORS.length];
    setShowPhase(`聚簇：第 ${d.day} 天落在「${(clusterZh && clusterZh[d.cluster]) || d.cluster}」片区`, `${(cl.atom_ids || []).length} 个候选在步行可达范围内`);
    (cl.atom_ids || []).forEach((id) => {
      const cm = dotById[id];
      if (cm) cm.setStyle({ fillOpacity: 0.95, opacity: 1, radius: 6, color, weight: 1.5 });
    });
    if (cl.centroid && Number.isFinite(+cl.centroid.lat)) {
      L.circle([+cl.centroid.lat, +cl.centroid.lon], {
        radius: 1400, color, weight: 2, opacity: 0.8, fillColor: color, fillOpacity: 0.06, dashArray: "6 6",
      }).addTo(lineLayer);
    }
    ci++;
    await showSleep(1300);
  }
  await showFreezePoint(3);

  // ④ day_assignments 逐日连线（每天一色；只连真实排入的原子）
  ci = 0;
  for (const d of assignments) {
    if (showCtl.skipped) break;
    const color = SHOW_DAY_COLORS[ci % SHOW_DAY_COLORS.length];
    const pts = (d.atom_ids || [])
      .map((id) => {
        const c = cands.find((x) => x.atom_id === id) || (atomById[id] && atomById[id].spatial) || null;
        return c && Number.isFinite(+c.lat) ? [+c.lat, +c.lon] : null;
      })
      .filter(Boolean);
    setShowPhase(`第 ${d.day} 天动线连成`, `${pts.length} 个安排`);
    if (pts.length >= 2) {
      const pl = L.polyline(pts, { color, weight: 3.5, opacity: 0, lineCap: "round" }).addTo(lineLayer);
      // 线条淡入
      for (let op = 0.2; op <= 0.95 && !showCtl.skipped; op += 0.25) {
        pl.setStyle({ opacity: op });
        await showSleep(120);
      }
    }
    pts.forEach((_, k) => {
      const cm = dotById[d.atom_ids[k]];
      if (cm) cm.setStyle({ radius: 8, weight: 2.5, color: "#fff", fillOpacity: 1 });
    });
    ci++;
    await showSleep(1000);
  }
  await showFreezePoint(4);

  // ⑤ 收场
  setShowPhase("行程织好了，正在展开…", "");
  await showSleep(1100);
}

async function finishShow() {
  // 演出自然播完或被跳过：淡出演出层
  els.showScreen.style.transition = "opacity .5s";
  els.showScreen.style.opacity = "0";
  await sleep(500);
  cancelShow();
}

function cancelShow() {
  showCtl.running = false;
  showCtl.skipped = false;
  if (state.showMap) {
    try { state.showMap.remove(); } catch { /* noop */ }
    state.showMap = null;
  }
  els.showScreen.style.opacity = "";
  els.showScreen.classList.add("hidden");
}

/* ================= V1.1 P3：L1 顾问叙事层 =================
 * 纪律：叙事加在事实收据上方，不替换收据；llm_used:false / 失败 → 静默保持模板收据。 */

async function fetchNarratives(useMock) {
  const planId = state.planId;
  try {
    const res = useMock
      ? await MOCK.postNarrate(planId)
      : await apiPost(`/api/plan/${planId}/narrate`, {}, 25000);
    if (state.planId !== planId || !state.plan) return; // 用户已 restart
    if (!res || res.llm_used === false) {
      // 降级是常态不是错误：仅 conflict_note（确定性模板，LLM OFF 也有）可展示
      if (res && res.conflict_note_zh) showAdvisorBanner(res.conflict_note_zh);
      return;
    }
    state.narratives = res.narratives || {};
    state.dayThemes = res.day_themes || {};
    if (res.conflict_note_zh) showAdvisorBanner(res.conflict_note_zh);
    renderDayTabs();
    renderDay();
  } catch { /* 静默降级：叙事是附加层 */ }
}

function showAdvisorBanner(text) {
  els.advisorBannerText.textContent = text;
  els.advisorBanner.classList.remove("hidden");
}

/* ================= V1.1 P4：顾问对话（自然语言改行程） ================= */

function chatScroll() { els.chatLog.scrollTop = els.chatLog.scrollHeight; }

function addChatMsg(role, html) {
  els.chatLog.classList.remove("hidden");
  const el = document.createElement("div");
  el.className = "chat-msg " + role;
  if (role === "advisor") el.innerHTML = `<span class="cm-tag">顾问</span>` + html;
  else el.innerHTML = html;
  els.chatLog.appendChild(el);
  chatScroll();
  return el;
}

async function sendChat(text, confirmed) {
  if (state.chatBusy || !state.planId) return;
  state.chatBusy = true;
  els.chatSend.disabled = true;
  const seq = ++state.chatSeq;
  if (!confirmed) addChatMsg("user", esc(text));
  const loading = addChatMsg("advisor loading", `<span class="ticker-dot"></span>在想怎么调…`);
  try {
    const body = { text };
    if (confirmed) body.confirmed = true;
    const res = state.mode === "mock"
      ? await MOCK.postChat(state.planId, text, confirmed)
      : await apiPost(`/api/plan/${state.planId}/chat`, body, 30000);
    loading.remove();
    if (state.chatSeq !== seq) return; // 过期响应丢弃
    if (!res || res.error) {
      addChatMsg("advisor", esc((res && res.error && res.error.message_zh) || "这次没调成，换个说法试试？"));
      return;
    }
    if (res.llm_used === false) {
      addChatMsg("advisor", esc(res.fallback_zh || "顾问模式暂时不可用，可以用「换一个」手动调整"));
      // 高亮现有「换一个」入口引导
      document.querySelectorAll(".tl-swap").forEach((b) => b.classList.add("glow"));
      setTimeout(() => document.querySelectorAll(".tl-swap").forEach((b) => b.classList.remove("glow")), 4000);
      return;
    }
    if (res.applied) {
      addChatMsg("advisor",
        esc(res.understood_zh || "明白") +
        (res.tradeoff_zh ? `<div class="cm-tradeoff">⇄ ${esc(res.tradeoff_zh)}</div>` : "") +
        (res.soft_note_zh ? `<div class="cm-tradeoff">${esc(res.soft_note_zh)}</div>` : ""));
      // diff 合同与 swap 一致：复用 applySwapResult（高亮 + banner + pin 权威）
      if (res.plan) applySwapResult({ plan: res.plan, diff: res.diff || { changed: [] } }, null);
      // 方案变了，旧叙事可能失效：后台重新拉叙事
      fetchNarratives(state.mode === "mock");
      return;
    }
    if (res.advisor_only) {
      // 顾问式回答（ADR-002 v2）：超出自动执行范围 → 承认 + 建议，不瞎改方案
      addChatMsg("advisor",
        (res.understood_zh ? esc(res.understood_zh) : "") +
        `<div class="cm-tradeoff">${esc(res.advisor_note_zh || "这个调整超出了我现在能自动执行的范围")}</div>`);
      return;
    }
    if (res.needs_confirm) {
      const bubble = addChatMsg("advisor",
        esc(res.understood_zh || "") +
        `<div class="cm-tradeoff">${esc(res.confirm_zh || "确认这样调整吗？")}</div>`);
      const row = document.createElement("div");
      row.className = "chat-confirm-row";
      const ok = document.createElement("button");
      ok.className = "cc-ok"; ok.textContent = "确认调整";
      const no = document.createElement("button");
      no.className = "cc-no"; no.textContent = "算了";
      ok.onclick = () => { row.remove(); sendChat(text, true); };
      no.onclick = () => { row.remove(); addChatMsg("advisor", "好，那保持现在的安排。"); };
      row.appendChild(ok); row.appendChild(no);
      bubble.appendChild(row);
      chatScroll();
      return;
    }
    // M1.2：advisor_only（applied:false + advisor_note_zh）——顾问只给建议不改方案，
    // 作为正常顾问气泡展示，不落进「没理解」的错误口吻
    if (res.advisor_note_zh) {
      addChatMsg("advisor", esc(res.advisor_note_zh));
      return;
    }
    addChatMsg("advisor", esc(res.confirm_zh || res.understood_zh || "我没完全理解，换个说法我马上改"));
  } catch (err) {
    loading.remove();
    addChatMsg("advisor", "网络开小差了，稍后再试（也可以直接用「换一个」）");
  } finally {
    state.chatBusy = false;
    els.chatSend.disabled = false;
  }
}

/* state.chooser = {day, idx, loading, cards} | null —— 同一时间只允许一个选择器 */

/* ================= 换一个（三候选选择器 + 局部再生） ================= */

const BUDGET_ZH = { 1: "省钱", 2: "平价", 3: "舒适", 4: "轻奢" };
const CAT_ZH_EXTRA = { onsen_spa: "温泉", transport_experience: "交通体验", day_trip: "近郊" };
function catsZh(cats) {
  return (cats || []).map((c) => catMetaOf(c).zh).join(" / ");
}

async function doSwap(day, idx, btn) {
  const d = state.plan.days.find((x) => x.day === day);
  const item = d.items[idx];
  // pin（含后端换后自动 pin）= 锁定：先取消锁定再替换
  if (item.pinned || item.pinnedByUser) { showBanner("「" + item.name_zh + "」已锁定，先点 📌 取消锁定再替换"); return; }

  // 1) 先取三候选预览（只读，不动 plan）；失败/无候选 → 退回单发替换
  const token = ++state.chooserSeq;
  state.chooser = { day, idx, token, loading: true, confirming: false, cards: null };
  renderDay();
  scrollSlotIntoView(day, idx);
  let cards = null;
  try {
    const prev = state.mode === "mock"
      ? await MOCK.postSwapPreview(state.planId, day, idx)
      : await apiPost(`/api/plan/${state.planId}/swap`, { day, slot_index: idx, candidates: 3 }, 8000);
    if (prev && !prev.error && Array.isArray(prev.candidates) && prev.candidates.length) {
      cards = prev.candidates;
    }
  } catch { /* 预览失败走兜底 */ }

  // 竞态守卫：响应回来时选择器已被取消/被新请求顶替 → 丢弃过期结果
  if (!state.chooser || state.chooser.token !== token) return;
  if (!cards) {
    state.chooser = null;
    renderDay();
    await legacySwap(day, idx, btn);
    return;
  }
  state.chooser.loading = false;
  state.chooser.cards = cards;
  renderDay();
  scrollSlotIntoView(day, idx);
}

function scrollSlotIntoView(day, idx) {
  const el = document.querySelector(`[data-slot-key="${slotKey(day, idx)}"]`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeChooser() {
  if (!state.chooser) return;
  state.chooser = null;
  renderDay();
}

/* 渲染在某个行程项内部的选择器面板（loading / 三张候选卡 / 确认中） */
function buildChooserEl(day, idx) {
  const ch = state.chooser;
  const box = document.createElement("div");
  box.className = "chooser";
  box.onclick = (e) => e.stopPropagation();
  if (ch.loading) {
    box.innerHTML = `<div class="chooser-loading"><span class="ticker-dot"></span>正在同片区找可行候选…</div>`;
    return box;
  }
  const head = document.createElement("div");
  head.className = "chooser-head";
  head.innerHTML = `<span class="chooser-title">换哪个？<em>标「首选」的是求解器综合分最高的可行项</em></span>`;
  const x = document.createElement("button");
  x.className = "chooser-close";
  x.textContent = "✕";
  x.title = "取消，保持原安排";
  x.disabled = !!ch.confirming;
  x.onclick = (e) => { e.stopPropagation(); closeChooser(); };
  head.appendChild(x);
  box.appendChild(head);

  const list = document.createElement("div");
  list.className = "chooser-cards";
  ch.cards.forEach((c, i) => {
    const a = c.atom || {};
    const card = document.createElement("button");
    card.className = "cand-card";
    const tier = a.budget_tier ? `预算档 ${a.budget_tier} · ${BUDGET_ZH[a.budget_tier] || ""}` : "";
    const energy = a.energy_cost != null ? `体力 ${"●".repeat(Math.min(a.energy_cost, 5))}${"○".repeat(Math.max(5 - a.energy_cost, 0))}` : "";
    card.innerHTML =
      `<div class="cc-top">` +
        (i === 0 ? `<span class="cc-first">首选</span>` : "") +
        `<span class="cc-name">${esc(a.name_zh || "未命名")}</span>` +
      `</div>` +
      `<div class="cc-meta">${esc(catsZh(a.categories))}${tier ? " · " + esc(tier) : ""}${energy ? " · " + energy : ""}</div>` +
      `<div class="cc-receipt">${esc(receiptIcon(c.receipt))} ${esc(receiptText(c.receipt))}</div>` +
      (c.tradeoff_zh ? `<div class="cc-tradeoff">⇄ ${esc(c.tradeoff_zh)}</div>` : "");
    // 确认中：卡片禁用，防重复点击
    card.disabled = !!ch.confirming;
    if (ch.confirming && ch.picking === a.id) card.classList.add("picking");
    card.onclick = (e) => { e.stopPropagation(); pickCandidate(day, idx, c); };
    list.appendChild(card);
  });
  box.appendChild(list);
  if (ch.confirming) {
    const ft = document.createElement("div");
    ft.className = "chooser-loading";
    ft.innerHTML = `<span class="ticker-dot"></span>正在局部重解，其余安排不动…`;
    box.appendChild(ft);
  }
  return box;
}

/* 用户选定一个候选 → 进入确认中状态 → 发真实替换，指定 target_atom_id 兑现所选 */
async function pickCandidate(day, idx, cand) {
  const wantId = cand && cand.atom && cand.atom.id;
  if (state.chooser && state.chooser.day === day && state.chooser.idx === idx) {
    state.chooser.confirming = true;
    state.chooser.picking = wantId;
    renderDay();
    scrollSlotIntoView(day, idx);
  }
  await legacySwap(day, idx, null, wantId, nameOf(cand && cand.atom));
}

/* 当前 pin 的 atom_id 列表（随 swap 请求带给后端，保持 pin 权威一致） */
function currentPinnedIds() {
  const ids = [];
  if (state.plan) {
    state.plan.days.forEach((dd) => dd.items.forEach((it) => {
      if ((it.pinned || it.pinnedByUser) && it.atom_id) ids.push(it.atom_id);
    }));
  }
  return ids;
}

/* 单发替换（带 target_atom_id 时是三候选选定；不带时是预览失败的兜底） */
async function legacySwap(day, idx, btn, wantId, wantName) {
  if (btn) { btn.disabled = true; btn.textContent = "重解中…"; }
  try {
    const body = { day, slot_index: idx, pinned: currentPinnedIds() };
    if (wantId) body.target_atom_id = wantId;
    const res = state.mode === "mock"
      ? await MOCK.postSwap(state.planId, day, idx, wantId)
      : await apiPost(`/api/plan/${state.planId}/swap`, body, 8000);

    if (res.error) { state.chooser = null; renderDay(); showBanner(res.error.message_zh || "暂时换不了，稍后再试"); return; }

    if (res.requires_confirmation) {
      state.chooser = null;
      renderDay();
      // 锚点确认是整体方案：校验是否兑现了用户点选的候选
      if (wantId) {
        const proposed = res.proposed || res;
        const pnew = proposed.diff && proposed.diff.changed && proposed.diff.changed[0] && proposed.diff.changed[0].new;
        if (!pnew || (pnew.atom_id && pnew.atom_id !== wantId)) {
          showBanner(`锚点调整是整体方案，无法单独兑现「${wantName}」，请在确认框中查看实际改动`);
        }
      }
      showConfirm(res, day);
      return;
    }

    state.chooser = null;
    const appliedId = res.diff && res.diff.changed && res.diff.changed[0]
      && res.diff.changed[0].new && res.diff.changed[0].new.atom_id;
    applySwapResult(res, day);
    // 防御性校验：指定候选与实际落位应一致，不一致说明后端未兑现
    if (wantId && appliedId && appliedId !== wantId) {
      const appliedName = nameOf(res.diff.changed[0].new);
      showBanner(`你选的是「${wantName}」，实际落了「${appliedName}」，请重试`);
    }
  } catch (err) {
    state.chooser = null;
    renderDay();
    showBanner("替换失败：" + (err && err.message ? err.message : "网络异常"));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "换一个"; }
  }
}

function applySwapResult(res, focusDay) {
  // pin 权威：以后端返回为准；前端只补记"用户手动 pin"（后端尚不持有该状态，
  // 已通过 swap 请求体 pinned 列表上送，供后端对齐）
  const prevUserPins = new Set();
  if (state.plan) {
    state.plan.days.forEach((dd) => dd.items.forEach((it) => {
      if (it.pinnedByUser && it.atom_id) prevUserPins.add(it.atom_id);
    }));
  }
  state.plan = normPlan(res.plan);
  state.plan.days.forEach((dd) => dd.items.forEach((it) => {
    if (prevUserPins.has(it.atom_id)) { it.pinned = true; it.pinnedByUser = true; }
  }));
  const diff = res.diff || { changed: [], unchanged_count: 0 };
  if (diff.changed && diff.changed.length) {
    const c0 = diff.changed[0];
    showBanner(`已替换「${nameOf(c0.old)}」→「${nameOf(c0.new)}」，其余 ${diff.unchanged_count} 个安排不变`);
    state.currentDay = focusDay || c0.day;
  } else {
    showBanner("已更新方案");
  }
  state.selected = null;
  renderDayTabs();
  renderDay();
  renderMap();
  renderLegend();
  // 高亮变化的槽位：优先 slot_index；缺省时把时段名映射回当日 atom 槽位序号
  (diff.changed || []).forEach((c) => {
    if (c.day !== state.currentDay) return;
    const idx = c.slot_index != null ? c.slot_index : slotNameToIndex(c.day, c.slot);
    if (idx == null || idx < 0) return;
    const el = document.querySelector(`[data-slot-key="${slotKey(c.day, idx)}"]`);
    if (el) {
      el.classList.add("flash");
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => el.classList.remove("flash"), 1700);
    }
  });
}

/* 时段名 → 当日 atom 槽位序号（diff 缺 slot_index 时的兜底） */
function slotNameToIndex(day, slotName) {
  const dd = state.plan && state.plan.days.find((x) => x.day === day);
  if (!dd || !slotName) return -1;
  return dd.items.findIndex((it) => it.slot === slotName && it.type === "atom");
}

function showBanner(text, sticky) {
  els.bannerText.textContent = text;
  els.banner.classList.remove("hidden");
  clearTimeout(showBanner._t);
  // 截图演示模式（任意 swapDemo）保持横幅可见；
  // sticky（如目的地兜底 note）不自动消失，由用户手动关闭
  if (!qs.get("swapDemo") && !sticky) {
    showBanner._t = setTimeout(() => els.banner.classList.add("hidden"), 6000);
  }
}

/* ---------- requires_confirmation 对话框 ---------- */
function showConfirm(res, focusDay) {
  const proposed = res.proposed || res;
  const diff = proposed.diff || { changed: [] };
  els.confirmLead.textContent = res.message_zh || "这个替换会牵动其他安排：";
  els.confirmList.innerHTML = diff.changed.map((c) =>
    `<li><span class="cl-day">第 ${c.day} 天</span><br>「${esc(nameOf(c.old))}」→「${esc(nameOf(c.new))}」<br><span class="cl-day">✦ ${esc(receiptText(c.receipt))}</span></li>`
  ).join("");
  state.pendingConfirm = { proposed, focusDay };
  els.overlay.classList.remove("hidden");
}

els.confirmOk.onclick = () => {
  els.overlay.classList.add("hidden");
  if (state.pendingConfirm) {
    const { proposed, focusDay } = state.pendingConfirm;
    applySwapResult({ plan: proposed.plan, diff: proposed.diff }, focusDay);
    state.pendingConfirm = null;
  }
};
els.confirmCancel.onclick = () => {
  els.overlay.classList.add("hidden");
  state.pendingConfirm = null;
  showBanner("已取消，原计划不变");
};

/* ================= 入口事件 ================= */
els.go.onclick = () => {
  const text = els.input.value.trim();
  if (!text) { els.input.focus(); return; }
  startJourney(text);
};
els.input.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") els.go.click();
});
els.chips.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  els.input.value = chip.dataset.text;
  els.input.focus();
});
els.restart.onclick = () => {
  state.plan = null; state.planId = null; state.selected = null; state.chooser = null; state.note = null;
  state.exploreOn = false; state.hiddenCats = new Set(); state.wanted = new Set();
  state.narratives = {}; state.dayThemes = {};
  els.chatLog.innerHTML = ""; els.chatLog.classList.add("hidden");
  els.advisorBanner.classList.add("hidden");
  updateWantCount();
  els.atomCard.classList.add("hidden");
  document.querySelectorAll(".mm-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === "plan"));
  showScreen("landing");
};
els.bannerClose.onclick = () => els.banner.classList.add("hidden");
els.advisorBannerClose.onclick = () => els.advisorBanner.classList.add("hidden");

/* V1.1 P4：顾问对话输入 */
els.chatSend.onclick = () => {
  const text = els.chatInput.value.trim();
  if (!text) return;
  els.chatInput.value = "";
  sendChat(text, false);
};
els.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") els.chatSend.click();
});

/* V1.1 P1：地图 行程/探索 分段切换 */
els.mapMode.querySelectorAll(".mm-btn").forEach((b) => {
  b.onclick = () => {
    if (b.dataset.mode === "explore" && !state.plan) return;
    setMapMode(b.dataset.mode);
  };
});

/* 窄屏视图切换：行程 / 地图（桌面端该控件不显示，状态无影响） */
document.querySelectorAll(".vt-btn").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".vt-btn").forEach((x) => x.classList.toggle("active", x === b));
    document.body.classList.toggle("plan-map", b.dataset.view === "map");
    if (b.dataset.view === "map" && state.plan) {
      // 懒初始化：首次切到地图才创建 Leaflet 实例
      if (!state.map) { initMap(); renderMap(); }
      else setTimeout(() => {
        state.map.invalidateSize();
        if (state.exploreOn && state.exploreLayer) {
          // 探索模式：容器从 0 尺寸恢复后重取视野
          const pts = [];
          state.exploreLayer.eachLayer((cm) => { if (cm.getLatLng) pts.push(cm.getLatLng()); });
          if (pts.length) state.map.flyToBounds(L.latLngBounds(pts).pad(0.1), { duration: 0.4 });
        } else renderMap();
      }, 80);
    }
  };
});

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 启动：显式 forceMock 立即显示演示徽标 ---------- */
if (qs.get("forceMock") === "1") setMode("mock");

/* ---------- 启动：首屏目的地动态化（chips / hero-foot 从 /api/destinations 渲染） ---------- */
initLanding();

/* ---------- 诊断：?diag=1 时把最宽元素写进 title 供 headless 排查 ---------- */
if (qs.get("diag") === "1") {
  window.addEventListener("load", () => setTimeout(() => {
    const vw = document.documentElement.clientWidth;
    let worst = null, worstW = 0;
    document.querySelectorAll("body *").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > worstW) { worstW = r.width; worst = el; }
    });
    const sw = document.documentElement.scrollWidth;
    document.title = worst
      ? `DIAG vw=${vw} sw=${sw} widest=${worst.tagName}.${worst.className}#${worst.id} w=${Math.round(worstW)} left=${Math.round(worst.getBoundingClientRect().left)}`
      : `DIAG vw=${vw} sw=${sw} none`;
  }, 800));
}

/* ---------- 截图/演示自动化钩子：?forceMock=1&auto=1&text=...&day=N ---------- */
/* noShow=1 跳过演出；showFreeze=N 演出播到第 N 阶段停住供截图；exploreDemo=1 生成后自动切探索模式 */
if (qs.get("noShow") === "1") sessionStorage.setItem("atom_show_seen", "1");
if (qs.get("auto") === "1") {
  const text = qs.get("text") || "清明想和女朋友去大阪，5天，拍照和美食为主，人均1万左右";
  els.input.value = text;
  window.addEventListener("load", () => {
    setTimeout(async () => {
      await startJourney(text);
      const day = parseInt(qs.get("day") || "0", 10);
      if (day && state.plan && state.plan.days.some((d) => d.day === day)) {
        state.currentDay = day;
        renderDayTabs(); renderDay(); renderMap();
      }
      if (qs.get("exploreDemo") === "1" && state.plan) {
        // 窄屏先切到地图 tab（探索切换在地图面板内）
        const mapTab = document.querySelector('.vt-btn[data-view="map"]');
        if (mapTab && window.innerWidth <= 900) mapTab.click();
        setMapMode("explore");
        // 等探索图层渲染后打开一张详情卡供截图
        setTimeout(() => {
          const markers = state.exploreMarkers || {};
          // 优先打开「带 tips 且有真实图片」的原子（UAT 详情卡断言依赖图片）；
          // 其次带 tips 的（M1.1b 行家提醒区块演示）；都没有就开第一个
          let pick = null, pickTips = null;
          Object.keys(markers).forEach((c) => markers[c].forEach((cm) => {
            const a = cm._atom;
            if (!a || !Array.isArray(a.tips) || !a.tips.length) return;
            if (!pickTips) pickTips = a;
            if (!pick && a.media && a.media.image_url) pick = a;
          }));
          if (!pick) pick = pickTips;
          if (!pick) {
            const cat = Object.keys(markers)[0];
            if (cat && markers[cat] && markers[cat][0]) pick = markers[cat][0]._atom;
          }
          if (pick) openAtomCard(pick);
        }, 1500);
      }
      if (qs.get("wantDemo") === "1" && state.plan) {
        // 探索模式里点一个「想要」，验证 explicit_pois 累积
        const mapTab = document.querySelector('.vt-btn[data-view="map"]');
        if (mapTab && window.innerWidth <= 900) mapTab.click();
        setMapMode("explore");
        setTimeout(() => {
          const markers = state.exploreMarkers || {};
          const cat = Object.keys(markers)[0];
          if (cat && markers[cat] && markers[cat][0]) toggleWant(markers[cat][0]._atom);
        }, 1500);
      }
      const chatDemo = qs.get("chatDemo");
      if (chatDemo && state.plan) {
        // 演示顾问对话：chatDemo=rain|tired|food|confirm，发送并停住供截图
        const DEMO_TEXT = { rain: "下雨了", tired: "今天太累了", food: "想吃蟹道乐", confirm: "不想去环球影城了", note: "留半天买手办" };
        setTimeout(() => sendChat(DEMO_TEXT[chatDemo] || chatDemo, false), 800);
      }
      const demo = qs.get("swapDemo");
      if (demo && state.plan) {        // 演示「换一个」：第 1 天第 2 槽（1:1）
        setTimeout(async () => {
          const btn = document.querySelector(`[data-slot-key="1:1"] .tl-swap`);
          if (!btn) return;
          if (demo === "chooser") {
            // 打开三候选选择器，停住供截图
            await doSwap(1, 1, btn);
          } else if (demo === "pick") {
            // 打开选择器后点第一个候选（真实 API 下与求解器首选一致）
            await doSwap(1, 1, btn);
            setTimeout(() => {
              const card = document.querySelector(`[data-slot-key="1:1"] .cand-card`);
              if (card) card.click();
            }, 700);
          } else {
            // demo === "1"：兼容旧钩子，打开选择器后点首选
            await doSwap(1, 1, btn);
            setTimeout(() => {
              const card = document.querySelector(`[data-slot-key="1:1"] .cand-card`);
              if (card) card.click();
            }, 700);
          }
        }, 900);
      }
      document.title = "READY · 原子旅行";
    }, 400);
  });
}
