# .env 配置说明（matrix-open-stack）

本文说明 `matrix-open-stack/.env` 每个字段的用途、推荐值与常见组合。

> 基本用法：
> 1. `cp .env.example .env`（或 `cp .env.secure.template .env`）
> 2. 按需修改字段
> 3. `docker compose up -d --build`

## 必填（至少要设置）

- `MATRIX_SERVER_NAME`：你的 Matrix 域名（例：`matrix.biglone.tech`）
- 管理员鉴权二选一：
  - `MATRIX_ADMIN_USER` + `MATRIX_ADMIN_PASSWORD`
  - 或 `MATRIX_ADMIN_TOKEN`
- `CONTROL_API_TOKEN`：管理 API 的 Bearer Token（强随机）

## 字段逐项说明

| 字段 | 默认值 | 作用 | 何时修改 |
|---|---|---|---|
| `PUID` | `1000` | 容器内进程运行用户 ID | 宿主机文件权限不一致时 |
| `PGID` | `1000` | 容器内进程运行组 ID | 宿主机文件权限不一致时 |
| `MATRIX_PORT` | `6167` | Matrix 服务绑定到本机端口（127.0.0.1） | 端口冲突时 |
| `MATRIX_CONTROL_PORT` | `6180` | 管理后台/API 绑定本机端口（127.0.0.1） | 端口冲突时 |
| `MATRIX_SERVER_NAME` | `matrix.example.com` | Matrix 服务器域名（影响用户 ID 后缀） | 上线前必须改 |
| `CONDUWUIT_VERSION` | `v0.4.6` | Conduwuit 版本标签 | 升级/回滚时 |
| `MATRIX_ADMIN_USER` | `admin` | 管理员用户名（用于 API 自动登录拿 token） | 使用账号密码模式时 |
| `MATRIX_ADMIN_PASSWORD` | `change-me` | 管理员密码 | 使用账号密码模式时 |
| `MATRIX_ADMIN_TOKEN` | 空 | 管理员 Access Token（有值时优先使用） | 想避免频繁登录时 |
| `CONTROL_API_TOKEN` | `change-me-to-a-long-random-token` | 保护 `/api/*` 的 Bearer Token | 生产必须强随机 |
| `EXPOSE_BOT_ACCESS_TOKEN` | `false` | `/api/bots` 是否返回机器人 access_token | 一般保持 `false` |
| `EXPOSE_USER_ACCESS_TOKEN` | `false` | `/api/users` 是否返回用户 access_token | 一般保持 `false` |
| `BOT_CREATE_MODE` | `disabled` | 机器人 API 创建模式：`disabled` / `legacy_register` | 需要开放 API 建机器人时 |
| `USER_CREATE_MODE` | `disabled` | 普通用户 API 创建模式：`disabled` / `legacy_register` | 需要开放 API 建用户时 |
| `AUDIT_LOG_PATH` | `/var/log/matrix-control/audit.log` | 控制面审计日志路径 | 自定义日志目录时 |
| `FULL_USERS_SNAPSHOT_PATH` | `/var/log/matrix-control/full-users-snapshot.json` | 全量用户快照路径 | 自定义快照目录时 |
| `BOT_STATE_PATH` | `/var/log/matrix-control/bot-state.json` | 机器人逻辑状态存储路径 | 自定义状态目录时 |
| `USER_STATE_PATH` | `/var/log/matrix-control/user-state.json` | 普通用户逻辑状态存储路径 | 自定义状态目录时 |
| `INVITE_RATE_LIMIT_WINDOW_SECONDS` | `60` | 邀请限流时间窗（秒） | 邀请频率策略调整时 |
| `INVITE_RATE_LIMIT_MAX` | `12` | 时间窗内最大邀请次数 | 邀请频率策略调整时 |
| `DOCKER_GID` | `979` | 管理页重启功能访问 `docker.sock` 的组 ID | 主机 `docker.sock` 组变化时（`stat -c '%g' /var/run/docker.sock`） |
| `RESTART_API_MODE` | `disabled` | 管理页服务重启模式：`disabled` / `docker_socket` | 需要在管理页触发重启时设为 `docker_socket` |
| `REGISTRATION_WINDOW_API_MODE` | `disabled` | 管理页临时创建窗口模式：`disabled` / `docker_socket` | 需要在管理页一键开/关临时创建窗口时设为 `docker_socket` |
| `REGISTRATION_WINDOW_DEFAULT_MINUTES` | `10` | 临时创建窗口默认分钟数 | 默认时长需要调整时 |
| `REGISTRATION_WINDOW_MAX_MINUTES` | `60` | 临时创建窗口最大分钟数 | 需要限制最大开放时长时 |
| `REGISTRATION_WINDOW_STATE_PATH` | `/var/log/matrix-control/registration-window-state.json` | 临时窗口状态持久化路径 | 自定义状态目录时 |
| `DOCKER_SOCKET_PATH` | `/var/run/docker.sock` | Docker Engine Socket 路径 | Docker socket 非默认路径时 |
| `COMPOSE_PROJECT_NAME` | `matrix-open-stack` | Compose 项目名（用于定位容器名） | 目录名变化或自定义 project name 时 |
| `RESTART_TIMEOUT_SECONDS` | `20` | 单个容器重启超时（秒） | 容器停机较慢时 |
| `STACK_HOST_PATH` | 空 | 宿主机项目绝对路径（用于 UI 临时窗口修改 conf） | 项目目录变化时必须更新 |
| `HOST_HELPER_IMAGE` | `local/matrix-control-api:0.1.0` | 控制面执行宿主机配置修改的 helper 镜像 | 镜像标签变更时 |

