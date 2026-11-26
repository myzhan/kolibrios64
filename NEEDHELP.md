# NEEDHELP — DESKTOP-v2 断点接手文档

编写：Hank（任务 144），2026-11-27。面向外部接手 agent。
仓库 `/Users/zhanqp/github/osdev/kolibrios64`，盘点时 HEAD=`46083ac`（=origin/main）。
本文档随 WIP 存档提交一起推送（提交消息 `WIP: DESKTOP-v2 wallpaper + start menu +
app entry (DEFECT-1 fix in progress, see NEEDHELP.md)`，哈希见 `git log`）。
**该 WIP 推送是用户显式指令的存档推送，不替代正式收口提交（任务 129）。**

---

## 1. 现状快照

### 1.1 工作树改动（盘点实测：15 修改 + 6 新增 = 21 项，全部属 DESKTOP-v2）

| 分组 | 文件 |
|------|------|
| T1 壁纸（4 文件） | `kernel/desktop/assets/wallpaper.bgrx`（新，3145728B）、`kernel/desktop/wallpaper.inc`（新）、`tests/stage2-desktop/check-wallpaper.py`（新）、`tools/gen-wallpaper.py`（新） |
| T2-T5 桌面/菜单/启动 | `kernel/desktop/{api,data,desktop,echo,selftest}.inc`、`kernel/desktop/desktop.asm`、`kernel/desktop/menu.inc`（新）、`kernel/desktop/taskbar.inc`（新）、`kernel/app/app.inc`、`kernel/core/{contracts,foundation}.inc` |
| 门禁/runner | `tests/stage2-desktop/{check.py,layout.asm}`、`tests/stage2-app/{check.py,layout.asm}`、`tests/stage2-fb/check-gui-judge.py`、`tools/run-qemu-debug.py` |

以上 21 项 + 本文档已随 WIP 提交入库；`build/` 产物与 `__pycache__` 不入库。

### 1.2 任务 143（DEFECT-1 修复）中断态 —— 盘点结论

**修复代码已写入，宿主门禁跑了一半，QEMU 实帧复验未跑。**

已完成的代码改动（git diff 实证）：
- `kernel/desktop/desktop.inc` `desktop_pump` 重构：**每条路径（含静止路径）都先
  restore 旧位 sprite**，静止分支 fall-through 到 old restore（注释标注 task 143），
  使 menu_open / cursor_place 的 readback 永不捕获活光标。
- `tests/stage2-desktop/check.py`：626→**629**（+2 正断言 + task-125 gate 移除
  fixture + sync 静止捷径同族审计）。
- `tests/stage2-fb/check-gui-judge.py`：181→**188**（+残影负向 fixture：menu
  launch/close、five game/game2/exit 起始盒残影；+独立模型满足残影盒探针自检）。
- `tools/run-qemu-debug.py`：新增 `RESIDUE_BOX (32,16,12,18)` 13 点探针
  `PPM_RESIDUE_BOX_PROBES` + `residue_box()` 全盒比对，已接入 `--menu`
  launch/close 帧与 `--five` game/game2 帧的**真实帧断言**。

门禁执行进度（`build/desktop-v2-fix/gates-summary.log` + `gates/*.log`）：
- 修复后已重跑且全绿：desktop **629/0**、judge **188/0**、fb（layout PASSED）、
  boot（FAILURES:0）、wallpaper（PASS）、app **752/0**、fs **540/0**。
- **fsys 跑到一半被 KeyboardInterrupt（exit 130）取消**（gates/fsys.log 尾部）。
- mm / cpu / blk / irq / input / model **未重跑**；QEMU 五腿矩阵**修复后未重跑**。
- `build/desktop-v2-fix/progress.md` 不存在（会话未及写进度即中断）。
- 构建确定性：desktop-v2-fix 的 KERNEL64.BIN 与本次 sanity 重建 sha 逐字节一致。

### 1.3 sanity 实测（任务 144，日志 `build/needhelp-144/`）

