---
type: delivery-report
date: 2026-11-18
status: verified-preview-deployed
tags: [trip-autopilot, writer, publication, p0]
---

# 草稿到正式发布 P0 修复

**已修复两个确定的发布阻断。真实新建杭州单已由模型自主正式保存 v1，实际 Studio 读取通过；3 个核心景点图片已加载，双 PDF 已生成，终态 completed。此次发布故障可关闭；本轮不等于修订、恢复、双 PDF 逐页质量的完整验收。**

## 根因与改动

1. 原杭州失败单机械缺项已清零，writer 继续语义补改和复查，但进度规则只认可机械缺项减少，导致发布前被误判三轮空转。现在首次清零、当前草稿仍清零且尚无正式版时，允许一次明确的收尾请求。空 findings 不代表语义通过；模型仍须自行复核，并调用原 publish_draft 或说明具体阻塞。
2. 升级前已被误停的旧单，在显式恢复时会被历史 stopped 状态提前拒绝。现在仅该类合格旧 checkpoint 可复用上述一次机会；不是清空旧状态重新无限运行。

机会在模型请求发出前持久消费。重复检查、无工具回复、截断、异常及再次恢复都不能补发。新出现缺项、已有发布版、不合格失败状态仍拒绝。生产改动只在 single_session_progress.py 与 single_session_roles.py；发布器的 hash、价格和引用检查未更改。

## 实际证据及边界

| 检查 | 结果 | 能证明什么 |
| --- | --- | --- |
| 原失败序列先红后绿 | PASS | 原机械误停可进入正式发布 |
| 实际旧杭州 checkpoint 只读副本回放 | V2 拒绝 → V3 保存 v1 并重新读取；原件 157 文件 hash 不变 | 升级恢复链可用；模型回复为注入，不能证明真实语义能力 |
| 独立对抗检查 | 原 5 项及 legacy 17 项均 PASS | 一次机会不会通过异常/恢复重复领取，fresh 路径未改变 |
| 相关回归 | 650 项：649 PASS、1 项既有 skip；其他 29/29 | 离线回归无新增失败；集合有重叠，不累加 |
| 最终加密包回解与测试 | 165 文件 hash 一致，包内 32/32 PASS | 发布包与已审源代码一致 |
| 新单真实生成 | 模型 publish_draft 保存 v1 | 新单首次正式发布通过，无人工改稿、无重建/续跑 |
| 真实 Studio 读取 | 终态正文 6,735 字符，桌面/手机截图已看，0 前端异常 | 当前版本可读；不等于逐项旅行事实验收 |
| 图片与 PDF | 3 个核心景点图片已加载；速览 3 页、完整 17 页，API 均 ready | 文件已产出；本轮未做逐页视觉质量验收 |

真实新单采用冻结 V2，运行中没有换代码；V3 只新增显式 legacy resume 分支。新单在第 18 次模型请求直接发布，未触发新增收尾机会，所以不能宣称这轮活跑单独证明了该分支的因果效果；因果证据是原失败回放及独立探针。

## 真实单与账目

- 行程：54eb04abc92df5ad055ec92c21d33fdc，原失败单 611003d8db211e3774cf954d9f8544c2 原件保留。
- 一次 UI 新建；隔离 7396；需求与旧失败杭州单相同。未触及 7392/7377 或生产库。
- 正式保存：2026-11-18 16:20:47 +08:00；从提交到保存 28 分 35 秒。
- v1 hash：961f29f7da1df106f0c063dad3919d66273a3f45e2cf86dcef500061eaba5218。
- 截至正式保存：26 笔（18 LLM + 8 research），已知 input 732,396 / output 92,335，其中 reasoning 78,355 已包含在 output 内；8 笔研究 token unknown，费用 unknown。最终追加 1 笔非推理选图，27 笔合计 input 788,008 / output 93,163，reasoning 仍 78,355；8 笔研究 token unknown，费用 unknown。共耗时 1,952.37 秒（32 分 32 秒），没有无回包请求。

## 仍未解决

首次成稿耗时过长：本单 28 分 35 秒才正式保存，单次编辑曾耗时 292 秒，后续输入已增长至约 7.9 万 token。本次没有改变模型/推理配置或做上下文压缩，不把发布修复写成延迟达标。配图与双 PDF 生成已通过；本轮未做同会话修订、Undo 或丢失凭据后的恢复验收。

## 版本与发布

最终 V3 生产 hash：progress 184b92f21516aa79f7d4475bf366d25f9c063fbc88b065596a443b9c58b6bd93；roles 86dafbe39a975ba06897e2fa5c3db3f46345db98b1c1431f0e6115a9f29ce72b。

最终包 core_manifest_hash：4d343e6fcb0d7c2aa803cc37305364ea5f57b9720a470ab178e6a7572d76bac3。临时验收服务已加载该版本：165 个后端文件逐一匹配 manifest；真实 HTTPS 登录通过，创建按钮可用，前端 0 异常，/release.json 返回同一 core_manifest_hash。原 7392/7377 未动，旧失败单仍在原私有目录。GitHub 发布结果将续记于下方。

证据：research/writer-finalization-p0-20261118/；独立报告 review/v3-legacy-review-PASS.md；原 FAIL 保留。会话原件、凭据和 provider 原始回包留私有目录，不进 Git。

临时 HTTPS 已重启为 https://region-cognitive-beginners-hayes.trycloudflare.com/studio.html；原口令保留。服务仅切换版本，外网未再新建产品单。