## 推荐配置方案

### 1) 安全优先（推荐生产）

```env
BOT_CREATE_MODE=disabled
USER_CREATE_MODE=disabled
EXPOSE_BOT_ACCESS_TOKEN=false
EXPOSE_USER_ACCESS_TOKEN=false
```

- 用户/机器人创建走本机安全脚本：
  - `./scripts/create_bot_secure.sh ...`
  - `./scripts/create_user_secure.sh ...`
- 不开放公网注册（`conf/conduwuit.toml` 已默认 `allow_registration=false`）

### 2) 便利优先（仅内网或短期）

```env
BOT_CREATE_MODE=legacy_register
USER_CREATE_MODE=legacy_register
```

同时需注意：Conduwuit 侧注册策略要匹配，否则 API 创建会失败。

## 常见问题

### `MATRIX_ADMIN_TOKEN` 不填会怎样？

- 不会立刻坏。
- 只要 `MATRIX_ADMIN_USER` + `MATRIX_ADMIN_PASSWORD` 正确，控制面会自动登录拿 token。
- 若两者都没有，涉及 Matrix 操作的 API 会失败。

### 为什么改了 `.env` 没生效？

改完后重启服务：

```bash
docker compose up -d --build
```

### 管理页“服务重启”为什么不可用？

- 默认是关闭的（`RESTART_API_MODE=disabled`）。
- 开启方式：
  - `RESTART_API_MODE=docker_socket`
  - `DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)`
  - 重建控制面容器：`docker compose up -d --force-recreate matrix-control-api`

### 想临时开放注册（用户/机器人）后自动关回去？

使用：

```bash
./scripts/open_registration_window.sh --minutes 10
```

脚本会在窗口期临时启用注册与 API 创建模式，结束后自动恢复为安全配置。

也可以在管理页 `服务重启` -> `临时创建窗口` 直接操作（需先设置 `REGISTRATION_WINDOW_API_MODE=docker_socket` 和 `STACK_HOST_PATH`）。

## 安全提醒

- 不要提交 `.env`、`data/`、cloudflared 凭据文件
- `CONTROL_API_TOKEN` 必须使用高强度随机值
- 建议给 `matrix-admin` 域名再叠加 Cloudflare Access