| 项 | 结果 |
|----|------|
| `./build_fasm2.sh` | exit 0，6 passes，KERNEL64.BIN **4034560B** |
| KERNEL64.BIN sha256 | `cdcd7b7abe8ce9b66bb600106836103d23362bfeec7aa738fdb175a4456c659f`（126 基线 `7960280c…` 因 143 改码而变化，**预期内**；尺寸不变） |
| `tests/stage2-desktop/check.py` | **629 checks, 0 failed, exit 0** |
| `tests/stage2-fb/check-gui-judge.py` | **188 checks, 0 failed, exit 0** |

### 1.4 常驻进程与端口（盘点实测，重要变化）

- **常驻 QEMU pid 40524 在盘点时已不存在**（`ps` 无任何 qemu 进程，12710 未监听）。
  非本任务所杀，如实记录。按用户指令**不启动新的常驻实例**。
- 验证端口 1274/1275/1276/1284+ 当前全部空闲。

### 1.5 冻结资产实测（盘点核验）

- `wallpaper.bgrx` sha `f9f8a12e5406e04b2007028bd168f72e9c25386d5bf92ef5ecb1aaeb18f36fec`，3145728B ✔
- FAT16 三映像（`build/fs-v1/`，disk-app 经本次构建重建后 sha 仍一致）：
  plain `e9e83dad3f6d5193ac15c7a35834cc5190ca34e978b537d80be9fa9beb7bc42b` /
  big `a810280509cd05f67dce37dc46e67cbfeb2e9432b73a105108f9a2d298500543` /
  disk-app `aec9f70167e6e18170a638bf4af3d5bf28817cf2ff37d6fec5a137cec12f3170` ✔
- `build/user-chip8/chip8.bin` 3255B ✔；`kernel/input/cursor.inc` git diff 空 ✔

---

## 2. 卡点清单

### P0 — 当前断点，接手后首先处理

#### P0-1 DEFECT-1 运行期光标残影（修复代码已写，实帧复验未完成）

- **现象**：`--five` game/game2 帧、`--menu` launch/close 帧在开始按钮点击位
  (32,16)-(43,33) 残留完整 12x18 光标 sprite；违背「运行期挂起光标」冻结设计。
  纯视觉、确定性、exit 全量重绘自愈；计数器/阶段/契约全部正确。
- **根因**：`desktop_pump`「静止 + menu_flag==0」start press 路径走 `.restored`
  不擦旧 sprite；`desktop_menu_open` readback 把 sprite 底行收进 `menu_save`，
  随后 `desktop_cursor_place` readback 把 sprite 行收进 `cursor_save`，后续
  restore 把 sprite 当背景写回 → 残影固化。系 task-125 根因 #2 修复
  （只覆盖静止+menu_flag==1）的未覆盖同族分支。
- **证据**：`build/desktop-v2-verify/DEFECT-1-cursor-residue.md`、
  `evidence-residue-{game,base,menu}-box.ppm`、`frames-verify.log`
  （33 checks 5 failed，5 项皆本缺陷）、`qemu-five/screen-five-game.ppm`。
- **143 中断态**（以 §1.2 盘点为准）：kernel 修复 + 门禁/探针增强**已写入**；
  desktop 629/judge 188 及 fb/boot/wallpaper/app/fs 修复后重跑绿；
  fsys 中断、mm/cpu/blk/irq/input/model 未跑、**QEMU 五腿未复跑**。
- **建议解法**：不重写代码，续做验证——补齐剩余宿主门禁 → 重建 → QEMU 五腿
  实帧复跑，确认残影盒探针在真实帧上全过。若实帧仍 FAIL 才回到代码排查同族分支
  （`desktop_cursor_sync` 的静止捷径已被 check.py 审计钉住，勿轻改冻结的 app POLL 路径）。
