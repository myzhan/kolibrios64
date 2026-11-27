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
| kolibrios64 | `2bd897b` | 桌面壳：最小交互桌面（QEMU 验证，见 §9） |
| kolibrios64 | `7080025` | FONT-FIX 字体位序修复 + 独立 oracle 门禁（QEMU 验证，见 §7.10） |
| kolibrios64 | `e2bcbe7` | docs：FONT-FIX 提交哈希补录 |
| kolibrios64 | `424fed2` | FS-v1：virtio-blk + 只读 FAT16 + 文件 syscall 4..9（QEMU 验证，见 §10；含 APP-v1 轨一 CPU/MM 用户态前置，收口文件不可拆分裁决） |
| kolibrios64 | `3b1617d` | docs：FS-v1 提交哈希补录 |
| kolibrios64 | `31d342c` | APP-v1：首个用户程序 CHIP-8（Ring 3 + SYSCALL + FS 装载，QEMU 验证，见 §11） |
| kolibrios64 | `8997110` | DESKTOP-v2：壁纸、开始菜单、CHIP-8 重启与光标残影复验（QEMU 验证，见 §12） |
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
  嵌入 `font.inc`（每行 1 字节，LSB 为最左像素；源文件 GPLv2，与本
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
- **勘误（FONT-FIX）**：本节原表述「MSB 为最左像素」有误，正确约定
  为 **LSB-first（bit0 = 最左像素）**，与 32 位基线 drawChar 的 bsf
  渲染一致。转录数据自始逐字节正确（sha256 冻结值未动），错在解码
  约定：text.inc 渲染器按 MSB-first 解码，且探针期望与渲染模型从
  同一错误约定派生，锁步同错自洽全绿，掩盖了全部文字逐字形水平
  镜像的缺陷（K→ꓘ 形态），直至用户实测常驻桌面才暴露。修复与
  独立 oracle 门禁见 §7.10。

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
  PPM P6，比对 28 个冻结探针（时为 GUI-G1 计数；FONT-FIX 真值化并
  扩至 32，见 §7.10）。**验证口径：串口标记 + 计数器全绿
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
  桌面壳扩至 493，见 §9.7；FONT-FIX 扩至 508，见 §7.10）；
  check-gui-judge 21（时为 GUI-G1 计数，judge 纯函数正基线 + 12
  负向变异全部被抓；IRQ-v2 扩至 35，见 §8.9；桌面壳扩至 92，
  见 §9.7；FONT-FIX 扩至 96，见 §7.10）。
- **QEMU TCG 冒烟**（唯一真实执行环境，禁 Rosetta 口径延续 §5.4）：
  128/256 MiB 多轮；8 串口标记按序各恰一次（K64-GUI-OK 位于 IRQ 与
  FOUNDATION 之间）；passed=0x5FF；6 计数器经 gdbstub 读取全为冻结
  值（mm_mask=0、hook_hits=3、core_mask=0、irq_mask=0、ticks=10、
  video_selftest_mask=0；ticks=10 为 GUI-G1 时口径，IRQ-v2 起为 11，
  见 §8.7）；screendump 1024x768，28 探针全绿（时为当时口径；该
  全绿后经 FONT-FIX 证明含锁步自洽成分，探针真值化并扩至 32，
  见 §7.10）。
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

### 7.10 FONT-FIX：字体位序锁步缺陷与独立 oracle 门禁（勘误补录）

> 状态：修复已通过独立验证（7/7）与收口审查（无阻断、无必改）；
> 已随提交 7080025 入库。本节是对 §7.5/§7.6 的勘误补录：
> GUI-G1 当时的全绿结论在当时的约定下真实成立，错的是约定本身。

- **32 位做法**：`kolibrios/kernel/trunk/gui/font.inc:316-323`
  drawChar 用 `bsf eax, edx` 找出行位图的最低置位、乘像素间距落点
  ——即 **LSB-first：bit0 = 最左像素**。这是字体位序的唯一渲染
  oracle，charUni.mt 的真值位序由它确定。
- **64 位做法（缺陷与修复）**：charUni.mt 提取的 95 字形子集数据
  逐字节正确（FONT_SHA256 `a61bf766…` 冻结值修复前后未动），但
  text.inc 渲染器按 MSB-first 解码（`test 0x80`/`shl`），且全部
  探针期望与 Python 渲染模型从同一错误约定派生 → 渲染器、模型、
  探针**锁步同错而自洽**：spec 模型探针 22/22、GUI 28 探针、
  judge 92 项曾全过，而实际全部文字逐字形水平镜像（K→ꓘ 形态），
  由用户实测常驻桌面发现。修复取方案 A（渲染器改 LSB，数据不动）：
  - `text.inc`：`test r9b, 0x01` / `shr r9b, 1`（静态钉改
    `41 F6 C1 01` / `41 D0 E9`）；font.inc/api.inc 仅改正注释；
  - check.py 与 check-gui-judge.py 渲染模型改
    `glyph[row] & (1 << col)`；
  - 探针真值化并补镜像敏感点：GUI 28→32、common 15→27。真值化
    并非全盘翻转：新增 20 点中实测 17 点对镜像敏感；旧 h/i 6 点
    期望本身是镜像版，按 charUni.mt 真值重推。对称行无鉴别力
    （如 '1' row3=0x18），实现期真值纠偏一例：'1' 的镜像敏感旗在
    row4=0x14（探针 (130,12) 亮 / (133,12) 灭），而非初选 row3；
  - **新增独立 oracle 门禁**（check_font_oracle）：手抄 K/1 原始
    真值 32 字节 + sha256 `2aa1d9ca…` 钉，直对 charUni.mt 逐字节
    比对（基线不可达时 pinned 副本顶替），并断言 LSB/MSB 解码在
    鉴别像素上必然相异——真值不再从渲染器/模型派生；
  - 负向加固：check.py 新增 `msb-bitorder` 源码 fixture（渲染器
    回 MSB，静态钉 + 镜像敏感探针必抓）；judge 新增镜像帧负向
    （逐字节 bit-reverse 渲染 GUI/echo/final 三帧必 FAIL，并断言
    镜像模型与真值确实相异）。
- **关键差异与教训**：
  - **探针期望必须从独立真值源派生**。§7.6 的外部像素裁决补上了
    「串口全绿 ≠ 画面正确」的缺口，但本例暴露更深一层：当期望
    本身从被测实现（或与之共享约定的模型）派生时，渲染器与模型
    可锁步同错，像素裁决退化为自证，产生全绿假象。
  - **oracle 门禁是防锁步的结构性手段**：真值直接钉在 32 位基线
    原始字节上（独立于 64 位渲染器与 Python 模型），加上「LSB/MSB
    解码必然相异」的鉴别断言，使任何一侧回退到错误约定都立即
    失败。
  - 数据 sha256 冻结只能防「数据漂移」，防不了「解码约定错」——
    两者正交，本例数据自始正确。
- **验证证据**（宿主门禁 + QEMU TCG，全部 0 失败）：
  - 门禁：stage2-fb 493→508、check-gui-judge 92→96；其余不变
    （cpu 524 / mm 1688 / irq 317 / input 402 / desktop 386）。
  - QEMU 128/256 MiB 各 2 次：137 [ok] / 0 FAIL。
  - 独立真值比对：两轮 ok 的三帧文字区域逐像素比对 charUni.mt
    LSB 解码，11776 像素 0 mismatch，其中 1224 像素可判别旧镜像
    （旧 MSB 解码下这些像素必错）。
  - 三个自制变异全被抓：渲染器 shl 回归（mutA，fb 门禁 FAIL）、
    模型 MSB 回归（mutB，fb 门禁 FAIL）、真实帧字形镜像（mutC，
    judge 三帧 FAIL）。
  - SKIP 与 panic profile 不回归（K64-DESKTOP-SKIP 40 [ok]；坏内核
    panic reason/message 照旧命中）。
  - 证据文件：`kolibrios64/kernel/video/text.inc`（修复点注释）、
    `font.inc`、`api.inc`（注释改正）、`tests/stage2-fb/check.py`
    （ORACLE_TRUTH/ORACLE_SHA256、check_font_oracle、msb-bitorder
    fixture）、`check-gui-judge.py`（mirror_font 负向）、
    `tools/run-qemu-debug.py`（32/27 探针）；
    `kolibrios/kernel/trunk/gui/font.inc`（drawChar bsf oracle）、
    `gui/charUni.mt`（真值源）；实测数字来自 build/fontfix-*、
    build/qa109/（gate-*.log、ok128/ok256、ppm-truth.log、
    truth-check.log、mutA/B/C 与 mut-replay.log、skip128*.log、
    fontfix-panic.log；build/ 产物不入库）。
  - 收口审查：无阻断、无必改；建议项 S1（text.inc/api.inc 补
    pre-existing 尾换行）由提交环节顺手落实。

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
- **64 位做法**：12×18 光标图块中的箭头与尾柄共用一条经过热点的 45° 轴线。每像素一字节
  （0 透明/1 黑/2 白）共 216 字节，像素和 SHA-256
  （f52a399c…b7caabc）由 stage2-desktop 门禁校验。移动协议
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

