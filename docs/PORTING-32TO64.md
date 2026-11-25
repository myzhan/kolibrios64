# KolibriOS 32 → 64 位移植变更史

> 本文档由项目「变更史官」角色维护，记录从 32 位 KolibriOS 到 64 位实现的主要变化。
> 每项变化按四段式记述：**32 位做法 → 64 位做法 → 变更理由 → 证据文件**。
> 只记录已验证事实：以两个仓库的当前源码、契约断言与构建/测试脚本为准；
> 无法在仓库内核实的内容明确标注「待核实」，不作臆断。
>
> 路径约定：`kolibrios64/…` 相对 64 位仓库根（本仓库），`kolibrios/…` 相对
> 32 位基线仓库根（`../kolibrios/`，只读对照）；`fasm2/…` 相对 `../fasm2/`。

## 里程碑提交锚点

| 仓库 | 提交 | 内容 |
| --- | --- | --- |
| kolibrios64 | `ead8369` | stage-1 启动链 + CPU 描述符/异常模块（已验证） |
| kolibrios64 | `dde7da1` | 已验证的 QEMU 启动调试工作流（gdbstub + ELF64 符号） |
| kolibrios64 | `028f27f` | MM 子系统 PMM/VMM/heap（冻结契约 K64-FOUNDATION-v1，复验） |
| kolibrios64 | `553a031` | CPU+MM 内核基础集成（QEMU 验证） |
| kolibrios64 | `983111c` | IRQ-v1 LAPIC 定时器闭环 + GOP 交接（QEMU 验证） |
| kolibrios64 | `7b2c4b7` | GUI-G1 GOP framebuffer 图形闭环（QEMU 验证） |
| kolibrios64 | `227c7dd` | docs：GUI-G1 提交哈希补录 |
| kolibrios64 | `ddad22d` | IRQ-v2 IOAPIC + PS/2 键鼠输入闭环（QEMU 验证，见 §8） |
| kolibrios64 | `ef6a29b` | docs：IRQ-v2 提交哈希补录 |
| kolibrios64 | `2bd897b` | 桌面壳：最小交互桌面（QEMU 验证，当前 HEAD，见 §9） |
| fasm2 | `0253b7f` | macOS x64 自举工作流收口（自举里程碑，含 `aad830d` 自举宿主与自宿主测试） |

以上锚点已用 `git log --oneline` 核对。本文档随后续里程碑一并提交，不单独入库。

---

## 1. 启动

### 1.1 加载路径：Legacy BIOS 自定义加载 → UEFI PE32+ 应用

- **32 位做法**：自定义 BIOS 启动链。内核文件头部即 16 位入口
  （`use16` / `org 0`，`'KolibriOS '` 签名 + 版本字符串），由引导扇区装入后
  从实模式进入保护模式；`build.sh` 用 FASM1 分别汇编 `bootbios.asm` 与
  `kernel.asm`，再经 `kpack` 压缩为 `kernel.mnt`。机器描述（内存大小等）靠
  内核自身探测（`detect/`、`memmap.inc`）。
- **64 位做法**：加载器是标准 UEFI 应用 `BOOTX64.EFI`（`format pe64 efi`，
  PE32+，machine 0x8664，subsystem 10），由固件（验证环境为 OVMF）直接加载。
  它经 SimpleFileSystem 读入 `KERNEL64.BIN`、校验头部、建页表、填 BootInfo、
  ExitBootServices 后跳入内核。机器描述不再探测，而是复制 UEFI GetMemoryMap
  的结果交给内核。
- **变更理由**：x86-64 内核无法从实模式引导，UEFI 是长模式启动的标准路径；
  固件内存图取代手工探测，消除 e820 解析等历史脆弱点；标准 PE32+ 使加载器
  可被任何 UEFI 固件/调试工具识别。
- **证据文件**：`kolibrios/kernel/trunk/bootbios.inc`（16 位入口与签名头）、
  `kolibrios/kernel/trunk/kernel.asm`、`kolibrios/kernel/trunk/build.sh`；
  `kolibrios64/kernel/boot/bootx64.asm`、`kolibrios64/build_fasm2.sh`。
  （注：`build.sh` 引用的 `bootbios.asm` 在 trunk 下未见，见文末待核实。）

### 1.2 交接契约：隐式约定 → 40 字节内核头 + 208 字节版本化 K64BOOTINFO

- **32 位做法**：引导扇区与内核之间的交接是隐式寄存器/内存布局约定，
  无版本化结构，无签名校验。
- **64 位做法**：契约集中定义在 `boot/bootinfo.inc`，加载器与内核共同 include：
  - `KERNEL64_HEADER`（40 字节，汇编期断言）：magic `'KERNEL64'`、
    `entry_point_offset`（相对 VIRT_KERNEL_BASE）、`stack_size`、
    由加载器回填的 `phys_start/phys_end`；
  - `K64BOOTINFO`（208 字节，签名 `'K64BOOTI'`，version 1）：内存图副本
    地址/大小/描述符步长（40 字节）/版本/个数、内核与栈物理区间、GOP/RSDP/
    initramfs 字段。特性 flags 只允许置位真实提供的数据（首版仅
    MEMORY_MAP=0x01；FRAMEBUFFER=0x02 随 IRQ-v1 启用，见 §6.5），
    未实现特性显式置零，绝不伪造；
  - 内核入口逐字段校验（签名、版本、struct_size、flags、memmap、区间包含
    关系），任何不符走 `K64-BOOT-FAIL <原因>` 报告后停机。
- **变更理由**：版本化 + 签名 + 显式零值使交接成为可断言、可演进的接口；
  加载器与内核共享单一定义源，杜绝两侧结构漂移；预留字段为 GOP/ACPI/
  initramfs 后续里程碑留出无破坏扩展空间。
- **证据文件**：`kolibrios64/kernel/boot/bootinfo.inc`、
  `kolibrios64/kernel/kernel64.asm`（k64_entry 校验链与失败消息）、
  `kolibrios64/kernel/mm/api.inc`（208 字节独立副本 + 偏移断言）、
  `kolibrios64/tools/check-boot-contract.py`。

### 1.3 ExitBootServices：一次调用 → stale-key 重试 + 失败路径守卫

- **32 位做法**：无对应阶段（不存在固件服务退出问题）。
- **64 位做法**：`exit_boot_services` 过程最多重试 4 次
  （`EXIT_BOOT_SERVICES_RETRIES = 4`）：返回 `EFI_INVALID_PARAMETER`
  （内存图 key 过期）时重新 GetMemoryMap 并重新发布整个 BootInfo 内存图
  描述后再试。自首次调用起 `boot_services_state` 置
  `EXIT_ATTEMPTED`，此后一切失败路径只允许走 COM1（固件控制台已不可信），
  且串口等待有界（`COM1_SPIN_LIMIT`），缺 UART 不得挂死无固件的机器。
- **变更理由**：UEFI 规范允许 GetMemoryMap 与 ExitBootServices 之间 key
  失效，重试是合规必需；「首次尝试后固件即不可信」是保守而正确的守卫，
  静态检查器证明没有任何失败路径在该状态后仍能到达 UEFI 控制台。
- **证据文件**：`kolibrios64/kernel/boot/bootx64.asm`
  （exit_boot_services、boot_fail/boot_fail_status）、
  `kolibrios64/tools/check-boot-contract.py`（检查 2、3）。

### 1.4 地址空间交接：单映射 → identity/高半区双映射 + 独立栈

- **32 位做法**：内核固定链接在 `OS_BASE = 0x80000000`，两级页表递归
  自映射（详见 §4），启动与运行共用同一套映射思路。
- **64 位做法**：加载器建两套映射后才 `mov cr3`：
  - 低 4 GiB 临时 identity 映射（2 MiB 页），覆盖加载器自身代码、当前栈、
    全局变量、内存图副本与 BootInfo（契约常量
    `K64_IDENTITY_MAP_LIMIT = 0x100000000`）；
  - 内核映像 + 栈按 4 KiB 页映射到 `VIRT_KERNEL_BASE = 0xFFFFFFFF80000000`
    （PML4[511]/PDPT[510]/PD[0]，汇编期断言索引），页表页取自固定 16 页
    P1 池，映射页数有界、不会回绕覆盖；
  - 切 CR3 后 `jmp VIRT_KERNEL_BASE + entry_point_offset`，RCX = BootInfo
    物理地址（仍在 identity 映射内可读）；内核入口立即把 RSP 换算为
    高半区栈顶（映像末尾之后，16 字节对齐），从此不再使用固件/加载器栈。
- **变更理由**：只建高半区映射会让 `mov cr3` 的下一条指令失去有效映射
  ——这是本项目早期真实的三重故障来源（提交 `2d7bef6`、`0c16a5f` 记录了
  该过程）；双映射 + 目标 RIP 用高半区地址 + 独立栈三者缺一不可，
  单独修任何一处都无法完成交接。
- **证据文件**：`kolibrios64/kernel/boot/bootx64.asm`（建表与交接段）、
  `kolibrios64/kernel/boot/bootinfo.inc`（交接规则注释）、
  `kolibrios64/kernel/kernel64.asm`（RSP 换算）、
  `kolibrios64/kernel/const.inc`。

### 1.5 早期诊断：屏幕输出 → COM1 串口唯一通道

- **32 位做法**：启动早期即有引导画面/屏幕字符串（`boot/bootstr.inc` 等）。
- **64 位做法**：COM1（0x3F8，115200 8N1）是唯一贯穿全程的控制台。加载器
  在首次 ExitBootServices 尝试后自行驱动 UART；内核入口先做 UART 回环
  自检（发 0xAE 必须原样收回），失败即停机；成功路径输出唯一里程碑
  `K64-BOOT-OK` 及 bootinfo/memmap/kernel/stack 详情行，全部 ASCII。
- **变更理由**：ExitBootServices 之后固件控制台不复存在，GOP 尚未接管，
  串口是唯一可被宿主脚本无歧义匹配的观测通道；回环自检保证「没有输出」
  与「没有控制台」可区分。
- **证据文件**：`kolibrios64/kernel/boot/bootx64.asm`（com1_init）、
  `kolibrios64/kernel/kernel64.asm`（com1_init/com1_puts/kmsg_*）、
  `kolibrios64/tools/run-qemu-debug.py`（serial.log 判据）。

---

## 2. 构建工具链

### 2.1 汇编器：FASM 1.x → fasm2（fasmg 核心）+ macOS x64 首代宿主自举

- **32 位做法**：FASM 1.x 直接汇编（`fasm -m 65536 kernel.asm kernel.mnt`），
  依赖发行版预编译宿主。
- **64 位做法**：全部 64 位源码用 fasm2（fasmg 引擎）汇编。开发宿主是
  macOS（Darwin arm64 上经转译运行 x64 工具链），fasm2 官方无 macOS x64
  宿主，`fasm2/tools/bootstrap-macos.sh` 实现两阶段首代自举：
  1. FASM1 汇编 `source/macos/x64/bootstrap-fasm1.asm` 得低地址 ELF64
     映像（Darwin 裸 syscall，无 dyld）——FASM1 无法处理 CALM 宏源码，
     故首代必须绕开；
  2. `tools/elf64_to_macho64.py` 把 ELF64 包装成可直接运行的静态
     Mach-O64；该自举宿主再汇编原生 `fasmg.asm` 得第二代原生宿主；
     `--selfhost` 要求第二代与第三代产物逐字节一致。
  权威 fasm2 来源为个人 fork `git@github.com:myzhan/fasm2.git`
  （已核对 `git remote -v`）。
- **变更理由**：fasmg 的 CALM 宏能力是 64 位宏库（原生 struct/proc64）的
  前提；自举链保证工具链可在无官方二进制的宿主上从零重建，且自宿主
  一致性测试证明引擎自译稳定。
- **证据文件**：`fasm2/tools/bootstrap-macos.sh`、
  `fasm2/tools/elf64_to_macho64.py`、`fasm2/source/macos/x64/`、
  `fasm2/.github/workflows/macos-x64-bootstrap.yml`；
  `kolibrios/kernel/trunk/build.sh`（32 位对照）。

### 2.2 产物生成：压缩内核映像 → 两次汇编、无链接器、40 字节自定义头

- **32 位做法**：`kernel.mnt` 经 kpack 压缩，由引导链解压装载。
- **64 位做法**：`build_fasm2.sh` 只做两次汇编调用，无任何链接器：
  - `kernel/boot/bootx64.asm` → `BOOTX64.EFI`（`format pe64 efi` 直接产出
    完整 PE32+）；
  - `kernel/kernel64.asm` → `KERNEL64.BIN`（平坦二进制，
    `org VIRT_KERNEL_BASE`，文件头 40 字节即 §1.2 的 KERNEL64_HEADER，
    入口 VA = VIRT_KERNEL_BASE + 头偏移 8 处的 offset）。
  脚本将 FASMG/FASM2_DIR/FASM1/OUTDIR 全部规范为绝对路径（自举脚本会
  cd 进 fasm2 源码树，相对路径会静默指错）；自举输出目录与内核产物目录
  严格分离；产物为空即报错退出。
- **变更理由**：无链接器意味着布局完全由源码与汇编期断言控制，任何字节
  偏移都可被宿主静态门禁直接断言；自定义头让加载器无需理解 ELF/PE
  即可装载内核，同时把「入口在哪、要多少栈」变成显式契约。
- **证据文件**：`kolibrios64/build_fasm2.sh`、
  `kolibrios64/kernel/kernel64.asm`（kernel_header 与 sizeof 断言）、
  `kolibrios64/tools/check-boot-contract.py`（检查 4：OUTDIR 隔离）。

