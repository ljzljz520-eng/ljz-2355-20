# 文档平台发布闸门（Release Gate）

为文档平台建立的**发布清单与可复验提交协议**：预览页对比（增删/重定向/反馈/资源），
后端对同一份候选 manifest 执行 11 项检查并把**逐项证据、豁免、发布状态落库**；
检查之间存在依赖（页面重命名会改变断链结果、素材撤权会改变构建资格），
系统用**依赖指纹 + 长事务 + 两阶段提交**保证“不同时间的绿灯不能拼成一份通过”。

仅使用 Python 3.10+ 标准库（SQLite / http.server），**零第三方依赖**。

## 快速开始

```bash
cd release-gate
python3 -m docgov.server --db ./data/gate.db --data ./data --port 8080
# 浏览器打开 http://127.0.0.1:8080
```

运行验收测试（45 条断言 + HTTP 并发冒烟）：

```bash
./run_tests.sh
# 或
python3 tests/acceptance.py
python3 tests/smoke_http.py
```

## 发布清单（11 项评估检查 + 2 项预检检查）

| 代码 | 检查内容 | 可豁免 | 关键依赖输入 |
|---|---|---|---|
| `MANIFEST_INTEGRITY` | 候选 manifest 结构、校验和自洽 | 否 | mf/pages/assets/artifacts/index/routes/redirects |
| `MANIFEST_SIGNATURE` | 作者对候选 manifest 的 HMAC 签名 | 否 | mf |
| `PAGE_DIFF` | 预览页面对比：新增/删除；**删页必须配重定向迁移** | 是（经理） | pages + baseline_pages + **redirects** |
| `BROKEN_LINKS` | 断链；链接目标含页面、重定向、素材（重命名改变结果） | 是 | pages + **redirects** + assets |
| `REDIRECTS` | 重定向目标存在、无自循环 | 是 | redirects + pages |
| `FEEDBACK_OPEN` | 阻断级反馈必须清零 | 否 | 开放反馈快照 + pages |
| `ASSET_RIGHTS` | 素材授权有效；**撤权/未知即阻断** | 否 | assets + 授权注册表快照 |
| `ASSET_REFS` | 页面引用的素材都存在 | 是 | pages + assets |
| `BUILD_ELIGIBLE` | **构建资格**：确定性构建产物一致且素材授权有效 | 否 | build + artifacts + pages + **asset_rights** |
| `INDEX_FRESH` | 搜索索引不落后：doc_ids 与产物集合一致、校验和匹配 | 否 | index + pages + artifacts |
| `ROUTES` | 路由表与产物一一对应、无悬挂 | 是 | routes + artifacts |
| `DEP_FINGERPRINTS`（预检） | 在新的长事务里**重算每项依赖指纹**，与批准时逐项核对 | 否 | 全部输入 |
| `SIGNED_CONSISTENCY`（预检） | 物化后**从磁盘回读**，公开资料与已签 manifest 字节一致 | 否 | 磁盘代次 |

## 核心机制

### 1. 同一候选 manifest，逐项证据入库
一次 `evaluate` 把 11 项检查写入 `check_rows`：状态、证据 JSON、证据哈希、
该检查消费的依赖输入指纹、行指纹。前端用证据里的 `locator_type/locator`
（如 `[PAGE] old.md`、`[INDEX] ghost.html`、`[ASSET] assets/logo.png`）
**直接定位阻断项**。

### 2. 依赖指纹：检查之间有依赖，不允许拼绿灯
- 每个检查声明 `consumes`，其输入用规范化 JSON 的 SHA-256 指纹表示。
- `BROKEN_LINKS` 同时消费 pages 与 redirects —— **页面重命名/迁移会改变断链结果**。
- `BUILD_ELIGIBLE` 消费 build 与 asset_rights —— **素材撤权会改变构建资格**。
- 清单 `seal = 所有行指纹的指纹`；批准签名绑定 `{release, mf, seal}`。
- 豁免（waiver）同样绑定授予时刻的输入指纹；相关输入一变，豁免自动失效。

