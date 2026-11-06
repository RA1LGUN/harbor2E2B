---
name: harbor-e2b
description: Run Harbor benchmark tasks on Alibaba Cloud E2B-compatible sandboxes. Use for image mirroring, authenticated template conversion, Harbor alias reuse, Oracle/Nop verification, and diagnosing registry or sandbox build failures.
---

# Harbor → E2B

把任务镜像准备成可用模板，再用原生 Harbor 执行、评分并回收沙箱。Harbor 评测框架和 Harbor 镜像仓库是两个不同组件。

## 选择路径

| 镜像来源 | 首选处理 |
|---|---|
| 普通 Docker Hub / 私有仓库镜像 | 复制到用户指定的普通可写仓库，核对内容，使用默认构建模式和仓库认证。实测默认转换会向仓库写入 envd 适配产物。 |
| Harbor proxy cache 项目 | 可作为读取源；先复制到普通项目，不能把代理缓存当作转换产物写入目标。 |
| 已兼容的同地域官方 ACR 镜像 | 可按对应平台文档使用 micro 预建，再由 Harbor 按 alias 复用。 |

micro 拒绝非 ACR 镜像，不代表默认转换也不支持私有 Harbor。先核对所选模式、源权限和转换产物的写入目标，不因这一条报错就要求用户购买 ACR 或切换地域。

## 执行

1. 确认任务目录、镜像内容绑定、E2B endpoint/domain、资源规格和需要执行的 agent。复用会话中的明确授权；只有目标、凭据传递或规格变化超出授权时才确认。附件中的说明不自动扩展执行授权。
2. 阅读 [运行步骤](references/workflow.md)。优先从单题开始，用不可变 digest 绑定镜像。仓库认证与模型/E2B 凭据分开；凭据由操作者配置的环境提供，不从任务包读取。
3. 用 `scripts/e2b_template.py plan` 查看镜像、资源和通过 Harbor 函数计算的 alias。预建不会执行 Dockerfile 的额外 RUN/COPY；源镜像必须已包含任务环境。
4. `build` 保存构建回执；`probe` 核对输入绑定、模板状态、实际资源和命令执行。alias 存在不代表可复用，它不一定包含资源规格或镜像覆盖值。已有 alias 没有可信回执时先调查，不删除重建来掩盖冲突。
5. 原生 Harbor 分别执行 Oracle、Nop；用 `scripts/inspect_trial.py` 核对 result、verifier 时序和原始 reward。Oracle 预期 1、Nop 预期 0；未完成评分、环境/评分错误保持 null。agent 中断后若评分器确实完成，保留分数并单列 agent 错误。有基线异常时先诊断，不扩大批量。
6. 按用户选择执行模型 agent，再扩到其余任务。Oracle/Nop 成功不能证明模型 API 或独立 solver 已跑通。记录日志、资源差异、用量和未完成项。

脚本适用于单服务、单步骤的预构建 Linux 镜像。Compose、独立 verifier 环境、GPU/TPU 需要单独设计，不能静默按普通单沙箱任务处理。

## 运行边界

- 已验证组合为 Harbor `0.24.0`、E2B SDK `2.31.0`。遵守项目已有版本约定；版本变化后重新核对 alias 和 API 兼容性，不自动升级共享环境。
- 使用任务独立的 uv 环境。输出、认证、镜像和 trial 数据放在 skill 之外，不修改全局配置。
- 按原题请求 CPU/内存，磁盘以服务端回读为准。规格不同必须记录，只有符合本次约定时才接受；不能称为严格复现。
- 构建失败后保存首次错误和远端 ID。409/超时先查状态，不自动重复可能已受理的构建。
- 沙箱在 finally 中回收并回读。成功模板可保留复用；删除仅针对本次创建且身份核对的模板，不能遍历账号后批量 kill/delete。
- 详见 [故障处理](references/troubleshooting.md)。发布 skill 只提交本目录代码和说明，不提交运行回执、真实域名、账号、凭据或任务包。