## 10. FS-v1：VFS + 块设备 + 文件 syscall（提交 `424fed2`）

> 状态：已通过独立验证（6/6，任务 134）与审查修复复验（8/8 +
> 全量终值 6/6，任务 139，含 214 项独立交叉复核）；收口审查
> W1–W4 / S1–S4 全部修复并复验（§10.8）。提交范围：kernel/blk、
> kernel/fs、kernel/fsys 三个新模块，mm 用户域落地
> （vmm_map_user_page / vmm_unmap_user_page），user/fsprobe 探针，
> core 集成（contracts.inc / foundation.inc / kernel64.asm include
> 顺序），tools/build-fat16-image.py 确定性映像构建器与
> run-qemu-debug.py --fs/--fsprobe 编排，tests/stage2-blk /
> stage2-fs / stage2-fsys 三个新门禁及 stage2-mm / stage2-cpu /
> stage2-desktop 扩展，本文档。提交哈希 `424fed2`（已补入锚点表；
> 含 APP-v1 轨一 CPU/MM 用户态前置，收口文件不可拆分裁决）。

### 10.1 背景与用户裁决：APP-v1 期间插入 FS-v1，先冻结文件契约

- **裁决经过（如实记录）**：APP-v1（首个用户程序 CHIP-8）轨一
  （CPU syscall/Ring 3 地基 + mm 用户域铺垫）进行中，用户
  插入 FS-v1 里程碑：**先做 VFS + 块设备，再回 APP 集成**——避免
  程序加载路径按「ROM 嵌入内核映像」定型后返工；APP-v1 集成
  （kernel/app）挂起，直至 FS 契约冻结。裁决来源为项目决策记录
  （任务历史），仓库内对应事实：user/chip8 的 ROM 仍为嵌入数据
  （api.inc 冻结 sha256 摘要断言），tests/stage2-app 为轨一门禁，
  kernel/app 目录尚未建立。
- **结果**：FS-v1 三里程碑（M1 块设备 / M2 FAT16 / M3 文件
  syscall）收口后契约冻结（§10.7），APP-v1 集成解冻（§10.10）。
- **证据文件**：`kolibrios64/user/chip8/api.inc`（嵌入 ROM 冻结
  摘要）、`tests/stage2-app/`（轨一门禁）；裁决本身无仓库内
  文本，按项目决策记录口径标注。

### 10.2 32 位基线澄清：virt_disk.asm 并非 virtio

- **32 位做法（基线澄清）**：`drivers/disk/virt_disk.asm` 名字
  带 "virt" 但**不是 virtio 驱动**——它是文件映像回环盘：把扇区
  偏移换算成文件偏移后 `invoke FS_Service`（fn70 文件服务，
  子功能 0 读文件 / 3 写文件），无任何 virtqueue/PCI 逻辑。
  真正的参照是两处：`kernel/trunk/blkdev/rd.inc`（ramdisk 后端
  函数表契约：querymedia/read/write/adjust_cache_size 等槽位，
  即「后端接缝」的组织方式）与 `kernel/trunk/fs/fat.inc`
  （FAT 簇链/目录/路径算法）。fn70 是**路径式无状态**接口：
  每次调用携带完整路径 + 偏移，内核不持有句柄。
- **64 位做法**：取**句柄式 syscall 4..9**（OPEN/READ/CLOSE/
  SEEK/STAT/READDIR）：OPEN 返回 fd（0..7 静态表），后续操作按
  fd 引用，路径解析只发生在 OPEN/STAT。块层后端接缝对齐 rd.inc
  的函数表思路但收敛为单指针 `blk_backend`（blk_read 叶子分发，
  §10.4）。
- **变更理由**：口径是**功能契约层对齐**——保留文件服务的功能
  语义（读/定位/目录/属性），不是 32 位二进制兼容（后者需兼容
  模式与 syscall 翻译，独立立项，§5 口径延续）。路径式每次调用
  重复全量路径解析且 32 位实现依赖 APPDATA slot 等地址位技巧；
  句柄式把解析收敛到 OPEN，fd 表成为可静态断言的有界状态。
- **证据文件**：`kolibrios/drivers/disk/virt_disk.asm`（FS_Service
  转调，read file/write file 子功能）、`kernel/trunk/blkdev/rd.inc`
  （ramdisk_functions 函数表）、`kernel/trunk/fs/fat.inc`；
  `kolibrios64/kernel/fs/api.inc`（句柄式 API 头注释）、
  `kernel/blk/blk.inc`（blk_read 接缝注释）。

### 10.3 路线裁决：legacy PIO + 轮询 + 只读 FAT16

- **64 位做法（冻结路线）**：后端取 **virtio-blk-pci-transitional
  的 legacy PIO 窗口 + 轮询单 virtqueue**（只读 T_IN），文件系统
  取**只读 FAT16 8.3 短名**，用户接口取句柄式 syscall 4..9。
- **被否方案与理由（裁决记录）**：
  - **AHCI**：32 位有 `blkdev/ahci.inc` 可参照，但 command list/
    FIS/端口 bring-up 复杂度远超首版需要；
  - **modern virtio**：virtio 1.0 modern PCI 需解析 capability 链
    与 64 位队列地址，legacy PIO 窗口在 transitional 设备上免费
    可得，首版取简；
  - **可写 FS**：写路径与一致性语义无法在首版闭环验证；
  - **自定义 FS**：失去与 32 位 fat.inc 算法及标准映像工具的
    互操作性；
  - **纯 ramdisk 主线**：rd.inc 式内存盘不能满足 APP 从真实磁盘
    读文件的语义，只保留为 blk_backend 回退接缝（BootInfo
    initramfs 字段预留），不作主线。
- **轮询而非中断的理由**：irq_dispatch 冻结字节不动（§8.4 纯
  增量纪律），不新增向量（未知向量走 CPU panic 路径），IF 纪律
  与 STI 所有权枚举（§6.6/§9.4）完全不受影响；blk 模块从不
  接入任何中断。
- **v2 坑位**：virtio modern、FAT12/32、LFN、可写支持均列 v2，
  首版显式拒绝（modern-only 设备拒认、非 FAT16 簇数 EIO、
  LFN 属性项跳过、写标志 EPERM）。
- **证据文件**：`kolibrios64/kernel/blk/api.inc`（transitional
  PCI 身份与 legacy PIO 寄存器表冻结）、`kernel/blk/blk.inc`
  （revision 0 only / modern-only 拒认注释）、`kernel/fs/api.inc`
  （FAT16 簇数范围 4085..65524、LFN 掩码）、
  `kernel/fsys/api.inc`（写访问 EPERM 映射）；
  `kolibrios/kernel/trunk/blkdev/ahci.inc`（被否方案对照）。

### 10.4 M1 kernel/blk：virtio-blk legacy PIO 后端

- **32 位做法**：无 virtio 驱动（§10.2 澄清）；后端组织参照
  rd.inc 函数表。
- **64 位做法**：五文件模块（api.inc / blk.asm / blk.inc /
  data.inc / selftest.inc）。PCI 枚举走冻结 **CF8/CFC 机制**
  （bus 0、dev 0..31、function 0），身份四重匹配
  1AF4:1001 + 子系统 0x0002 + revision 0；BAR0 取 legacy PIO
  窗口（bit0=IO，掩码 0xFFFC），COMMAND 置 IO|BUS_MASTER。
  单 virtqueue（queue PFN 写入、QUEUE_NOTIFY 踢门、used ring
  轮询），vring/DMA bounce 页/请求 scratch 页全在模块数据区，
  经 BootInfo.kernel_phys_start 换算为 identity 域物理地址。
  公开接缝 **blk_read(rcx=LBA, rdx=buf, r8=扇区数)** 是叶子分发器
  （jmp blk_backend），未来 initramfs ramdisk 以同 ABI 替换。
  自检七腿（容量/BPB 签名/ROM 前缀**读回验证**/参数与范围拒绝/
  末 LBA/多 chunk 拷贝/请求计数），blk_test_reads 退出值 7。
- **变更理由**：读回验证使「设备声称完成」必须落到字节真值；
  单指针后端接缝把「换后端」收敛为一次指针替换而非接口改造；
  全静态数据区 + 有界轮询使模块无 heap、无中断、无挂死路径。
- **证据文件**：`kolibrios64/kernel/blk/blk.inc`（PCI/PIO/
  virtqueue 与 blk_read 接缝）、`api.inc`（契约冻结 + 三副本
  断言）、`selftest.inc`、`tests/stage2-blk/check.py`、
  `tests/stage2-blk/virtqueue_model.py`（三环模型 18 项自测）。

### 10.5 M2 kernel/fs：只读 FAT16 单挂载

- **32 位做法**：`fs/fat.inc` FAT 算法 + 多挂载/写路径/LFN
  （`fs/fs_lfn.inc`）设施。
