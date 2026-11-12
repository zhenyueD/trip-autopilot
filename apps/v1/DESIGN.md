# Trip Autopilot · 共用呈现设计契约

版本：2026-11-12。主色：`#0071E3`。本文件是 `apps/v1`、Manus 四幕和原始 HTML 手册呈现层的共同约束；唯一 token 内容为 `design-tokens.css`，另外两边保留逐字节一致的部署副本。

## 1. 两层信息结构

- **第一眼层**：固定三页——①设计判断＋真地图／路线＋每日总表；②原文照片墙＋原文季节时段与理由；③安排、住吃预算与完整手册入口。只从原始 HTML 的 `.trip-decision`、地图／`.daily-route`、`.itinerary-day`、`.destination-card img` 等节点提取，不生成新事实。
- **完整深入层**：原始手册全文、每日安排、价格、待办、修订回执、核查记录与来源，不删、不改写。
- 真地图只接受原文明确标记的 `.trip-map`、`.handbook-map`、`[data-handbook-map]`、`[data-map="true"]` 或 `figure.map`。`.daily-route` 只能标为“路线示意，非导航地图”。无地图、无照片、无每日结构均明确留空，不猜坐标、不补 stock 图。
- 南法 v5 这类原文已经是三页 `.page` 的 HTML，允许按其原有标题、列表、地图、表格、`.cell` 照片和第三页内容重组；每日表没有 `.itinerary-day` 时只读展示，不伪造每日跳转。季节表只接受原文 2—3 个时段且两列齐全，不补写括号理由。
- 原件缺 `meta[name="viewport"]` 时由呈现层补标准手机视口；390px 验收同时检查 `innerWidth===390`、无横向溢出和正文 computed font-size≥15px，禁止把桌面缩小截图冒充手机可读。

## 2. 视觉原则

- Apple 的纪律＋旅行手册的可读性：白底、细分隔线、大标题、清晰留白；内容卡零阴影，悬浮导航才使用毛玻璃。
- 一套灰阶，一个强调色。蓝色只表达链接、当前位置与主行动，不用多色标签制造噪音。
- 字阶只有 `13 / 15 / 17 / 21 / 28 / 40px`；手册封面展示标题可到 `56px`，属于 display 特例。正文移动端不小于 `15px`，辅助信息 `13px`。
- 圆角 `12 / 18 / 24px`；内容间距使用 `4 / 8 / 12 / 16 / 24 / 32 / 48px`。
- 图片必须有原始 `src`，以 `4:3` 裁切展示，保留 `alt` 与原有来源；布局变化不改变事实归属。

## 3. 交互

- “第一眼／完整手册”切换只改变呈现，不调用模型、不提交修订、不改变版本。
- 每日总表点击某天进入深入层对应原日区块；`#rec` 来源链接自动进入深入层并打开原始核查记录。
- 三页导航仅滚动定位；完整按钮进入原文。手机三页是三个连续阅读章节，不用隐藏分页掩盖溢出。
- 所有按钮支持键盘与 focus-visible；动效只动 transform／opacity，遵循 `prefers-reduced-motion`。

## 4. Token 使用

```css
@import url('/design-tokens.css');

.cta { background: var(--ta-accent); color: white; }
.body { color: var(--ta-text-1); font-size: var(--ta-text-sm); }
.muted { color: var(--ta-text-2); font-size: var(--ta-text-xs); }
```

禁止另开第二主蓝、用 hex 复制 token、新增阴影层级或在组件里重写字阶。确实需要新 token 时，先改本文件和 canonical `apps/v1/design-tokens.css`，再同步部署副本并运行一致性测试。

## 5. 三边落点

| 边 | 消费方式 |
| --- | --- |
| `apps/v1/session.html` | 加载同一 `handbook-presentation.js`，在 iframe 内挂载两层结构；外层引用 token。 |
| Manus `source/` | `public/` 中的同字节 JS／CSS 部署副本；`HandbookScreen` 提供“第一眼／完整手册”切换。 |
| 7392 原始 HTML | 后端文件不改；同源 iframe 加载共享 token 和呈现 CSS，原 DOM 作为深入层唯一事实来源。 |

## 6. 验收边界

- 契约夹具只能验证结构、交互与响应式，不算真实出册证据。
- “手机三页内读完／南法数据实测”必须用南法 v5 原件或真实 completed 南法手册复核；没有原件时不得宣称通过。
- 真地图与照片墙是否有内容取决于原始 HTML；缺口透明不等于功能完成。
