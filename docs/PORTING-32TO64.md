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
| kolibrios64 | `553a031` | CPU+MM 内核基础集成（QEMU 验证，当前 HEAD） |
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
  七个里程碑 `K64-BOOT-OK → K64-CPU-OK → K64-PMM-OK → K64-VMM-OK →
  K64-HEAP-OK → K64-IRQ-OK → K64-FOUNDATION-OK` 各恰好一次且按序
  （K64-IRQ-OK 随 IRQ-v1 插入），禁止
  `K64-CPU-PANIC`/`K64-BOOT-FAIL`，CPU 自测 passed = 0x5FF（串口报告行），
  五计数器 `mm_mask = 0`、`hook_hits = 3`、`core_mask = 0`、`irq_mask = 0`、
  `irq_test_ticks = 10`（成功路径不打印，经 gdbstub 从 guest 内存读取；
  后两项随 IRQ-v1 新增）。默认 CPU `qemu64,+popcnt`（裸 qemu64 缺
  MM 所需 POPCNT）。QEMU 进程组随退出/中断整体清理，不留 stray 进程。
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

## 6. IRQ-v1：LAPIC 定时器闭环（本次提交，哈希待提交后补入锚点表）

> 状态：已通过独立验证与收口审查（含两项必改修复：ISR 降序冲刷、GOP
> 命中标记守卫），即将提交。提交范围：kernel/irq 新模块、mm 的
> vmm_map_mmio 与 bootmap 放宽、cpu 异步分发、boot GOP 交接、core 集成、
> tools 扩展、tests/stage2-irq 及 cpu/mm 测试扩展、本文档；**不含**
> kernel/video 与 tests/stage2-fb（GUI 轨未集成，另行提交）。

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

### 6.5 GOP 交接：VESA/BIOS → UEFI GOP（随本次提交）

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
- **变更理由**：有界窗口使中断行为完全可断言；IF 证据对证明中断门
  语义（handler 内 IF=0）与窗口上下文（IF=1）同时成立。
- **证据文件**：`kolibrios64/kernel/irq/selftest.inc`、
  `kernel/core/foundation.inc`、`tools/run-qemu-debug.py`；
  `kolibrios/kernel/trunk/kernel.asm`（sti 位置，32 位对照）。

### 6.7 验证口径更新（本次撰写时本地重跑实测，全部 0 失败）

- **门禁计数**：stage2-cpu 524、stage2-mm 1688、stage2-irq 295、
  stage2-fb 430（fb 属 GUI 轨，不在本次提交）；check-boot-contract
  106 项 PASS；其负向变异测试 62/62 夹具 + 57/57 寄存器模型查询 +
  2/2 清理检查。
- **冒烟口径**：7 标记按序各恰一次（新增 K64-IRQ-OK），passed=0x5FF，
  五计数器（新增 irq_mask=0、irq_test_ticks=10，经 gdbstub 读取）。
- **收口审查修复记录**：① ISR 降序冲刷（§6.1）；② GOP 命中标记守卫
  （§6.5）；③ BootInfo 8 对齐回归——GOP 字段填值不得破坏 208 字节
  布局的 8 字节对齐，`bootinfo.inc` 的 `sizeof mod 8` 汇编期断言守住。
- **证据文件**：`kolibrios64/tests/stage2-irq/`、`tests/stage2-cpu/`、
  `tests/stage2-mm/`、`tests/stage2-fb/`、`tools/check-boot-contract.py`、
  `tools/test-boot-contract-checker.py`、`tools/run-qemu-debug.py`、
  `kernel/boot/bootinfo.inc`。

---

## 7. 进行中（占位，待验证通过后补录）

### IRQ-v1：LAPIC 定时器闭环 —— 已完成

- 已收口并补录为 §6；提交哈希待产生后补入里程碑锚点表（本次不写未
  产生的哈希）。

### GUI-G1：图形输出闭环 —— 进行中

- FB 模块（`kernel/video/`）与 `tests/stage2-fb/`（430 项门禁）已独立
  交付，但**未集成**进内核映像，不在 IRQ-v1 提交范围，另行提交。
- 本小节在该里程碑集成并验证收口后按四段式补录，届时补充提交锚点
  与证据文件。

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
其收口审查通过之后（提交哈希待补）。后续每个里程碑收口时，由变更史官
增补对应小节并更新提交锚点表。*