### 2.3 宏库：FASM1 花括号宏 → fasm2 原生宏 + 兼容层

- **32 位做法**：`struct.inc`/`proc32.inc` 为 FASM1 花括号宏与转义写法。
- **64 位做法**：`kernel/fasm2_compat.inc` 加载 fasm2 原生
  `macro/struct.inc` 与 `macro/proc64.inc`；补齐 UEFI 所需的最小环境：
  TCHAR 固定 2 字节（CHAR16）、`DN fix dq`（原生指针宽）、`fstcall`
  转发到 fasm2 `fastcall`（fasm2 不允许把宏发布为别名，只能宏转发；
  fasm2 fastcall 恒保留 32 字节 shadow space 并维持 RSP 16 字节对齐，
  恰为 UEFI x64 调用约定所需）。旧 FASM1 宏库原样留在树内不改动。
- **变更理由**：fasmg 完全不接受 FASM1 花括号宏，兼容层是唯一出路；
  保持旧文件不动使仍以 FASM1 汇编的源码不受影响。
- **证据文件**：`kolibrios64/kernel/fasm2_compat.inc`、
  `fasm2/include/macro/struct.inc`、`fasm2/include/macro/proc64.inc`；
  `kolibrios/kernel/trunk/struct.inc`、`kolibrios/kernel/trunk/proc32.inc`
  （32 位对照）。

---

## 3. CPU

### 3.1 描述符与特权状态：32 位 GDT/IDT/TSS → 64 位 GDT/TSS/IST

- **32 位做法**：8 字节中断门 IDT（`build_interrupt_table` 以 `movsw/stosd`
  拼装，0x40 号外另拷 DPL3 trap gate 供 `int 0x40` 系统调用）；TSS 用
  `_esp0` 承接 Ring3→Ring0 栈切换（`kernel.asm`）。
- **64 位做法**（`kernel/cpu/`，契约 K64-FOUNDATION-v1）：
  - GDT 40 字节：null / code 0x08（`0x00AF9A000000FFFF`，L=1）/ data 0x10 /
    TSS 0x18；TSS 描述符 16 字节，运行时按真实 `cpu_tss` 地址重建；
  - TSS 104 字节：RSP0 + IST1..IST7 槽位；本阶段 5 条 16 KiB 栈
    （IST1=DF、IST2=NMI、IST3=MC、IST4=测试向量，另加 panic 栈）；
    IOMAP base 置于 limit 之外表示无位图；
  - 装载顺序冻结：LGDT → `retfq` 远返回重载 CS → 重载 DS/ES/SS →
    清 FS/GS/LDTR → LTR；LGDT 到 LIDT 之间零控制台输出；
  - IDT 256 × 16 字节门，type 0x8E（present、DPL0、64 位中断门），
    LIDT 前逐门校验选择子/类型/IST/目标 stub 区间；
  - `cpu_init` 前置环境检查：Ring0、IF=0、EFER.LMA、CR4.PAE、CR0.PG、
    CET 未启用；8259 双片全屏蔽（0xFF），此后 IDT 拥有全部向量。
- **变更理由**：长模式下分段基本失效，但 GDT（L 位）与 TSS 仍是必须的
  ——RSP0/IST 是硬件唯一的异常栈切换机制，DF/NMI/MC 必须落在独立好栈
  上才可能留下诊断；冻结装载顺序与「半切换状态零输出」防止用半张旧表
  打印证据（bring-up 内核丢失现场的经典方式）。
- **证据文件**：`kolibrios64/kernel/cpu/api.inc`、
  `kolibrios64/kernel/cpu/gdt_tss.inc`、`kolibrios64/kernel/cpu/cpu.asm`、
  `kolibrios64/kernel/cpu/data.inc`；
  `kolibrios/kernel/trunk/core/sys32.inc`、
  `kolibrios/kernel/trunk/kernel.asm`（tss._esp0，32 位对照）。

### 3.2 两个硬件置位陷阱：TSS busy 0x8B 与 GDT Accessed 位掩码

- **32 位做法**：无对应断言体系（描述符不做运行时字节级比对）。
- **64 位做法**：契约显式编码两个 CPU 行为：LTR 会把 TSS 描述符 type 从
  0x89（available）置为 0x8B（busy），断言按 0x8B 期望；CPU 装载段时置
  Accessed 位（qword bit 40），镜像构建时该位清零，一切运行时比对必须先
  用 `CPU_GDT_DESC_RUNTIME_MASK` 掩掉该位。两条规则均有汇编期断言
  （`0x8B = 0x89 | 2`、构建值 Accessed 必须为 0）。
- **变更理由**：这是「静态期望值 vs 硬件运行后置位」的固有差异；不写进
  契约并掩码，自测会在完全正确的内核上误报失败。
- **证据文件**：`kolibrios64/kernel/cpu/api.inc`
  （CPU_TSS_DESC_TYPE_BUSY、CPU_GDT_DESC_RUNTIME_MASK 及断言）、
  `kolibrios64/tests/stage2-cpu/check.py`（独立重复同一规则）。

### 3.3 调用约定：自定义宏约定 → MS x64 ABI + 统一 128 字节栈帧

- **32 位做法**：内核用自定义 proc32 宏约定（stdcall/自定义 fastcall），
  系统调用经 `int 0x40` 直接寄存器传参；上下文用 `pushad` 全压栈、
  `iretd` 返回（`core/sched.inc`、`core/taskman.inc`）。
  （32 位 fastcall 的确切寄存器顺序不在 trunk 本地宏库内定义，见文末待核实。）
- **64 位做法**：全内核统一 Microsoft x64 整数 ABI：RCX/RDX/R8/R9 传参，
  调用方留 32 字节 shadow space、call 前 RSP mod 16 = 0，无 red zone；
  RBX/RBP/RSI/RDI/R12..R15 callee-saved；EAX 返回带符号状态码
  （0 成功，-1..-12 冻结错误码），RDX 返回主结果；DF=0、IF 不变。
  每个导出函数统一 `push rbp / mov rbp,rsp / sub rsp,128 / … / leave / ret`
  帧：128 = 32 shadow + 局部变量，且使嵌套调用天然对齐，无需逐点记账。
- **变更理由**：与 UEFI 边界同一约定，消除加载器/内核两套约定的翻译成本；
  统一帧让「每个函数都满足 ABI」成为可逐字节静态断言的性质，而不是
  逐函数人工审查。
- **证据文件**：`kolibrios64/kernel/cpu/api.inc`（约定注释与
  CPU_STACK_FRAME=128）、`kolibrios64/kernel/mm/api.inc`、
  `kolibrios64/kernel/core/contracts.inc`；
  `kolibrios/kernel/trunk/core/sched.inc`、
  `kolibrios/kernel/trunk/core/taskman.inc`（pushad/iretd，32 位对照）。

### 3.4 异常处理：手写 handler → 184 字节统一 trap frame + 精确匹配自测

- **32 位做法**：各异常独立 handler（e0..e19、page_fault_exc），
  `EXCEPT_STACK` 等专用小结构，异常主要面向杀进程/停机，不可编程断言。
- **64 位做法**：256 个汇编期生成的 stub 把每个向量归一为 184 字节
  trap frame：CR2@0、R15..RAX@8..120、vector@128、error@136、
  硬件帧 RIP/CS/RFLAGS/RSP/SS@144..176（无错误码向量补哑元，压栈顺序
  即布局）。自测 API 四参数精确匹配：
  `cpu_trap_expect(rcx=向量, rdx=期望保存 RIP, r8=恢复 RIP, r9=CPU_EXPECT*或0)`
  仅接受向量 0/3/6/14/0xF0/0xF1；dispatcher 比对向量、保存 RIP、掩码后
  错误码与掩码后 CR2，**全匹配才接受**（拷贝整帧、改写返回 RIP、消费
  arming），任何偏差直接 panic；`cpu_trap_finish` 恰好消费一次结果。
  布局由独立 virtual 块逐偏移断言，任何漂移是构建失败而非静默错位。
- **变更理由**：把「异常」变成可布防、可精确断言、可恢复的事件，是自测
  覆盖 DE/BP/UD/PF/IST 路径的前提；精确匹配（而非宽容匹配）保证错误的
  异常不可能伪装成预期异常混过门禁。
- **证据文件**：`kolibrios64/kernel/cpu/api.inc`（CPU_FRAME_*、
  CPU_EXPECT_*）、`kolibrios64/kernel/cpu/idt_traps.inc`、
  `kolibrios64/kernel/cpu/selftest.inc`、
  `kolibrios64/tests/stage2-cpu/check.py`（逐字节重建 256 个 stub）；
  `kolibrios/kernel/trunk/core/sys32.inc`（EXCEPT_STACK，32 位对照）。

---

## 4. MM（内存管理）

### 4.1 分页结构：两级递归自映射 → 四级页表 + 高半区

- **32 位做法**：非 PAE 两级 32 位分页，`page_tabs = 0xFDC00000`，
  `master_tab = page_tabs + (page_tabs shr 10)` 递归自映射，内核固定
  链接 `OS_BASE = 0x80000000`。
- **64 位做法**：四级页表（PML4/PDPT/PD/PT），内核高半区
  `0xFFFFFFFF80000000`。stage-2 VMM **沿用加载器装入的 CR3**，不自建
  地址空间：页表页经 identity 映射访问，故一切表页限低 4 GiB；叶子
  物理地址则允许 CPUID 报告的全物理宽度（`pa_mask` 按实际位宽导出）。
  walk 支持 4 KiB/2 MiB/1 GiB 叶子，有效权限按路径合成：RW/US 取 AND、
  NX 取 OR；PML4 保留 PS 位、非规范地址、表页出 identity 窗口均显式拒绝。
- **变更理由**：x86-64 长模式强制四级分页，递归自映射的 32 位技巧不再
  成立；沿用 CR3 避免早期再造一次「切 CR3 丢映射」事故；表页限低 4 GiB
  是 identity 窗口的诚实约束，叶子全宽度则不浪费真实物理内存。
- **证据文件**：`kolibrios64/kernel/mm/vmm.inc`（mm_vmm_resolve 及
  模块头注释）、`kolibrios64/kernel/mm/bootmap.inc`（mm_cpu_phys_bits）；
  `kolibrios/kernel/trunk/const.inc`（page_tabs/master_tab）、
  `kolibrios/kernel/trunk/core/memory.inc`（32 位对照）。

### 4.2 物理分配：单 bitmap + cli → 三 bitmap PMM（eligible/allocated/pinned）

- **32 位做法**：单一 `sys_pgmap` 位图，`alloc_page` 用 `bsf/btr` 扫描，
  全程 `pushfd/cli` 关中断保护全局状态。
- **64 位做法**：三张位图各 128 KiB，覆盖低 4 GiB 全部 1M 个 PFN：
  - eligible：可发放（仅 UEFI conventional 且 WB、剔除
    RUNTIME/RP/RO/WP/NV 属性）；
  - allocated：公共分配器当前持有；
  - pinned：永不发放、永不经公共接口释放（固件保留区、BootInfo/内存图、
    CR3 表页等）。
  初始化顺序冻结：eligible=0、allocated=0、pinned=全 1 → 合法 conventional
  页置 eligible 并解 pin → 各保留区重新 pin。空闲 = eligible ∧ ¬allocated
  ∧ ¬pinned；分配为全 1M PFN 空间 next-fit，新页经 identity 映射清零。
  计数增量维护，`pmm_get_stats` 重数位图并交叉校验
  `total = free + allocated + reserved` 不变量。低 1 MiB 永久保留，
  加载器映像+栈上限 32 MiB。本阶段单核 IF=0，无锁需求。
- **变更理由**：「可分配」「已分配」「绝不可动」是三种正交状态，单 bitmap
  无法表达固件保留与表页 pin 的语义；冻结初始化顺序 + 重数校验使 PMM
  状态可被宿主模型与 QEMU 双重审计。
- **证据文件**：`kolibrios64/kernel/mm/pmm.inc`、
  `kolibrios64/kernel/mm/bootmap.inc`、`kolibrios64/kernel/mm/api.inc`、
  `kolibrios64/kernel/core/contracts.inc`（K64_LOW_RESERVED_END/K64_IMAGE_MAX）；
  `kolibrios/kernel/trunk/core/memory.inc`（alloc_page，32 位对照）。

### 4.3 映射策略：统一 RW → public 窗口 + W^X + boot_protect

- **32 位做法**：内核页普遍可写可执行，无 NX（32 位非 PAE 无 NX 位），
  无按区域的权限收紧。
- **64 位做法**：公共映射只允许落在
  `[0xFFFFC00000000000, 0xFFFFC20000000000)`（2 TiB 窗口，规范高半区，
  汇编期断言）；`vmm_map` 仅接受两种权限组合：只读可执行或可写不可执行
  （`MM_MAP_ALLOWED = RW | NX`，RW+可执行、任何 cache 属性一律拒绝），
  恒 present + supervisor。缺表离线分配、在新零页内链接、经**单次存储**
  发布进已存在的上级表；失败回滚全部新表页。`vmm_boot_protect` 仅供
  集成阶段使用：只接受当前内核+栈高半区范围内的既有 4 KiB 叶子，
  先整段验证后首次存储，把 CPU/MM/core 数据区收紧为 RW/NX。
- **变更理由**：W^X 消除「可写又可执行」的内核攻击面/自伤面；单存储发布
  保证并发观察者（以及故障路径）看不到半成品映射；boot_protect 范围
  白名单防止早期集成代码误改任意映射。
