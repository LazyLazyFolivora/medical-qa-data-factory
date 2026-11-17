# Medical QA Data Factory

医学问答数据生产工作台。从生物医药实体出发，调用模型与证据来源完成研究、出题、评审和验证，保存执行轨迹，导出问答数据集。

当前开发分支为 `dev`。本文面向当前代码的安装、部署、升级和恢复；历史设计与实验结论见 [文档索引](docs/medical-qa-data-factory/README.md)。

## 功能与组成

- 自动出题：按目标题数与并发实体数启动批次，查看进度、暂停与继续。
- 题目管理：按批次或全局筛选已发布题目，导出 JSONL。
- 单实体工作台：查看实体任务、证据与运行轨迹。
- PostgreSQL：保存持续生产的批次、实体历史及题目索引。
- 文件工作区：保存证据、检查点、运行轨迹与发布文件。
- Vue 前端与 Python API 分开运行；后端不托管前端页面。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| Python | 项目声明 Python ≥ 3.10，建议使用独立的 Python 3.12 环境 |
| Node.js | 当前 Vite 要求 `^20.19.0` 或 `>=22.12.0` |
| PostgreSQL | 可连接的实例；持续自动出题必须配置数据库 |
| 模型服务 | 可访问的 OpenAI-compatible 服务，按实际服务填写配置 |
| 数据来源 | 准备 PrimeKG 数据库，并按所用来源配置网络与凭据 |

依赖范围在 `requirements.txt`，固定版本在 `constraints.txt`；前端使用 `frontend/package-lock.json`。

## 1. 获取代码与安装依赖

首次获取：

```bash
git clone --branch dev git@github.com:LazyLazyFolivora/medical-qa-data-factory.git
cd medical-qa-data-factory
```

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
export PYTHONNOUSERSITE=1
export PYTHONPATH="$PWD/src"
python -m pip install -r requirements.txt -c constraints.txt
```

Windows PowerShell（Conda）：

```powershell
conda create --name qa_env python=3.12 pip -y
conda activate qa_env
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONPATH = "$PWD/src"
python -m pip install -r requirements.txt -c constraints.txt
```

每次进入新终端，都要重新激活环境并设置 `PYTHONPATH`。禁用用户级 Python 包可以避免全局安装的依赖干扰项目。

安装前端依赖：

```bash
cd frontend
npm ci
cd ..
```

## 2. 配置数据库、模型和数据源

以下文件是本机配置，不提交 Git。复制模板只在首次配置时执行，不要覆盖服务器现有文件。

Linux：

```bash
cp .env.example .env
cp config/models.example.json config/models.json
```

PowerShell：

```powershell
Copy-Item .env.example .env
Copy-Item config/models.example.json config/models.json
```

### PostgreSQL

在项目根目录的 `.env` 中填写：

```dotenv
MEDICAL_QA_PRODUCTION_DATABASE_URL=postgresql://qa_app@127.0.0.1:5432/medical_qa
```

该示例未包含密码；可使用 PostgreSQL passfile，或者按部署环境配置连接凭据。若密码写入连接 URI，应正确进行 URL 编码。

- 环境变量优先于 `.env`；数据库配置从启动时的当前目录读取 `.env`。
- 可让管理员预先创建数据库并授予应用账户所需权限。
- 数据库不存在时，迁移命令会尝试连接同服务器的 `postgres` 管理库创建数据库，此时账户需要 `CREATEDB` 权限。
- 迁移账户需要在目标数据库创建 schema 和表的权限。

### 模型与 PrimeKG

编辑 `config/models.json`，设置 `default_provider`，并填写对应 provider 的 `base_url`、`model_id`、认证方式与结构化输出模式。示例中的 `base_url` 是完整的 chat completions 地址。

需要认证时，`api_key_env` 填环境变量名；将密钥注入后端进程环境。无需认证的自部署服务使用 `auth_mode: "none"`。

Linux 示例：

```bash
export MEDICAL_QA_PRIMEKG_PATH=/srv/datasets/PrimeKG/primekg.sqlite
export MEDICAL_QA_MODEL_CONFIG="$PWD/config/models.json"
# 若所选 provider 的 api_key_env 为 MEDICAL_QA_MODEL_API_KEY：
# 将 MEDICAL_QA_MODEL_API_KEY 配置到后端进程环境。
```

PowerShell 示例：

```powershell
$env:MEDICAL_QA_PRIMEKG_PATH = 'D:/datasets/PrimeKG/primekg.sqlite'
$env:MEDICAL_QA_MODEL_CONFIG = "$PWD/config/models.json"
```

本项目的 PostgreSQL 配置读取 `.env`，不代表所有模型和来源环境变量都会自动从该文件加载。服务部署时应显式注入所需变量。

诊断配置：

```bash
python -m medical_qa_data_factory model providers
python -m medical_qa_data_factory kg health --provider primekg
```

可选全文下载能力见 [配置说明](config/fulltext-setup.md)。模板中的本地可执行文件路径需要按服务器实际位置修改。

## 3. 初始化数据库并启动

在项目根目录执行。以下使用 `data/runs/production` 作为固定工作区；已有部署应继续使用原来的工作区，不要为了套用示例而更换。

```bash
python -m medical_qa_data_factory web migrate-production --workspace data/runs/production
python -m medical_qa_data_factory web serve --workspace data/runs/production --host 127.0.0.1 --port 8765
```

迁移与启动必须使用同一个工作区。迁移命令会记录执行版本和校验和，可重复执行；已经执行的 SQL 文件不可修改，后续升级通过新增迁移完成。

**工作区绝对路径决定 PostgreSQL schema。** 更换项目目录、工作区路径或操作系统后，可能选中不同 schema。因此数据库中有数据，不代表新路径会自动看到原来的批次。

另开终端启动开发前端：

```bash
cd frontend
npm run dev
```

打开 <http://127.0.0.1:5173/production>。Vite 将 `/api` 请求代理到 `127.0.0.1:8765`。

后端 <http://127.0.0.1:8765/> 返回服务状态 JSON，不是工作台页面。

## 4. Linux 服务器部署

先完成上面的依赖安装、配置与数据库迁移，再构建前端：

```bash
cd frontend
npm ci
npm run build
cd ..
```

部署时保持以下关系：

1. 使用 systemd 或现有进程管理器运行后端，工作目录设置为项目根目录。
2. 后端命令使用该项目虚拟环境中的 Python，并配置 `PYTHONPATH`、模型密钥和数据源路径。
3. Web 服务器托管 `frontend/dist/`；前端页面路由需要回退到 `index.html`。
4. `/api/` 反向代理到 `http://127.0.0.1:8765`，保留请求路径。
5. 同一工作区仅运行一个持续生产后端进程；数据库锁会拒绝第二个管理进程。

