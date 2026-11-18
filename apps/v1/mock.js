/* 原子旅行 V1 — 内置演示数据集（Mock Mode）
 * 用途：后端不可达时驱动完整流程，坐标取自 data/atoms/osaka.json 真实原子。
 * 收据遵循「真实性铁律」：只引用意图明示 / 时段 / 空间 / 节奏四类事实。
 */
"use strict";

const MOCK = (() => {

  // ---------- 意图解析（演示用启发式） ----------
  function intent(text, explicitPois) {
    const isBangkok = /曼谷|bangkok/i.test(text);
    const dest = isBangkok ? "bangkok" : "osaka";
    return {
      intent_vector: {
        schema_version: "0.1",
        raw_input: text,
        party: { composition: /女朋友|情侣/.test(text) ? ["couple"] : /爸妈|父母/.test(text) ? ["elderly_parents"] : ["solo"], size: 2, mobility_max_physical_level: 5 },
        time: { departure_date_range: null, duration_days: 5, season: "spring", flexible: true },
        budget: { total_cny: null, per_person_cny: 10000, tier_preference: 3, hard_cap: false },
        geography: { origin: null, destination_specified: dest, climate_wish: null, max_flight_hours: null },
        pace: { atoms_per_day: 3, early_start_ok: false, rest_afternoon: false },
        emotion_goals: ["photo", "foodie", "romantic"],
        hard_constraints: [],
        explicit_interests: /拍照/.test(text) ? ["拍照"] : [],
        explicit_exclusions: [],
        explicit_pois: Array.isArray(explicitPois) ? explicitPois.slice(0, 20) : [],
        uncertainty: [],
        confidence: 0.86,
        inferred: ["season", "pace.atoms_per_day"],
      },
      plan_id: "mock-" + Math.random().toString(36).slice(2, 8),
    };
  }
  // ---------- 骨架 ----------
  const SKELETON = {
    days: [
      { day: 1, cluster: "namba",        cluster_zh: "难波 · 道顿堀", gateway_in: "关西机场 KIX", summary: "落地后直奔南区，市场午餐 + 小巷漫步 + 霓虹夜拍" },
      { day: 2, cluster: "osaka_castle", cluster_zh: "大阪城 · 梅田", summary: "上午城池樱花，傍晚登空中庭园看日落夜景" },
      { day: 3, cluster: "bay_area",     cluster_zh: "湾区",          summary: "环球影城一整天，夜里渡轮去天保山收尾" },
      { day: 4, cluster: "tennoji",      cluster_zh: "天王寺 · 新世界", summary: "慢节奏：古寺、串炸老街、300 米高空日落" },
      { day: 5, cluster: "sumiyoshi",    cluster_zh: "住吉 · 心斋桥", gateway_out: "关西机场 KIX", summary: "轻量收尾：神社晨参 + 心斋桥补货，从容去机场" },
    ],
    eta_seconds: 4,
  };

  // ---------- 完整方案（15 个安排，坐标全部来自真实原子库） ----------
  function buildPlan() {
    return {
      plan_id: null, // 由调用方填
      destination: "osaka",
      destination_zh: "大阪",
      title: "大阪 · 5 天 4 晚 · 拍照美食向",
      days: [
        {
          day: 1, cluster: "namba", cluster_zh: "难波 · 道顿堀",
          gateway_in: "关西机场 KIX",
          summary: "落地后直奔南区，市场午餐 + 小巷漫步 + 霓虹夜拍",
          items: [
            {
              atom_id: "osaka.namba_3.kuromon_ichiba_market_2", name_zh: "黑门市场",
              category: "market_street", slot: "noon", start: "13:30", duration_min: 90,
              lat: 34.666443, lon: 135.507039, pinned: false,
              receipt: "你说想吃好：市场下午两点后旅行团散去，现烤海鲜正是时候",
              alts: [
                { atom_id: "osaka.osaka_castle_2.spot_3", name_zh: "空堀商店街", category: "market_street", lat: 34.673403, lon: 135.517461, receipt: "你点了换一个：同属老市场街，本地人居多少游客，烤物与章鱼烧老店集中" },
                { atom_id: "osaka.namba_2.harijyu", name_zh: "はり重（寿喜烧老铺）", category: "food", lat: 34.668593, lon: 135.500678, receipt: "换成正餐：道顿堀旁的寿喜烧老铺，榻榻米座席，适合落地第一顿" },
              ],
            },
            {
              atom_id: "osaka.namba_2.hozen_ji", name_zh: "法善寺横丁",
              category: "culture_history", slot: "afternoon", start: "15:30", duration_min: 60,
              lat: 34.667871, lon: 135.502603, pinned: false,
              receipt: "离黑门市场步行 7 分钟；石板小巷 + 水挂不动尊，饭后刚好慢逛消食",
              alts: [
                { atom_id: "osaka.namba.namba_yasaka_shrine", name_zh: "难波八坂神社", category: "culture_history", lat: 34.661392, lon: 135.496744, receipt: "换成狮子殿：12 米高狮子头张口造型，出片率和记忆点都拉满" },
                { atom_id: "osaka.umeda_3.osaka_museum_of_housing_and_living", name_zh: "大阪生活今昔馆", category: "art_museum", lat: 34.710466, lon: 135.511433, receipt: "换成室内：江户老街实景复原，可租浴衣拍照，雨天也稳" },
              ],
            },
            {
              atom_id: "osaka.namba_2.glico_man_sign", name_zh: "道顿堀 · 固力果跑跑人",
              category: "sightseeing", slot: "evening", start: "18:00", duration_min: 90,
              lat: 34.668926, lon: 135.501055, pinned: false,
              receipt: "你说想拍照：日落后霓虹全亮，戎桥上是拍跑跑人的黄金机位",
              alts: [
                { atom_id: "osaka.namba_2.dotonbori_bridge", name_zh: "道顿堀桥河畔步道", category: "sightseeing", lat: 34.669041, lon: 135.500332, receipt: "换个机位：河畔步道人少，能拍到霓虹倒影进运河" },
                { atom_id: "osaka.umeda_2.le_pont_de_ciel", name_zh: "LE PONT DE CIEL（法餐）", category: "food", lat: 34.690388, lon: 135.511019, receipt: "换成正式晚餐：天满桥畔法餐，落地第一天来点仪式感" },
              ],
            },
          ],
        },
        {
          day: 2, cluster: "osaka_castle", cluster_zh: "大阪城 · 梅田",
          summary: "上午城池樱花，傍晚登空中庭园看日落夜景",
          items: [
            {
              atom_id: "osaka.osaka_castle.osaka_castle_park", name_zh: "大阪城公园",
              category: "nature_park", slot: "morning", start: "09:00", duration_min: 80,
              lat: 34.686576, lon: 135.527252, pinned: false,
              receipt: "清明正值樱花季：清晨九点前入园人最少，先逛公园再进城楼",
              alts: [
                { atom_id: "osaka.osaka_castle.minami_temma_park", name_zh: "南天满公园", category: "nature_park", lat: 34.691977, lon: 135.514607, receipt: "换到河畔：大川沿岸樱并木，本地晨跑路线，游客更少" },
                { atom_id: "osaka.osaka_castle.fujita_art_museum", name_zh: "藤田美术馆", category: "art_museum", lat: 34.694906, lon: 135.525244, receipt: "换成室内：庭园 + 国宝级藏品，雨天备案" },
              ],
            },
            {
              atom_id: "osaka.osaka_castle.osaka_castle", name_zh: "大阪城天守阁",
              category: "culture_history", slot: "morning", start: "10:30", duration_min: 90,
              lat: 34.686951, lon: 135.525671, pinned: false,
              receipt: "离公园步行 5 分钟；上午顺光，从天守阁 8 层俯瞰樱吹雪",
              alts: [
                { atom_id: "osaka.osaka_castle.osaka_museum_of_history", name_zh: "大阪历史博物馆", category: "art_museum", lat: 34.682618, lon: 135.520813, receipt: "换成博物馆：难波宫遗址真迹在地下一层，落地窗正对天守阁" },
                { atom_id: "osaka.osaka_castle.houkoku_jinja_shrine", name_zh: "丰国神社", category: "culture_history", lat: 34.684309, lon: 135.526416, receipt: "换成神社：祭祀秀吉，就在城内南侧，顺路不绕" },
              ],
            },
            {
              atom_id: "osaka.umeda.umeda_sky_building", name_zh: "梅田蓝天大厦 · 空中庭园",
              category: "viewpoint", slot: "sunset", start: "17:30", duration_min: 90,
              lat: 34.705242, lon: 135.489636, pinned: false,
              receipt: "你说想看夜景：日落前 30 分钟登顶，一次收进黄昏蓝调与夜景两种",
              alts: [
                { atom_id: "osaka.umeda_5.nakanoshima_museum_of_art_osaka", name_zh: "中之岛美术馆", category: "art_museum", lat: 34.692442, lon: 135.491265, receipt: "换成美术馆：黑色立方体建筑本身就很出片，傍晚顺光" },
                { atom_id: "osaka.umeda.daimaru", name_zh: "大丸梅田店 · 美食街", category: "food", lat: 34.701992, lon: 135.496727, receipt: "换成吃吃吃：地下食品层一站式，适合晚上不想赶路" },
              ],
            },
          ],
        },
        {
          day: 3, cluster: "bay_area", cluster_zh: "湾区",
          summary: "环球影城一整天，夜里渡轮去天保山收尾",
          items: [
            {
              atom_id: "osaka.bay_area.universal_studios_japan", name_zh: "日本环球影城 USJ",
              category: "activity", slot: "morning", start: "08:30", duration_min: 540,
              lat: 34.665737, lon: 135.432598, pinned: false, anchor: true,
              receipt: "整天留给 USJ：开门前 30 分钟到场直冲任天堂园区，省一张快速券",
              alts: [
                { atom_id: "osaka.bay_area.osaka_aquarium_kaiyukan", name_zh: "海游馆", category: "activity", lat: 34.654881, lon: 135.428913, receipt: "换成海游馆：鲸鲨镇馆，半天刚好，下午可接天保山" },
              ],
            },
            {
              atom_id: "osaka.bay_area.tempozan_marketplace", name_zh: "天保山市场街",
              category: "food", slot: "evening", start: "18:30", duration_min: 60,
              lat: 34.656198, lon: 135.429801, pinned: false, paired: true,
              receipt: "USJ 出来坐船长线渡轮 10 分钟到对岸；浪花美食横丁解决晚餐",
              alts: [],
            },
            {
              atom_id: "osaka.bay_area.tempozan_ferris_wheel", name_zh: "天保山大摩天轮",
              category: "viewpoint", slot: "night", start: "19:45", duration_min: 40,
              lat: 34.656185, lon: 135.431091, pinned: false, paired: true,
              receipt: "晚餐后步行 3 分钟：夜灯亮起后坐一圈，给湾区一天收尾",
              alts: [],
            },
          ],
        },
        {
          day: 4, cluster: "tennoji", cluster_zh: "天王寺 · 新世界",
          summary: "慢节奏：古寺、串炸老街、300 米高空日落",
          items: [
            {
              atom_id: "osaka.tennoji.shitenno_ji_temple", name_zh: "四天王寺",
              category: "culture_history", slot: "morning", start: "09:00", duration_min: 80,
              lat: 34.654629, lon: 135.516403, pinned: false,
              receipt: "昨天玩到深夜，今早不早起：九点参拜人最少，日本最古官寺慢慢看",
              alts: [
                { atom_id: "osaka.tennoji.abeno_harukas_art_museum", name_zh: "阿倍野 HARUKAS 美术馆", category: "art_museum", lat: 34.645843, lon: 135.513169, receipt: "换成美术馆：16 层高空展厅，常与展望台联票" },
                { atom_id: "osaka.sumiyoshi_2.abeno_jinja_shrine", name_zh: "阿部野神社", category: "culture_history", lat: 34.628035, lon: 135.499449, receipt: "换成更安静的小社：上町台地南缘，几乎只有本地人参拜" },
              ],
            },
            {
              atom_id: "osaka.tennoji.shinsekai", name_zh: "新世界 · 串炸老街",
              category: "sightseeing", slot: "noon", start: "12:30", duration_min: 100,
              lat: 34.65209, lon: 135.506191, pinned: false,
              receipt: "离四天王寺步行 12 分钟；午餐就吃串炸，记住规矩：酱汁只能蘸一次",
              alts: [
                { atom_id: "osaka.tennoji.abeno_cues_town", name_zh: "阿倍野 CUES TOWN", category: "shopping", lat: 34.6447, lon: 135.511485, receipt: "换成商场美食街：选择多、有座位，走累了可以久坐" },
                { atom_id: "osaka.sumiyoshi_2.taiyoshihyakuban", name_zh: "太阳楼百番（老铺食堂）", category: "food", lat: 34.64258, lon: 135.505681, receipt: "换成老铺食堂：昭和风定食，街坊价，体验本地日常" },
              ],
            },
            {
              atom_id: "osaka.tennoji.harukas_300", name_zh: "阿倍野 HARUKAS 300 展望台",
              category: "viewpoint", slot: "sunset", start: "16:45", duration_min: 90,
              lat: 34.64598, lon: 135.513523, pinned: false,
              receipt: "你说想拍照：300 米高空日落时段，从天王寺看夕阳沉进大阪湾",
              alts: [
                { atom_id: "osaka.tennoji.tsutenkaku", name_zh: "通天阁展望台", category: "viewpoint", lat: 34.652539, lon: 135.50631, receipt: "换成通天阁：高度低一些，但复古霓虹本身就是主角" },
                { atom_id: "osaka.tennoji.shinsekai", name_zh: "新世界夜游（二刷）", category: "sightseeing", lat: 34.65209, lon: 135.506191, receipt: "晚上再回新世界：灯笼亮起后完全是另一条街" },
              ],
            },
          ],
        },
        {
          day: 5, cluster: "sumiyoshi", cluster_zh: "住吉 · 心斋桥",
          gateway_out: "关西机场 KIX",
          summary: "轻量收尾：神社晨参 + 心斋桥补货，从容去机场",
          items: [
            {
              atom_id: "osaka.sumiyoshi.sumiyoshi_taisha", name_zh: "住吉大社",
              category: "culture_history", slot: "morning", start: "09:00", duration_min: 70,
              lat: 34.612711, lon: 135.493004, pinned: false,
              receipt: "返程日安排轻量：早班电车直达，反桥红色拱弧是收尾好镜头",
              alts: [
                { atom_id: "osaka.osaka_castle_2.tamatsukuri_inari_shrine", name_zh: "玉造稻荷神社", category: "culture_history", lat: 34.677525, lon: 135.529957, receipt: "换成市区小社：离酒店区更近，多睡半小时" },
                { atom_id: "osaka.umeda_2.osaka_tenmangu_shrine_osaka_tenmangu", name_zh: "大阪天满宫", category: "culture_history", lat: 34.696148, lon: 135.512779, receipt: "换成天满宫：求学业灵验，天神桥筋商店街就在旁边" },
              ],
            },
            {
              atom_id: "osaka.namba.shinsaibashi_suji", name_zh: "心斋桥筋商店街",
              category: "shopping", slot: "noon", start: "12:30", duration_min: 120,
              lat: 34.673221, lon: 135.501091, pinned: false,
              receipt: "最后补货：药妆与伴手礼一条街上搞定，预留两小时不慌张",
              alts: [
                { atom_id: "osaka.umeda.lucua_1100", name_zh: "LUCUA 1100（梅田）", category: "shopping", lat: 34.702713, lon: 135.494765, receipt: "换到梅田：车站直结，买完直接上楼坐 JR 去机场线" },
                { atom_id: "osaka.umeda.hankyu_umeda_main_store", name_zh: "阪急百货梅田总店", category: "shopping", lat: 34.702713, lon: 135.498432, receipt: "换成阪急百货：伴手礼档次更高，地下食品层很好逛" },
              ],
            },
            {
              atom_id: "osaka.namba.namba_yasaka_shrine", name_zh: "难波八坂神社",
              category: "culture_history", slot: "afternoon", start: "15:30", duration_min: 40,
              lat: 34.661392, lon: 135.496744, pinned: false,
              receipt: "离心斋桥步行 15 分钟；狮子殿前拍张收尾照，再坐南海电铁去机场",
              alts: [
                { atom_id: "osaka.umeda_2.nakanoshima_park", name_zh: "中之岛公园玫瑰园", category: "nature_park", lat: 34.692423, lon: 135.507772, receipt: "换成水岸散步：玫瑰园 + 中央公会堂红砖外墙，适合收尾" },
                { atom_id: "osaka.namba_2.hozen_ji", name_zh: "法善寺横丁（二刷）", category: "culture_history", lat: 34.667871, lon: 135.502603, receipt: "临走再去一次小巷：白天与夜晚气质完全不同" },
              ],
            },
          ],
        },
      ],
    };
  }

  // ---------- 阶段文案（refine 响应自带，含真实耗时） ----------
  const STAGES = [
    { label: "硬过滤：体力档位 × 预算档 × 人群适配", ms: 620 },
    { label: "450 个原子聚类，分配到 5 天", ms: 840 },
    { label: "正在核对开放时间与闭馆日…", ms: 760 },
    { label: "重排第 2 天动线：先公园后城楼，顺光", ms: 690 },
    { label: "节奏检查：每半天留 ≥30 分钟空档", ms: 540 },
    { label: "为 15 个安排生成决策收据…", ms: 480 },
  ];

  // ---------- 通用兜底阶段文案（真实 API 未返回 stages 时用） ----------
  const GENERIC_STAGES = [
    "正在核对开放时间…",
    "估算片区间通行时间…",
    "匹配你的情绪标签…",
    "检查节奏与休息窗口…",
    "避开闭馆日与高峰时段…",
  ];

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------- Mock API：与 §4 合同同构 ----------
  const plans = new Map();

  async function postIntent(text, explicitPois) {
    await wait(650);
    const res = intent(text, explicitPois);
    if (res.intent_vector.geography.destination_specified === "bangkok") {
      // 演示数据集只有大阪：如实告知，回落大阪（note 对齐真实后端形状）
      res.intent_vector.geography.destination_specified = "osaka";
      res.intent_vector.inferred.push("destination_fallback:osaka");
      res.destination = "osaka";
      res.note = "演示数据只有大阪，先按「大阪」出草稿——接上后端后曼谷也能直接生成";
    } else {
      res.destination = "osaka";
    }
    // 记下「想要」清单，refine 时做锚定演示
    pendingWants.set(res.plan_id, res.intent_vector.explicit_pois);
    return res;
  }
  const pendingWants = new Map();

  async function postSkeleton(planId) {
    await wait(800);
    const sk = JSON.parse(JSON.stringify(SKELETON));
    plans.set(planId, { skeleton: sk, plan: null });
    return sk;
  }

  async function postRefine(planId, onStage) {
    for (const s of STAGES) {
      if (onStage) onStage(s.label);
      await wait(s.ms);
    }
    const st = plans.get(planId) || {};
    if (!st.plan) {
      st.plan = buildPlan();
      st.plan.plan_id = planId;
      plans.set(planId, st);
    }
    const plan = JSON.parse(JSON.stringify(st.plan));
    // 「想要」锚定演示：命中的 atom 就地 pin + 换收据（对齐后端 explicit_pois 语义）
    const wants = pendingWants.get(planId) || [];
    if (wants.length) {
      const all = getAtoms("osaka");
      wants.forEach((wid) => {
        const hit = all.find((a) => a.id === wid);
        if (!hit) return;
        for (const d of plan.days) {
          const slot = d.items.find((it) => it.atom_id === wid);
          if (slot) {
            slot.pinned = true;
            slot.receipt = `你点名要去：已在探索模式标记「想要」，本次行程为你锚定`;
            return;
          }
        }
        // 未排入的「想要」：换进第 1 天首个可换槽位（演示锚定路径）
        const d1 = plan.days[0];
        const idx = d1.items.findIndex((it) => !it.anchor && it.alts && it.alts.length);
        if (idx >= 0) {
          const old = d1.items[idx];
          d1.items[idx] = {
            atom_id: hit.id, name_zh: hit.name.zh, category: hit.categories[0],
            slot: old.slot, start: old.start, duration_min: old.duration_min,
            lat: hit.spatial.lat, lon: hit.spatial.lon, pinned: true,
            receipt: `你点名要去：已在探索模式标记「想要」，本次行程为你锚定`,
            alts: [stripAlt(old)].concat(old.alts || []),
          };
        }
      });
      st.plan = JSON.parse(JSON.stringify(plan));
      plans.set(planId, st);
    }
    return { plan, stages: STAGES, generation_trace: buildMockTrace(plan) };
  }

  // ---------- V1.1 P1/P2：演示原子集 + 假 generation_trace（演示模式明确减量并标注） ----------
  // 真实点位（坐标取自真实原子库） + 确定性伪随机补点；补点 summary 明示"演示"
  let DEMO_ATOMS = null;
  function getAtoms(dest) {
    if (dest && dest !== "osaka") return [];
    if (DEMO_ATOMS) return DEMO_ATOMS;
    // 演示 tips（对齐 v0.4 schema；标注演示，接后端后为真实 Wikivoyage 数据）
    const DEMO_TIPS = { "osaka.bay_area.tempozan_ferris_wheel": [
      { tip_zh: "（演示提醒）透明地板舱不加价但排队久，日落时段普通舱体验已足够好", applies_to: [], source: { url: "https://en.wikivoyage.org/wiki/Osaka/Bay_Area" }, expires_at: "2026-12-12" },
    ] };
    const real = [];
    buildPlan().days.forEach((d) => d.items.forEach((it) => {
      if (it.atom_id && it.lat != null) {
        real.push({ id: it.atom_id, name: { zh: it.name_zh, en: null }, categories: [it.category],
          spatial: { lat: it.lat, lon: it.lon, cluster: d.cluster }, summary: it.receipt,
          media: null, constraints: { budget_tier: 2, energy_cost: 2 }, time_structure: { best_time_of_day: [it.slot] },
          tips: DEMO_TIPS[it.atom_id] || [] });
      }
      (it.alts || []).forEach((a) => {
        if (a.atom_id && a.lat != null) real.push({ id: a.atom_id, name: { zh: a.name_zh, en: null }, categories: [a.category],
          spatial: { lat: a.lat, lon: a.lon, cluster: d.cluster }, summary: a.receipt,
          media: null, constraints: { budget_tier: 2, energy_cost: 2 }, time_structure: { best_time_of_day: [] } });
      });
    }));
    EXTRA_POOL.forEach((a) => real.push({ id: a.atom_id, name: { zh: a.name_zh, en: null }, categories: [a.category],
      spatial: { lat: a.lat, lon: a.lon, cluster: a.cluster }, summary: a.receipt,
      media: null, constraints: { budget_tier: a.budget_tier, energy_cost: a.energy_cost }, time_structure: { best_time_of_day: [] } }));
    const seen = new Set();
    const atoms = real.filter((a) => !seen.has(a.id) && seen.add(a.id));
    // 确定性伪随机补点到 449（mulberry32，种子固定，截图可复现）
    let seed = 20260912;
    const rnd = () => { seed |= 0; seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
    const CLUSTERS = [
      { c: "namba", zh: "难波", lat: 34.668, lon: 135.501 }, { c: "umeda", zh: "梅田", lat: 34.702, lon: 135.498 },
      { c: "osaka_castle", zh: "大阪城", lat: 34.687, lon: 135.525 }, { c: "bay_area", zh: "湾区", lat: 34.656, lon: 135.431 },
      { c: "tennoji", zh: "天王寺", lat: 34.649, lon: 135.512 }, { c: "sumiyoshi", zh: "住吉", lat: 34.613, lon: 135.491 },
      { c: "shinsekai", zh: "新世界", lat: 34.652, lon: 135.506 }, { c: "nakanoshima", zh: "中之岛", lat: 34.691, lon: 135.497 },
    ];
    const CATS = ["food", "culture_history", "nature_park", "art_museum", "shopping", "sightseeing", "activity", "nightlife", "market_street", "viewpoint"];
    while (atoms.length < 449) {
      const i = atoms.length;
      const cl = CLUSTERS[Math.floor(rnd() * CLUSTERS.length)];
      const cat = CATS[Math.floor(rnd() * CATS.length)];
      atoms.push({
        id: `osaka.demo.demo_${i}`,
        name: { zh: `演示点位 ${i}（${cl.zh}）`, en: null },
        categories: [cat],
        spatial: { lat: cl.lat + (rnd() - 0.5) * 0.024, lon: cl.lon + (rnd() - 0.5) * 0.03, cluster: cl.c },
        summary: "演示模式示意点位（非真实原子，接入后端后此处为真实数据）",
        media: null,
        constraints: { budget_tier: 1 + Math.floor(rnd() * 4), energy_cost: 1 + Math.floor(rnd() * 5) },
        time_structure: { best_time_of_day: [] },
      });
    }
    DEMO_ATOMS = atoms;
    return atoms;
  }

  function buildMockTrace(plan) {
    const atoms = getAtoms("osaka");
    const inPlan = new Set();
    const dayAssignments = plan.days.map((d) => ({
      day: d.day, cluster: d.cluster,
      atom_ids: d.items.filter((it) => it.atom_id).map((it) => { inPlan.add(it.atom_id); return it.atom_id; }),
    }));
    // 演示排除集：确定性取非演示点位的 12%，理由用真实文案（与 trace.py 一致）
    const REASONS = ["排除：体力要求超过同行人承受上限", "排除：预算档超出本次承受范围", "排除：场所标注不适合同行人群"];
    const candidates = atoms.map((a, i) => {
      const excluded = !inPlan.has(a.id) && a.id.startsWith("osaka.demo.") && i % 8 === 0;
      return excluded
        ? { atom_id: a.id, lat: a.spatial.lat, lon: a.spatial.lon, kept: false, reason_zh: REASONS[i % REASONS.length] }
        : { atom_id: a.id, lat: a.spatial.lat, lon: a.spatial.lon, kept: true };
    });
    const byCluster = {};
    atoms.forEach((a) => { (byCluster[a.spatial.cluster] = byCluster[a.spatial.cluster] || []).push(a); });
    const clusters = Object.keys(byCluster).sort().map((name) => {
      const list = byCluster[name];
      const lat = list.reduce((s, a) => s + a.spatial.lat, 0) / list.length;
      const lon = list.reduce((s, a) => s + a.spatial.lon, 0) / list.length;
      return { cluster: name, atom_ids: list.map((a) => a.id).sort(), centroid: { lat, lon } };
    });
    const excludedN = candidates.filter((c) => !c.kept).length;
    return { total_atoms: atoms.length, candidates, excluded_total: excludedN, excluded_sampled: false, clusters, day_assignments: dayAssignments };
  }

  // 局部重解：只改指定槽位；USJ 锚点演示 requires_confirmation
  // targetAtomId（可选）：用户在三候选选择器里点选的原子——mock 直接兑现用户选择
  async function postSwap(planId, day, slotIndex, targetAtomId) {
    await wait(480 + Math.random() * 500); // <2s
    const st = plans.get(planId);
    if (!st || !st.plan) throw new Error("plan not found");
    const plan = st.plan;
    const dayObj = plan.days.find((d) => d.day === day);
    if (!dayObj) throw new Error("day not found");
    const item = dayObj.items[slotIndex];
    if (!item) throw new Error("slot not found");

    // USJ 锚点：替换会牵连同日晚间两个衔接安排 → 需要确认
    if (item.anchor) {
      if (!item.alts || item.alts.length === 0) {
        return { error: { code: "NO_ALTERNATIVE", message_zh: "这个锚点安排暂无替代方案" } };
      }
      const alt0 = item.alts[0];
      const proposed = JSON.parse(JSON.stringify(plan));
      const pd = proposed.days.find((d) => d.day === day);
      pd.items[0] = Object.assign({}, alt0, {
        slot: item.slot, start: "10:00", duration_min: 300, pinned: false, alts: [stripAlt(item)],
      });
      pd.items[1] = Object.assign({}, pd.items[1], {
        name_zh: "天保山市场街", start: "15:00",
        receipt: "海游馆半天刚好，下午接着在市场街逛吃",
      });
      pd.items[2] = Object.assign({}, pd.items[2], { start: "17:30", receipt: "傍晚摩天轮换到日落时段，看夕阳沉进大阪湾" });
      // diff 对齐 §4 新合同：{day, slot_index, slot, old:{atom_id,name_zh}, new:{atom_id,name_zh}, receipt}
      return {
        requires_confirmation: true,
        message_zh: "「日本环球影城」是一整天的锚点安排，替换它会让同日晚间的渡轮衔接失效，需要一并调整：",
        proposed: {
          plan: proposed,
          diff: {
            changed: [
              { day, slot_index: 0, slot: item.slot,
                old: { atom_id: item.atom_id, name_zh: item.name_zh },
                new: { atom_id: alt0.atom_id, name_zh: alt0.name_zh },
                receipt: "换成海游馆：鲸鲨镇馆，半天刚好，下午可接天保山" },
              { day, slot_index: 1, slot: "evening",
                old: { atom_id: pd.items[1].atom_id, name_zh: "天保山市场街" },
                new: { atom_id: pd.items[1].atom_id, name_zh: "天保山市场街（时间调整）" },
                receipt: "海游馆半天刚好，下午接着在市场街逛吃" },
              { day, slot_index: 2, slot: "night",
                old: { atom_id: pd.items[2].atom_id, name_zh: "天保山大摩天轮" },
                new: { atom_id: pd.items[2].atom_id, name_zh: "天保山大摩天轮（提前到日落）" },
                receipt: "傍晚摩天轮换到日落时段，看夕阳沉进大阪湾" },
            ],
            unchanged_count: countItems(plan) - 3,
          },
        },
      };
    }

    if (!item.alts || item.alts.length === 0) {
      return { error: { code: "NO_ALTERNATIVE", message_zh: "这个安排是同片区唯一选项，试试换别的槽位吧" } };
    }

    let next;
    if (targetAtomId) {
      const i = item.alts.findIndex((a) => a.atom_id === targetAtomId);
      if (i < 0) {
        // 用户点选的候选不在可行集里：明确报错，绝不静默换成别的
        return { error: { code: "TARGET_NOT_AVAILABLE", message_zh: "所选候选已不可用（可能刚被换过），请重新打开候选" } };
      }
      next = item.alts.splice(i, 1)[0];
    } else {
      next = item.alts.shift();
    }
    const oldName = item.name_zh;
    const newItem = Object.assign({}, next, {
      slot: item.slot, start: item.start, duration_min: item.duration_min,
      pinned: true, // 与后端 resolve.py 对齐：换过的槽位永久 pin
      alts: [stripAlt(item)].concat(item.alts),
    });
    dayObj.items[slotIndex] = newItem;

    const total = countItems(plan);
    return {
      plan: JSON.parse(JSON.stringify(plan)),
      diff: {
        changed: [{ day, slot: slotIndex, slot_index: slotIndex,
          old: { atom_id: item.atom_id, name_zh: oldName },
          new: { atom_id: newItem.atom_id, name_zh: newItem.name_zh },
          receipt: { icon: "🎯", text_zh: newItem.receipt, basis: ["mock"] } }],
        unchanged_count: total - 1,
      },
      requires_confirmation: false,
    };
  }

  // ---------- 三候选预览（只读，不动 plan；对齐后端 swap_candidates 形状） ----------
  const CAT_ZH_MOCK = {
    food: "餐饮", market_street: "市集", culture_history: "文化历史", art_museum: "博物馆",
    nature_park: "自然公园", viewpoint: "观景", sightseeing: "观光", activity: "活动",
    shopping: "购物", nightlife: "夜生活", photo_spot: "出片点",
  };
  // 通用占位收据（与 packages/solver/receipts.py 第 6 条一致，不编造具体理由）
  const GENERIC_RECEIPT = "同片区候选里综合评分最高（季节×情绪×拥挤度加权）";
  // 补充池：让 alts 不足 3 个的槽位也能凑出三候选（坐标均取自真实原子库，带所属 cluster）
  const EXTRA_POOL = [
    { atom_id: "osaka.namba_2.dotonbori_bridge", name_zh: "道顿堀桥河畔步道", category: "sightseeing", cluster: "namba_2", lat: 34.669041, lon: 135.500332, budget_tier: 1, energy_cost: 1, receipt: "河畔步道人少，能拍到霓虹倒影进运河" },
    { atom_id: "osaka.namba.namba_walk", name_zh: "难波 Walk 地下街", category: "shopping", cluster: "namba", lat: 34.6635, lon: 135.5012, budget_tier: 1, energy_cost: 1, receipt: GENERIC_RECEIPT },
    { atom_id: "osaka.osaka_castle.mint_museum", name_zh: "造币局博物馆", category: "art_museum", cluster: "osaka_castle", lat: 34.695894, lon: 135.521684, budget_tier: 1, energy_cost: 2, receipt: "樱花季造币局「樱花大道」是本地人的私藏" },
    { atom_id: "osaka.namba_3.kuromon_ichiba_market_2", name_zh: "黑门市场（二刷）", category: "market_street", cluster: "namba_3", lat: 34.666443, lon: 135.507039, budget_tier: 2, energy_cost: 2, receipt: GENERIC_RECEIPT },
    { atom_id: "osaka.umeda.hep_navio", name_zh: "HEP NAVIO", category: "shopping", cluster: "umeda", lat: 34.703174, lon: 135.500011, budget_tier: 3, energy_cost: 2, receipt: GENERIC_RECEIPT },
    { atom_id: "osaka.tennoji.abeno_harukas_art_museum", name_zh: "阿倍野 HARUKAS 美术馆", category: "art_museum", cluster: "tennoji", lat: 34.645843, lon: 135.513169, budget_tier: 3, energy_cost: 2, receipt: GENERIC_RECEIPT },
    { atom_id: "osaka.sumiyoshi.sumiyoshi_park", name_zh: "住吉公园", category: "nature_park", cluster: "sumiyoshi", lat: 34.612304, lon: 135.488365, budget_tier: 1, energy_cost: 1, receipt: GENERIC_RECEIPT },
  ];

  async function postSwapPreview(planId, day, slotIndex) {
    await wait(350 + Math.random() * 300);
    const st = plans.get(planId);
    if (!st || !st.plan) throw new Error("plan not found");
    const plan = st.plan;
    const dayObj = plan.days.find((d) => d.day === day);
    if (!dayObj) throw new Error("day not found");
    const item = dayObj.items[slotIndex];
    if (!item) throw new Error("slot not found");

    if (item.anchor) {
      return { candidates: [], day, slot_index: slotIndex, requires_confirmation: false };
    }
    if (!item.alts || item.alts.length === 0) {
      return { error: { code: "NO_ALTERNATIVE", message_zh: "同片区没有更多候选了" } };
    }

    // 候选 = 该槽位 alts + 同 cluster 补充池（去重、排除当前原子），凑满 3 个
    const seen = new Set([item.atom_id]);
    const raw = [];
    for (const a of item.alts) if (!seen.has(a.atom_id)) { seen.add(a.atom_id); raw.push(a); }
    for (const a of EXTRA_POOL) {
      if (raw.length >= 3) break;
      if (a.cluster !== dayObj.cluster || seen.has(a.atom_id)) continue;
      seen.add(a.atom_id);
      raw.push(a);
    }

    const candidates = raw.slice(0, 3).map((a) => {
      // 真实性铁律：没有 budget_tier 就不生成价格比较文案
      const budget = Number.isFinite(a.budget_tier) ? a.budget_tier : null;
      const energy = Number.isFinite(a.energy_cost) ? a.energy_cost : null;
      let tradeoff;
      if (a.category !== item.category) {
        tradeoff = `不同类型：${CAT_ZH_MOCK[a.category] || a.category}`;
        if (budget === 1) tradeoff += "；更便宜（预算档 1）";
        else if (budget === 4) tradeoff += "；更贵一些（预算档 4）";
      } else {
        tradeoff = "同片区同类型替代";
        if (budget === 1) tradeoff += "；更便宜（预算档 1）";
      }
      return {
        atom: {
          id: a.atom_id, name_zh: a.name_zh, categories: [a.category],
          budget_tier: budget, lat: a.lat, lon: a.lon, energy_cost: energy,
        },
        receipt: { icon: "🎯", text_zh: a.receipt || GENERIC_RECEIPT, basis: ["mock"] },
        tradeoff_zh: tradeoff,
      };
    });
    return { candidates, day, slot_index: slotIndex, requires_confirmation: false };
  }

  // ---------- M1.1b mock：L1 叙事 + 顾问对话（演示数据，文案标注演示） ----------
  async function postNarrate(planId) {
    await wait(900);
    const st = plans.get(planId);
    if (!st || !st.plan) throw new Error("plan not found");
    const narratives = {};
    const picks = [];
    st.plan.days.forEach((d) => d.items.forEach((it) => {
      if (it.atom_id && picks.length < 3) picks.push(it);
    }));
    picks.forEach((it) => {
      narratives[it.atom_id] = `（演示叙事）「${it.name_zh}」正好贴你的拍照/美食节奏，这个时段人最少`;
    });
    return {
      narratives,
      day_themes: { "1": "（演示主题）落地南区，从市场吃到霓虹", "2": "（演示主题）城池樱花与空中日落" },
      conflict_note_zh: null,
      llm_used: true, cache_hit: false, calls_used: 1, ms: 900,
    };
  }

  async function postChat(planId, text, confirmed) {
    await wait(700 + Math.random() * 400);
    const st = plans.get(planId);
    if (!st || !st.plan) throw new Error("plan not found");
    const plan = st.plan;

    // 识别三个演示关键词：下雨（室内化）/ 累（降强度）/ 想吃（加餐饮）
    const isRain = /下雨|雨/.test(text);
    const isTired = /累|太累|走不动/.test(text);
    const isFood = /想吃|想吃掉|蟹|吃/.test(text);
    if (!isRain && !isTired && !isFood) {
      // M1.2：advisor_only 演示（对齐后端新响应形状 applied:false + advisor_note_zh）——
      // 顾问只给建议不改方案，正常气泡展示
      return { applied: false, llm_used: true,
        advisor_note_zh: "收到。演示模式下我只给建议不动方案——真要去的话，我会把手办店集中到最后一天下午，临走前一站买完不绕路。",
        plan };
    }

    // 找一个可动槽位：演示「移除末尾景点 → 留空休整」（对齐后端 remove 型 diff：new 可为 null）
    let target = null, tDay = null, tIdx = -1;
    for (let di = plan.days.length - 1; di >= 0 && !target; di--) {
      const d = plan.days[di];
      for (let i = d.items.length - 1; i >= 0; i--) {
        const it = d.items[i];
        if (it.atom_id && !it.anchor && !it.pinned && it.alts) { target = it; tDay = d; tIdx = i; break; }
      }
    }
    if (!target) {
      return { applied: false, llm_used: true, needs_confirm: true,
        understood_zh: `你是想：${text}`, confirm_zh: "演示方案里没有可调整的槽位了", plan };
    }

    // 破坏性操作演示：移除已 pin 项会先复述确认（本分支演示 pinned 时触发）
    if (target.pinned && !confirmed) {
      return { applied: false, llm_used: true, needs_confirm: true,
        understood_zh: `你是想：${text}`,
        confirm_zh: `「${target.name_zh}」是你之前钉住的，确认移除吗？确认后我会重排那一天`,
        plan };
    }

    const REPLACEMENT = {
      rain: { name_zh: "室内备案：商场咖啡馆歇脚", category: "shopping", receipt: "（演示）雨天室内化：先把户外点换成有屋檐的" },
      tired: { name_zh: "（留空休整）", category: "nature_park", receipt: "（演示）降强度：这个时段留空回血" },
      food: { name_zh: "加餐：片区口碑餐馆（演示）", category: "food", receipt: "（演示）按你说的加了一顿正经饭" },
    };
    const kind = isRain ? "rain" : isTired ? "tired" : "food";
    const r = REPLACEMENT[kind];
    const old = { atom_id: target.atom_id, name_zh: target.name_zh };
    tDay.items[tIdx] = Object.assign({}, target, {
      atom_id: kind === "tired" ? null : target.atom_id + ".demo_" + kind,
      name_zh: r.name_zh, category: r.category, receipt: r.receipt, pinned: false, alts: [stripAlt(target)],
    });
    const total = countItems(plan);
    const UNDERSTOOD = {
      rain: "下雨了，把户外行程往室内收", tired: "今天太累了，少排一点、降强度", food: "想吃口好的，给你加一顿正经饭",
    };
    return {
      applied: true, llm_used: true, needs_confirm: false,
      understood_zh: UNDERSTOOD[kind],
      tradeoff_zh: `（演示）第${tDay.day}天「${old.name_zh}」→「${r.name_zh}」，其余 ${total - 1} 个安排不变`,
      diff: { changed: [{ day: tDay.day, slot: target.slot, slot_index: tIdx,
        old, new: kind === "tired" ? null : { atom_id: tDay.items[tIdx].atom_id, name_zh: r.name_zh },
        receipt: { icon: "💬", text_zh: r.receipt, basis: ["mock"] } }], unchanged_count: total - 1 },
      plan: JSON.parse(JSON.stringify(plan)),
    };
  }

  function stripAlt(item) {
    return {
      atom_id: item.atom_id, name_zh: item.name_zh, category: item.category,
      lat: item.lat, lon: item.lon, receipt: "换回原来的安排",
    };
  }

  function countItems(plan) {
    return plan.days.reduce((n, d) => n + d.items.length, 0);
  }

  return { postIntent, postSkeleton, postRefine, postSwap, postSwapPreview, getAtoms, postNarrate, postChat, GENERIC_STAGES };
})();