- **64 位做法**：五文件模块，单静态挂载。init 解析 LBA0 BPB
  （仅 512B 扇区，FAT16 簇数范围校验，spf ≤ 32 否则 EIO），
  **整个第一 FAT 在 init 缓存（16 KiB = 4 页）**；根目录/数据扇区
  走**单扇区直映射缓存**——任何调用序列的 IO 模式确定，门禁
  mirror 可逐字节复现。簇链遍历（越界/坏簇 0xFFF7/保留值/跳数
  预算耗尽 = EIO）、固定根目录 512 项、8.3 大小写不敏感路径
  解析（0xE5 Kanji 转义折叠）、**8 槽 × 64B 静态 fd 表**、路径
  预算 128B。**零 heap**：全部状态为模块静态数据；从不 panic、
  从不 STI、无任何写路径。自检八腿（几何/IBM 全读+EOF/TETRIS/
  seek 边界/拒绝路径+readdir/BIG 跨簇/stat/扇区计数），
  fs_test_reads 退出值 24（plain）/48（big）。
- **变更理由**：只读 + 8.3 使 FAT 算法可与 32 位 fat.inc 及独立
  Python oracle（fat16_model.py）双向对照；双缓存策略把 IO
  计数变成确定性推导而非观测；零 heap 使 fs 不依赖 mm heap 的
  失败语义，错误纪律与 blk 一致（signed EAX，绝不挂死）。
- **证据文件**：`kolibrios64/kernel/fs/fs.inc`（缓存/簇链/路径
  解析）、`api.inc`（BPB 偏移表与几何冻结断言）、`selftest.inc`、
  `tests/stage2-fs/check.py`、`tests/stage2-fs/fat16_model.py`
  （独立 oracle 15 项自测）；`kolibrios/kernel/trunk/fs/fat.inc`
  （32 位算法对照）。

### 10.6 M3 kernel/fsys + mm 用户域 + user/fsprobe

- **32 位做法**：fn70 经中断门进入，路径指针直接在内核解引用；
  无用户域页表契约（§10.2）。
- **64 位做法**：
  - **kernel/fsys 适配层**（四文件）：syscall handler 从 184B
    trap frame 取参（RDI@72/RSI@80/RDX@104/RAX@120，偏移汇编期
    互断言），把 fs 的 **EAX 状态/RDX 结果约定适配为 RAX 单寄存器
    返回**；错误经 13 项 fsys_errmap 映射到 K64 五码集（§10.7）。
    载荷一律经 **4 KiB bounce 页**中转（≤4 KiB chunk 拷贝）；
    **用户指针全区间校验**：copy_path 逐字节 mm_vmm_resolve
    （要求 4 KiB 叶 + 有效 US，越界字符串在第一个未映射字节即
    EFAULT，绝不进入 fs）；copy_to_user 逐 chunk 同规则解析。
    数据区三页：状态/bounce/syscall 栈。
  - **mm 用户域落地**：VA 域冻结 **[0x100400000, 0x100800000)**
    （PML4 slot 0 / PDPT[4]，复用 identity PDPT 空槽，不分裂任何
    identity 2 MiB 叶）；code 16 KiB US+RX、data 2 MiB US+RW+NX、
    栈顶 16 KiB；**+3 表预算**（1 PD + 2 PT；原 +4 草案重复计
    identity PDPT，裁决修正并冻结）；vmm_map_user_page 首映射
    置 PML4[0] US（anchor 0，单向），identity PDPT/PD 保持
    supervisor，低 4 GiB identity 有效权限不变。
  - **user/fsprobe**：首个经 FS syscall 的 Ring 3 探针，9 叶页
    足迹（4 code + 1 data + 4 stack），**11 腿**（open/read/EOF/
    seek 三 whence + W1 大偏移/close/stat/readdir/ENOENT/EFAULT/
    EINVAL/ENOTSUP），probe_mask=0x7FF，8 个证据 qword；全程
    IF=0，GDT 方案 A 选择子（CS 0x33/SS 0x2B）；每条退出路径
    执行用户域 teardown（§10.7 W3）。
- **变更理由**：bounce + 全区间解析使「用户指针」成为逐页可裁决
  的安全契约而非信任假设；EAX/RDX→RAX 适配把内核内部 ABI 与
  用户 ABI 显式解耦；teardown 使探针不留足迹，为 APP-v1 退出
  路径提供地基。
- **证据文件**：`kolibrios64/kernel/fsys/fsys.inc`（handler/
  bounce/copy_path/copy_to_user）、`api.inc`（域镜像互断言）、
  `kernel/mm/api.inc`（用户域裁决注 + map/unmap 契约）、
  `kernel/mm/vmm.inc`、`user/fsprobe/fsprobe.asm`、`api.inc`、
  `tests/stage2-fsys/check.py`、`tests/stage2-mm/check.py`。

### 10.7 契约冻结清单（FS-v1 冻结面）

- **SYS_CALL_MAX=16 两处同步**：cpu/api.inc CPU_SYS_CALL_MAX ↔
  contracts.inc K64_APP_SYS_CALL_MAX 互断言；0..3 保持 APP-v1
  冻结集，4..9 归 FS；fsys/api.inc 断言 READDIR(9) < MAX。
- **错误映射表（不照搬 32 位码值）**：fs 的 FS_E* 经 fsys_errmap
  13 项映射到 K64 五码集（EINVAL/ENOENT/ENOTSUP/EIO/EFAULT）：
  EPERM→ENOTSUP（只读 v1）、ENOMEM/ENOTREADY/EBUSY→EIO、
  EEXIST/EALREADY/EOVERFLOW→EINVAL、EFAULT→EFAULT，其余→EIO；
  contracts.inc 与门禁独立副本三方一致。
- **S2 POSIX 部分读口径（审查裁决冻结）**：READ/READDIR 只计
  已交付字节（copy_to_user 成功后才推进总数）；N>0 后遇 EIO/
  EFAULT 返回 N 而非 -errno；首字节交付前失败才返回负 errno；
  EOF 返回计数（新 EOF 为 0）。
- **W1/S1 全宽返回（审查裁决冻结）**：READ/READDIR/SEEK 成功
  结果以 **RAX 全 64 位**返回（≥2 GiB 偏移原样返回，探针以
  SEEK_SET 0x100000000 腿实证）；movsxd 符号扩展只服务负 errno
  路径，成功结果绝不经过它。
- **W3 用户域 teardown 契约（APP-v1 退出路径地基）**：
  vmm_unmap_user_page 只服务冻结域，清叶 + invlpg 后自底向上
  回收全零 PT/PD（mm_pmm_drop_table，table_drops 由调用方计）；
  identity PDPT/PML4 永不回收，anchor 0 US 单向保持；域外 EPERM/
  未对齐 EINVAL/无叶 ENOENT（幂等容忍）/大叶 ENOTSUP；成功 rdx
  返回释放物理地址（帧仍归调用方，经 pmm_free_page 释放）。
  完整 map+unmap 周期后 allocs-drops 回到基线、恰释放 +3 表。
  fsys_probe 每条退出路径执行 teardown，证据页先快照进 bounce
  页再拆域（QEMU judge 字节证据不丢）。
- **GDT 方案 A 沿用**：用户数据 0x28/代码 0x30（加载值 0x2B/
  0x33），TSS 保持 0x18——APP-v1 轨一裁决经 FS-v1 M3 落地冻结，
  未重排 GDT。
- **证据文件**：`kolibrios64/kernel/core/contracts.inc`（FS-v1
  M3 段 + task 138 冻结修订尾 append）、`kernel/cpu/api.inc`、
  `kernel/fsys/api.inc`、`fsys.inc`（errmap）、`kernel/mm/api.inc`、
  `user/fsprobe/api.inc`、`tests/stage2-fsys/check.py`。

### 10.8 缺陷与修复史（三层验证价值的实证）

- **实现期真缺陷三例（如实记录）**：
  - **PCI 写端口覆写 edx**：blk_pci_cfg_write 以 `mov dx, port`
    装端口号，dx 是 edx 低半——待写值被端口号覆写，IO 使能写
    失效。修复：值先 push rdx 暂存、pop rax 后写出（注释钉住
    「值必须活过端口装载」）。
  - **fs_read rbp 双重推进**：.cbyte 逐字节循环内已 inc rbp，
    循环外又 add rbp, r9，每扇区光标走 2×chunk。单扇区文件
    （IBM/TETRIS 单 chunk）不暴露故 **plain 映像全绿**；跨簇
    BIG.BIN 8229B 奇数块留零且越界写坏相邻数据区，由 **QEMU
    big 映像腿捕获**（fs_test_mask=0xA0）。修复删除循环外 add；
    门禁补 read.cursor_single_advance 静态钉（read 区禁
    4C 01 CD）。Python mirror 按切片语义建模，无法复现此类
    寄存器双重推进——QEMU 是唯一权威执行（§5.4 口径再证）。
  - **copy_path 二次叠加页偏移**：resolve 记录 rec+48 已含页内
    偏移，实现又叠加一次 cursor 偏移，读到页内 2×offset 处。
    **宿主门禁曾编码同一错误序列**（期望与实现锁步同错，静态
    全绿），由 QEMU 真机暴露（task-133 catch，与 FONT-FIX
    §7.10 锁步教训同型）。修复后门禁改负向断言
    path.no_double_page_offset（禁 4C 89 E0 25 FF 0F 00 00）。