- **证据文件**：`kolibrios64/kernel/mm/vmm.inc`（映射规则、
  vmm_boot_protect）、`kolibrios64/kernel/mm/api.inc`、
  `kolibrios64/kernel/core/foundation.inc`（数据区 RW/NX 收紧调用）。

### 4.4 早期内存池：malloc/slab → 16 MiB bump heap（无 free、失败全回滚）

- **32 位做法**：内核 heap（`core/heap.inc`）+ slab（`core/slab.inc`）
  通用分配器。
- **64 位做法**：public 窗口基址处固定 16 MiB bump heap：初始映射
  64 KiB，按 4 KiB 页惰性增长（RW/NX），bump 指针分配、调用方给 2 的幂
  对齐（16..4096，0 视为 16），**没有 free**。增长中任何一次映射失败：
  逆序 unmap 本次新增页并把数据页归还 PMM；新建页表页有意保持 pin
  （与 VMM 回滚契约一致，不回收表页）。
- **变更理由**：早期阶段只需要单调增长的少量内存，bump 指针无元数据、
  无碎片、失败语义简单到可以完整回滚并被失败注入验证；通用分配器
  留待后续里程碑。
- **证据文件**：`kolibrios64/kernel/mm/heap.inc`、
  `kolibrios64/kernel/mm/api.inc`（MM_HEAP_*）；
  `kolibrios/kernel/trunk/core/heap.inc`、
  `kolibrios/kernel/trunk/core/slab.inc`（32 位对照）。

### 4.5 可测性：无 → 失败注入钩子（第 N 次分配失败）

- **32 位做法**：无失败注入机制，分配失败路径不可主动触发。
- **64 位做法**：`mm_pmm_hook` 倒计数钩子：`mm_test_fail` 置 N 使第 N 次
  表页分配返回 ENOMEM，`mm_test_fail_hits` 记录命中次数。实现要点被
  写成注释与静态断言：分支必须测试 dec 后 EAX 中的倒计数，而非 hits
  inc 的 flags（历史缺陷：`inc` 覆盖 flags 致 `jnz` 误判、钩子永不触发）。
  QEMU 集成判定冻结 `hook_hits = 3`。
- **变更理由**：PMM/VMM/heap 的卖点是失败回滚，而回滚路径在真机上几乎
  不会自然发生；确定性注入使「第 N 次失败必须完整回滚」成为可重复断言。
- **证据文件**：`kolibrios64/kernel/mm/pmm.inc`（mm_pmm_hook）、
  `kolibrios64/tests/stage2-mm/check.py`（钩子 flags 顺序静态断言）、
  `kolibrios64/tools/run-qemu-debug.py`（EXPECTED_COUNTERS）。

---

## 5. 验证体系

### 5.1 总体：宿主直接运行测试 → 分层验证（静态门禁 + QEMU 唯一真实执行）

- **32 位做法**：`runtests.py` 等宿主侧测试直接运行程序/内核相关测试，
  执行环境即宿主。
- **64 位做法**：两层分工明确——
  - **宿主静态门禁**（原生 arm64 python3，不执行任何 guest 机器码）：
    `tests/stage2-cpu/check.py`、`tests/stage2-mm/check.py`、
    `tools/check-boot-contract.py`；
  - **QEMU TCG 全系统模拟**：唯一真实执行环境，
    `tools/run-qemu-debug.py` 统一入口（调试/冒烟双模式）。
- **变更理由**：内核代码需要 CR3/EFER/CPUID/真实物理内存等特权状态，
  宿主用户态无法诚实提供；静态门禁在字节级抓布局与契约漂移，QEMU
  提供可信 CPU 语义且崩溃完全隔离在 guest 内。
- **证据文件**：`kolibrios64/tests/stage2-cpu/check.py`、
  `kolibrios64/tests/stage2-mm/check.py`、
  `kolibrios64/tools/run-qemu-debug.py`；
  `kolibrios/kernel/trunk/runtests.py`（32 位对照）。

### 5.2 宿主静态门禁的四根支柱

- **32 位做法**：无对应机制。
- **64 位做法**：
  1. **双 ORG 汇编**：`tests/*/layout.asm` 按内核 include 方式包进探针，
     check.py 各汇编两次（org 0 与 org 0xFFFFFFFF80000000）：org 0 供
     宿主模型按物理地址读 fixture；高半区证明模块可链接于内核基址且
     相对布局不变、数据区严格封尾。
  2. **契约独立副本**：check.py 内的契约常量**刻意重复**而非 import
     汇编源（CPU/MM 模块内部也各持 CPU_*/MM_* 私有副本，
     `core/contracts.inc` 汇编期证明三份一致）；任何一侧漂移必须显式
     修另一侧，而不是自动「达成一致」。
  3. **字节级断言**：GDT/TSS/GDTR/IDTR、256 个 IDT 门、256 个 stub 逐字节
     重建比对（含哑元错误码规则、压栈顺序、16 字节对齐、shadow space、
     iretq 尾部）；导出函数统一 128 字节帧逐个验证；MM 侧由纯 Python
     主机模型直接从汇编产物字节中读页表/BootInfo/内存图，与 layout.asm
     内**手写** fixture 期望比对（模型漂移会失败而非自洽）。
  4. **负向 fixture**：对故意破坏的源码副本重跑整条流水线，要求每一个
     都必须失败（fail-closed）。
  另有 `check-boot-contract.py`：解析 include 图并构建跨文件控制流图，
  结构化解码调用/跳转与寄存器证明，验证 ExitBootServices 重试重新发布
  内存图、首次尝试后无路径可达 UEFI 控制台、COM1 路径真实存在于产物、
  自举 OUTDIR 与内核 OUTDIR 隔离。
- **变更理由**：不运行机器就能覆盖「布局、契约、控制流」三类最容易
  静默漂移的性质；独立副本 + 手写期望 + 负向 fixture 三招合起来防止
  「测试与实现一起错」。
- **证据文件**：`kolibrios64/tests/stage2-cpu/layout.asm`、
  `kolibrios64/tests/stage2-cpu/check.py`、
  `kolibrios64/tests/stage2-mm/layout.asm`、
  `kolibrios64/tests/stage2-mm/check.py`、
  `kolibrios64/kernel/core/contracts.inc`、
  `kolibrios64/tools/check-boot-contract.py`、
  `kolibrios64/tools/test-boot-contract-checker.py`。

### 5.3 QEMU TCG：唯一真实执行环境

- **32 位做法**：无强制模拟环境约定。
- **64 位做法**：`tools/run-qemu-debug.py`（Darwin arm64 宿主，QEMU x86_64
  仅 TCG）：私有产物目录构建 + vvfat ESP（EFI/BOOT/BOOTX64.EFI、
  EFI/KERNEL64.BIN、startup.nsh）+ 仓库自带 OVMF.fd + COM1 落 serial.log
  + gdbstub 仅绑定 127.0.0.1。默认 `-S` 冻结调试（lldb 经
  `tools/gen-kernel-symbols.py` + `tools/kernel64-elf.asm` 产出的符号
  ELF 下硬件断点；TCG 无 watchpoint）；`--no-freeze` 冒烟模式判定冻结：
  十个里程碑 `K64-BOOT-OK → K64-CPU-OK → K64-PMM-OK → K64-VMM-OK →
  K64-HEAP-OK → K64-IRQ-OK → K64-INPUT-OK → K64-GUI-OK →
  K64-DESKTOP-OK → K64-FOUNDATION-OK` 各恰好一次且按序
  （K64-IRQ-OK 随 IRQ-v1、K64-INPUT-OK 随 IRQ-v2、K64-GUI-OK 随
  GUI-G1、K64-DESKTOP-OK 随桌面壳插入，见 §6/§7/§8/§9），禁止
  `K64-CPU-PANIC`/`K64-BOOT-FAIL`，CPU 自测 passed = 0x5FF（串口报告行），
  十六计数器 `mm_mask = 0`、`hook_hits = 3`、`core_mask = 0`、
  `irq_mask = 0`、`irq_test_ticks = 11`、`input mask = 0`、
  `kbd_count = 6`、`mouse_count = 6`、`stage = 6`、
  `video_selftest_mask = 0`、`desktop mask = 0`、`desktop stage = 5`、
  `keys = 2`、`cursor = (557,411)`、`gui_frame_stage = 3`（成功路径
  不打印，经 gdbstub 从 guest 内存读取；irq 两项随 IRQ-v1、video
  一项随 GUI-G1、input 四项随 IRQ-v2、desktop 六项随桌面壳新增，
  ticks 终值 10→11 的推导见 §8.7，kbd/mouse 2/3→6/6 与 desktop
  计数推导见 §9.5）。冒烟循环另驱动 IRQ-v2 输入握手与桌面壳握手：
  经 gdbstub 轮询 input_test_stage / desktop_test_stage、按 stage 经
  monitor 注入 sendkey/mouse_move/mouse_button（见 §8.8/§9.6）；
  GUI 帧取证窗口经 gdb_write_mem 'M' 包写 RELEASE 放行 guest 有界
  自旋（§9.5）。像素判定为**三帧取证**：screen-gui.ppm（28 探针）+
  screen-echo.ppm（回显帧）+ screen.ppm（终帧），另有 -d int 向量
  交付计数断言（ok 模式 v21=6 / v2c=18，skip 模式 2/9），见 §9.6；
  `--gui-skip` 回退判定模式（随 GUI-G1 新增，桌面壳扩展为
  GUI-SKIP + DESKTOP-SKIP 级联，见 §7.6/§7.8/§9.7）。默认 CPU
  `qemu64,+popcnt`（裸 qemu64 缺 MM 所需 POPCNT）。QEMU 进程组随退出/中断整体清理，不留 stray 进程。
  注意：QEMU TCG 自行翻译 guest 指令，**与宿主 Rosetta 无关**；正常启动
  在 `-d cpu_reset` 下有 2 次复位（上电 + OVMF 平台复位），判异常须排除。
- **变更理由**：TCG 提供确定性的特权级全系统环境，失败即 guest 内三重
  故障/panic，可观测、可复现、与宿主完全隔离；冻结数值判据使「跑通」
  变成可回归的硬门禁而非目测。
- **证据文件**：`kolibrios64/tools/run-qemu-debug.py`、
  `kolibrios64/tools/gen-kernel-symbols.py`、
  `kolibrios64/tools/kernel64-elf.asm`、`kolibrios64/OVMF.fd`、
  `kolibrios64/kernel/core/foundation.inc`（里程碑发射顺序）。

### 5.4 决策记录：弃用 Rosetta 进程内执行验证

- **32 位做法 / 曾经的 64 位尝试**：早期 stage2-mm 门禁含进程内执行层：
  宿主 Python 以 `arch -x86_64`（Rosetta 转译）经 ctypes mmap arena
  直接执行 fasm2 产出的裸机器码，配跳转桩重定位与 `--exec-child` 子进程。
- **64 位现行做法**：该执行层已**整体删除**（不留双轨），check.py 仅在
  原生 arm64 python3 下运行静态字节断言 + 纯 Python 主机模型，无任何
  `arch -x86_64` 调用；行为覆盖由模型夹具 + 静态断言承接，端到端 CPU
  语义验证全部迁移至 QEMU 集成阶段（§5.3）。
- **变更理由（决策记录）**：
  1. Rosetta 是不可控黑盒：低 4 GiB 保留、翻译行为不透明，无法诚实
     模拟内核所需地址空间；
  2. 进程内执行使每次探针失败都变成宿主 Python 崩溃与 macOS
     ReportCrash 弹窗，惊扰用户并污染诊断报告；
  3. 被否方案：子进程隔离 + Mach EXC_CRASH 崩溃报告抑制 + 预期故障
     returncode 断言——技术上已实证可行，仍被判定为不必要的复杂化而
     弃用。
  失效条件：将来引入可控 x86-64 原生宿主或用户级模拟器（如 Unicorn）
  可重新评估。该决策已写入仓库（check.py 第 7 节注释），非仅口头约定。
- **证据文件**：`kolibrios64/tests/stage2-mm/check.py`（section 7，
  「behavioural coverage lives in the QEMU integration stage」注释完整
  记录删除原因与承接方案）、`kolibrios64/tools/run-qemu-debug.py`。
  （历史崩溃次数、检查计数 1403→1309 等细节来自项目决策记录，仓库内
  无计数存档，标注为决策记录口径。）

---

## 6. IRQ-v1：LAPIC 定时器闭环（提交 `983111c`）

> 状态：已通过独立验证与收口审查（含两项必改修复：ISR 降序冲刷、GOP
> 命中标记守卫），已随提交 `983111c` 入库。提交范围：kernel/irq 新模块、
> mm 的 vmm_map_mmio 与 bootmap 放宽、cpu 异步分发、boot GOP 交接、
> core 集成、tools 扩展、tests/stage2-irq 及 cpu/mm 测试扩展、本文档；
> **不含** kernel/video 与 tests/stage2-fb（GUI 轨当时未集成，后随
> GUI-G1 收口提交，见 §7）。

### 6.1 中断控制器：PIC 兜底 + APIC 可选双轨 → 纯 LAPIC xAPIC MMIO

- **32 位做法**：`irq_mode` 变量在 IRQ_PIC/IRQ_APIC 间切换，PIC_init 兜底、
  APIC_init→LAPIC_init 可选升级；LAPIC_init 的 ISR 冲刷为**升序**
  ISR0→ISR7（`add edx, 0x10`）。