页面与 API 同域部署时，无需设置 `VITE_API_BASE_URL`。分域时，该变量需要在前端构建时设置，并相应配置后端 CORS。

`npm run dev` 用于开发调试，不作为这里的正式静态文件部署方案。面向外部网络时，应由部署入口提供访问控制。

## 5. 更新服务器

先在页面暂停批次并等其保存断点，再备份数据库与工作区，然后停止后端。检查服务器未提交改动后更新：

```bash
git status --short
git switch dev
git pull --ff-only origin dev
```

服务器首次切换到远端 `dev` 时：

```bash
git fetch origin
git switch --track origin/dev
```

激活环境后执行：

```bash
python -m pip install -r requirements.txt -c constraints.txt
python -m medical_qa_data_factory web migrate-production --workspace data/runs/production
cd frontend
npm ci
npm run build
cd ..
```

然后按现有服务管理方式启动后端并检查页面。若服务器使用其他工作区，迁移命令也必须替换为该路径。

不要使用 `git reset --hard` 或 `git clean -fdx` 作为日常更新步骤；后者可能删除被忽略的本地配置与运行资料。

## 6. 备份与恢复

恢复完整服务需要保存下面几部分，Git 只能恢复第一项。

| 内容 | 备份方式与用途 |
| --- | --- |
| 代码版本 | 记录 `git rev-parse HEAD`，确认对应提交已推送 |
| PostgreSQL | 数据库逻辑备份，例如 `pg_dump -Fc`；包含批次、实体和题目索引 |
| 文件工作区 | 在暂停任务并停止后端后备份整个实际 `--workspace` 目录 |
| 配置与凭据 | 单独保存 `.env`、本地模型配置、来源配置及服务环境变量 |
| 外部数据 | 保留 PrimeKG 等数据库及其版本、路径信息 |

数据库备份示例（凭据通过 passfile 或受控环境提供）：