- **收口审查 W1–W4 / S1–S4 全部修复并复验（任务 138/139）**：
  - **W1/S1**：SEEK/READ/READDIR 32 位截断 → 全宽 RAX 返回
    （§10.7），字节钉 48 89 D0 / 4C 89 E0 + seek-truncation
    负向夹具 + 探针大偏移腿真机通过。
  - **W2**：fs_init 缺 clusters↔spf 覆盖守卫，坏 BPB 可致 FAT
    缓存越界 → 补 `clusters+2 <= spf*256` 守卫（违反 EIO 且
    fs_ready 保持 0），模型/mirror 同步同式 +
    spf_clusters_mismatch 夹具（BPB spf=16 必抓）。
  - **W3**：探针无 teardown，用户域足迹残留阻塞 APP 集成 →
    vmm_unmap_user_page 契约冻结 + 探针全退出路径 teardown
    （§10.7）；teardown-unmap/free-removed 双夹具；同址再映射
    抽查 25 项 PASS（再映射前须重新 alloc 帧，否则 EPERM——
    属正确所有权规则，首轮 FAIL 为探针自身缺帧，已修正复跑）。
  - **W4**：desktop 门禁 file_set 绊线弱化（名字过滤式使陌生
    文件静默通过）→ 改白名单式：仅目录项与 wallpaper.inc
    （DESKTOP-v2 轨）豁免，其余陌生文件必 FAIL；沙箱三态实测
    （真实形态 PASS / 注入 foreign.txt FAIL / 陌生目录豁免），
    desktop 386 判定不变。
  - **S2**：部分读口径冻结（§10.7）+ 字节钉 + 模型 EOF 腿。
  - **S3**：fsys_probe 失败路径 RSP0 恢复（3 次 cpu_set_rsp0 /
    2 处恢复分支，rsp0-restore-removed 夹具被抓）。
  - **S4**：foundation 头注释补 FS-v1 M1/M2/M3 阶段叙述。
- **证据文件**：`kolibrios64/kernel/blk/blk.inc`（写端口修复
  注释）、`kernel/fs/fs.inc`、`kernel/fsys/fsys.inc`、
  `tests/stage2-fsys/check.py`（no_double_page_offset 注释）、
  `tests/stage2-fs/check.py`、`tests/stage2-mm/check.py`、
  `tests/stage2-desktop/check.py`（W4 白名单注释）；实测回放
  来自 build/stage2-fsys-fix/、build/stage2-fs-reverify/
  （w3-remap-spot / w4-tripwire，不入库）。

### 10.9 集成与验证证据（实测，全部 0 失败）

- **foundation 顺序**：desktop 之后插入三阶段——blk_init →
  boot_protect → blk_selftest → `K64-BLK-OK`；fs_init →
  boot_protect → fs_selftest → `K64-FS-OK`；fsys_init →
  boot_protect → fsys_probe（Ring 3 腿，含 teardown）→
  `K64-FSPROBE-OK`；三者均**强制、无 skip**（区别于 GUI/
  desktop 的 ENOTSUP 可跳过）。include 序 desktop → blk → fs →
  fsys → core 收口。leg7 早期表页核算 **3/3/3 不变**（用户域
  +3 预算在核算基线之后，自计账）。
- **宿主静态门禁终值**（独立重跑，15 个 rc=0）：**stage2-fsys
  528（19 负向夹具全执行全被抓）**、**stage2-fs 530**、
  **stage2-mm 2188（25 夹具）**、stage2-cpu 689、**stage2-blk
  481**、stage2-irq 317、stage2-input 402、stage2-fb 508、
  stage2-desktop 386、check-gui-judge 96、boot-contract
  FAILURES 0、check-wallpaper 12 PASS；模型自测 virtqueue 18 /
  fat16 15。
- **映像确定性**：tools/build-fat16-image.py 两遍逐字节一致——
  plain（8 MiB，512B 扇区/spc 2/reserved 1/双 FAT/spf 32/根 512
  项/总扇区 16384，/ROMS/ 两 ROM + /PROGRAMS 空目录）sha256
  `e9e83dad…bc42b`；big（manifest 追加 BIG.BIN 8229B，冻结公式
  ((i*7+3)&0xFF)，sha `f799e250…`）sha256 `a8102805…00543`；
  与实现者 build/fs-v1/disk.img、disk-big.img 逐字节相同。
- **QEMU TCG 端到端矩阵**（唯一真实执行环境，§5.4 口径延续）：
  **128/256 MiB × plain/big 四组合全 PASS**（独立验证端口
  12631..12634、复验 12641..12644，均用验证侧自建新映像）。
  每组合：**13 串口标记**按序各恰一次（BOOT/CPU/PMM/VMM/HEAP/
  IRQ/INPUT/GUI/DESKTOP/BLK/FS/FSPROBE/FOUNDATION）；
  blk_test_reads=34(plain)/58(big)、fs_test_reads=24/48、mask
  全 0、capacity=0x4000；**probe_mask=0x7FF**、exit_code=0、
  **table_delta=3**、8 证据 qword + stat/dirent/四 errno 全真；
  **teardown 收据**四组合全 [ok]：用户 VA（code/data/栈顶）读
  均失败、fsys_pages_held=0、fsys_pages 72B 全零、
  fsys_table_released=3、净 allocs-drops 回基线、**PDPT[4]==0**、
  anchor 0 US 保持；ROM/BIG 缓冲经 gdbstub 与源逐字节比对，
  独立侧另以自写 FAT16 解析器从 backing 映像提取比对；冻结
  判定值四组合不变（CPU 0x5FF、ticks 0xb、kbd/mouse 6/6、
  cursor (557,411)、v21=6/v2c=18、三帧 PPM 像素断言全 [ok]、
  无 triple fault）。
- **独立验证与复验**：独立验证 **6/6**（任务 134，cross-check
  142 项）；审查修复复验 **8/8 + 全量终值 6/6**（任务 139，
  cross-check **214 项**全 PASS）。两轮均纯验证零修改、不采信
  实现者日志。
- **证据文件**：`kolibrios64/kernel/core/foundation.inc`、
  `contracts.inc`、`kernel/blk|fs|fsys/`、`user/fsprobe/`、
  `tests/stage2-blk|fs|fsys/`、`tools/build-fat16-image.py`、
  `tools/run-qemu-debug.py`；实测数字来自
  build/stage2-fs-verify/REPORT.md、build/stage2-fs-reverify/
  REPORT.md 及其 gates/、images/、qemu-* 产物（不入库）。

### 10.10 已知遗留与展望（如实记录，不粉饰）

