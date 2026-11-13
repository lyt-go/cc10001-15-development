# 网络数据采集管理平台

面向网页数据接入、定时增量采集、抽取规则管理、代理调度、限速策略、失败重试与采集结果验证的网络数据采集工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集源登记 | `datasource` | 采集源 | 源端编号、目标域名、入口URL |
| 解析规则 | `parserule` | 解析规则 | 规则编号、页面选择器、内容选择器 |
| 代理调度 | `proxyconfig` | 代理节点 | 节点编号、代理地址、代理端口 |
| 限速策略 | `ratelimiter` | 限速策略 | 策略编号、目标域名、每秒请求数 |
| 采集任务 | `taskcrawl` | 采集任务 | 任务编号、采集源名称、规则编号 |
| 预处理管道 | `pipeline` | 处理管道 | 管道编号、数据来源、字段映射 |
| RPA流程编排 | `robotflow` | 自动化流程 | 流程编号、入口页面、页面定位器 |
| 失败重试 | `retrypolicy` | 重试策略 | 策略编号、错误类型、最大重试次数 |
| 结果校验 | `resultcheck` | 校验规则 | 校验编号、目标字段、非空检查 |
| 采集告警 | `alertrule` | 告警规则 | 告警编号、监控指标、比较运算符 |
| 诊断记录 | `diagnose` | 诊断报告 | 报告编号、关联任务、异常类型 |
| 调度计划 | `scheduler` | 调度计划 | 计划编号、关联任务、cron表达式 |
| 输出配置 | `exportconf` | 输出配置 | 配置编号、输出格式、目标路径 |
| 登录态管理 | `lmcfg` | 登录凭证 | 凭证编号、目标站点、认证方式 |
| 反爬对抗 | `blacklist` | 对抗策略 | 策略编号、目标域名、UA模拟列表 |
| 采集会话 | `session` | 采集会话 | 会话编号、目标站点、凭证编号、绑定代理 |
| 采集日志 | `storagelog` | 日志记录 | 日志编号、关联任务、开始时间 |
| 模板库 | `templatelib` | 采集模板 | 模板编号、站点分类、模板名称 |
| 数据标注 | `datalabel` | 标注任务 | 标注编号、数据集名称、标注类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
