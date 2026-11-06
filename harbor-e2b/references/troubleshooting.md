# 按失败层定位

| 观察 | 能说明什么 | 下一步 |
|---|---|---|
| 模型列表 200 | 列表接口可读 | 实际推理另测，不能称 solver 成功 |
| Registry token 200 | token 请求被受理 | 核对 repository/actions 并实际读写，不能当作 push 成功 |
| `registry credentials are required` | 转换路径缺少仓库认证 | 检查 from_image 认证；区分源 pull 与目标 push |
| `push ... UNAUTHORIZED` | 转换产物写回被拒 | 检查是否使用只读代理缓存；复制到普通可写项目或明确合法输出目标 |
| Registry 请求出现 `CSRF token not found` | 自制客户端可能意外携带 portal cookie | 使用标准 OCI 客户端，或分开 Bearer 与 cookie 会话；不关闭服务端 CSRF |
| micro 下 `Image not stored in ACR is not supported yet` | 当前直用路径不接受该来源 | 普通 Harbor 镜像可试已授权的默认认证转换，不推断所有模式都必须手工搬 ACR |
| `CREATE_FAILED` / 一个 alias 只能 build 一次 | 可能在重试失败模板 | 读首次 build 日志，解决原因后仅清理有归属证据的失败模板 |
| 500 / timeout | 请求结果未确定或服务端失败 | 保存 ID 并查状态，不推断资源不足、不盲目重发 |
| alias 存在但资源/镜像不符 | 旧模板被复用，构建参数未重新生效 | 核对可信回执、内容与资源；alias 不证明等价 |
| 模板 ready、首次命令失败 | 控制面 ready 不等于数据面可用 | 保留错误，只有无副作用探针可在有限时限内重试 |
| reward 缺失、verifier 报错/超时 | 无有效评分 | 保留 null，查看原始 test stdout/stderr |
| Oracle=1、Nop=0 | 两个控制运行成功 | 不能泛化到整个数据集、独立模型或线上平台已验收 |

## 重试与资源

- 创建已受理但本地没拿到 ID 时，不认领同 alias 的任意模板，也不自动删除它。先保留不确定性并调查远端。
- 同一协调流程中串行处理相同 alias 的首次构建；独立任务并发遵守用户和平台限制。
- 改镜像域名不等于同步了另一地域。实际检查 manifest、版本和 digest。
- 平台规格不同必须记录；严格复现要求不满足时停止评分，不静默降低内存或扩大资源。
- 模型超时、结构化响应无效、结果提交被拒分别记录；保留已发生调用的用量，不自动重复付费实验。

## 清理

默认回收本次沙箱，成功模板按需要保留。删除前核对 endpoint、template ID、build ID 与 alias；DELETE 后回读确认消失。`spawnCount` 等字段可能不更新，应使用实际创建、命令、kill 与列表回读证明生命周期。

保留 source tag 与转换产物 tag。模板清理不等于允许删除源镜像、整个项目或其他人的沙箱。
