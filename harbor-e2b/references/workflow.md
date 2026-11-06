# 运行步骤

## 工作目录与依赖

由当前任务设置 `SKILL_DIR`（本 skill）、`RUN_DIR`（skill 之外的独立工作目录）、`TASK_DIR`（Harbor 任务目录）。示例中的镜像和仓库需替换为已获授权的目标。

```bash
mkdir -p "$RUN_DIR"
uv venv "$RUN_DIR/.venv" --python 3.12
uv pip install --python "$RUN_DIR/.venv/bin/python" 'harbor==0.24.0' 'e2b==2.31.0'
```

如项目已有受管环境，复用它；不安装到系统 Python。安装 skill 时，将整个 `harbor-e2b/` 文件夹放入工具支持的 skills 目录，例如 Codex 的 `~/.codex/skills/harbor-e2b/`。

| 环境变量 | 用途 |
|---|---|
| `E2B_API_URL` | 完整 HTTPS API，如 `https://api.<region>.e2b.fc.aliyuncs.com` |
| `E2B_DOMAIN` | 与 API 对应的 domain，如 `<region>.e2b.fc.aliyuncs.com` |
| `E2B_API_KEY` | 该端点的沙箱凭据 |
| `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` | 默认构建的源仓库认证；此路径还需要转换产物 push 权限 |
| `REGISTRY_HOST` | 认证绑定的镜像仓库域名，必须与所选镜像 host 完全匹配 |

由操作者的环境/密钥管理器注入真实值，不写进命令行参数、任务包、日志或 Git。预建完成后，运行模型 agent 的进程不应继承仓库管理凭据。

## 镜像准备

先查询已有仓库配置/权限，不凭空选择外部目标。公共代理缓存能 pull 不代表能 push。选择普通可写项目和独立 tag，保留原镜像，不覆盖现有版本。

使用已有 Docker 或 OCI 工具。Docker 示例中 `SOURCE_IMAGE` 优先为 `registry/repository@sha256:...`，`DEST_IMAGE` 为已获授权的目标和新 tag：

```bash
mkdir -p "$RUN_DIR/docker-auth"
printf '%s' "$REGISTRY_PASSWORD" | docker --config "$RUN_DIR/docker-auth" login \
  "$REGISTRY_HOST" --username "$REGISTRY_USERNAME" --password-stdin
docker --config "$RUN_DIR/docker-auth" pull --platform linux/amd64 "$SOURCE_IMAGE"
docker --config "$RUN_DIR/docker-auth" tag "$SOURCE_IMAGE" "$DEST_IMAGE"
docker --config "$RUN_DIR/docker-auth" push "$DEST_IMAGE"
```

认证目录保持私有且不提交。跨仓库可能要分别认证源和目标。已有 skopeo 时也可用它复制并保留 digest，不为一次任务安装全局工具。

复制后读取目的 manifest、config 和 layers。manifest 字节相同可直接核对 SHA-256；格式转换导致 manifest digest 变化时，比较相应平台的 config/layers 并记录差异，不能只比较 tag 名。manifest index 与 linux/amd64 子 manifest 的 digest 不同。

源镜像须已包含 Dockerfile 的任务环境。不要只取第一条 FROM 而忽略 COPY/RUN。需要自己构建时，先在受控环境构建、推送；原题输入保持冻结，派生修改另存。

## 计划、预建与探针

```bash
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/e2b_template.py" plan --task "$TASK_DIR" --image "$BUILD_IMAGE"
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/e2b_template.py" build --task "$TASK_DIR" --image "$BUILD_IMAGE" \
  --receipt "$RUN_DIR/template.json"
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/e2b_template.py" probe --task "$TASK_DIR" \
  --receipt "$RUN_DIR/template.json"
```

默认模式用 `from_image(image, username=..., password=...)`，由 SDK 通过 JSON 传认证；非 ASCII 用户名不直接放进 HTTP header。构建服务会收到这些凭据，账号/robot 权限应符合用户授权范围。

`--mode micro` 仅用于已确认适配、同地域的 ACR 镜像，不能直接套用到普通私有 Harbor 镜像。两条路径的错误不能互相泛化。

脚本不改变原题 CPU/内存。磁盘不一致时返回 `RESOURCE_MISMATCH`，保留模板及实际规格。符合本次约定后，用 `probe --accept-disk-mb <实际值>` 明确接受，回执仍保留原始声明；不能用它掩盖 CPU/内存差异。

已有 alias 时新 build 会停止，避免碰撞。用先前可信回执执行 probe 复用；没有来源回执则先调查，不能仅凭名称认定镜像等价。

## 原生 Harbor 与评分

```bash
uv run --no-project --python "$RUN_DIR/.venv/bin/python" harbor trial start \
  -p "$TASK_DIR" -a oracle -e e2b --trial-name oracle --trials-dir "$RUN_DIR/trials"
uv run --no-project --python "$RUN_DIR/.venv/bin/python" harbor trial start \
  -p "$TASK_DIR" -a nop -e e2b --trial-name nop --trials-dir "$RUN_DIR/trials"
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/inspect_trial.py" "$RUN_DIR/trials/oracle" --expected-reward 1
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/inspect_trial.py" "$RUN_DIR/trials/nop" --expected-reward 0
```

保留 result、verifier stdout/stderr、reward 和 agent 日志；检查 trial 没有意外重建模板。默认删除沙箱，中断后按本次 trial 身份清理残留，不清空整个账号。

检查器只核对本地原始结果的一致性，不证明评分逻辑正确。若 agent 中断但随后有完成的 verifier 和匹配的原始 reward，输出 `SCORED_WITH_AGENT_ERROR` 并保留实际分数；退出码仍为 1，提示不能当作无错误的链路验收。

模型使用用户指定的 agent/model/provider；不要默认换成示例模型或把模型密钥当仓库密码。Oracle/Nop 不验证模型 API。扩到批量前先通过相关基线和一次真实模型调用。

成功模板可保留。确认不再需要后，仅删除本工具明确创建的模板：

```bash
uv run --no-project --python "$RUN_DIR/.venv/bin/python" python \
  "$SKILL_DIR/scripts/e2b_template.py" delete-template --receipt "$RUN_DIR/template.json"
```

## 一手资料

- [Harbor 源码](https://github.com/harbor-framework/harbor)：以安装版本的 `environments/e2b.py` 和 `environments/definition.py` 为实际依据。
- [阿里云模板构建](https://help.aliyun.com/zh/functioncompute/build-a-custom-image-template)：模式、镜像与网络要求可能变化。
- [私有镜像接入](https://www.alibabacloud.com/help/en/agent-sandbox/build-sandbox-templates-with-ghcr-custom-images)：核对 SDK 认证方式和服务端原始回执。
