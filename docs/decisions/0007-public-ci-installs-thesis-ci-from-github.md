# 0007 公开仓库的 CI 从 GitHub 安装 thesis-ci

## 背景

owners-office 的 CI 要跑 `thesis-ci lint`。thesis-ci 是单独的公开仓库（见 `0001`），第 0 阶段还没有发布版本，规范也还是 v0.x，会随财报季调整。需要定下公开 CI 怎样拿到 thesis-ci；私有仓库的 CI 同样需要它。

## 选项

1. 把 thesis-ci 的代码复制进 owners-office。
2. 用 git 子模块引用 thesis-ci。
3. 用 pip 从 GitHub 安装：`requirements-lint.txt` 写 `thesis-ci @ git+https://github.com/kentian742-creator/thesis-ci@main`。
4. 发布到 PyPI，按版本号安装。
5. 在 thesis-ci 里做一个可复用的 GitHub Action，owners-office 用 `uses:` 调用。

## 决定

采用选项 3。

- `requirements-lint.txt` 只有这一行；公开 CI 的 lint 任务 `pip install -r requirements-lint.txt` 后运行 `thesis-ci lint .`。
- 私有仓库的 CI 同时检出本仓库，用同一个 `requirements-lint.txt` 安装，保证两边用的是同一个版本。
- thesis-ci 打出第一个版本标签后，把 `@main` 改成固定标签（例如 `@v0.1.0`），升级 thesis-ci 变成一次有记录的提交。
- 第 4 阶段 thesis-ci 定 v1.0 时，再考虑发布到 PyPI 或提供可复用 Action。

## 理由

- 只有一份代码，不会出现复制品和原件不一致。
- 任何人照着 `requirements-lint.txt` 就能在本地复现 CI，正好兑现“任何人都能安装”的承诺。
- 不需要令牌：thesis-ci 是公开仓库。
- 子模块和 PyPI 在 v0.x 阶段都是额外负担，而版本标签已经能提供可复现性。

## 被否决的方案

- **复制代码**：两份代码必然漂移，lint 的结果会和 thesis-ci 自己的测试不一致。
- **git 子模块**：检出和更新都更麻烦，CI 还要多一步，收益与固定标签相同。
- **PyPI**：v0.x 期间改动频繁，发版成本高；等 v1.0 再说。
- **可复用 Action**：以后可以做，但第 0 阶段还没有这层包装，先用最简单的安装方式。

## 影响

- 必须先把 thesis-ci 推到 GitHub，owners-office 的 CI 才能通过（STATUS 待办 T3 的推送顺序）。
- 在固定标签之前，thesis-ci 主分支上的不兼容改动会直接让 owners-office 的 CI 变红；这正是尽快打标签的理由。

## 日期

2026-09-24