- **64 位做法**：纯 LAPIC xAPIC，冻结顺序：vmm_map_mmio 把架构物理地址
  0xFEE00000 映射到固定 VA 0xFFFFC00002000000（PCD|RW|NX 单 4 KiB 页）→
  8259 完整 ICW1-4 重映射 0x20/0x28 + OCW1 全屏蔽并**读回验证**（端口
  0x80 posted-write 延迟）→ IA32_APIC_BASE MSR 0x1B 验证 base 与 bit 11
  并显式写回 → TPR=0、SVR=0x1FF（使能 + spurious 0xFF）、四个 LVT 全
  屏蔽 → ISR **降序** ISR7→ISR0 冲刷固件残留在服位，每个快照位一次
  EOI，上界 8×32=256 次，无无界等待。
- **变更理由**：单 CPU 首阶段不需要 IOAPIC/双轨；EOI 的硬件语义是清
  **最高**在服向量而非软件正在看的位——升序扫描会把 EOI 耗在更高位
  上、低位永留在服（32 位代码恰为升序，此为收口审查必改修复点）；
  读回/MSR 验证把「以为屏蔽了」变成证据。
- **证据文件**：`kolibrios64/kernel/irq/api.inc`、`lapic.inc`、`irq.asm`；
  `kolibrios/kernel/trunk/core/apic.inc`（PIC_init/APIC_init/irq_mode、
  升序冲刷，32 位对照）。

### 6.2 时钟：PIT 100Hz tick + HPET/PIT refresh 校准 → LAPIC periodic + PIT ch2 校准

- **32 位做法**：PIC 模式下 PIT channel 0 mode 2（0x34）100Hz tick；
  APIC 模式 LAPIC timer 用 divide=1、最大初值 + delay_ms(640) 校准，
  delay_ms 优先 HPET get_clock_ns，否则端口 0x61 refresh 位轮询。
- **64 位做法**：LAPIC timer periodic，vector 0x20，10 ms/100 Hz 冻结
  契约。校准用 PIT channel 2 mode 0（gate 开、speaker 关），0xFFFF 窗口
  （约 54.9 ms @1.193182 MHz），有界轮询 0x61 的 OUT2 位；divide=16，
  三轮取中值，±5% sanity（标称 62.5 MHz = TCG 固定 1 GHz 总线钟/16），
  显式除零守卫。periodic 初值 = 实测 irq_calib_hz/100，**非硬编码**
  （标称 625000 仅作 sanity 锚点，汇编期断言推导关系）；tick 计数为
  64 位 qword（irq_tick_count 终身计数 / irq_test_ticks 每腿复位）。
- **变更理由**：实测推导使频率在 ±5% 带内漂移时 tick 仍为 10 ms；中值
  抑制单轮抖动；PIT mode 0 的 +1 周期 quirks 为 1/65536 量级，远低于
  sanity 带，显式记录后忽略；所有循环有界。
- **证据文件**：`kolibrios64/kernel/irq/calib.inc`、`api.inc`、
  `selftest.inc`；`kolibrios/kernel/trunk/core/apic.inc`（0x34/100Hz、
  delay_ms 校准）、`kolibrios/kernel/trunk/kernel.asm`（delay_ms 的
  HPET/PIT refresh 双路，32 位对照）。

### 6.3 IRQ 分发：56 槽 handler 链表 → 单回调 + dispatch 异步分支

- **32 位做法**：`IRQ_RESERVED = 56` 槽 irqh_tab handler 链表 + 池分配，
  中断到来逐槽 ack 认领，统一 EOI。
- **64 位做法**：`cpu_irq_set_handler` 单回调注册（handler 须高半区
  规范地址、Ring0、IF=0；已注册非零再注册报 EALREADY，传 0 注销）。
  `cpu_trap_dispatch` **先**走 armed-expect 四事实精确匹配（逐字节不
  变），未匹配才进异步分支：vector ≥ CPU_IRQ_VECTOR_MIN(0x20) 且有
  注册 handler，以 RCX=frame 调用。`irq_dispatch`：0x20 增 tick 计数、
  记录 ISR/IF 证据后写 EOI（先于 stub 的 iretq）；0xFF 只计 spurious
  数**不 EOI**（LAPIC 契约）；未知向量在 handler 内直接 cpu_panic。
- **变更理由**：单 CPU 首阶段最小化；复用既有 256 stub/184 字节 frame/
  统一 128 字节帧，零新增异常机械；expect 优先保证自测语义不被异步
  分支吞掉；EOI 先于 iretq 避免同向量重入风暴。
- **证据文件**：`kolibrios64/kernel/cpu/idt_traps.inc`、
  `kolibrios64/kernel/irq/dispatch.inc`；
  `kolibrios/kernel/trunk/core/irq.inc`（32 位对照）。

### 6.4 MMIO 映射：PG_NOCACHE 直接映射 → vmm_map_mmio 公开 API

- **32 位做法**：map_io_mem + PG_NOCACHE（PG_PCD+PG_PWT）直接映射
  LFB/LAPIC/HPET，无所有权区分。
- **64 位做法**：`vmm_map_mmio(rcx=VA, rdx=PA)`：叶子固定 PCD|RW|NX，
  无 flags 参数，仅 public 窗口内 4 KiB 页；PA **负向判定**——目标页
  不得 eligible（conventional 内存必须走 vmm_map_page），MMIO/保留页
  豁免所有权检查（GOP framebuffer 实测缺席全部 memmap 描述符，故不
  做物理范围/重叠检查）；heap 范围与 core 测试 PML4 槽拒绝 EPERM，
  既有叶子 EALREADY；表页仍经 mm_pmm_alloc_table，pin/核算不变量不破。
  bootmap 的 flags 校验从「仅 MEMORY_MAP」放宽为掩码规则（未知位拒绝、
  MAP 必需、FRAMEBUFFER 可选），GOP 块按 flag 条件校验：无 flag 全零，
  有 flag 逐字段验证。
- **变更理由**：MMIO 与 RAM 的所有权语义相反，正向 eligible 检查会拒绝
  合法设备页；条件校验使 BootInfo v1 零结构变更地容纳可选特性。
- **证据文件**：`kolibrios64/kernel/mm/vmm.inc`（vmm_map_mmio）、
  `kernel/mm/bootmap.inc`、`kernel/mm/api.inc`；
  `kolibrios/kernel/trunk/const.inc`（PG_NOCACHE）、
  `kolibrios/kernel/trunk/kernel.asm`（map_io_mem 调用，32 位对照）。

### 6.5 GOP 交接：VESA/BIOS → UEFI GOP（随 IRQ-v1 提交 `983111c`）

- **32 位做法**：引导期 VESA/BIOS 设置显示模式（`boot/bootvesa.inc`）。
- **64 位做法**：加载器 LocateProtocol(GOP) → 有界 QueryMode 扫描
  （MaxMode 与 GOP_MAX_MODES=64 双重上界）找 1024x768 精确匹配；
  **命中标记守卫**：仅精确匹配路径置 gop_match=1，未命中路径（预算
  耗尽/QueryMode 失败/空 Info）绝不 SetMode（收口审查必改修复点）；
  SetMode 失败保留当前模式；BltOnly 无线性帧缓冲拒绝；Mode/Info 为
  EfiBootServicesData，必须在 EBS 前拷贝；帧缓冲须落在 identity 低
  4 GiB 内；一切失败非致命，BootInfo GOP 字段置零 + flag 清零回退。
  BootInfo 仅预留字段填值，208 字节结构零变更（8 字节对齐断言守住，
  见 §6.7 回归记录）。
- **变更理由**：未验证的 mode 号调 SetMode 是野固件调用；GOP 失败不得
  阻断启动（串口仍是主控制台）；EBS 前拷贝避免访问已死固件内存。
- **证据文件**：`kolibrios64/kernel/boot/bootx64.asm`（gop_handover）、
  `kernel/boot/bootinfo.inc`、`kernel/mm/bootmap.inc`；
  `kolibrios/kernel/trunk/boot/bootvesa.inc`（32 位对照）。

### 6.6 IF 纪律与自检：很晚 sti 常开 → 全程 IF=0 + 唯一有界 STI 窗口

- **32 位做法**：初始化大部完成后才 sti（`kernel.asm`），此后中断长期
  常开，无窗口化纪律。
- **64 位做法**：foundation 全程 IF=0；本次提交范围内全内核**唯一一条
  sti** 在 `irq/selftest.inc` 的 irq_sti_wait 助手内，助手返回前必 cli，
  无路径泄漏 IF=1。九腿自检：PIC 读回/SVR/one-shot 恰 1 tick/periodic
  恰 20 tick/mask 窗口冻结/skip-EOI 阻塞证明/IF 证据对（handler 当前
  RFLAGS.IF=0 + 被中断帧 IF=0x200）/校准带内/final 恰 10 tick。退出时
  timer 屏蔽、IF=0、irq_test_ticks=10、mask=0，冻结供 gdbstub 门禁。
  （ticks=10 为 IRQ-v1 退出口径；IRQ-v2 input 补证腿后全程终值为 11，
  见 §8.7。末腿 mask 的运行时证据缺口当时未闭合，后由 IRQ-v2
  masked-oneshot 腿补证，见 §8.7。IRQ-v2 延续本纪律：input 模块零
  STI，窗口全走 irq_sti_wait。桌面壳起全内核为**两个**合法 STI
  拥有者：irq_sti_wait 与 desktop_run 常驻循环，见 §9.4。）
- **变更理由**：有界窗口使中断行为完全可断言；IF 证据对证明中断门
  语义（handler 内 IF=0）与窗口上下文（IF=1）同时成立。
- **证据文件**：`kolibrios64/kernel/irq/selftest.inc`、
  `kernel/core/foundation.inc`、`tools/run-qemu-debug.py`；
  `kolibrios/kernel/trunk/kernel.asm`（sti 位置，32 位对照）。

### 6.7 验证口径更新（本次撰写时本地重跑实测，全部 0 失败）

- **门禁计数**：stage2-cpu 524、stage2-mm 1688、stage2-irq 295
  （时为 IRQ-v1 计数；IRQ-v2 扩至 317，见 §8.9）、
  stage2-fb 430（时为 GUI 轨独立交付计数，不在 IRQ-v1 提交内；该轨
  现已集成收口并扩至 449，见 §7.8）；check-boot-contract
  106 项 PASS；其负向变异测试 62/62 夹具 + 57/57 寄存器模型查询 +
  2/2 清理检查。
- **冒烟口径**：7 标记按序各恰一次（新增 K64-IRQ-OK），passed=0x5FF，
  五计数器（新增 irq_mask=0、irq_test_ticks=10，经 gdbstub 读取）。
  （此为 IRQ-v1 收口时口径；GUI-G1 起为 8 标记/六计数器，IRQ-v2
  起为 9 标记/十计数器，桌面壳起为 10 标记/十六计数器，见
  §5.3/§7.8/§8.9/§9.7。）
- **收口审查修复记录**：① ISR 降序冲刷（§6.1）；② GOP 命中标记守卫
  （§6.5）；③ BootInfo 8 对齐回归——GOP 字段填值不得破坏 208 字节
  布局的 8 字节对齐，`bootinfo.inc` 的 `sizeof mod 8` 汇编期断言守住。
- **证据文件**：`kolibrios64/tests/stage2-irq/`、`tests/stage2-cpu/`、
  `tests/stage2-mm/`、`tests/stage2-fb/`、`tools/check-boot-contract.py`、
  `tools/test-boot-contract-checker.py`、`tools/run-qemu-debug.py`、
  `kernel/boot/bootinfo.inc`。

---

## 7. GUI-G1：图形输出闭环（提交 `7b2c4b7`）

> 状态：已通过独立验证与收口审查（2 必改 + 3 建议全采纳），已随
> 提交 `7b2c4b7` 入库。
> 提交范围：kernel/video 新模块、core 集成（contracts.inc /
> foundation.inc / kernel64.asm include 顺序）、tools/run-qemu-debug.py
> 冒烟判定扩展、tests/stage2-fb（含 check-gui-judge.py）及本文档。
> GOP 交接的加载器改动已随 IRQ-v1 提交 `983111c` 入库，本章仅作
> 上下文简述（§7.1，详见 §6.5）。

### 7.1 GOP 交接上下文（已随 IRQ-v1 入库，详见 §6.5）

- **32 位做法**：引导期 VESA/BIOS 设置显示模式（`boot/bootvesa.inc`）。
- **64 位做法**：加载器 LocateProtocol(GOP) → 有界 QueryMode 枚举
  （MaxMode 与 GOP_MAX_MODES=64 双重上界）→ 仅精确 1024x768 命中
  （gop_match=1 守卫）才 SetMode；BltOnly 无线性帧缓冲拒绝。一切失败
  非致命：未命中保留固件 live mode（OVMF 默认 800x600，--gui-skip
  场景实测 mode 0x2）仍如实置 FLAG_FRAMEBUFFER。BootInfo GOP 字段
  偏移 @88（fb PA）/96（size）/104（width）/108（height）/112（format）/
  116（pitch），208 字节结构零变更、8 字节对齐断言守住。本轮冒烟
  实测：BootInfo @0x405158，GOP fb PA 0x80000000，1024x768 BGRX
  （mode 0x6）。
- **变更理由**：GOP 失败不阻断启动、live mode 如实交接，是 §7.4
  GUI-SKIP 回退路径的前提（完整理由见 §6.5）。
- **证据文件**：`kolibrios64/kernel/boot/bootx64.asm`（gop_handover）、
  `kernel/boot/bootinfo.inc`、`kernel/video/api.inc`（GOP 字段偏移
  私有副本 + 断言）；实测数字来自本轮冒烟 serial.log（build/ 产物
  目录，不入库）。

### 7.2 显示体系：VESA/LFB + 自有驱动 → 直接消费 GOP 物理 framebuffer

