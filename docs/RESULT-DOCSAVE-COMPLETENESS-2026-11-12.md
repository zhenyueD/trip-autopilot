# RESULT-DOCSAVE-COMPLETENESS-2026-11-12

六次相同拒绝的单一直接根因是 `markdown` 的 `destination-detail-v1` 块缺少末尾独占一行的关闭围栏（不是 intake、JSON 括号、行长或自引用问题）；旧提取器在进入字段校验前统一抛出 `malformed or unsupported block`，导致模型无法定位。修法：保留严格围栏/JSON/价格协议，所有结构校验错误补具体路径与规则；保存提交前及 API completed 转换前增加日期覆盖、非空标题/摘要、非空活动集的结构闸门。按 CMD 允许的“等价完整两日文档”验收：补围栏、修正原稿中独立存在的预算列表数字与未绑定历史价格后保存成功，完整 `days` 对象与现场首份 payload 完全相同。无架构、协议、intake 或渲染层改动；未自动补全/宽容接收原始缺围栏文本。账目影响：原 23 条证据台账不变，价格 marker、单价/人数/总额未改；只删去未绑定旧价格断言及金额列表中的数量文字。没有调用真实模型、外网、7377、生产或合入 main。

## 状态和交付

状态：**IMPLEMENTED；专项离线验收及独立复核 PASS；既有回归未全绿**。本任务 CMD 的复现、修复、等价文档保存、占位桩阻断、字段错误断言与回归如实记录均已执行。不是上线、真实模型联调或产品整体完成声明。旧已 completed 的现场记录未迁移/覆盖。

代码位于工作树 `/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024`；仅本报告写入主仓。以下证据链接均指向工作树。

## 根因证据

原现场：`research/session-frontend-2026-11-04/api-sessions/04baf872bca1fe375b3e636e045681fe/session/`。

- 逐份从 `conversation.json` 的七个 `save_handbook` 调用提取参数：前六份长度分别为 11281、11362、11319、11319、11319、12633 字符，均有 opening fence、无 closing fence；其块内 JSON 均可直接 `json.loads`。
- 修改前实际调用现有 `extract()`：六份均复现原 `ValueError: destination detail: malformed or unsupported block`。第七份 202 字符的占位桩原先通过。
- 在旧校验下仅补关闭围栏：前两份进一步报预算 `estimated_items` 未绑定价格，后四份报 `verified_items` 未绑定价格。这是被围栏错误遮住的独立后续拒绝，未把它们伪称为同一个根因。
- 首份原稿 `estimated_items` 含“酒店1晚待比价”“餐饮4餐待定”；既有金额字段规则禁止未绑定数字。修正版改为“酒店待比价”“餐饮待定”。正文历史票价段中的 `45元/人`、`20元`、`45元`、`18元一杯、素包2元` 改成不带金额的历史描述；未引入新事实。
- 同一两日日程（2026-12-12、2026-12-13）与所有活动、景点、时间轴完整保留。独立复核逐对象确认 `days` 完全相等。

[修改前七次复现](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/reproduction.json)

## 实现范围

- `single_session_detail.py`：围栏、JSON 语法、版本、数组、ID、路线端点、数字、时间、餐饮价格等错误提供 JSON 路径和规则名。示例：`$.markdown.destination-detail-v1 [closed_fenced_block]`、`$.days[1].timeline[0].time [clock_hhmm]`；JSON 语法含行列号。
- `single_session_context.py`：overview 校验逐层传递路径，如预算档/列表元素和日照事件索引；保持原校验条件及渲染代码。
- 新 `single_session_completeness.py`：完整日期区间要求恰好一日对应一个日期、无重复/缺漏/越界；每一天标题与 summary 必须为非空文本；stops 与 timeline 不可同时为空；手册 H1 必须同一行非空。仅集合、日期和字段存在性核对，无语义评审或关键词黑名单。
- 仅给一侧日期的现有合法 intake 仍支持：只检查已给边界与唯一日期，不推测另一端。无日期的旧纯文字文档保留旧行为；带日期的输入必须提供结构化天数才能证明覆盖。
- `DocumentStore._save` 在渲染与版本提交之前验证；失败不会改变已保存版本。`SessionService._run` 在模型回执/版本 hash 核对后、PDF 与 completed 之前再次验证，即使 agent 自称 saved/completed 也不能绕过。
- 既有 overview/API/supply 测试夹具补足其非空逐日结构；编排供给测试明确设置其需要的 `TRAVEL_ORCH=1`，避免在普通工具协议中无限重试 `finish_stage`。未修改编排生产代码。

