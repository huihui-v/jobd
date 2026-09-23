# jobd

边缘上的薄任务代理：收一段 bash，后台跑，查状态，拉日志，取消。Python 3 标准库，无第三方依赖。本仓库 pull 下来即可起。

不提供 shell / PTY / stdin。不 source `~/.bashrc`。不自动重跑失败任务。一机一个活任务。不负责开机、clone、写 key。

## 起

```bash
python3 jobd.py --bind 127.0.0.1 --port 18789 \
  --token "$HKPC_JOB_TOKEN" \
  --machine-env /root/.machine.env \
  --workdir /root/hkpc-job
```

`--token` 必填。`--machine-env` 缺省 `/root/.machine.env`。数据目录缺省 `/root/hkpc-job`（`current/user.sh`、`current/job.env`、`current/job.log`、`current/status.json`）。

进程挂掉不得带走已 `setsid` 的子任务。

## 环境

每次跑用户脚本前，wrapper 在同一进程里：

1. `set -a; source --machine-env; set +a`（文件不存在则跳过）
2. `set -a; source $workdir/current/job.env; set +a`（本趟 POST 写入）
3. `cd` 到 `$WORKSPACE_ROOT/AIGCTeam_comfy_boot`（`WORKSPACE_ROOT` 来自上一步；未设置则保持当前目录）
4. `exec stdbuf -oL -eL bash user.sh`

禁止 `bash -i`、`bash -lc`、`source ~/.bashrc`。

`job.env` 只允许 `ENV_FILE`、`ENV_VERSION`（及请求里显式给出的同名键）。不要把 host 的整份环境灌进去。

`machine.env` 由机主准备，例如：

```bash
export WORKSPACE_ROOT=/root/autodl-tmp
```

jobd 不创建、不修改这个文件。

## HTTP

`Authorization: Bearer <token>`，否则 401。

已有 running 时 `POST /jobs` → 409 + 现有 `job_id`。`force=1` 时先 cancel 再提交。

### POST /jobs

```json
{
  "script": "#!/bin/bash\n…",
  "env": {
    "ENV_FILE": "/abs/path/env_xxx.conf",
    "ENV_VERSION": "bbb-v2"
  },
  "name": "provision-start"
}
```

`script` 原样写入 `user.sh`。`env` 写入 `job.env`（`export KEY=value`，值安全单引号）。handler 不得等待脚本结束。立刻返回 `{ "job_id", "status": "running" }`。

### GET /jobs/current

`job_id`、`name`、`status`（`running`|`exited`|`failed`）、`pid`、`exit_code`、`started_at`。pid 不在则按 `exit_code` 标 exited/failed。无任务 404。

### GET /jobs/current/log?offset=

从该字节读 `job.log`；响应 JSON：`data`、下一 `offset`。无任务 404。

### POST /jobs/current/cancel

杀进程组。无活任务 404。

## 运行约定

- stdout+stderr 同一日志文件
- 取消杀整个 process group
- 不解释脚本内容，不包 source bashrc，不改用户脚本
- 活任务与 jobd 生命周期解耦：重启 jobd 后 GET 仍能对上 pid/日志（凭 `status.json` + pid 探测）

## 验收

```bash
python3 tests/test_jobd.py
```

覆盖：无 token → 401；`echo $WORKSPACE_ROOT` 等于 `--machine-env` 里的值；`ENV_VERSION=bbb-v2` 在脚本与 `ENV_VERSION=$ENV_VERSION cmd` 都能读到；杀掉 jobd 后用户脚本仍在，再起 jobd 能续读日志、状态仍对；running 时第二次 POST → 409；cancel 后子进程一并结束。