- **32 位做法**：VESA/LFB 加自有显示驱动体系（`drivers/video/`、
  `kernel/trunk/gui/`），LFB 经 map_io_mem + PG_NOCACHE 直接映射
  （§6.4）。
- **64 位做法**：首阶段不建显示驱动，直接消费 UEFI GOP 交接的物理
  framebuffer。FB 窗口 `FB_VA = 0xFFFFC00001000000` 恰起于 heap
  limit、PML4 slot 384，上界严格低于 LAPIC VA 0xFFFFC00002000000，
  得 16 MiB / 4096 页映射预算（全部汇编期断言）；1024x768x4 = 3 MiB
  参考帧经 `vmm_map_mmio` 逐 4 KiB 页映射 768 页（PCD|RW|NX）。
  PA 走 mm 的**负向判定**：framebuffer 实测缺席全部 UEFI memmap
  描述符，非 eligible 即放行（§6.4）。几何上限 2560x1600、仅 BGRX、
  size ≤ 16 MiB、pitch ≥ width、PA 页对齐；模块对外唯一依赖是
  vmm_map_mmio。栈帧为模块自有 video_enter/leave（8 push + sub 40：
  32 shadow + 第五参数槽），与 §3.3 同一 MS x64 ABI，帧形与 CPU/MM
  的 128 字节帧不同，门禁按模块宏逐函数断言。
- **变更理由**：直接消费 GOP 交接绕开实模式 VESA 与驱动枚举，是最短
  图形闭环；FB 窗口夹在 heap 与 LAPIC 之间使「不重叠、有预算」成为
  可静态断言的性质。
- **证据文件**：`kolibrios64/kernel/video/api.inc`（窗口/预算断言与
  video_enter 宏）、`kernel/video/fb.inc`（video_init）、
  `kernel/mm/vmm.inc`（vmm_map_mmio）；
  `kolibrios/drivers/video/`、`kolibrios/kernel/trunk/gui/`（32 位对照）。

### 7.3 像素格式与集成期抓修的两个真实缺陷

- **32 位做法**：无字节级颜色顺序断言，无外部像素真值判定。
- **64 位做法**：BGRX（B 在低字节，X 字节写 0）；0x00RRGGBB 的小端
  存储恰为 B,G,R,X 字节序，故 `video_bgrx` 仅 `and eax, 0x00FFFFFF`
  丢弃 alpha，掩码即全部转换。集成期抓修两个真实缺陷：
  ① **bswap 双重转换**：颜色路径多一次 bswap 使存储变 R,G,B,X，
  屏幕上红蓝互换——串口全绿无法发现，由 screendump 探针抓出
  （draw.inc 注释记录在案，负向夹具 bgrx-byteswap 钉死）；
  ② **几何寄存器被破坏**：width/height/pitch/format 暂存
  caller-saved r8-r11，被 768 次 vmm_map_mmio 调用破坏后发布垃圾
  几何；修复为映射完成后从 r12 持有的 BootInfo 重载再发布
  （负向夹具 stale-geometry 钉死）。
- **变更理由**：两个缺陷都是「调用序列成功但结果错」类真实 bug，
  其中 bswap 缺陷只能靠像素真值（§7.6）抓住；修复后均以负向
  夹具固化，防回退。
- **证据文件**：`kolibrios64/kernel/video/draw.inc`（video_bgrx 及其
  注释）、`kernel/video/fb.inc`（.mapped 重载段）、
  `tests/stage2-fb/check.py`（bgrx-byteswap/stale-geometry 夹具）。

### 7.4 几何门与失败语义（冻结口径）

- **32 位做法**：无对应分级失败语义。
- **64 位做法**：ENOTSUP **只**来自几何门——video_init 的格式/
  宽高上限/size 窗口，与 video_selftest 的非精确 1024x768 拒绝——
  foundation 视为非致命，发 `K64-GUI-SKIP` 后继续；
  FLAG_FRAMEBUFFER 缺失同样 SKIP。映射中途任何一次 vmm_map_mmio
  失败 → `VIDEO_EIO(-9)` → panic，**绝不透传原始 MM 状态**
  （MM_ENOTSUP 若透传会被 foundation 误读为几何 skip）；
  mapped_pages 在每条退出路径保留精确进度供诊断。该口径在
  api.inc 状态码注释与 foundation.inc 冻结，负向夹具
  map-fail-passthrough 钉死。
- **变更理由**：结构性 MM 故障若伪装成可跳过的几何不匹配，会静默
  吞掉真实缺陷；错误码语义分区使 SKIP 路径可信。
- **证据文件**：`kolibrios64/kernel/video/api.inc`、`fb.inc`、
  `kernel/core/foundation.inc`（.gui_skip 分支）、
  `tests/stage2-fb/check.py`。

### 7.5 字体：charUni.mt 全量字体 → 95 字形子集嵌入 + sha256 双重冻结

- **32 位做法**：`kernel/trunk/gui/charUni.mt` 全量字体文件，按码点
  索引字形。
- **64 位做法**：提取 0x20..0x7E 共 95 字形 × 16 字节 = 1520 字节
  嵌入 `font.inc`（每行 1 字节，MSB 为最左像素；源文件 GPLv2，与本
  仓库 LICENSE 一致）。sha256 双重冻结：stage2-fb/check.py 与
  check-gui-judge.py 各钉同一哈希；32 位基线文件不可达时，judge 从
  模块内嵌字节回退推导字体，仍必须过同一 sha256（收口建议修复，
  回退推导与冻结子集逐字节相等有专项断言）。
- **变更理由**：复用基线字体避免手绘漂移；双重冻结 + 回退推导使
  「字体漂移」在两侧门禁都必然失败，且基线缺席不会静默改变渲染帧。
- **证据文件**：`kolibrios64/kernel/video/font.inc`、
  `tests/stage2-fb/check.py`（FONT_SHA256）、
  `tests/stage2-fb/check-gui-judge.py`（font_bytes/derived_font）；
  `kolibrios/kernel/trunk/gui/charUni.mt`（32 位基线）。

### 7.6 draw-only 自检与像素真值裁决（验证口径显式声明）

- **32 位做法**：无自检画面判定机制。
- **64 位做法**：`video_selftest` 为 **draw-only（无 readback）**，
  冻结图案四层按序：8 色条各 128 列（红绿蓝黄青品红白黑）→ 1px
  白边框 → 中央 (412,284)-(611,483) 200x200 0x808080 矩形 → 左上
  (8,8) "K64 GUI-G1" 白色文本（8x16，10 字形）。图案 spec 冻结在
  api.inc 顶部表格，坐标绝对不缩放（仅接受 1024x768）；失败层号
  记入 video_selftest_mask（1 bars / 2 border / 3 rect / 4 text）。
  **像素真值由外部裁决**：冒烟在终止标记后经 QEMU monitor
  screendump 采帧到 screen.ppm，run-qemu-debug.py 以纯 Python 解析
  PPM P6，比对 28 个冻结探针。**验证口径：串口标记 + 计数器全绿
  ≠ 画面正确**——串口证据只证明绘制调用序列与状态，像素正确性
  只能由 screendump 探针裁决。
- **变更理由**：无 readback 使 FB 保持只写证据、语义最简；外部像素
  裁决补上「画成功但画错」的检测缺口（能力边界见 §7.9）。
- **证据文件**：`kolibrios64/kernel/video/api.inc`（spec 表）、
  `kernel/video/selftest.inc`、`tools/run-qemu-debug.py`
  （PPM_PROBES 与 P6 解析）、`tests/stage2-fb/check-gui-judge.py`。

### 7.7 收口审查修复记录（2 必改 + 3 建议全采纳）

- **必改① size 上界守卫**：取整前 `cmp size, 16 MiB`——size≈2^64 时
  `add 4095` 回绕成 0 页，绕过页预算门在未映射 VA 上发布 ready=1；
  该守卫同时使 pitch*4 远离 32 位 stride 回绕（负向夹具
  size-wrap-unguarded）。
- **必改② blit 源行距补偿**：源行恒按**原始 w** 紧密打包，右缘部分
  裁剪后每行源指针补偿 `(orig_w - clipped_w) * 4`，修复第二行起读错
  源像素的缺陷（负向夹具 blit-src-stride）。
- **建议③ 映射故障 EIO 区分**：见 §7.4（负向夹具
  map-fail-passthrough）。
- **建议④ judge 字体基线缺失容错**：回退推导仍过 sha256 冻结，见
  §7.5。
- **建议⑤ foundation.inc 尾换行**：文件尾补换行（卫生项）。
- **证据文件**：`kolibrios64/kernel/video/fb.inc`、`draw.inc`
  （修复点注释）、`tests/stage2-fb/check.py`（三个新负向夹具）、
  `tests/stage2-fb/check-gui-judge.py`、`kernel/core/foundation.inc`。

### 7.8 集成与验证证据（实测，全部 0 失败）

- **foundation 顺序**：core_selftest 之后 video_init →
  vmm_boot_protect（video 数据区 RW|NX 恰 1 页）→ video_selftest →
  `K64-GUI-OK` → `K64-FOUNDATION-OK`。GUI 有意放在 leg7 表页核算
  基线之后：冻结的 3/3/3 表页 delta 不受影响（FB 映射仅新增 PT 页）。
- **宿主静态门禁**（原生 arm64 python3，不执行 guest 机器码）：
  check-boot-contract FAILURES 0；其负向变异测试 62/62 夹具 +
  2/2 清理检查 + 57/57 寄存器模型查询；stage2-cpu 524、stage2-mm
  1688、stage2-irq 295（时为 GUI-G1 时计数；IRQ-v2 扩至 317，见
  §8.9）、stage2-fb 449（11 负向夹具全部被抓，含
  size-wrap-unguarded / blit-src-stride / map-fail-passthrough；
  桌面壳扩至 493，见 §9.7）；
  check-gui-judge 21（时为 GUI-G1 计数，judge 纯函数正基线 + 12
  负向变异全部被抓；IRQ-v2 扩至 35，见 §8.9；桌面壳扩至 92，
  见 §9.7）。
- **QEMU TCG 冒烟**（唯一真实执行环境，禁 Rosetta 口径延续 §5.4）：
  128/256 MiB 多轮；8 串口标记按序各恰一次（K64-GUI-OK 位于 IRQ 与
  FOUNDATION 之间）；passed=0x5FF；6 计数器经 gdbstub 读取全为冻结
  值（mm_mask=0、hook_hits=3、core_mask=0、irq_mask=0、ticks=10、
  video_selftest_mask=0；ticks=10 为 GUI-G1 时口径，IRQ-v2 起为 11，
  见 §8.7）；screendump 1024x768，28 探针全绿。
- **--gui-skip 回退场景**（GOP_MAX_MODES=0 加载器）：保留固件 live
  mode 800x600，`K64-GUI-SKIP` + `K64-FOUNDATION-OK`，无 panic、
  无像素断言。
- **独立验证者复核**：全帧 786432 像素（1024x768）与冻结 spec 模型
  逐一比对，0 差异。
- **坏内核 panic profile**：变异内核触发 #UD，rip 精确命中变异 VA，
  证明 panic 诊断路径有效。
- **证据文件**：`kolibrios64/kernel/core/foundation.inc`、
  `kernel/video/data.inc`（恰 1 页数据区断言）、
  `tests/stage2-cpu/`、`tests/stage2-mm/`、`tests/stage2-irq/`、
  `tests/stage2-fb/`（check.py + check-gui-judge.py）、
  `tools/check-boot-contract.py`、`tools/test-boot-contract-checker.py`、
  `tools/run-qemu-debug.py`；冒烟实测数字来自本轮 build/ 产物
  （g1-final-128 / g1-smoke-256 / g1-skip 等，不入库）。

### 7.9 已知遗留（如实记录，不粉饰）

- `draw.inc` 文件尾无换行（卫生项，下次顺手；本轮实测文件以 ret
  结尾无 0x0A）。（此项已随桌面壳改动修复，见 §9.7；本节保留
  当时口径。）
- draw-only 自检的数据变异（如颜色常量漂移）依赖外部 screendump
  探针检测——这是**能力边界而非漏检**：串口/计数器无法区分「画对」
  与「画错」，口径已在 §7.6 显式写明。
- 末腿 timer mask 的运行时证据缺口当时留待 IRQ-v2；现已由 IRQ-v2
  masked-oneshot 腿补证收口（见 §8.7）。

---

## 8. IRQ-v2：输入闭环（提交 `ddad22d`）

> 状态：已通过独立验证（8/8）与复验（6/6），收口审查（1 必改 +
> 5 建议全采纳）完成，已随提交 `ddad22d` 入库。（「1 必改」定性
> 后有勘误，见 §8.4 勘误注。）提交范围：kernel/input 新模块、
> irq dispatch 纯增量与向量契约、mm IOAPIC VA 契约、core 集成
> （contracts.inc / foundation.inc / kernel64.asm include 顺序）、
> tools/run-qemu-debug.py 注入编排与判定扩展、tests/stage2-input
> 新门禁及 stage2-irq / fb-judge 扩展、本文档。

### 8.1 32 位基线对照：保留语义、丢弃实现

- **32 位做法**：PIC+APIC 双轨（`kernel.asm` 顺序 PIC_init→PIT_init→
  APIC_init，irq_mode 切换，见 §6.1）；56 槽 irqh_tab handler 链表
  （§6.3）；键盘 `hid/keyboard.inc` 以 set1 keymap（`data32.inc`
  keymap/keymap_shift 行）+ 修饰键状态机解码；鼠标
  `drivers/mouse/ps2mouse4d` 走 0xA8 启鼠标口→0xFF 复位→0xF4 启报告
  序列，支持 3/4 字节包（滚轮）；`hid/mousedrv.inc` 做光标
  save/restore；事件经 EVENT_KEY=0x02 / EVENT_MOUSE=0x20 掩码
  （`const.inc`）送达 top-window 进程。