- **APP-v1 集成解冻**：FS 契约已冻结，kernel/app 从 FS 读
  chip8.bin（build-fat16-image.py --manifest 追加，映像 sha 随之
  变化属预期）；user/chip8 嵌入 ROM 改走 **/ROMS/** 文件路径；
  W3 teardown 契约即 APP 退出路径地基；READDIR 为桌面菜单
  （程序列表）预留。范围待 APP-v1 集成里程碑启动时冻结。
- **v2 坑位**：virtio modern、FAT12/32、LFN、可写 FS（§10.3）；
  blk_backend 的 initramfs ramdisk 回退接缝已预留未启用。
- **只读限制**：写标志 EPERM、modern-only 设备拒认、非 512B
  扇区/非 FAT16 几何 EIO——均为显式冻结拒绝而非静默降级。
- FS-v1 提交哈希 `424fed2`（已补入锚点表）。

---

## 11. APP-v1：首个用户程序 CHIP-8（提交 `31d342c`）

> 状态：已通过独立验证（**10/10**，任务 116）与审查修复复验
> （**六大项 6/6**，任务 142）；收口审查 W1 / S1–S5 与门禁污染、
> GdbSession 双缺陷全部修复并复验（§11.6）。提交范围：kernel/app
> 宿主装载器新模块，user/chip8 FS 化（含 roms/），core 集成
> （contracts.inc APP 段 + task 141 尾追加裁决段 / foundation leg /
> kernel64.asm include 序），fs selftest leg4 第三态，build_fasm2.sh
> chip8.bin / disk-app.img 衔接，tools/run-qemu-debug.py --app 编排
> 与 GdbSession 修复，check-boot-contract.py OUTDIR fixture 沙箱化，
> tests/stage2-app 新门禁及 stage2-fs / blk / fsys / irq 扩展，
> 本文档。提交哈希 `31d342c`（已补入锚点表）。

### 11.1 背景与用户裁决：FS 先行装载 + 用户 VA 域方案 B

- **裁决经过（如实记录）**：APP-v1 轨一（CPU syscall/Ring 3
  地基 + mm 用户域铺垫）进行中，用户插入 FS-v1（§10.1）——
  **APP 装载必须走文件系统**，避免「ROM 嵌入内核镜像」路径
  定型后返工；FS 契约冻结后 APP 集成解冻（§10.10），本章即
  该集成里程碑的收口记录。
- **用户 VA 域方案 B 裁决（独立高位域）**：用户域取
  **[0x100400000, 0x100800000)**，既非 32 位「应用装载地址零」
  布局，也不与 identity 域共享——经 FS-v1 M3 落地冻结
  （§10.6）；user/chip8 完成方案 B VA 重定位，api.inc sha
  注释即重定位后终值。
- **GDT 方案 A 裁决**：冻结草案用户选择子 0x20/0x28 与 0x18
  处 16 字节 TSS 描述符（占 0x18..0x27）重叠，56 字节 GDT 容
  不下——裁决用户段对后移至 TSS 之后：数据 0x28 / 代码 0x30，
  内核选择子不动（§11.5）。两处偏差在 contracts.inc 注释中
  如实记录（reported pending adjudication → 冻结）。
- **证据文件**：`kolibrios64/kernel/core/contracts.inc`（APP-v1
  段 GDT/VA 裁决注释）、`user/chip8/api.inc`（scheme B VA
  relocation 注释）、本文档 §10.1/§10.6/§10.10；裁决经过本身
  无仓库内文本，按项目决策记录口径标注。

### 11.2 32 位基线：MENUET01 可执行格式 + int 0x40 系统调用

- **32 位做法**：用户程序为 MENUET01 头可执行映像（'MENUET01'
  魔数 + 入口/内存布局字段），内核 taskman 装载时校验 'MENU'
  签名后按头字段建立进程；系统调用走 **int 0x40** 中断门
  （core/syscall.inc 调用面），programs/ 目录应用皆此格式
  （仓库内参照 core/test_malloc.asm：MENUET01 头 + int 0x40）。
- **64 位做法（最小形态）**：**Ring 3 + SYSCALL/SYSRET**（无
  int 门）；首个程序取**无格式头 flat binary**（chip8.bin），
  经 **/PROGRAMS/CHIP8.BIN 文件 syscall 路径**装载到冻结 VA，
  无格式解析、无重定位。
- **变更理由**：口径仍是功能契约层对齐（§5/§10.2 延续）——
  保留「程序从磁盘装载 + syscall 服务」的功能语义，不是
  MENUET01 二进制兼容；flat binary 使装载器可逐字节验证
  （尺寸门禁 + sha 冻结），格式头/重定位留待后续里程碑。
  32 位「装载地址零 + 地址位提取 slot」假设不照搬。
- **证据文件**：`kolibrios/kernel/trunk/core/taskman.inc`
  （'MENU' 头校验）、`core/syscall.inc`（int 0x40 调用面）、
  `core/test_malloc.asm`（MENUET01 + int 0x40 仓库内参照）；
  `kolibrios64/kernel/app/app.inc`（FS 装载路径）、`user/chip8/`。

### 11.3 kernel/app 宿主装载器：516 页装载与握手/退出协议

- **32 位做法**：taskman 按 MENUET01 头建立进程地址空间，
  常驻多进程（对照，不照搬）。
- **64 位做法**：五文件模块（api.inc / app.asm / app.inc /
  data.inc / selftest.inc），include 于 fsys 之后 contracts
  之前。app_launch 走**内核内部 fs API**（fs_stat/fs_open/
  fs_read/fs_close，EAX 状态/RDX 结果）读 /PROGRAMS/CHIP8.BIN，
  尺寸门禁 0 < size ≤ 16 KiB；**516 页装载**（4 code RX+US +
  512 data RW+NX+US 覆盖整个 2 MiB 域，16 KiB 栈位于 data 域
  顶部、无额外叶帧），**+3 表预算**（1 PD + 2 PT）。握手四阶段
  app_stage：IDLE → IBM_READY（首 BLIT 交付，host 截屏）→
  EXIT_REQ（host 写退出请求）→ DONE；键队空且 stage=EXIT_REQ
  时 READ_KEY **伪造一次 ESC make**（rax=27 / rdx=0x01，不
  推进 consumed/key_count），未修改的用户 key_drain 走真实
  sys_exit(0)。**每条快照后退出路径**（EXIT / Ring 3 fault /
  注册失败）全量 teardown：516 叶经 vmm_unmap_user_page
  （部分失败容忍 ENOENT）、恰释放 +3 表、帧释放、数组清零、
  app_pages_held=0、fd 泄漏清扫、桌面完整重绘 + 光标按桌面
  自身坐标重置。**fault seam**：cpu_user_fault_set_handler 把
  Ring 3 故障泊入 app_fault_vector（向量+1，0=无），EIO 裁决
  走 teardown；RSP0 纪律沿 S3 先例（每条失败路径恢复已保存
  的 foundation TSS.RSP0）。**ENOENT 非致命裁决**：plain/big
  磁盘无 CHIP8.BIN → `K64-APP-SKIP`（不 panic），13 标记口径
  不变。**fs/blk 冻结计数器快照恢复**：app 数据区 21→22 页
  （新增 app_fs_snapshot 页，launch 入口 rep movsq 快照
  fsys_bounce），避免探针/APP bounce 复用冲突；fsys 三页冻结
  不动。内核由此 856064→**860160 B**（+4096）。
- **变更理由**：hosted 语义（launch 注册 syscall 0..3、每条
  退出路径注销）使 APP 调用面非常驻，4..9 fsys 适配器常驻供
  chip8 读 ROM；快照恢复保住 M1/M2 冻结终值可判定；全退出
  路径 teardown 是 W3 契约（§10.7）的 APP 级复用。
- **证据文件**：`kolibrios64/kernel/app/api.inc|app.inc|data.inc|
  selftest.inc`、`kernel/core/contracts.inc`（task 115 段
  A1–A6 裁决注释）、`foundation.inc`（app leg / K64-APP-OK /
  K64-APP-SKIP）、`kernel/fs/selftest.inc`（leg4 第三态）；
  实测回放 build/stage2-app-int/progress.md（不入库）。

### 11.4 user/chip8 FS 化与 interactive pacing（A7/A8）