- **验收标准**：残影盒探针（menu launch/close + five game/game2 共 4 帧×13 探针
  + 全盒比对）实帧全过；负向 fixture 全检出；QEMU 五腿回归绿——基线
  `--five 351 / default 185 / --app 255 / --app-interactive 259 / --menu 215`
  **只增不弱化**（143 给 five/menu 腿新增了残影探针，ok 数会相应上升，
  以首跑实测为新基线并记录）。

#### P0-2 judge five_game 模型覆盖缺口（143 已部分补齐，全帧比对仍缺）

- **现象**：judge 的 five_game 独立模型原先只做合成自检，从未与真实 QEMU game
  帧全帧比对，导致 626/181/752/351 全绿仍漏检 DEFECT-1。
- **根因**：runner game 探针避开残影盒 + judge 模型不接真实帧。
- **证据**：`DEFECT-1-cursor-residue.md` §门禁覆盖缺口；
  `tests/stage2-fb/check-gui-judge.py`（143 后含模型残影盒自检）。
- **143 已做**：runner 真实帧残影盒探针 + 全盒比对；judge 模型满足探针自检 +
  残影负向 fixture。**仍未做**：真实 game 帧 vs 独立派生模型的**全帧**逐像素比对
  （当前只有 blit 矩形比对 + 残影盒 + 采样点）。
- **建议解法**：在 judge 或独立 verify 脚本中把 five_game 模型输出与
  `screen-five-game.ppm` 做全帧比对（光标盒/echo 行等已知差异区显式豁免）。
- **验收标准**：全帧比对落地且真实帧过；人为注入 1 像素残影的负向样本必 FAIL。

#### P0-3 DESKTOP-v2 收口流水线未走完

- **现象**：独立验证（任务 126）8/9 PASS 仅 DEFECT-1 FAIL → 修复（143）中断 →
  复验、收口审查（127）、史官补录（128）、正式收口提交推送+常驻桌面更新（129）
  全部未做。
- **证据**：`build/desktop-v2-verify/verification-report.md`（126 全记录）、
  `build/desktop-v2-fix/gates-summary.log`（143 中断点）、
  `docs/PORTING-32TO64.md` §12 目前仍是「进行中（占位）」，现有 §10 FS-v1 /
  §11 APP-v1，DESKTOP-v2 应补录为 §12 正式章节。
- **建议解法**：按 §3.1 断点续做步骤走完 143 复验 → 127 → 128 → 129。
- **验收标准**：14 门禁全绿 + QEMU 五腿全绿 + 收口审查通过 + §12 补录 +
  正式收口提交推送（哈希回填里程碑锚点表）。**本次 WIP 推送不替代该收口。**

### P1 — 已知遗留 / 环境坑

#### P1-4 N1：用户程序故障后桌面不常驻恢复（v1 冻结裁决）

- 现象：用户程序故障（EIO verdict）后 foundation 走 `K64-CPU-PANIC`
  （`kernel/cpu/data.inc:321`），桌面不恢复常驻。
- 根因：APP-v1 冻结裁决——故障走标准 fatal 路径，不做故障恢复。
- 证据：`kernel/app/app.inc`（teardown EIO verdict 注释）、foundation 调用链。
- 建议：APP-v2 立项做故障恢复（teardown 后回桌面），不在 DESKTOP-v2 顺手改。
- 验收：APP-v2 独立验收，本阶段仅确认 N1 行为未被意外改变。

#### P1-5 单程序 hosting、无抢占调度

- 现象：一次只能跑一个用户程序（app_launch gate 双比较钉住），无调度器。
- 根因：项目现状即「UEFI 加载器 + 串口/桌面内核原型」，调度需新建。
- 证据：`kernel/app/app.inc` launch gate；`tests/stage2-app/check.py` 钉住。
- 建议：多程序/调度独立立项；接手者勿在 DESKTOP-v2 内尝试并发启动。
- 验收：不适用（遗留项记录）。

#### P1-6 QEMU 映像写锁