- **64 位做法**：保留语义——set1（translated）keymap 索引、修饰键 +
  break 0x80 + 0xE0 前缀、3 字节包 bit3 sync 校验 + 符号扩展；丢弃
  实现——OS_BASE 绝对布局（高半区契约取代）、多键盘链表/mutex（单
  键盘单回调）、locale/hotkey（冻结 en_US 双 map）、滚轮/加速度
  （首版仅 3 字节包）、用户态事件投递（首版事件留在内核态缓冲，
  桌面壳里程碑再消费）。
- **变更理由**：语义对齐使 32 位 keymap 行可直接对照复核，丢弃的
  都是多任务/多设备/本地化设施——单核首阶段没有它们的宿主，提前
  搬运会引入无法验证的复杂度。
- **证据文件**：`kolibrios/kernel/trunk/kernel.asm`（PIC_init/PIT_init/
  APIC_init）、`core/irq.inc`、`hid/keyboard.inc`、`data32.inc`、
  `hid/mousedrv.inc`、`const.inc`（EVENT_KEY/EVENT_MOUSE）、
  `drivers/mouse/ps2mouse4d/ps2mouse.asm`（0xA8/0xFF/0xF4）；
  `kolibrios64/kernel/input/keyboard.inc`（keymap 语义对照注释）、
  `mouse.inc`。

### 8.2 IOAPIC bring-up：间接窗口 + 派生 RTE 索引（含规格笔误纠错）

- **32 位做法**：APIC_init 只管 LAPIC，外部中断走 PIC 兜底，无
  IOAPIC RTE 管理（§6.1）。
- **64 位做法**：IOAPIC PA 0xFEC00000 经 `vmm_map_mmio` 映射到 VA
  0xFFFFC00003000000——严格高于 LAPIC VA、间距 16 MiB（同时越过
  整个 FB 窗口）、2 MiB 对齐、同 PML4 slot 384，全部汇编期断言
  （mm/api.inc），**零 MM API 改动**；IOREGSEL/IOWIN 间接读写叶子；
  VER 寄存器实测 sanity（version ≥ 0x10 且 ≠ 0xFF、项数 ≥ 13，
  实测值记入证据而不硬编码假设）；RTE pin1 向量 0x21、pin12 向量
  0x2C，索引按 **0x10+2n 汇编期派生**（0x12/0x13、0x28/0x29）；
  Fixed/Physical/active-high/edge，先写高 dword（dest 0）后写低
  dword，bringup 一律 mask=1（0x10021/0x1002C），四个 dword 全部
  读回精确比对；8259 保持全屏蔽（§6.1 口径延续）防双中断。
- **规格笔误纠错（如实记录）**：规格曾把鼠标 RTE 索引写成
  0x38/0x39——按派生公式 0x10+2n 这对应 **pin 20** 而非 pin 12。
  实施期以派生公式修正为 0x28/0x29，api.inc 汇编期断言「RTE 索引
  必须从引脚号派生」+ 负向夹具 mouse-rte-index-drift（变异回 0x38
  必须汇编失败）双重钉死。
- **变更理由**：派生公式 + 断言使「索引对不上引脚」这类规格/实现
  漂移在汇编期即失败；bringup mask=1 防止固件噪声在配置完成前
  竞态触发；VER sanity 用实测下限而非硬编码版本，换 QEMU 版本不
  静默失效。
- **证据文件**：`kolibrios64/kernel/input/ioapic.inc`、`api.inc`
  （派生断言）、`kernel/mm/api.inc`（MM_MMIO_IOAPIC_VA 六条断言）、
  `kernel/core/contracts.inc`（K64_*/MM_*/INPUT_* 三副本比对）、
  `tests/stage2-input/check.py`（ioapic-rte-mask-removed /
  mouse-rte-index-drift 夹具）。

### 8.3 8042 bring-up：冻结序列 + 全有界 + 致命失败语义

- **32 位做法**：ps2mouse4d 驱动内直接操作端口，无统一有界原语层，
  失败语义不分级。
- **64 位做法**：冻结序列——禁双口（0xAD/0xA7）→ flush（上界 64，
  返回 drain 数）→ 读 CCB（0x20）确认 **translation bit6=1**（SET
  0x43 / CLEAR 0x30，CPU 侧见 set1，与 32 位 keymap 语义对齐）→
  KIE/MIE → 启双口（0xAE/0xA8）→ 0xD4 前缀 + 0xFF 复位（ACK 0xFA +
  0xAA + ID 必须 0x00）→ 0xF6 默认参数 → 0xF4 启报告 → 尾 flush。
  一切等待有界；0xFE 重发 ≤ 5 次；**零预算立即失败**（防 dec/jnz
  回绕成 2^32 次自旋）。初始化失败 = 致命 panic：IOAPIC/8042 是
  强制平台设备，**没有 skip**（区别于 GUI 的 ENOTSUP 可跳过，
  §7.4）。
- **变更理由**：translation 位决定 CPU 看到的码制，不确认就复用
  set1 keymap 是静默错译；有界 + 重发上限 + 零预算兜底使 bring-up
  在设备缺席/异常时确定性失败而非挂死；强制设备失败若可跳过，
  后续自检会在无输入硬件的假环境上「通过」。
- **证据文件**：`kolibrios64/kernel/input/ps2.inc`（原语与序列注释）、
  `input.asm`（input_init）、`kernel/core/foundation.inc`（panic
  分支）；`kolibrios/drivers/mouse/ps2mouse4d/ps2mouse.asm`
  （32 位对照）。

### 8.4 dispatch 纯增量与 bit5 来源过滤（收口审查必改）

- **32 位做法**：irq 链表逐槽 ack 认领（§6.3），无来源过滤概念。
- **64 位做法**：单回调契约不变；`.keyboard`/`.mouse` 分支放在
  leave/ret **之后**纯增量——timer/spurious 的冻结字节序列不动
  （门禁断言单 ret、恰两个 EOI 写点、`.input_eoi` 直跳回 `.done`）；
  EOI 归 dispatch（`.input_eoi`），handler 是 IF=0 叶子（无 heap、
  无串口、无 EOI、无无界等待）。**bit5（AUX）来源过滤**：键盘叶子
  不消费鼠标字节（bit5 置位即返回），鼠标叶子不消费键盘字节
  （bit5 清零即返回），均不读 0x60 不动计数；未消费字节保持 OBF
  满 → 8042 保持另一口 IRQ 线 asserted（masked RTE 在 IOAPIC IRR
  锁存边沿），由对方叶子在自己的交付中消费——防双流互吞，此为
  收口审查**必改修复**。两个守卫极性相反（键盘 jnz / 鼠标 jz），
  各以独立负向夹具钉死（keyboard-aux-check-removed /
  mouse-aux-check-removed），共享钉桩无法证明两侧。
- **变更理由**：冻结字节不动使 IRQ-v1 的全部门禁断言原样回归；
  来源过滤把「谁消费哪个字节」变成硬件状态位裁决而非时序假设，
  双流同时 live 时计数仍精确。
- **证据文件**：`kolibrios64/kernel/irq/dispatch.inc`、`api.inc`
  （IRQ_VEC_KEYBOARD/MOUSE 冻结 + 排序断言）、
  `kernel/input/keyboard.inc`、`mouse.inc`（来源过滤注释）、
  `tests/stage2-irq/check.py`（two_eoi_sites / input_eoi_jmps_to_done
  等）、`tests/stage2-input/check.py`。
- **勘误注（桌面壳收口审查 I1，如实记录；上方历史正文不改写）**：
  本节把 bit5 来源过滤定性为「必改修复」系 over-claim——8042 输出
  缓冲为单字节 OBF，IRQ 仅在字节进入 OBF 时 assert，单 CPU +
  handler IF=0 下正常流量的键鼠 misrouting 不可能发生。bit5 过滤的
  实际定性是**防御性硬化**（phantom 交付 + flush 组合的理论路径），
  而非修复真实可触发缺陷；实现与两侧负向夹具本身不受影响，
  仍然有效。后续章节与记录以本注定性为准。

### 8.5 键盘解码与缓冲：set1 语义对齐 + 消费字节数口径

- **32 位做法**：`hid/keyboard.inc` set1 解码 + `data32.inc` keymap，
  多键盘链表/mutex、locale/hotkey 设施。
- **64 位做法**：en_US set1 双 keymap 各 128 字节（语义跟随
  data32.inc keymap/keymap_shift 行，汇编期长度断言）；修饰键状态机
  （LShift/RShift/Ctrl/Alt set-clear，Caps 仅 make 翻转且只翻字母）；
  0xE0 前缀（扩展键 raw|0x100、ascii 0）；**0xE1 Pause 吞掉两个
  payload**（否则 E1 1D 会污染 CTRL，负向夹具
  pause-e1-guard-disarmed）；break 0x80 保留 raw、ascii 0；空 OBF
  的 phantom 交付忽略（masked RTE 残留边沿无害化）。缓冲
  **120 × 16B**（ascii + raw 双存），KBD_BUF 数据区偏移 **168**
  （汇编期断言）；满则丢弃计数，绝不覆盖。**计数口径**：
  input_test_kbd_count 为「消费字节数」——含 E1 吞码、E0 前缀、
  修饰键与 break（信息性口径，api.inc 明示），不是「按键事件数」。
- **变更理由**：keymap 语义对齐使 32 位基线可逐行对照；吞码/前缀
  状态机保证修饰键状态不被控制序列污染；双存缓冲让桌面壳既能拿
  ascii 也能拿原始码；口径明示防止把字节数误读成事件数。
- **证据文件**：`kolibrios64/kernel/input/keyboard.inc`、`api.inc`、
  `data.inc`（偏移断言）、`tests/stage2-input/check.py`；
  `kolibrios/kernel/trunk/hid/keyboard.inc`、`data32.inc`
  （32 位对照）。

### 8.6 鼠标包状态机：sync 强制 + 溢出整包丢弃

- **32 位做法**：ps2mouse4d 支持 3/4 字节包（滚轮），mousedrv.inc
  做光标 save/restore 与加速度。
- **64 位做法**：3 字节包状态机，一次交付最多消费一字节；byte1
  **bit3 sync 强制**（缺失即坏包：丢弃、计数、状态机复位）；
  **bit6/7 溢出标志任一置位 → 整包丢弃**（移动计数器饱和，delta
  不可信，负向夹具 mouse-overflow-check-removed）；bit4/5 符号扩展
  （delta = byte - 256）；按键位 OR 累积；坐标屏幕空间 x += dx、
  **y -= dy**（PS/2 Y 向上为正），clamp 到冻结 1024x768 几何；
  完整有效包才进计数。此处不绘制——光标绘制属桌面壳里程碑。
- **变更理由**：sync/溢出裁决使坏包不可能污染坐标与计数；符号
  扩展与 Y 轴翻转是 PS/2 协议语义，显式注释防「修反」。
- **证据文件**：`kolibrios64/kernel/input/mouse.inc`、`api.inc`
  （SYNC/SIGN/OVERFLOW/clamp 常量）、`tests/stage2-input/check.py`
  （packet-bit3-check-removed 等夹具）；
  `kolibrios/drivers/mouse/ps2mouse4d/`、`kernel/trunk/hid/mousedrv.inc`
  （32 位对照）。

### 8.7 timer mask 补证：IRQ-v1 遗留缺口收口，ticks 10→11

- **遗留背景**：IRQ-v1 末腿 mask 窗口只证明「屏蔽期间无 tick」，
  无法区分「mask 生效」与「到期恰好没来」（§6.6/§7.9 记录的运行时
  证据缺口）。
- **64 位做法**：input 自检 leg5 **masked-oneshot**：masked LVT 下
  arm one-shot，IF=1 窗口内若到 tick 即失败；unmask + re-arm 恰交付
  1 tick。**ticks 终值 10→11 是确定性推导而非观测**：IRQ-v1 退出值
  10 + 补证腿恰 1；两种 QEMU masked-expiry 行为下均确定——(a) 到期
  丢弃 → re-arm 交付 1 tick；(b) 到期锁存 IRR → unmask 交付 phantom
  为 tick 11，mode-0 等待数条指令内退出并 mask LVT，远早于 re-arm
  的 10 ms 倒计时可能交付第 12 个。one-shot 每次 arm 至多 fires 一次，
  不依赖任何时序窗口。**变量分离**同 leg2 键盘 masked 腿：以 drain
  计数 ≥ 2（make+break 均被抽出）作到达证据，证明 kbd_count=0 是
  mask 生效而非按键缺席。**四处同步冻结**：input/api.inc
  （INPUT_IRQ_TICKS_FINAL = BASE + 1）、contracts.inc
  （K64_INPUT_TICKS_FINAL = 11 含推导注释）、run-qemu-debug.py
  （EXPECTED_COUNTERS ticks=11）、check-gui-judge.py（期望 11 +
  ticks 漂移负向夹具）；负向夹具 timer-final-drift（BASE+2 必须
  汇编失败）。
- **变更理由**：把「mask 生效」从缺席证据升级为到达证据 + 确定性
  计数推导，缺口在两种模拟行为下都闭合，不靠运气。
- **证据文件**：`kolibrios64/kernel/input/selftest.inc`（leg5 论证
  注释）、`api.inc`、`kernel/core/contracts.inc`、
  `tools/run-qemu-debug.py`、`tests/stage2-fb/check-gui-judge.py`、
  `tests/stage2-input/check.py`（timer-final-drift）。