- **FS 化**：删两处内嵌 ROM 数据段，改 sys_open/sys_read/
  sys_close 包装从 **/ROMS/** 读取（IBM.CH8→RAM+0x200，Enter
  后 TETRIS1.CH8）；ROM sha256 常量保留为门禁期望。chip8.bin
  由 build_fasm2.sh 产出，尺寸/sha 常量链四处同步
  （selftest.inc / stage2-fs / stage2-app / runner）。
- **interactive pacing 冻结语义（A7/A8，W1 裁决见 §11.6）**：
  user 每轮固定 **IPS_PER_TICK=5** 条指令预算 + 按轮累加
  **TIMER_HZ=60** 进 acc、**TIMER_ACC_BASE=100** 分频（~417
  ips），不再消费 POLL r8d tick 快照；kernel interactive
  POLL：LVT timer mask → KBD/MOUSE RTE live → **单一 sti…cli
  窗口内 APP_IPOLL_CHUNK=0x40000 有界 pause 自旋** → RTE
  re-mask。A8 取代 A1 的 sti;hlt;cli（gdbstub 键注入无法唤醒
  HLT，LVT masked 下无其他唤醒源）；A1 IRQ 口径不动：
  irq_test_ticks 恒 11、13 标记冻结终值不变、模块内仍恰一个
  STI owner。IBM leg（interactive=0）保持 IF=0
  APP_POLL_CHUNK=0x2000000 pause 窗，**字节行为不变**。
- **证据文件**：`kolibrios64/user/chip8/chip8.asm`（固定预算
  循环）、`api.inc`（IPS/TIMER 常量）、`kernel/app/app.inc`
  （interactive 分支与注释）、`kernel/core/contracts.inc`
  （A7/A8 + K64_APP_IPOLL_CHUNK 双向断言）。

### 11.5 契约冻结清单（APP-v1 冻结面）

- **GDT 方案 A**：用户数据 **0x28** / 代码 **0x30**（加载值
  0x2B/0x33），TSS 保持 **0x18**，GDT **56 B**；
  **STAR=(0x20<<48)|(0x08<<32)**，SFMASK=0x200 恰清 IF，
  SYSRET CS 0x33 / SS 0x2B 按 SDM 规则从 STAR 基派生
  （汇编期断言）。
- **VA 域与足迹**：[0x100400000, 0x100800000)（PML4 slot 0 /
  PDPT[4]），code 16 KiB RX+US / data 2 MiB RW+NX+US / 栈顶
  16 KiB；**516 叶 + 3 表**；vmm_map_user_page /
  vmm_unmap_user_page teardown 收据 **released=3 /
  pages_held=0**。
- **syscall 面**：0..3 = **EXIT(0) / BLIT(1) / READ_KEY(2) /
  POLL(3)**（语义以 contracts.inc APP 段为准），
  **SYS_CALL_MAX=16**；0..3 仅程序运行期注册，4..9 常驻 fsys；
  Linux 风格 ABI（RAX 号，RDI/RSI/RDX/R10/R8/R9 参数，
  RCX/R11 由 SYSCALL 本身破坏）。
- **BLIT 几何与握手**：dst (192,224)、640×320；stage 0..3；
  ESC 伪造 ascii 27 / make 0x01，ENTER make 0x1C。
- **pacing 常量**：IPS_PER_TICK=5 / TIMER_HZ=60 /
  TIMER_ACC_BASE=100；APP_IPOLL_CHUNK=0x40000 双向等值 +
  < APP_POLL_CHUNK 上界断言（尾追加零删除行）。
- **终值冻结**：chip8.bin **3255 B** sha256
  `9da19f2d0b537ac85cfb758e9615f0a111f32b96c125150b91218bb8ff630f82`；
  disk-app.img sha256
  `aec9f70167e6e18170a638bf4af3d5bf28817cf2ff37d6fec5a137cec12f3170`；
  plain/big FAT16 映像 sha **不变**（`e9e83dad…bc42b` /
  `a8102805…00543`）；内核 KERNEL64.BIN **860160 B**
  （sha `c13518b5…`）；kernel app 数据区 **22 页**（含
  app_fs_snapshot）。
- **证据文件**：`kolibrios64/kernel/core/contracts.inc`（APP
  段 + task 141 尾追加段）、`kernel/app/api.inc`、
  `user/chip8/api.inc`、`tests/stage2-app/check.py`、
  `build_fasm2.sh`。

### 11.6 缺陷与修复史（三层验证与门禁卫生的实证）

- **draw_sprite 位循环偏移丢 col（两段修复，如实记录）**：
  首段 `lea ecx,[rdi+rax]` 误用移位后 sprite 值作像素偏移 →
  改 `[rdi+r8]`（位索引）只对一半（chip8.bin 3311→3312 B）；
  运行时取证（state 页 dump V 寄存器正确 + 双侧 trace 传参
  col/row/n 正确）推翻 V[0]≡0 假设，真根因：col+bit 算在 eax
  后被 sprite 字节覆盖，偏移只剩 row*64+bit → **6 字符全部
  叠在 col=0 XOR**。正确修复：裁剪后 `mov r9d,eax` 保存
  col+bit，偏移改 `lea ecx,[rdi+r9]`（3312→3315 B）；门禁
  升级 r9 列保存**正反断言**（source.chip8_draw_bit_index，
  r9→r8 变异被抓）。临时 trace 全部移除还原。
- **收口审查 W1（interactive pacing 死路，裁决修复而非
  遗留）**：tetris interactive 循环消费冻结 irq_test_ticks
  （LVT_MASK 口径下恒 11）→ tick-delta 恒 0 → **Enter 后 0
  条指令，不可玩**。修复两侧：kernel interactive POLL 改
  A8 有界 pause 自旋（§11.4）；user 改 A7 固定预算 + 按轮
  累加分频，**不触碰冻结 IRQ 口径**（irq_test_ticks 恒 11、
  13 标记冻结终值不变），**IBM 冻结分支字节不变**；
  contracts.inc 尾追加 A7/A8 裁决段 + K64_APP_IPOLL_CHUNK
  断言（单一 hunk 零删除行，严格尾追加）。chip8.bin
  3315→**3255 B** 终值。
- **门禁污染真缺陷（门禁卫生实证）**：check-boot-contract.py
  OUTDIR fixture 伪造 fasmg host（printf x > "$out"）跑真实
  build_fasm2.sh，S3/M3 之后脚本写真实仓库 build 树——真实
  chip8.bin/fsprobe.bin 被覆写为 1 字节 'x'，S3 步骤把污染
  chip8.bin 烘进 disk-app.img（10795aec…）。实证二分定位
  真凶。修复：fixture 改跑**沙箱副本**（copy2 脚本+builder+
  api.inc+ROMs+桩源+manifest 全落 tmp，root 按 __file__
  定位），新增正断言 "OUTDIR fixture **leaves the real
  chip8.bin untouched**"（只增不弱化）。修复后 boot 门禁
  107 PASS / FAILURES:0，跑前/跑后三工件 sha 逐字节不变
  （复验专项回归）。
- **GdbSession 双缺陷（runner，非产品）**：(A) _recv_reply
  O-skip 把 b'OK' 当 console-output 吞掉 → 改 fullmatch hex
  判定（'K' 非 hex，OK 通过）；(B) QEMU TCG hbreak 停在断点
  时 'c' 立即重触发 → continue_ 加 GDB 式 **step-over
  （z1/s/Z1）**；_drain 改 select 有界窗口。前段
  「deferred reply/nudge」误判完整撤销。
- **S1–S5 全部落实**：S1 user/chip8/api.inc sha 注释更新为
  终值（非发射记录）；S2 kernel/app/api.inc + data.inc
  「21 page」→22 pages 对齐（含 22*4096 err 断言）；
  S3 build_fasm2.sh chip8.bin 后追加 disk-app.img 构建步骤
  （入库）；S4 run-qemu-debug.py --app/--app-interactive
  help 注明先跑 tests/stage2-app/check.py；S5 ESC 伪造注释
  对齐实现（每次空读伪造，无 one-shot 标志，不推进
  consumed/key_count）。
- **证据文件**：`kolibrios64/kernel/app/app.inc`、
  `user/chip8/chip8.asm|emu.inc|api.inc`、
  `kernel/core/contracts.inc`、`tools/check-boot-contract.py`、
  `tools/run-qemu-debug.py`、`tests/stage2-app/check.py`；
  实测回放来自 build/stage2-app-int/（progress.md + logs/）、
  build/stage2-app-reverify/（REPORT-142.md + mutation-*.log，
  不入库）。

### 11.7 集成与验证证据（实测，全部 0 失败）

- **foundation 顺序**：fsys_probe 之后插入 app leg——launch
  gates（ring 0 / IF=0 / app+fsys+fs+desktop+video ready /
  desktop_echo_row=0 / stage IDLE，通过前零分配）→ IBM leg →
  teardown + 桌面重绘 → `K64-APP-OK`；ENOENT →
  `K64-APP-SKIP`（非致命）。--app 时 FSPROBE 与 FOUNDATION
  之间插入 APP，**14 标记**按序各恰一次；skip 腿 13 标记 +
  K64-APP-SKIP 在序。include 序 fsys → app → contracts。
  leg7 早期表页核算不变（用户域 +3 自计账，§10.9 口径延续）。
- **fs selftest leg4 第三态**：app 映像 /PROGRAMS 非空触发
  冻结自检 bit4（QEMU 128 首跑 FAIL 根因）→ .prog_empty 增
  CHIP8.BIN 合法分支（尺寸+名双校验），plain/big 字节路径
  不变；stage2-fs **530→540**（纯追加 10 项），manifest 同步。
- **宿主静态门禁终值**（任务 142 独立重跑）：**stage2-app
  724/0（24 负向 fixture 全执行全被抓，含 poll-remask-removed /
  poll-hlt-regression / interactive-tick-delta-regression）**、
  fs 540、fsys 528、mm 2188、cpu 689、blk 481、irq 317、
  input 402、fb 508、desktop 386、judge 96、boot
  **FAILURES:0（107 PASS）**、wallpaper PASSED；chip8_model
  **16 checks**（含 tetris 固定预算步进帧 20/67/68/69/71 轮
  探针）；virtqueue 18 / fat16 15 自测不变。
- **构建确定性与干净重建**：KERNEL64.BIN/BOOTX64.EFI/
  chip8.bin/disk-app.img 移走后 build_fasm2.sh 全部从源码
  再生（disk-app.img 自动产出，S3）；baseline/post-rebuild/
  final 三次 sha 一致：chip8.bin 3255 B `9da19f2d…`、
  disk-app.img `aec9f701…`、plain `e9e83dad…` / big
  `a8102805…` 不变、内核 860160 B `c13518b5…`、fsprobe
  `c770e33f…`。污染回归专项：boot 门禁跑前/跑后真实 build
  树 chip8.bin 与 disk-app.img sha 逐字节不变。
- **QEMU TCG 矩阵**（唯一真实执行环境，复验端口
  1264/1265/1266）：**128/256 MiB interactive 各 259 ok/0**
  （interactive=1、key_count=4、poll=72、blit=58；stop 诊断
  poll 1→2→3 真实递增；IBM@20 帧（2400 lit）与 tetris@71-key
  帧（7800 lit）均与纯 Python 模型独立重放派生**逐像素一致**
  （1024×768 PPM 中 (192,224) 640×320 矩形），key7 帧 ≠
  no-key 参照；ESC teardown 收据全 [ok]：VA 不可读 / 帧数组
  清零 / released=3 / pages_held=0 / PDPT[4] 清除 / anchor0
  US 保留）；**frozen --app 255 ok/0**（IBM 腿字节行为与
  任务 115 终值一致）；**无 --app 185 ok/0 + K64-APP-SKIP
  在序**（ENOENT 非致命）。
- **ring3 实证**（独立验证自跑 gdbstub 硬件断点采样）：用户
  sys_poll RIP=0x100400b55 **CS=0x33 CPL=3**、
  RSP=0x1007fffd8（用户栈域）；内核 app_h_poll **CS=0x8
  CPL=0**；ud2 负向冒烟：**v=06 cpl=3** 捕获、
  app_fault_vector=7（向量+1）、全量 teardown（3/3/0）、
  桌面重绘帧与基线字节相同、**无 triple fault**（Reset=2
  仅正常上电+OVMF）。
- **独立验证与复验**：独立验证 **10/10**（任务 116，端口
  1254..1256，在 687 门禁 / 3315 B 形态上进行，含 IBM 帧独立
  派生、ring3/ud2 实证、变异抽查 3/3 被抓）；W1/S1–S5 修复后
  复验 **6/6 大项**（任务 142：全门禁 / 构建 / QEMU 矩阵 /
  独立帧比对 / 变异抽查 / 卫生零失败），变异抽查 M1
  tick-delta 回退 / M2 IPS_PER_TICK=0 / M3 映像翻 bit 全被
  抓——门禁非恒真证明。两轮均纯验证零产品源码修改、不采信
  实现者日志。
- **证据文件**：`kolibrios64/kernel/core/foundation.inc`、
  `contracts.inc`、`kernel/app/`、`user/chip8/`、
  `kernel/fs/selftest.inc`、`tests/stage2-app/`、
  `tests/stage2-fs/`、`build_fasm2.sh`、
  `tools/run-qemu-debug.py`；实测数字来自
  build/stage2-app-verify/REPORT.md、
  build/stage2-app-reverify/REPORT-142.md 及其 gates/qemu/
  frame 产物（不入库）。

### 11.8 已知遗留与展望（如实记录，不粉饰）

- **N1 用户故障终态口径（显式裁决）**：fault seam 在 CPL3
  捕获 #UD 后完成全量 teardown + 桌面重绘（帧与基线字节
  一致），无 triple fault；但 APP 裁决为 EIO，foundation 按
  冻结注释（"a failure is a real fault that panics"）走
  **K64-CPU-PANIC**（"foundation: app bring-up failed"），
  桌面不常驻恢复——此为 v1 冻结行为（实现与门禁自洽，非
  产品缺陷）；**桌面常驻故障恢复列 APP-v2**。
- **仓库自包含（FS-v1 教训）**：收口提交必须完整纳入
  kernel/app/、user/chip8/（含 roms/）、tests/stage2-app/
  全部新文件及全部已修改文件——FS-v1 提交曾因缺 user/chip8
  契约文件致干净检出构建失败，本次一并修复（独立验证 N2
  风险项）。
- **单程序 hosting**：launch gates 要求 stage IDLE、
  desktop_echo_row=0，一次仅一个 hosted 程序；多程序/窗口
  系统集成属路线图后续（§12）。
- APP-v1 提交哈希 `31d342c`（已补入锚点表）。

---

## 12. DESKTOP-v2：常驻桌面、原生应用与窗口输入

截至 2026-11-27，普通版本可以在 QEMU TCG 上独立启动到桌面。CHIP-8 和只读 FAT16 浏览器 Files 可通过协作式调度同时运行，支持切换焦点、后台运行和独立的窗口操作。拖动 QEMU 窗口可经 VirtIO GPU 调整客户机扫描输出，桌面保持 4:3。§12.1–§12.4 保存里程碑 `8997110` 的验收基线，§12.5 起记录此后的实现和验证。

### 12.1 桌面绘制与单程序启动

- **32 位做法**：桌面绘制依赖窗口背景与重绘系统，应用由内核的程序加载入口启动（见 §9.1、§11.2）。壁纸源图为 `data/common/wallpapers/T_Home.png`。
- **64 位里程碑基线**：桌面通过 GOP framebuffer 绘制 1024×768 BGRX 壁纸、顶部任务栏、开始按钮和菜单。菜单打开时保存覆盖区，关闭时还原。选择 CHIP-8 后由常驻桌面消费启动请求，从 FAT16 装载程序。验收基线在程序运行期间挂起光标并点亮运行指示，退出后按壁纸、任务栏、回显、光标的顺序恢复画面。普通版本的可操作应用窗口见 §12.6–§12.8。
- **变更理由**：在现有 APP-v1 单程序契约上接入鼠标启动和连续重启，复用已验证的装载、释放和回显保存路径。APP-v1 的开机探针故障终态见 §11.8。
- **证据文件**：`kernel/desktop/{wallpaper,taskbar,menu,desktop}.inc`、`kernel/app/app.inc`、`tests/stage2-desktop/`、`tests/stage2-app/`。

### 12.2 静止光标残影（DEFECT-1）

- 光标停在开始按钮时，旧的 `desktop_pump` 静止分支跳过背景恢复。菜单和光标的读回缓冲区把仍在 framebuffer 上的光标像素当作背景，关闭菜单或启动程序时又把这些像素写回。
- `desktop_pump` 在处理菜单和回显前统一恢复旧光标位置的背景，并在最后重放光标。独立的 APP POLL 路径继续使用 `desktop_cursor_sync`。
- QEMU runner 在 `--menu` 的 launch、close 帧和 `--five` 的 game、game2 帧检查 13 个固定探针及整个 12×18 残影盒。菜单两帧各自与首次开菜单前的画面比较，游戏两帧各自与退出重绘后的画面比较。合成夹具覆盖完整光标和多帧共有单像素残影。

### 12.3 实帧与独立模型

- `tests/stage2-fb/check-gui-judge.py --five-art-dir DIR` 以冻结壁纸、字体、桌面几何和独立 CHIP-8 模型生成期望图像，与 `screen-five-game.ppm` 和 `screen-five-game2.ppm` 分别比较全部 786432 个 RGB 像素，覆盖回显行及光标残影盒。运行前由 `tests/stage2-app/check.py` 生成模型画面。
- 两轮实帧均通过全帧比对。在两张实帧的副本中分别注入一个位于 `(32,17)` 的黑色光标像素，比对均失败并报告该坐标及实际、期望 RGB 值。

### 12.4 宿主门禁与 QEMU 复验

14 项宿主门禁通过。desktop 629、judge 190、app 752、fb 508、fs 540、
fsys 528、mm 2188、cpu 689、blk 481、irq 317、input 402、壁纸 12、
CHIP-8 模型 16 项断言均无失败，boot contract 为 FAILURES: 0。
另有 virtqueue 模型 18 项通过。boot checker 的完整运行夹具补齐了其引导沙箱所需的 FAT16 构建器和 ROM 输入。

| 模式 | 通过断言 |
| --- | ---: |
| `--five --fs --fsprobe` | 381 |
| 默认模式 `--fs --fsprobe` | 185 |
| `--app --fs --fsprobe` | 255 |
| `--app-interactive --fs --fsprobe` | 259 |
| `--menu` | 246 |

五腿均在 macOS arm64 上以 QEMU 11.1.3 TCG 运行，分别使用从冻结映像复制并校验的 FAT16 副本。内核为 4034560 字节，SHA-256 为
`cdcd7b7abe8ce9b66bb600106836103d23362bfeec7aa738fdb175a4456c659f`。
本地日志和截帧位于 `build/desktop-v2-resume/`。

### 12.5 普通启动与常驻输入修复

- **独立启动**：`build_fasm2.sh` 默认生成可独立启动的内核，初始化键鼠和显示后直接绘制桌面，CHIP-8 由用户从菜单启动。宿主输入、截帧 RELEASE 和开机自动运行 CHIP-8 移到 `K64_SMOKE_TEST=1`，由原 QEMU 验证入口选用。
- **真实键盘输入**：常驻桌面保留键鼠中断路由。等待首键的 APP POLL 在关中断下复查队列，再用 `sti; hlt` 等待设备中断。进入游戏后有界 POLL 仍允许输入中断。退出后桌面继续接收键鼠。
- **常驻输入缓冲**：保留 120 个槽位和原始事件计数，键盘队列按读写序号取模。消费释放槽位，桌面与应用切换时同步读序号。
- **多行回显**：普通桌面保存每行字符，应用启动接受任意回显行。退出时先重绘壁纸和任务栏，再重绘所有回显，最后恢复光标。
- **真实输入复验**：用 `python3 tools/run-qemu-debug.py --desktop --display none --art-dir DIR` 启动普通版本，在另一个终端运行 `python3 tests/stage2-desktop/check-runtime.py DIR`。用例通过虚拟 PS/2 设备发出 140 个按键事件，再验证多行回显覆盖下的菜单启动、Enter 进游戏、Esc 返回、原回显重绘及继续输入。macOS 可用 `--desktop` 直接打开 Cocoa 窗口。

### 12.6 双倍输出与游戏按键

- 普通启动优先选 2048×1536 GOP 模式，缺少该模式时再找 1024×768。高分辨率下每个 1024×768 逻辑像素绘成 2×2，鼠标命中、回显布局和 CHIP-8 坐标继续使用同一套逻辑坐标。诊断启动选 1024×768，便于逐像素核对既有基线。
- 本阶段使用 QEMU Cocoa 的 `zoom-to-fit` 缩放固定 GOP 画面。§12.11 增加了客户机运行时调整扫描输出。
- CHIP-8 通过 PS/2 扩展扫描码识别 ↑ 旋转、↓ 加速下落、←/→ 移动。方向键和对应数字键分别记忆按住状态，释放只清除对应来源，Esc 退出。
- 普通应用宿主绘制带最小化、最大化/还原和关闭按钮的标题栏。应用绘制写入由内核保留的客户区，最小化后的任务栏入口恢复原窗口状态。截至本节的单窗口实现已由 §12.10 扩展。

### 12.7 原生应用菜单与 Files

- 开始菜单按应用表绘制标签并传递所选条目。装载器从该表读取 FAT16 路径和窗口标题，CHIP-8 与 Files 共用 Ring 3 装载、窗口与退出回收路径。
- 默认桌面映像 `disk-desktop.img` 提供两个程序和 `README.TXT`。普通启动挂载 FAT16 并注册文件 syscall。固定测试盘的 blk、fs 和 fsprobe 检查仍在诊断启动中运行。
- Files 以只读方式浏览 8.3 目录，支持键盘选择、翻页、进入子目录和返回父目录。文件预览显示前 160 字节的十六进制和 ASCII。
- 普通 QEMU 回归实测了从菜单打开 Files、进入 `/ROMS` 和 `/PROGRAMS`、返回根目录，再打开 `README.TXT` 对照实际文件内容。

### 12.8 窗口拖动与客户区输入

- 普通窗口可拖动标题栏。内核跟踪抓取点并限制窗口范围，使标题栏和客户区留在任务栏下方。最大化、最小化和还原共用窗口状态，普通位置保持为最后拖动的位置。
- 输入 IRQ 在完整 PS/2 包后记录左键边沿坐标和时间。普通应用运行期间使用已有 LAPIC 校准开启 100 Hz 时钟，退出时关闭。窗口壳优先消费标题栏和任务栏事件，客户区的按下事件按顺序交给应用。32 槽队列溢出时放弃积压边沿并取消拖动，以避免漏掉释放后卡在拖动态。
- 普通应用可用 `POLL(rdi=0x434C4E54)` 请求客户区坐标。返回 `RAX=x|y<<16`，客户区之外为 `-1`。`RDX` 位 8 表示新的左键按下，`R8` 返回事件或采样时刻的 10 ms 节拍。最大化时坐标反算到原始应用画布。Files 单击选择。同一行且相隔最多 500 ms、每轴位移最多 4 个逻辑像素的两次按下会打开条目。

### 12.9 单窗口阶段验证与复现

- 普通桌面回归直接经 QEMU 的虚拟 PS/2 键鼠输入，覆盖 2048×1536 输出、键盘队列复用、窗口拖动与屏幕边界、最大化与最小化后的原位置、Files 慢速连点与双击、最大化后点击坐标，以及文件内容和预览恢复。结果见 `build/window-input/runtime.log`。
- 诊断路径复跑 `--five --fs --fsprobe` 通过 381 项，`--app-interactive --fs --fsprobe` 通过 259 项。两张 `--five` 游戏实帧也再次通过 §12.3 的全帧比对。各运行保留独立 FAT16 副本。宿主 INPUT、APP 和 DESKTOP 门禁分别通过 402、754、629 项，均含负向夹具。
- 用 `python3 tools/run-qemu-debug.py --desktop` 打开普通桌面。自动复验可在一个终端用 `python3 tools/run-qemu-debug.py --desktop --display none --art-dir build/runtime-check` 启动默认盘，再在另一终端运行 `python3 tests/stage2-desktop/check-runtime.py build/runtime-check`。

### 12.10 协作式多任务与双窗口

- 普通桌面可同时运行 CHIP-8 和 Files，每个应用表条目对应一个任务槽。已打开的应用从开始菜单或任务栏再次选中时会置顶。第二个窗口初始错开摆放，点击露出的窗口可切换焦点，关闭或最小化前景窗口时焦点转到另一个可见窗口。最小化保留窗口位置、模式和客户区。
- 单核调度器在应用调用 `POLL` 时接回控制权，由现有的 100 Hz LAPIC 时钟安排后续时间片。轮流运行前景、背景和最小化的任务。任务分别持有 Ring 3 代码、数据、栈、系统调用栈、文件描述符和 x87/SSE 状态。同一用户虚拟地址通过切换 `PDPT[4]` 映射到不同物理页，切换后刷新 TLB。桌面接回控制权时解除该用户映射。
- 键鼠按窗口顺序分发。鼠标优先命中任务栏、开始菜单及最上层窗口。焦点改变后，内核为原应用已按下的键补发释放事件。客户区按层次重绘，最后合成菜单和光标。用户态退出或故障仍走既有 APP 页表、物理页和文件描述符回收路径，只回收当前任务。
- QEMU 普通桌面回归用 PS/2 打开两个应用，验证窗口遮挡、切换焦点、Tetris 在 Files 前景时继续运行、Files 保留目录、最小化还原，以及关闭任一应用后另一应用继续接收输入。验证入口仍为 §12.9 所列的 `check-runtime.py`，本轮画面和日志位于 `build/multitask/dual/` 和 `build/multitask/dual-check.log`。

### 12.11 QEMU 宿主窗口与客户机输出联动

- 普通 QEMU 启动改用 `virtio-vga`。GOP 绘制开机画面，桌面常驻后初始化 VirtIO GPU 2D 控制队列。当后端报告尺寸变化时，客户机清除显示事件并用 `GET_DISPLAY_INFO` 获取新尺寸，绘制并上传新的 scanout，然后替换旧资源。Cocoa 传入以宿主像素计的尺寸。普通桌面的初始无界面输出仍为 2048×1536。
- 桌面在保留的 1024×768 画布上合成任务栏、应用窗口、菜单和光标，再按比例采样至新输出。超出 4:3 画布的空间填黑。窗口位置、应用客户区和键鼠命中继续使用逻辑坐标。单次申请最大 4096×3072，底层页按需增长。没有 VirtIO GPU 时使用启动时的 GOP 输出。
- Cocoa 将连续拖动合并，在窗口尺寸约一秒未变时通知客户机。原生 AppKit 窗口改到 700×525、900×675 和 640×480 时，客户机输出都跟随，三张双窗口画面通过全帧比对。[QEMU 显示通知代码](https://github.com/qemu/qemu/blob/master/ui/console.c)
- `check-runtime.py --vnc-socket` 可以在双窗口打开时发出 RFB 尺寸请求，并按独立生成的期望帧逐像素比对。用 `python3 tools/run-qemu-debug.py --desktop --display vnc=unix:resize-vnc.sock --art-dir build/resize-check` 启动后，运行 `python3 tests/stage2-desktop/check-runtime.py build/resize-check --vnc-socket build/resize-check/resize-vnc.sock`。
- 实测 1280×800、901×677、1600×900、4096×3072 及返回 2048×1536 的双窗口帧均与预期一致，缩放后焦点、后台运行和关闭、还原也通过回归。日志为 `build/resize/final-check.log`。
- 本轮 APP 与 DESKTOP 门禁分别通过 754 和 629 项，含负向夹具。FB 布局检查通过 493 项。QEMU 的 `--five --fs --fsprobe` 和 `--app-interactive --fs --fsprobe` 均通过，`--five` 的两张 CHIP-8 实帧再次通过全帧比对。另用标准 VGA 验证了普通内核的 2048×1536 GOP 回退。

### 12.12 后续范围

- 双窗口调度需要应用主动调用 `POLL`。抢占式调度、多实例启动和通用窗口列表属于后续阶段（§9.1、§9.8）。
- 存储目前为只读 FAT16（§10.10）。VirtIO GPU 承担 QEMU 的运行时输出调整，原生显卡的运行时切换各需对应驱动。
- APP-v1 的故障注入结论属于开机诊断路径（§11.8）。普通宿主发生应用错误时走常规回收，恢复语义仍需独立故障验收。右 Ctrl/Alt 跟踪的简化见 §8.10。

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
验证与收口审查通过之后（已随提交 2bd897b 入库）；FONT-FIX 字体位序
修复补录于其独立验证（7/7）与收口审查（无阻断、无必改）通过之后
（§7.5 勘误 + §7.10，已随提交 7080025 入库）；FS-v1 章节补录于其
独立验证（6/6）与审查修复复验（8/8，含 214 项交叉复核）通过之后
（§10，已随提交 424fed2 入库）；APP-v1 章节补录于其独立验证
（10/10）与审查修复复验（6/6 大项，任务 142）通过之后（§11，
已随提交 31d342c 入库）。后续
每个里程碑收口时，由变更史官增补对应小节并更新提交锚点表。*
