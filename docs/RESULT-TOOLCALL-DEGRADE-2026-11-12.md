---
type: implementation-result
date: 2026-11-12
status: offline-verified
---

# TOOLCALL-DEGRADE 执行报告

范围无偏离：仅修改能力线 worktree 的单会话格式校验处置，工具 schema、16 条上限、裁决语义与价格绑定均保留。产品调用 **0**。离线搜索使用明确的 unavailable 回执，后续保存使用明确标识的合成续写；原事故没有后续搜索结果或模型续写可回放，因此不宣称真实模型续写、真实旅行手册或线上修复已验收。未触碰 7377/生产、未合并 main、未重启 7392。

## 单根因：批量条数上限，而非某条 JSON 不可解析

事故 `8befa4f8d835f2e200b6787f897efb29` 的 `model-003-response.json` 返回 **18** 条调用（14 record_evidence、4 search）。`autopilot_tokenizer.validate_calls` 第一条规则为 `1 <= len(calls) <= MAX_TOOL_CALLS`，常量 **16**；因此在进入逐条校验前整批被拒绝。原 `_assistant` 离线重现 `_Rejected(code=invalid_tool_calls, kind=format)`，内部细项也只是 `invalid_tool_calls`。

按原解析器既有行为去除 provider `index` 元数据后，**18/18 单条协议校验通过**。第 17、18 条 search 超出批量上限；没有证据支持把此次事故归因于某条参数 JSON、类型或枚举错误，也未发现需要修改工具 schema 的自相矛盾。

原始响应 SHA256：`821a1662c8561f16c821159a66c4812bf6fab3fc52d36cf4d2fd0b8b3d8d1937`。原事故会话与空版本记录保持不动，回放使用单独目录。

## 改动

- 新增 `single_session_tool_calls.py`：仅单会话使用的逐条格式边界，保留原序号上限；协议形状、ID、重复 ID、JSON、已有 schema 类型/枚举/必填字段/额外字段/长度逐条检查，不修补或强制转换参数。其他 autopilot 调用继续使用原严格解析器。
- `single_session_agent.py`：调用新边界；执行前保存 `model-artifacts/<request>-tool-call-rejections.json` 和会话 `tool_call_rejections`。每条丢弃含请求、原序号、名称、ID、具体规则；JSON 错误含类型或行列细项。合规调用保留原参数并照常执行。整批丢弃时给合法协议反馈，继续下一轮，不伪造 tool ID/结果。
- `single_session_orch_backend.py`：阶段链路同样处理整批降级，继续原阶段流程；不修改裁决或价格绑定算法。
- 新增 `tests/test_single_session_tool_call_degrade.py`：事故回放、坏调用与正常兄弟调用、重复 ID、畸形列表、schema 错误、全批丢弃及阶段续接。

## 事故回放结果

| 项目 | 结果 |
|---|---|
| 原响应总调用 | 18 |
| 接纳并执行 | 16/16 |
| record_evidence | 14/14 完成，14 条记录被现有 DocumentStore 接受 |
| search | 2/2 进入执行；离线桩返回 `offline_unavailable`、空 results，不冒充成功搜索 |
| 原序号 17 | search，`call_16_3lLlMYHpIPFPievR2GVk6621`，丢弃：`MAX_TOOL_CALLS=16; ordinal=17` |
| 原序号 18 | search，`call_17_W5SkR4XzjfWKTTS2yGGc6604`，丢弃：`MAX_TOOL_CALLS=16; ordinal=18` |
| 后续离线续写 | 通过真实保存工具生成 Markdown/HTML v1，回放 turn=`completed` |
| 产品/网络调用 | 0 / 0 |

保存内容明确写明“离线诊断合成续写，非模型撰写的旅行交付”。该证据证明格式错误不再使此轮归零、执行及保存链路可继续；不证明原模型在下一轮必然完成手册。证据记录通过既有检查也不等于本任务重新验收其旅行事实语义。

## 检查与独立复核

- 新增专项：**8/8 PASS**。
- 既有相关回归：**72/72 PASS**，模块为 `test_single_session_agent`、`test_autopilot_model`、`test_autopilot_tokenizer`、`test_single_session_document`。
- 修改的 3 个源码文件和 1 个测试文件：**4/4 AST 语法检查 PASS**。执行契约自身不变量测试另有 **10/10 PASS**，不计入产品回归。
- 独立复核：单独上下文 `/root/review_degrade`，检查格式/安全边界、阶段恢复、台账恢复、重复 ID 与现有 schema/价格路径；最终 **PASS**，首次 7 项独立检查及末次错误细项改动的 5 项定向检查均通过；全部 7 个受审产物哈希已在交付时匹配。最终结论及哈希见下方独立报告。模型/provider 的实际身份未独立验证，不作跨模型验收声明。
- 未执行的扩展 pytest 模块：`test_orch_shape_core.py`、`test_orch_shape_integration.py`、`test_research_parse_degrade.py`、`test_search_pool_degrade.py`。现有环境缺 pytest，未安装依赖；不将这四项算作通过。阶段降级已有专项 unittest 与独立实际 backend 检查覆盖，但不等于上述完整套件通过。

首轮回归失败完整保留：日志写入默认 `~/.cache/trip-autopilot/model-boundary` 被工具实际拒绝，异常被包成 `network_error`；另有错误模块名/pytest 环境问题。最终离线 runner 仅把测试 `BOUNDARY_ARTIFACT_ROOT` 重定向到本次证据目录，并封锁 `socket.connect`，不改生产配置。最终两套日志为干净 PASS。

既有源码是 HEAD 中不存在的工作区文件，无法用 HEAD 构造可靠的本轮完整 diff。独立复核将现有 schema、执行与价格方法 AST 和既有 20261109 备份对比，并直接检查当前调用路径；此对比不冒充同轮修改前快照。

## 证据与复跑

全部实现、测试和证据位于能力线：`/Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024`。

- [单根因复现](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/diagnosis.json)
- [实现及测试账目](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/implementation-summary.json)
- [事故回放逐条账目](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/incident-replay/offline-replay-result.json)
- [专项最终日志](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/format-regression-final.log) / [相关回归最终日志](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/related-regression-final.log)
- [独立复核](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/independent-review.json) / [最终哈希核对](../../travel-compare-card-20261024/research/toolcall-degrade-2026-11-12/review-hash-check.json)

```sh
cd /Users/duan/Documents/kimi/Workspaces/travel-compare-card-20261024
PYTHONPYCACHEPREFIX=/private/tmp/toolcall-degrade-repeat /Users/duan/.cache/trip-autopilot/compare-card-20261024/venv/bin/python -m unittest tests.test_single_session_tool_call_degrade -v
PYTHONPYCACHEPREFIX=/private/tmp/toolcall-degrade-repeat /Users/duan/.cache/trip-autopilot/compare-card-20261024/venv/bin/python research/toolcall-degrade-2026-11-12/run_related_tests.py
```

CodeGraph 调用被审批策略拒绝后采用本地源码读取。`mem-search`、`openclaw_chat` 未在可用工具中发现，因此未通过它们查询/发送；最终结果写入本指定报告，不外发。检查点：能力线 `docs/TOOLCALL-DEGRADE-checkpoint-2026-11-12.json`。