### 8.8 宿主注入编排：gdbstub 握手 + HMP 注入

- **32 位做法**：无对应机制（runtests.py 不驱动 guest 输入）。
- **64 位做法**：guest 发布 `input_test_stage`（0..6），host 在
  K64-IRQ-OK 之后经 gdbstub 轮询（不干扰 OVMF 与早期内核阶段），
  按 stage 经 QEMU monitor 注入：sendkey b（masked 腿）→ sendkey a
  （live 腿）→ mouse_move 5 3 → mouse_button 1（press）；**release
  仅在 mouse_count ≥ 2 后发送**，QEMU 不可能把 press+release 合并成
  一包。超时 = 腿失败 panic，绝不静默通过。inject 识别 monitor
  Error/unknown 回复记 FAILED 并在后续轮次重试，不记假 sent；
  gdb_read_mem **每条退出路径都发 detach（finally）**——E 错误回复
  后裸关 socket 会把 VM 永久挂起（input 早期 stage 的 VA 可能尚未
  映射，此路径真实存在）。QEMU 11.1.3 `-display none` 下 PS/2 鼠标
  实测在（mouse_move/mouse_button 有效）。input 计数失配时杀 QEMU
  前经 gdbstub 读 7 个取证符号（原始包/坐标/状态机）留诊断。
- **变更理由**：握手把「宿主注入」与「guest 消费」变成双向可断言
  协议；press/release 分离与 Error 重试消除 QEMU 合并/拒收两类
  假失败；detach 修复的是编排自身挂起 VM 的真实缺陷。
- **证据文件**：`kolibrios64/tools/run-qemu-debug.py`
  （INPUT_STAGE_INJECT / inject / gdb_read_mem）、
  `kernel/input/selftest.inc`（握手注释）、`api.inc`（stage 常量）。

### 8.9 集成与验证证据（实测，全部 0 失败）

- **foundation 顺序**：core_selftest 之后插入 input 阶段（IRQ 与 GUI
  之间）：input_init → vmm_boot_protect（input 数据区 RW/NX）→
  input_selftest → `K64-INPUT-OK`。leg7 表页核算 **3/3/3 不变**：
  IOAPIC 映射在核算基线之后，仅新增 PT 页（与 GUI 同一手法，§7.8）。
- **宿主静态门禁**：check-boot-contract FAILURES 0；其检查器自测
  62/62 夹具 + 2/2 清理检查 + 57/57 寄存器模型查询；stage2-cpu 524、
  stage2-mm 1688、**stage2-irq 317**（12 负向夹具：新增
  dispatch-kbd-branch-removed / dispatch-input-eoi-removed /
  keyboard-vector-drift，原 dispatch-eoi-removed 以 jne .done 上下文
  钉住受守卫的 timer 写点）、stage2-fb 449、**stage2-input 402**
  （13 负向夹具全部被抓）、**check-gui-judge 35**（新增 INPUT 标记
  3 负向 + 5 计数器漂移负向 + 结构钉）。
- **QEMU TCG 冒烟**（唯一真实执行环境，禁 Rosetta 口径延续 §5.4）：
  128/256 MiB 多轮；**9 串口标记**按序各恰一次（K64-INPUT-OK 位于
  IRQ 与 GUI 之间）；passed=0x5FF；**10 计数器**经 gdbstub 读取全为
  冻结值：mm_mask=0、hook_hits=3、core_mask=0、irq_mask=0、
  **ticks=0xb**、input mask=0、**kbd=2、mouse=3、stage=6**、
  video_selftest_mask=0；GUI 28 探针全绿。
- **-d int 向量计数口径**：v21=2 / v2c=9 / v20=36，五轮运行
  （128/256 MiB）跨运行一致。**口径注释**：irq_test_ticks 为
  per-leg 计数（每腿复位），36 为全程总交付（IRQ-v1 各腿 + input
  补证腿），两种口径不矛盾。
- **panic profile 双证据**：BIN 变异（直接改产物字节）与源码变异
  （改 ps2.inc 重建）均触发 K64-CPU-PANIC，冒烟 judge 均 FAIL——
  证明失败路径与判定器两侧都有效。
- **独立验证与复验**：独立验证 8/8、复验 6/6（复验产物：4 负向夹具
  + 128/256 MiB 冒烟各一轮，全 PASS/全被抓）。
- **收口审查**：1 必改（bit5 来源过滤，§8.4）+ 5 建议，全部修复。
  （「必改」定性后有勘误：bit5 过滤实为防御性硬化，见 §8.4
  勘误注。）
- **证据文件**：`kolibrios64/kernel/core/foundation.inc`、
  `contracts.inc`、`kernel/input/`、`tests/stage2-input/`、
  `tests/stage2-irq/`、`tests/stage2-fb/check-gui-judge.py`、
  `tools/run-qemu-debug.py`、`tools/check-boot-contract.py`、
  `tools/test-boot-contract-checker.py`；实测数字来自本轮 build/
  产物（qa95 门禁与冒烟日志、irq2-t94-128/256、irq2-smoke-*、
  qa90 panic 双证据等，不入库）。

### 8.10 已知遗留与推迟（如实记录，不粉饰）

- judge 的 -d int 向量计数断言（v21/v2c/v20）**推迟到桌面壳**里程碑
  与双流常驻一并落地；本轮仅以跨运行一致性记录（§8.9）。
  （已随桌面壳落地：v21=6 / v2c=18 进 judge 硬判定，见 §9.6。）
- 右 Ctrl/Alt（E0+break）**不跟踪**：已文档化的简化（E0 扩展键直接
  入缓冲不进修饰键状态机），桌面壳按需扩展。
- `draw.inc` 文件尾换行仍缺（GUI-G1 遗留卫生项，§7.9）。（已随
  桌面壳改动修复，见 §9.7。）
- `build/ps2.inc.panic-backup` 为实现者遗留备份（gitignored，
  非交付物）。

---

## 9. 桌面壳：最小交互桌面（提交 `2bd897b`）

> 状态：已通过独立验证（期间发现并修复 D1/D2/GUI 帧证据丢失三个
> 真实缺陷，见 §9.6）与收口审查（**无阻断、无必改**；建议 S1 尾
> 换行 / S3 GPL 头 / I3 不变量注释已落实，修复前后 KERNEL64.BIN
> sha256 相同，证明语义零改动），即将提交。提交范围：
> kernel/desktop 新模块（七文件）、video_readback_rect 读回原语、
> core 集成（contracts.inc / foundation.inc / kernel64.asm include
> 顺序）、tools/run-qemu-debug.py 三帧取证 + gdb 写握手 + 向量
> 断言、tests/stage2-desktop 新门禁及 stage2-fb / stage2-irq 扩展、
> 本文档。

### 9.1 32 位基线对照：窗口系统 → 内核态最小桌面

- **32 位做法**：完整窗口系统——`gui/window.inc` 窗口链表/焦点/
  遮挡与按窗口选光标；事件掩码常量 EVENT_KEY=0x02 /
  EVENT_BUTTON=0x04 / EVENT_MOUSE=0x20（`const.inc`），
  `gui/event.inc` 事件队列，get_event 的键/按钮事件只投递
  WIN_STACK 栈顶窗口（栈位置必须等于 thread_count，hotkey 缓冲
  例外）；`hid/mousedrv.inc` 光标 save/restore：draw_mouse_under
  按 X_UNDER/Y_UNDER 恢复旧图、save_draw_mouse 保存背景并按光标
  下窗口选光标，__sys_draw_pointer 的 .redrawmouse 在 pushfd/cli
  下执行 restore→save→draw。
- **64 位做法**：首版为**内核态最小桌面**，无用户态进程/窗口
  管理器。保留语义——光标 save/restore（§9.3）与键鼠事件消费
  模型（handler 只写缓冲/计数，主循环消费重绘，§9.4）；丢弃
  实现——窗口链表/焦点/遮挡、按窗口选光标、用户态事件投递
  （均为后续里程碑，§9.8）。
- **变更理由**：单核无调度首阶段窗口系统没有宿主；保留光标
  save/restore 与事件消费语义使 32 位基线可直接对照，丢弃窗口
  设施避免引入无法验证的复杂度。
- **证据文件**：`kolibrios/kernel/trunk/const.inc`（EVENT_* 掩码）、
  `gui/event.inc`（.Keys/.Buttons top-window 判定）、`gui/window.inc`、
  `hid/mousedrv.inc`（draw_mouse_under/save_draw_mouse/
  __sys_draw_pointer）；`kolibrios64/kernel/desktop/api.inc`
  （契约头注释）。

### 9.2 桌面帧与键盘回显（DESKTOP-v1 冻结契约）

- **32 位做法**：桌面背景/标题栏由用户态程序绘制，无内核冻结帧
  契约。
- **64 位做法**：kernel/desktop 七文件（api.inc / cursor.inc /
  data.inc / desktop.asm / desktop.inc / echo.inc / selftest.inc）。
  帧契约冻结：背景 0x1A2B3C 全屏、标题栏 0x2C3E50
  (0,0,1024,32)、"K64 Desktop v0.1" (8,8) 白色 16 字符（字节长度
  断言）。回显区 (16,64) 126 列 × 44 行，**精确铺满剩余屏幕**
  （16+126*8=1024、64+44*16=768 汇编期断言）：可打印 0x20..0x7E
  绘制并计数（keys），Enter 换行，Backspace 以背景色擦除字符格
  （col>0 守卫）；满行 wrap，**屏满停止并计 drops，不滚动**；
  **row≥44 ⟹ col==0 不变量**以注释钉住（wrap 与 Enter 均置
  col=0，屏满 drop 不进 col，未来滚动支持必须保持）。回显经
  私有 desktop_kbd_consumed 索引消费 input_kbd_buf，**input 模块
  零改动**；selftest 先快照 input 退出 fill level，input 自测的
  'a' 永不被重回显。契约沿用三副本模式：DESKTOP_* 私有副本、
  K64_DESKTOP_* 共享副本（contracts.inc 汇编期比对）、
  stage2-desktop/check.py 独立副本。
- **变更理由**：精确铺满断言使「文本区几何漂移」成为构建失败；
  私有消费索引使桌面与 input 解耦（IRQ-v2 冻结字节不动）；
  屏满停止而非滚动是首版冻结策略，使回显状态机及其不变量
  可静态断言。
- **证据文件**：`kolibrios64/kernel/desktop/api.inc`、`echo.inc`
  （不变量注释）、`kernel/core/contracts.inc`（DESKTOP-v1 段）、
  `tests/stage2-desktop/check.py`。

### 9.3 光标 save/restore 与 video_readback_rect（含 D2 缺陷修复）

- **32 位做法**：mousedrv.inc save/restore（§9.1），背景读回由
  显示驱动完成，无统一读回原语契约。
- **64 位做法**：光标 sprite 为冻结 12×18 箭头，每像素一字节
  （0 透明/1 黑/2 白）共 216 字节，逐字节 + sha256
  （f46ce0e0…497ad7）由 stage2-desktop 门禁双重钉死。移动协议
  冻结：**先 restore 旧位**（video_blit_rect 写回 save 缓冲）→
  save 新位背景（video_readback_rect）→ composite（透明保留
  背景）→ blit draw 缓冲；hotspot clamp 1012/750 保 sprite 全在
  屏内；位置不变不重绘。`video_readback_rect` 是**内核唯一
  sanctioned FB 读路径**，与 video_blit_rect 精确镜像：源行按 FB
  stride 推进（右缘裁剪补偿）、目的按原始 w 紧密打包、raw BGRX
  无颜色转换；null dst EINVAL、未 ready ENOTREADY、全裁剪
  silent no-op。**仅运行时 cursor save 使用读回；video_selftest
  保持 draw-only**（§7.6 能力边界不变）。
- **D2 缺陷（独立验证发现，已修复）**：readback src/dst 角色静态
  钉缺口——xchg rsi/rdi 变异曾被 fb/desktop 两门禁**双漏检**
  （qa97 回放：fb 469 项、desktop 378 项均 PASS）。修复补 5 条
  静态钉（rdi 取自目的缓冲、rsi 取自 fb_va、copy 前先建立
  rdi=buffer 再 rsi=FB+stride、永不 xchg、rsi 先按 stride 推进
  再 rdi 按打包 w 推进）+ readback-src-dst-swap 负向夹具；
  qa103 回放同一变异 fb 门禁 2 项 FAIL，抓住。
- **变更理由**：save/restore 语义与 32 位基线对齐；读回与 blit
  镜像使「读/写方向」成为可字节级断言的性质；D2 证明「模型
  自洽」不等于「方向正确」，角色钉必须两侧独立钉死。
- **证据文件**：`kolibrios64/kernel/desktop/cursor.inc`（sprite +
  sha256 注释）、`kernel/video/draw.inc`（video_readback_rect）、
  `tests/stage2-fb/check.py`（5 静态钉 + 夹具）、
  `tests/stage2-desktop/check.py`（SPRITE_SHA256）；
  `kolibrios/kernel/trunk/hid/mousedrv.inc`（32 位对照）；
  D2 双漏检与抓住回放日志在 build/qa97、build/qa103 产物
  （不入库）。

### 9.4 常驻事件循环：第二合法 STI 拥有者

- **32 位做法**：内核初始化后 sti 常开（§6.6），osloop 按事件
  重绘光标/窗口，无 STI 所有权纪律。
