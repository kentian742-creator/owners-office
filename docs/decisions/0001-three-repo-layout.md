# 0001 三个仓库的布局

## 背景

DESIGN.md 规定三个仓库各司其职：thesis-ci 是任何人都能安装的开源工具，owners-office 是主人用它维护的公开记录，owners-office-private 放完整档案和金额。第 0 阶段要把这三个仓库的骨架在本地建起来，之后由主人审阅再推送到 GitHub 账号 `kentian742-creator`。需要定下：本地怎么摆、两个档案仓库怎么互相找到、公开与私有的边界落在哪一层，以及 owners-office 里的代码用什么许可（DESIGN.md 只写了“方法文档”和“研究内容”两类）。

## 选项

1. 一个仓库，私有内容放进加密目录或子模块。
2. 两个仓库：工具并入公开档案仓库，另加一个私有仓库。
3. 三个兄弟仓库（DESIGN.md 的方案）：thesis-ci、owners-office、owners-office-private。

## 决定

采用选项 3。

- 本地工作区 `/Users/asuka/OwnersOffice/` 下三个兄弟目录：`thesis-ci/`、`owners-office/`、`owners-office-private/`。原始资料留在工作区的 `inputs/`，不进任何仓库；PDF 的副本进私有仓库的 `reports/`。
- 两个档案仓库根目录各有 `repo.yml`：`visibility`（public / private）、`owner: kentian742-creator`、`spec_version: "0.1"`、`counterpart`（对方仓库名）。thesis-ci 按 `visibility` 决定跑哪些检查；私有仓库的 lint 通过 `--counterpart ../owners-office` 读取公开仓库做跨仓检查。
- 公开仓库的 CI 看不到私有仓库；跨仓检查只在私有仓库的 CI、本地和验收脚本里运行。
- 许可：thesis-ci 代码 MIT、规范 CC BY 4.0（DESIGN.md）；owners-office 方法文档 CC BY 4.0、研究内容保留权利（DESIGN.md），胶水代码（`pipeline/`、`scripts/`、`tests/`、`.github/`）用 MIT，与 thesis-ci 的代码一致；私有仓库不公开。细节见 `LICENSE.md`。

## 理由

- 可见性边界就是仓库边界。GitHub 没有目录级的可见性，边界落在仓库上最不容易出错，金额和价格不会因为一次配置失误进入公开历史。
- 工具可以被别人单独安装和复用，不必拿到主人的档案；许可也各自清楚。
- `counterpart` 让跨仓检查（例如公开仓库的每家公司在私有仓库都有 `valuation.yml`）有明确的入口，而公开 CI 永远不需要读私有仓库的权限。
- 代码用 MIT 是因为 CC BY 不适合软件，而保留权利会让别人无法照着 CI 复现检查。

## 被否决的方案

- **一个仓库加加密目录或子模块**：密钥管理复杂，一次误操作就把金额写进公开历史，且无法撤回；子模块在 CI 里还要额外的访问令牌。
- **工具并入公开档案仓库**：别人想用工具就得拉下主人的档案；工具的 MIT 许可和研究内容的保留权利混在一起，也不利于第 4 阶段把 thesis-ci 发成 v1.0。
- **owners-office 的代码也用保留权利**：会让“任何人都能核对”的承诺落空。

## 日期

2026-09-24