“测试”字符串本身仍合法；专项测试明确验证标题/summary 写“测试”而结构完整时通过。现场桩失败是活动集为空且日期不完整，不是因为关键词。

## 离线保存及失败恢复

[修正后的完整输入](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/replay/corrected.source.md)、[保存快照](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/replay/documents/version-0001.json)、[生成 HTML](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/replay/documents/document-0001.html)。

保存源 SHA-256：`9dc98c2363cb79699e9ff8b94da3c1d6d67f66f1f82eb24f79727afc24e7b987`。

保存使用现场 ledger 的副本，未获取任何新来源。测试用临时目录重放同一保存并核对 current hash/两日数据/HTML 文件存在；失败修订测试确认旧版本 hash 保持不变。存档现场只读，未修写原会话。

## 验证账目

| 检查 | 结果 | 证据 |
|---|---|---|
| 新专项 suite | 12/12 PASS | focused-final.log |
| 六份现场拒绝逐项断言 | 6/6 命中具体围栏路径和规则 | test_six_archived_failures_identify_fence_rule |
| 现场桩实际 Agent 工具路径 | rejected、turn failed、无 current 文档，provider budget=0 | test_archived_stub_never_completes_real_agent_turn |
| API 防绕过 | fake agent 自称 completed，实际 trip failed、revision=0，PDF 不执行 | test_api_rejects_saved_stub_even_if_agent_claims_completed |
| 独立反例及审查 | PASS，7 个独立结构反例符合预期 | review.md |
| 既有单会话 suite | 133 项：123 PASS、10 ERROR | regression-complete.log |
| 修改的 10 个 Python 文件语法 | AST parse PASS | verification.json |

10 个既有回归 ERROR 不计通过：

1. `test_single_session_api.SessionAPITests` 的 9 项在 `SessionHTTPServer` 的 socket bind 阶段报 `PermissionError: [Errno 1] Operation not permitted`，是实际工具运行中观察到的本地端口权限限制；不影响直接 service 层专项反例测试。
2. `test_single_session_supply.SupplyWiringTests.test_full_offline_turns_across_api_session_layout` 在 `_validate_decisions` 报 `adjudication requires only accepted, rejected and decisions`。旧 stub 只返回 accepted/rejected，缺 decisions；错误发生在文档保存之前。未将该编排 fixture 协议问题扩展为本任务生产修复；未声称完成跨阶段回归。

环境与执行过程：系统 Python 缺 pytest/httpx2；主仓 `.venv` 缺 openai，部分旧 pycache 读取卡住。最终使用本地 Python 3.13.12 和 uv 已缓存依赖，临时 pycache 路径，不安装、不联网。版本：openai 3.37.0、pydantic-ai-slim 2.61.0、pydantic 2.14.1、httpx2 2.16.0、tokenizers 0.23.3。早期失败和中断日志保留，不作为通过证据。

复现命令（从工作树执行）：

```sh
/Users/duan/.local/share/uv/python/cpython-3.13.12-macos-aarch64-none/bin/python3.13 -X pycache_prefix=/private/tmp/docsave-pycache research/docsave-completeness-2026-11-12/run_offline_tests.py 'test_docsave*.py'
/Users/duan/.local/share/uv/python/cpython-3.13.12-macos-aarch64-none/bin/python3.13 -X pycache_prefix=/private/tmp/docsave-pycache research/docsave-completeness-2026-11-12/run_offline_tests.py 'test_single_session*.py'
```

独立 reviewer：`/root/independent_review`，fresh bounded context、只读实现、有否决权。初审发现跨行空标题绕过及单侧日期兼容问题，两项均修正后再验；完整 FAIL→PASS 记录和 source hash 绑定在 [review.md](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/review.md)。实际 canonical model/provider 不可核验，不归因于具名外部模型。最终重新计算全部 9 项 review artifact hash 均匹配。

## 约束、例外与恢复

- CodeGraph 调用被当前审批策略实际拒绝，因此用本地文件检查。列出的全局 `~/.Codex/rules/common/*.md` 不存在。未发现可调用的 mem-search/openclaw_chat 工具；未外发消息。
- Git status/diff 在本环境未返回，因此没有声称全仓 clean 或提交完成。所有改动限定在上述单会话路径、测试与本任务证据；无 commit、merge、push。
- 恢复游标/清单：[state.json](/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024/research/docsave-completeness-2026-11-12/state.json)。后续仅需在允许绑定 loopback 的环境复跑 9 项 HTTP 测试，并单独修订旧编排测试的 adjudication fixture；当前未把它们计为 PASS。
- 本 CMD 要求的结构性修复已完成；不宣称事实、文案语义质量、浏览器视觉或 PDF 真实渲染验收。