```bash
pg_dump -h 127.0.0.1 -U qa_app -d medical_qa -Fc -f /backup/medical_qa.dump
```

恢复到管理员准备的空数据库示例：

```bash
pg_restore -h 127.0.0.1 -U qa_app -d medical_qa_restored --no-owner /backup/medical_qa.dump
```

恢复流程：检出备份对应代码 → 安装依赖 → 恢复配置、数据库与文件工作区 → 核对 schema 对应的工作区绝对路径 → 执行兼容的迁移 → 构建前端 → 启动检查。

新机器应尽量保留原工作区绝对路径。如果路径必须变化，需要显式规划原 schema 与新路径的对应关系；目前没有自动重映射命令。不要通过删除旧 schema 来解决“看不到历史”的问题。

`migrate-production` 负责 PostgreSQL 建表和版本升级，**不会自动把旧 `production.sqlite` 的完整批次历史搬入 PostgreSQL**。从 SQLite 版本升级时先保留旧库和文件工作区，另行安排历史数据迁移。

回退代码也不会自动回退数据库结构；应使用兼容版本，或在停机后恢复与代码匹配的数据库及文件工作区备份。

## 7. 测试与排查

```bash
python -m pytest tests -q
cd frontend
npm test
npm run build
```

运行普通后端测试时，建议在隔离环境中禁用本机生产数据库配置，避免测试中的临时工作区意外连接本地生产库。Linux 示例：

```bash
MEDICAL_QA_PRODUCTION_DATABASE_URL='' python -m pytest tests -q
```

PostgreSQL 集成测试必须指定独立测试数据库：

```bash
MEDICAL_QA_TEST_POSTGRES_URL='postgresql://qa_test@127.0.0.1:5432/medical_qa_test' \
  python -m pytest tests/test_continuous_production.py -q
```

测试会创建并清理临时 schema，不要将测试连接指向生产数据库。真实 PrimeKG 测试默认排除，可显式执行：

```bash
python -m pytest tests -m primekg_integration -q -o addopts=""
```

本次 PostgreSQL 改造验证了 11 项数据库集成测试，以及 33 项配置与 Web 测试。仓库仍有旧 Agent 测试使用已停用工具调用方式，已在改造前版本复现失败；这些局部验证不代表全套测试通过。

| 现象 | 检查项 |
| --- | --- |
| `/api/production` 返回 503 | 数据库连接配置是否可读；是否从项目根目录启动 |
| 报 `triggers` 表不存在 | 是否对相同 `--workspace` 执行了迁移；路径是否变化 |
| 数据库已有记录，页面为空 | 数据库名、工作区绝对路径及对应 schema 是否与原部署一致 |
| 报另一个后端正在管理生产 | 是否重复启动了同一工作区的后端 |
| 页面刷新后 404 | 静态服务是否配置回退到 `index.html` |
| API 返回 HTML / 非 JSON | `/api/` 是否被错误地送到前端页面回退规则 |
| 无模型或模型请求失败 | provider、端点、认证环境变量和网络是否正确 |
| Python 依赖行为异常 | 确认虚拟环境，并设置 `PYTHONNOUSERSITE=1` |

## 目录导航

```text
frontend/       Vue 页面与前端测试
src/            Python 服务、生成流程、存储及来源适配
  medical_qa_data_factory/web/migrations/  PostgreSQL 版本迁移
tests/          后端测试
config/         配置模板与说明
skills/         运行时提示词及规则
schemas/        数据契约
eval/           评估清单
scripts/        调试、重放、分析工具
design/         当前视觉规范与实现截图
experiments/    独立方法实验
plans/          实施计划
docs/           架构与专题文档，历史开发记录位于 archive/
data/           本地运行、缓存及实验产物，不提交 Git
```

- [项目文档](docs/medical-qa-data-factory/README.md)
- [前端说明](docs/medical-qa-data-factory/generation-frontend-vue.md)
- [当前 Apple 风格工作台](design/apple-workbench/README.md)
- [实验资料](experiments/README.md)
- [历史开发记录](docs/medical-qa-data-factory/archive/README.md)
- [尚未完成事项](UNFINISHED_CHECKLIST.md)

文件工作区、数据库记录与冻结实验基线都不是可任意删除的缓存。旧界面原型已从工作树移除，可通过 Git 历史查阅。
