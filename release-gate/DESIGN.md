# 设计说明：数据模型与不变量

## 数据表
- `releases`：候选 manifest（canonical JSON）、源目录、当前运行、状态。
- `check_runs`：evaluate/publish 运行、聚合指纹、pre/post 源树 checksum、
  `committed` / `aborted_drift`、passed。
- `check_items`：每项名称、标题、pass/fail/waived、是否阻断、**逐项证据 JSON**、
  明细、依赖指纹、关联豁免。
- `waivers`：豁免理由、审批人、**scope_fprint**（仅在输入未变时有效）、过期时间。
- `approvals`：签准人、绑定 run_id、签名 manifest 哈希、聚合指纹（(release,人) 唯一）。
- `generations`：不可变代次目录、签名封套、状态 staged/active/superseded/rolled_back。
- `active`：单行活动代次指针；`publish_intents`：幂等键去重；`audit`：审计追踪。

## 关键不变量
1. 单次运行只读一个快照、一份候选 manifest；项结果按依赖图拓扑序计算。
2. 项的 dependency fingerprint = H(其证据 + 依赖项指纹)；聚合指纹 = H(全部项指纹)。
3. 评估事务 pre/post 树 checksum 必须一致，否则中止，不允许签准/发布。
4. 有效签准必须满足：run_id 最新、聚合指纹与 manifest 哈希均为当前值，且人数≥2
   （不同签准人）。批准后相关变更 → 旧签准进入 `stale_approvals`。
5. 发布门禁复核：无 fail 项（waived 可放行）、状态 committed、双人签准有效。
6. 活动代次的产物、索引、路由表来自同一暂存目录，随一个符号链接原子切换。
7. 切换点之后的任何失败都还原链接到上一代；切换点之前的失败不触碰活动链接。
8. `verify` 要求：HMAC 签名有效、公开产物校验和与 manifest 一致、签名代次==活动代次。
9. 幂等：成功切换才记录幂等结果；失败/拒绝回滚且不占用幂等键；并发不同 release
   由进程内互斥 + `BEGIN IMMEDIATE` 串行化，后来者收到 409。

## 指纹与签名
- canonical JSON：键排序、紧凑分隔、UTF-8。
- `fprint(namespace, obj)` = sha256(namespace | canonical(obj))。
- manifest 签名：HMAC-SHA256(secret, canonical(manifest))，随封套发布并复验。