### 3. 长事务里校验，预检时重核依赖指纹
- 评估在单个 SQLite `BEGIN IMMEDIATE` 长事务中读取候选、线上基线、开放反馈、
  素材授权注册表的同一冻结快照；写锁串行化所有发布操作。
- `prepare` 在另一个长事务中**重新构建快照并重跑全部检查**，逐项比对
  状态/证据哈希/依赖输入指纹；批准后发生任何相关变更（反馈、撤权、基线切换…）
  都会得到 `409 CHECKLIST_STALE`，必须**重新评估并重新批准**。

### 4. 可复验的提交协议（两阶段）+ 同代次切换
```
submit(DRAFT) → evaluate(SEALED/BLOCKED) → [waiver] → approve(APPROVED)
              → prepare(PREPARED：物化代次 + 回读签名核对)
              → publish(PUBLISHED：CAS 基线切换)
              → rollback(回到任一旧代次)
```
- 构建产物、`index.json`、`routes.json`、`manifest.json` 物化到同一个
  不可变目录 `generations/<gen>/`，**作为同一发布代次**切换。
- 切换时刻再次从磁盘回读校验；任何不一致 → 拒绝切换，**旧版保持完整服务**。
- 活动指针单行 CAS：`expected_baseline == current`，否则 `409 BASELINE_DRIFT`。
- 幂等：相同 `idem_token` 返回**同一次**切换结果；无令牌重复提交 →
  `409 ALREADY_PUBLISHED`。
- 回滚只切换活动指针到校验完好的旧代次，旧代次资料永不被覆盖。

### 5. 验收场景（tests/acceptance.py，45/45 通过）
- **两人同时发版**：两线程屏障并发提交，恰一成功，另一人 `BASELINE_DRIFT`，
  线上恰为胜出版本。
- **索引落后**：索引引用已删页面 → `INDEX_FRESH` 阻断且不可豁免。
- **删页无迁移**：`PAGE_DIFF` 定位删除页；补上重定向后通过。
- **重复发布请求**：同令牌幂等返回同代次，异令牌 `ALREADY_PUBLISHED`。
- **切换后回滚**：活动指针、产物与路由表整体回到旧代次。
- 另含：批准后反馈变更拒绝拼绿灯、素材撤权改变构建资格、重命名改变断链、
  豁免绑定指纹、落盘篡改在切换时被拒、基线漂移强制重评。

## HTTP API 摘要

```
POST /api/releases/submit      {manifest, signature}
POST /api/dev/sign             {manifest} -> {signature}      # 演示/CI 用
POST /api/releases/{id}/evaluate | /waiver | /approve | /prepare | /publish
GET  /api/releases/{id}/status | /api/releases | /api/active | /api/events
POST /api/feedback             {page_path, severity, summary}
POST /api/asset-licence        {path, licence}                # LICENSED/REVOKED/UNKNOWN
POST /api/rollback             {gen}
GET  /api/serve/<route>        # 从当前活动代次读取公开产物
```

演示密钥（仅演示，生产应由 KMS 注入）：
作者 `demo-author-key`，批准经理 `demo-approver-key`（HMAC-SHA256，`HS256.<hex>`）。

## 目录结构

```
release-gate/
├── docgov/
│   ├── canonical.py   # 规范化 JSON / SHA-256 指纹 / HMAC 签名
│   ├── manifest.py    # 候选模型、校验、确定性构建、内容寻址
│   ├── db.py          # SQLite schema 与长事务（IMMEDIATE/WAL）
│   ├── evaluator.py   # 冻结快照与 11 项检查、行指纹
│   ├── governor.py    # 状态机、豁免、批准、两阶段提交、切换、回滚
│   ├── api.py         # stdlib HTTP 路由
│   ├── server.py      # 启动入口
│   └── web/           # 前端控制台（阻断项定位）
├── tests/
│   ├── acceptance.py  # 验收场景（45 断言）
│   └── smoke_http.py  # 真实 HTTP 并发冒烟
└── run_tests.sh
```