- 现象：常驻实例持有 `disk-app.img` 写锁时，其他验证腿直接开同一映像会冲突。
- 建议：验证腿一律用映像**副本**，跑前 `shasum -a 256` 核验副本与源一致
  （§1.5 冻结 sha）。当前无常驻实例，但纪律保留。
- 验收：每腿日志记录副本 sha 与源一致。

#### P1-7 gdbstub quirk（已修，勿回退）

- 现象/根因：QEMU TCG gdbstub 双 Z1 硬件断点仅用户断点触发；hbreak 需
  z1/s/Z1 step-over 序列；O-skip 包 payload 需 `re.fullmatch` hex 判定。
- 证据：`tools/run-qemu-debug.py` `GdbSession`（L1791 起，fullmatch 处理已落地）。
- 建议：接手者**勿回退**这些处理；改 runner 时保持该口径。
- 验收：`--five` 等腿的 gdbstub 交互全通。

#### P1-8 PS/2 鼠标注入分包

- 现象：|delta|>127 的相对位移会被 QEMU PS/2 自动拆包，计数错乱。
- 建议：注入脚本每包 |dx|,|dy| ≤127 分拆（t23 progress.md 实测裁决，
  `--menu` 腿 21 包已按此实现）。
- 验收：mouse_count/v2c 计数与契约一致。

#### P1-9 长任务会话中断频发（本次 143 即死于 exit 130）

- 现象：网络超时 / exit 130 打断长后台命令，进度丢失。
- 建议（纪律）：**逐条前台跑门禁并落日志**（参照
  `build/desktop-v2-fix/run-gates.sh` 的 run() 模式）；进度随时写
  `build/<任务目录>/progress.md` 保证断点可续；禁止长后台命令不落盘。