- **64 位做法**：`desktop_run` 常驻循环——cli → **先 mask LVT
  timer 再 STI**（irq_test_ticks 永冻结 11，循环只被键鼠唤醒）→
  两 RTE 置 live → `sti / hlt / cli / desktop_pump` 无限循环，
  永不返回（host 取证后关 QEMU）。这是**全内核第二合法 STI
  拥有者**（第一是 irq_sti_wait 自测窗口），desktop 模块恰含一条
  STI；STI 中断影子覆盖 hlt，**无丢失唤醒**。重绘全在主循环
  IF=0（desktop_pump = echo_pump + cursor_sync），IRQ handler
  只写缓冲/计数。自测等待走 desktop_wait_count：每轮**先 pump
  再 irq_sti_wait**（host 观察的计数在等待内推进），64 轮有界
  超时 EIO；模块无私有 STI。stage2-irq 源码扫描更新为两拥有者
  白名单（kernel/desktop/desktop.inc + kernel/irq/selftest.inc），
  任何第三条 STI 即门禁失败。
- **变更理由**：常驻循环是桌面的正常终态，但 STI 所有权必须
  保持冻结可枚举；mask 先于 STI 使「ticks=11」是确定性推导
  而非时序运气；pump 先于 wait 消除「计数到了但没人重绘」的
  握手死锁。
- **证据文件**：`kolibrios64/kernel/desktop/desktop.inc`、`api.inc`
  （STI 所有权注释）、`kernel/core/contracts.inc`、
  `tests/stage2-irq/check.py`（kernel_sti_sites 两拥有者断言）、
  `tests/stage2-desktop/check.py`（desktop-sti-leak /
  timer-unmask-drift 夹具）。

### 9.5 foundation 集成与 GUI 帧取证窗口

- **32 位做法**：无对应分阶段 bring-up 与取证窗口。
- **64 位做法**：GUI-OK 之后、桌面重绘之前插入 **GUI 帧取证
  窗口**：foundation 置 desktop_gui_frame_stage=READY(1)，pause
  有界自旋（64 轮 × 0x2000000，IF=0，**无新 sti**）等 host
  screendump screen-gui.ppm 后经 gdbstub 写 RELEASE(2)，guest 置
  DONE(3) 才放行 desktop_init 重绘；**超时 EIO 走标准致命路径，
  绝不挂起**（skip 路径不进窗口，slot 保持 0）。随后
  desktop_init（FB ready + 1024x768 几何门，ENOTSUP → 非致命
  `K64-DESKTOP-SKIP`）→ vmm_boot_protect（desktop 数据区 RW/NX）
  → desktop_selftest（六腿：BG/TITLE/CURSOR/ECHO/MOUSE/FINAL，
  失败位 bit0..5）→ `K64-DESKTOP-OK` → `K64-FOUNDATION-OK`
  （**仍为终止判定标记，先于常驻循环打印**）→ desktop_run
  （desktop_state=0 → cpu_halt_forever）。GUI SKIP 级联
  DESKTOP-SKIP，非致命。leg7 表页核算 **3/3/3 不变**：desktop
  不映射自有 MMIO（复用 FB/LAPIC/IOAPIC 窗口），sprite 与
  save/draw 缓冲在数据区。
- **确定性计数推导**（汇编期断言，非观测）：cursor
  (512,384) → input 注入 (5,-3) → (517,387) → desktop 注入
  (40,24) → (557,411)；kbd=6（input 退出 2 + h/i make+break 4）、
  mouse=6（3 + move/press/release 3）、keys=2（仅可打印 h/i）、
  ticks=11（常驻循环 mask timer）。**四处同步冻结**：
  desktop/api.inc、core/contracts.inc、tools/run-qemu-debug.py
  （EXPECTED_COUNTERS）、tests/stage2-desktop/check.py（独立
  推导）。
- **变更理由**：GUI 图案帧会被桌面重绘覆盖，串口标记无法证明
  「图案曾经画对」（§7.6 像素真值口径），取证窗口在不引入新
  STI 的前提下恢复 GUI-G1 真机像素断言；计数推导使整条注入链
  可审计。
- **证据文件**：`kolibrios64/kernel/core/foundation.inc`（GUI 帧
  窗口与 desktop 阶段）、`kernel/desktop/selftest.inc`（六腿与
  推导注释）、`api.inc`、`kernel/core/contracts.inc`
  （K64_GUI_FRAME_*）、`tools/run-qemu-debug.py`。

### 9.6 验证体系演进：三帧取证 + 向量断言 + gdb 写握手

- **32 位做法**：无对应机制。
- **64 位做法**：
  - **三帧取证模型**：screendump 从单帧扩为三帧——
    `screen-gui.ppm`（GUI-G1 图案帧，取证窗口内捕获，**28 探针
    恢复 GUI-G1 图案真机断言**）、`screen-echo.ppm`（回显帧，
    desktop stage 3 先采帧再注入 move，光标仍在 (517,387)）、
    `screen.ppm`（终帧，光标 (557,411) 且**旧位背景已恢复**）。
    探针集：28 GUI + 15 桌面公共 + 3 回显 + 7 终帧。
  - **judge 向量断言落地**：-d int 交付计数进 judge 硬判定——
    ok 模式 v21=6（2+4）、v2c=18（9+9）；skip 模式 2/9。
    **IRQ-v2 推迟项就此闭环**（§8.10）。
  - **gdb_write_mem 'M' 包写握手**：host 经 gdbstub 向 guest 内存
    写 RELEASE，与读同套 detach 纪律（每条退出路径 finally 发
    D，VM 绝不留在 paused）。
  - **注入编排扩展**：desktop stage 1/2/3 注入 sendkey h /
    sendkey i / mouse_move 40 24；stage 4 press/release 分离
    （release 仅在 mouse_count≥5 后发送，QEMU 不可能合并包）。
- **独立验证发现并修复的三个真实缺陷（如实记录）**：
  - **D1**：run-qemu-debug.py judge 扩展为三帧后，
    check-gui-judge.py 未同步重写，运行即崩溃；修复为同步重写
    judge 门禁（现 92 项，§9.7）。
  - **D2**：readback src/dst 静态钉缺口，xchg 变异曾双漏检，
    现 5 静态钉 + 夹具抓住（§9.3）。
  - **GUI 帧证据丢失**：desktop 重绘覆盖 GUI 图案帧，GUI-G1
    真机像素断言丢失；修复为三帧握手（§9.5 取证窗口 +
    screen-gui.ppm）。
- **变更理由**：单帧取证只能证明「最后一帧对」，三帧模型使每
  阶段像素证据独立可裁决；向量断言把「跨运行一致」升级为硬
  判定；写握手复用读侧 detach 教训，不重蹈挂 VM 缺陷。
- **证据文件**：`kolibrios64/tools/run-qemu-debug.py`（三帧探针 /
  EXPECTED_VECTORS / gdb_write_mem / DESKTOP_STAGE_INJECT）、
  `tests/stage2-fb/check-gui-judge.py`（三帧正基线 + 负向样本）、
  `kernel/core/foundation.inc`。

### 9.7 集成与验证证据（实测，全部 0 失败）

- **宿主静态门禁**：check-boot-contract FAILURES 0；其检查器自测
  62/62 夹具 + 2/2 清理检查 + 57/57 寄存器模型查询；
  stage2-cpu 524、stage2-mm 1688、stage2-irq 317、**stage2-fb 493**
  （readback 5 静态钉 + 新负向夹具）、stage2-input 402、
  **stage2-desktop 386**（7 负向夹具全部被抓：desktop-sti-leak /
  timer-unmask-drift / cursor-restore-removed /
  echo-backspace-removed / sprite-data-drift /
  desktop-final-count-drift / cursor-clamp-x-removed）、
  **check-gui-judge 92**（36 正向基线/结构钉 + 56 负向样本全部
  被抓，覆盖标记/三帧像素/计数器漂移/向量漂移/skip 模式）。
- **QEMU TCG 冒烟**（唯一真实执行环境，禁 Rosetta 口径延续
  §5.4）：128/256 MiB 多轮，判定 **109 [ok]**；**10 串口标记**
  按序各恰一次（K64-DESKTOP-OK 位于 GUI 与 FOUNDATION 之间）；
  passed=0x5FF；**16 计数器**经 gdbstub 读取全为冻结值（新增
  desktop mask=0、stage=5、keys=2、cursor=(557,411)、
  gui_frame_stage=3；kbd/mouse 退出值 2/3 → 6/6）；三帧探针
  全绿；v21=6 / v2c=18。
- **GUI SKIP 场景**：8 标记 + K64-GUI-SKIP + K64-DESKTOP-SKIP，
  判定 **40 [ok]**，desktop 计数器全零、gui_frame_stage=0、
  向量 2/9，无 panic、无像素断言。
- **panic profile**：① echo 计数断言删除变异 →
  `K64-DESKTOP-SELFTEST failed legs = 0x8` → K64-CPU-PANIC
  （reason -9 EIO，"foundation: desktop bring-up failed"）；
  ② GUI 帧窗口超时（host 不写 RELEASE）→ EIO panic，**不挂起**。
- **收口审查**：无阻断、无必改；建议 S1 尾换行（draw.inc，
  §7.9/§8.10 卫生遗留就此闭环）/ S3 desktop 新文件 GPL 头 /
  I3 echo 不变量注释已落实——**修复前后 KERNEL64.BIN sha256
  相同**（3e4dee2c…740b），证明语义零改动。
- **证据文件**：`kolibrios64/tests/stage2-desktop/`、
  `tests/stage2-fb/`（check.py + check-gui-judge.py）、
  `tests/stage2-irq/`、`tools/run-qemu-debug.py`、
  `tools/check-boot-contract.py`、`tools/test-boot-contract-checker.py`；
  实测数字来自本轮 build/ 产物（t102-* / t104-* 门禁、qa103
  门禁与 ok128/ok256、desktop-t102-skip / timeout2、qa97 变异
  回放等，不入库）。

### 9.8 已知遗留与后续（如实记录，不粉饰）

- **S2 cursor_sync 返回值防御硬化**：desktop_cursor_sync 未检查
  restore blit / place 的返回值——当前冻结几何与 ready 门下该
  失败路径不可达，记录为防御性硬化建议，独立后续任务。
- **S4 五门禁 assembler 发现口径统一**：既有五门禁 3:2 分裂——
  cpu/irq/input 用 `--fasm2-dir` 参数（+ 同级目录回退），mm/fb
  用 `FASM2_DIR` 环境变量；新 desktop 门禁沿用参数风格。统一
  为独立后续任务。
- 右 Ctrl/Alt 不跟踪：§8.10 已文档化简化延续。
- 用户态窗口系统（窗口链表/焦点/遮挡）与进程调度：路线图
  后续阶段，范围待该里程碑启动时冻结。
- 桌面壳提交哈希待产生后补入锚点表（本次不写未产生的哈希）。

---

## 10. 进行中（占位，待验证通过后补录）

### IRQ-v1：LAPIC 定时器闭环 —— 已完成

- 已收口并补录为 §6；提交 `983111c` 已入里程碑锚点表。

### GUI-G1：图形输出闭环 —— 已完成

- 已收口并补录为 §7；提交 `7b2c4b7` 已入里程碑锚点表。

### IRQ-v2：输入闭环 —— 已完成

- 已收口并补录为 §8；提交 `ddad22d` 已入里程碑锚点表。IRQ-v1
  遗留的 timer mask 运行时证据缺口已在 §8.7 补证；judge 向量
  计数断言推迟项已随桌面壳落地（§9.6）。

### 最小桌面壳 —— 已完成

- 已收口并补录为 §9；提交哈希待产生后补入锚点表（本次不写
  未产生的哈希）。

### 用户态窗口系统与进程调度 —— 路线图后续

- 窗口链表/焦点/遮挡、用户态进程与调度为后续阶段（§9.1/§9.8）；
  右 Ctrl/Alt 跟踪仍为已文档化简化（§8.10）。范围待该里程碑
  启动时冻结。

---

## 附：待核实事项

1. **32 位引导扇区源文件位置**：`kolibrios/kernel/trunk/build.sh` 引用
   `bootbios.asm`，但该文件不在 trunk 下（仅存在于
   `kernel/branches/*/bootbios.asm`）。trunk 实际引导扇区的构建入口待核实；
   本文 §1.1 关于 16 位入口的表述以 `trunk/bootbios.inc` 为据，不受影响。
2. **32 位自定义 fastcall 的寄存器顺序**：trunk 本地 `proc32.inc` 未定义
   fastcall 宏（内核源码可见 stdcall 用法），其定义应来自 FASM1 工具链
   宏库；确切传参寄存器顺序待核实，本文不作断言。
3. **Rosetta 弃用决策的量化细节**（宿主崩溃次数、执行检查删减计数）：
   来自项目决策记录（任务历史），仓库内仅有定性注释
   （`tests/stage2-mm/check.py` section 7），量化数字以决策记录口径标注。
4. **32 位 runtests.py 在当前宿主的可运行性**：项目环境记录称其依赖
   （pexpect/qemu/PIL/fuse 及旧 Python API）在本机 Python 3.15 下不可用，
   故不作硬门禁；该结论属环境事实，未在本次撰写中重新实测，待核实。

---

*首版撰写于里程碑 553a031（CPU+MM 集成）收口之后；IRQ-v1 章节补录于
其收口审查通过之后（已随提交 983111c 入库）；GUI-G1 章节补录于其收口
审查通过之后（已随提交 7b2c4b7 入库）；IRQ-v2 章节补录于其独立验证与
收口审查通过之后（已随提交 ddad22d 入库）；桌面壳章节补录于其独立
验证与收口审查通过之后（已随提交 2bd897b 入库）。后续
每个里程碑收口时，由变更史官增补对应小节并更新提交锚点表。*
