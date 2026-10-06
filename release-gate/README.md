# 文档平台发布门禁（docrelease-gate）

为文档平台建立的**发布清单与发布代次治理系统**。预览页比较、页面增删、重定向、
反馈、素材/构建资源等检查共用**同一份候选 manifest**，数据库保存**逐项证据、豁免
与发布状态**；检查之间的依赖以**依赖指纹**串联；清单批准后发生相关变更必须重新
评估；构建产物、搜索索引、路由表作为**同一发布代次**原子切换，部分失败保持旧版
完整服务。

仅依赖 Python 3 标准库，离线可运行；自带前端（可逐项定位阻断项）与 18 个验收测试。

## 为什么不能“把不同时间的绿灯拼成一份通过”

每个检查项的结果都带有 **依赖指纹**（其输入 + 所依赖检查项输出的哈希）。所有项
再汇总成一次评估运行的**聚合指纹**：

- 页面重命名 → 改变 `page_diff` → 改变 `redirect` / `deleted_migration`
  → 改变 `broken_links`（重命名后的旧链接经重定向可达，断链结果随之改变）。
- 素材撤权 → 改变 `asset_license` → 改变 `build_eligibility`
  → 改变 `artifact_integrity`。

评估在**单个长事务**内完成：预检源树 checksum → 对同一快照跑完整清单 → 核对依赖
指纹 → 提交前再次 checksum；两次不一致即 `aborted_drift`，事务中止，不产生绿灯。
签准绑定运行号、聚合指纹与 manifest 哈希；批准后的任何相关变更都会使旧签准变为
`stale`，必须重新评估、重新签准。

## 发布清单（11 项，含依赖图）

| 检查 | 阻断 | 依赖 |
|---|---|---|
| `page_diff` 预览/线上页面增删比较 | ✓ | — |
| `deleted_migration` 删除页必须有迁移/重定向 | ✓ | page_diff |
| `redirect` 重定向目标必须存在 | ✓ | page_diff |
| `broken_links` 内部断链（重命名/重定向感知） | ✓ | page_diff, redirect |
| `feedback` 变更/删除页无未解决反馈 | ✓ | page_diff |
| `asset_license` 素材授权有效、未撤权 | ✓ | — |
| `build_eligibility` 撤权素材被引用则构建不合格 | ✓ | asset_license |
| `artifact_integrity` 产物/索引/路由表校验完整 | ✓ | build_eligibility |
| `routes_table` 路由表与页面+重定向一致 | ✓ | page_diff, redirect |
| `index_coverage` 搜索索引覆盖且不落后 | ✓ | page_diff, routes_table |
| `manifest_consistency` 清单引用与产物校验和一致 | ✓ | artifact_integrity |

每项证据以 JSON 落库（如断链给出页面、目标、原因；索引落后给出缺失页与陈旧页）。
豁免与**作用域指纹**绑定，输入变化后自动失效。

## 可复验的提交协议（发布代次）

`publish` 在 `BEGIN IMMEDIATE` 长事务内分阶段执行并全程审计：

1. **幂等判定**：相同幂等键，或同一 release 已持有活动代次 → 返回既有结果。
2. **门禁与签准复核**：无阻断、事务 committed、两名不同签准人且签准未失效。
3. **暂存**：把 dist 复制进不可变目录 `generations/gen-N/`。
4. **暂存校验**：逐产物与 manifest 校验和比对；失败立即删除暂存，旧版不受影响。
5. **签名**：对候选 manifest 计算 HMAC-SHA256，写入 `manifest.signed.json`。
6. **同代次切换**：`active` 符号链接 `os.replace` 原子换入（产物+索引+路由表一起）。
7. **切换后校验**：验证签名、公开内容与已签 manifest 完全一致；任何异常都把链接
   原子还原到上一代。
8. 全部成功才提交代次记录、活动指针、幂等键与审计。

回滚在同一套签名/完整性校验下把 `active` 切回指定代次；代次内容从不删除。

## 使用

```bash
cd release-gate

# 从一个候选站点目录建立 release（站点布局见 tests/fixtures.py）
./rgctl init rel-2 ./tmp/cand
./rgctl evaluate rel-2                 # 长事务跑清单，打印每项状态
./rgctl waiver rel-2 asset_license "供应商授权已确认" --approver legal
./rgctl evaluate rel-2                 # 豁免在新运行中生效
./rgctl approve rel-2 alice            # 两名不同签准人
./rgctl approve rel-2 bob
./rgctl publish rel-2 --key deploy-2026-10-06
./rgctl verify                         # 公开资料 == 已签 manifest
./rgctl rollback                       # 回到上一代
./rgctl gate rel-2                     # JSON：阻断项/证据/签准/公开一致性
./rgctl serve --port 8080              # 前端 + API
```

前端打开后可：选择 release、触发评估、查看每项**状态/证据/依赖指纹**、对阻断项
登记豁免、双人签准、发布（带幂等键）、回滚，并看到“签准后变更已失效”的警告与
当前活动代次的公开一致性。

HTTP API：`GET /api/releases`、`GET /api/gate/{id}`、`POST /api/evaluate/{id}`、
`/api/waiver/{id}`、`/api/approve/{id}`、`/api/publish/{id}`、`/api/rollback`、
`GET /api/public`、`GET /api/audit`。

## 测试

```bash
python3 run_tests.py
```

覆盖：双人同发版（幂等/冲突）、索引落后阻断、删页无迁移、重命名改变断链、重复
发布请求、切换失败自动回滚与回滚后完整性、签准后变更强制重评、不同时间绿灯不可
拼接、依赖指纹级联、撤权改变构建资格、长事务漂移中止、公开产物被篡改被发现、
API 阻断项定位、豁免作用域失效、切换前部分失败保留旧版。
