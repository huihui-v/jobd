# jobd

边缘上的薄任务代理：收一段 bash，后台跑，查状态，拉日志，取消。Python 3 标准库，无第三方依赖。本仓库 pull 下来即可起。

不提供 shell / PTY / stdin。不 source `~/.bashrc`。不自动重跑失败任务。一机一个活任务。不负责开机、clone、写 key。

## 起

```bash
python3 jobd.py --bind 127.0.0.1 --port 6006 \
  --token "$HKPC_JOB_TOKEN" \
  --machine-env /root/.machine.env \
  --workdir /root/jobd
```

`--token` 必填（也可环境变量 `HKPC_JOB_TOKEN`，守护启动用这个，避免出现在 `ps`）。`--port` 缺省 `6006`。`--machine-env` 缺省 `/root/.machine.env`。数据目录缺省 `/root/jobd`（与仓库同目录，`current/` 不进 git）。不传 `cwd` 时脚本在 `/root` 下跑。

进程挂掉不得带走已 `setsid` 的子任务。

## 守护启动

token 写在 `jobd.env`（mode 600，不进 git）：

```bash
cp jobd.env.example jobd.env
chmod 600 jobd.env
# 填 HKPC_JOB_TOKEN=
./daemon.sh install
./daemon.sh start
./daemon.sh status
./daemon.sh stop
```

有 systemd：装 `jobd.service`，`Restart=always`，`KillMode=process`（杀 jobd 不带走 setsid 子任务）。没有 systemd（多数 Vast/Docker）：`nohup` + `jobd.pid`。

## 环境

每次跑用户脚本前，wrapper 在同一进程里：

1. `set -a; source --machine-env; set +a`（文件不存在则跳过）
2. `set -a; source $workdir/current/job.env; set +a`（本趟 POST 写入）
3. 请求带了 `cwd` 则 `cd` 过去；不传则 `cd /root`（`--default-cwd`）
4. `exec stdbuf -oL -eL bash user.sh`

禁止 `bash -i`、`bash -lc`、`source ~/.bashrc`。

`job.env` 只允许 `ENV_FILE`、`ENV_VERSION`（及请求里显式给出的同名键）。不要把 host 的整份环境灌进去。

`machine.env` 由机主准备，例如：

```bash
export WORKSPACE_ROOT=/root/autodl-tmp
```

jobd 不创建、不修改这个文件。source 之后，里面 **所有 export 的变量** 都能在请求的 `cwd` 和 `env` 值里用 `$VAR` / `${VAR}` 展开（在 source machine.env 之后、source job.env / cd 时由 bash 展开）。不接受 `$()`、反引号、`${VAR:-x}` 这类替换。

## HTTP

`Authorization: Bearer <token>`，否则 401。

已有 running 时 `POST /jobs` → 409 + 现有 `job_id`。`force=1` 时先 cancel 再提交。

### POST /jobs

```json
{
  "script": "#!/bin/bash\necho \"hello\"\n",
  "env": {
    "ENV_FILE": "${WORKSPACE_ROOT}/env_xxx.conf",
    "ENV_VERSION": "bbb-v2"
  },
  "cwd": "${WORKSPACE_ROOT}/AIGCTeam_comfy_boot",
  "name": "provision-start"
}
```

`script` 原样写入 `user.sh`。`env` 写入 `job.env`。可选 `cwd`：展开后 `cd` 到该目录；不传则 `/root`。handler 不得等待脚本结束。立刻返回 `{ "job_id", "status": "running" }`。

请求体是 **JSON**。`script` 里的双引号写成 `\"`，换行写成 `\n`。用语言自带的 JSON 编码即可，不要手拼。curl 示例：

```bash
python3 -c 'import json,sys; json.dump({"script":"echo \"hi\"\n","cwd":"${WORKSPACE_ROOT}/AIGCTeam_comfy_boot","env":{"ENV_FILE":"${WORKSPACE_ROOT}/env_xxx.conf","ENV_VERSION":"bbb-v2"}}, sys.stdout)' \
    | curl -sS -H "Authorization: Bearer $HKPC_JOB_TOKEN" -H 'Content-Type: application/json' \
      --data-binary @- http://127.0.0.1:6006/jobs
```

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