- 验收：任一时刻被杀，接手者能凭 progress.md + gates/*.log 判断断点。

### P2 — 约束与纪律（接手必读）

#### P2-10 环境与三层验证边界

- macOS arm64。**严禁 Rosetta / `arch -x86_64` 进程内执行目标码**；真实执行
  仅 QEMU TCG（历史裁决，见记忆/PORTING 文档）。fasmg 宿主工具链见
  `build_fasm2.sh`（host: `../fasm2/build/fasm2-bootstrap/fasmg-macos-x64`）。
- 三层验证：宿主静态门禁 / 纯 Python 模型 / QEMU 实帧，各层能力边界见
  `docs/PORTING-32TO64.md`。静态门禁绿 ≠ 像素正确（DEFECT-1 即教训）。

#### P2-11 共享收口文件串行约束

- `kernel/core/contracts.inc`：**仅文件尾追加**（126 核验当前 diff 为单 hunk
  尾追加 154 行，零删除；接手后保持）。
- `kernel/core/foundation.inc`、`kernel64.asm` include 序、
  `tools/run-qemu-debug.py` 口径必须同步改；多任务并行时这些文件串行持有。

#### P2-12 冻结不变量清单（改动即门禁 FAIL，勿动）

- `DESKTOP_IRQ_TICKS_FINAL = 11`（api.inc 有 err 断言钉住）。
- 光标 sprite sha256 `f46ce0e0921bb78e2ffc420f062d21575ce4ea1e62ff5e65cd12c81965497ad7`
  （check.py SPRITE_SHA256）；`kernel/input/cursor.inc` 零改动。
- 桌面数据区旧标签偏移 0..1872 零改动（新标签 1912 起纯追加）。
- 用户 VA 域 `[0x100400000, 0x100800000)`；teardown +3 页表预算、
  released=3 / pages_held=0。
- FAT16 三映像 sha（§1.5 全文）；`chip8.bin` 3255B；
  `wallpaper.bgrx` sha `f9f8a12e…` 3145728B。

#### P2-13 常驻进程与端口

- 盘点时常驻 QEMU 40524（cocoa 窗口、gdbstub 12710、tetris 可玩态）**已不在运行**
  （见 §1.4）。若他人重启常驻实例：不得杀、避开 12710。
- 验证端口建议 **1274/1275/1276 或 1284+**；用前 `lsof -nP -iTCP:<port>` 确认空闲。
- 不启动新的常驻实例（用户指令）；验证 QEMU 跑完必须退出并核验 ps。

#### P2-14 门禁基线表（126 实测 → 143 后实测）

| 门禁 | 126 基线 | 143 后（本文 sanity/fix 目录实测） |
|------|---------|-----------------------------------|
| desktop | 626/0 | **629/0** |
| judge | 181/0 | **188/0** |
| app | 752/0 | 752/0（fix 目录重跑） |
| fb | 508/0 | layout PASSED（fix 目录重跑） |
| boot | FAILURES:0 | FAILURES:0（fix 目录重跑） |
| wallpaper | 12 PASS | PASS（fix 目录重跑） |
| fs | 540/0 | 540/0（fix 目录重跑） |
| fsys | 528/0 | **中断未跑完，待补** |
| mm 2188 / cpu 689 / blk 481 / irq 317 / input 402 / model 16 | 全 0 fail | **待补跑** |

内核：4034560B；126 sha `7960280c9af3d78284c8fd88d1fdb00439154077940cf404dd03ee1610185a16`
→ 143 改码后 **`cdcd7b7abe8ce9b66bb600106836103d23362bfeec7aa738fdb175a4456c659f`**
（本次 sanity 与 fix 目录产物逐字节一致）。QEMU 腿基线见 P0-1 验收。

---

## 3. 接手指南

### 3.1 断点续做方法（建议顺序）

1. 读本文档 + `build/desktop-v2-verify/{verification-report.md,DEFECT-1-cursor-residue.md}`
   + `build/desktop-v2-t23/progress.md` + `build/desktop-v2-t45/progress.md`。
2. 建 `build/desktop-v2-resume/`（或复用 desktop-v2-fix），写 progress.md。
3. 补齐剩余宿主门禁，**逐条前台**落日志：fsys → mm → cpu → blk → irq → input →
   model（参照 `build/desktop-v2-fix/run-gates.sh`，从中断点续跑即可）。
4. `./build_fasm2.sh` 重建，核验 KERNEL64.BIN sha == `cdcd7b7a…`（确定性）。
5. QEMU 五腿实帧复跑（映像副本 + sha 核验，端口 1274/1275/1276）：
   `--five --fs --fsprobe` / default `--fs --fsprobe` / `--app` /
   `--app-interactive` / `--menu`。确认残影盒探针实帧全过；ok 数只增不弱化，
   记录新基线。最小复现命令见 DEFECT-1 md §最小复现。
6. 全绿后走收口：127 收口审查 → 128 史官补录 `docs/PORTING-32TO64.md` §12
   （替换现占位内容）→ 129 正式收口提交推送 + 常驻桌面更新（常驻实例重启
   需用户/leader 裁决，本任务未启动）。
7. 若实帧复跑仍见残影：回到 `desktop_pump`/`desktop_cursor_sync` 同族分支排查，
   补负向 fixture 后再走 3-6。

### 3.2 端口分配

- 1274/1275/1276：验证腿轮换；1284+：备用。12710：预留给常驻 gdbstub，避开。
- 每腿启动前确认端口空闲，结束后核验 QEMU 进程退出。

### 3.3 纪律（违反即返工）

- 严禁 Rosetta 进程内执行目标码；真实执行仅 QEMU TCG。
- contracts.inc 仅尾追加；冻结不变量（P2-12）零改动；门禁只增不弱化。
- 禁止 `git add -A`；显式路径 stage；build/ 与 __pycache__ 不入库。
- 长任务逐条前台跑 + 落日志 + progress.md 断点可续（P1-9）。
- 不杀常驻实例（若存在）、不启动新常驻实例；临时产物（变异副本、fixture 树）
  用后即清，日志/diff 保留。
- 正式收口前不改 `docs/PORTING-32TO64.md` 里程碑锚点表（史官职责，任务 128）。