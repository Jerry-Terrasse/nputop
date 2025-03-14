#!/usr/bin/env python3
import subprocess
import re
import time
from rich.live import Live
from rich.table import Table
from rich.console import Console, Group

console = Console()

def get_npu_smi_output():
    """
    调用 npu-smi 命令，并返回其输出文本
    """
    try:
        output = subprocess.check_output(["npu-smi", "info"], text=True)
        return output
    except Exception as e:
        console.print(f"[red]执行 npu-smi 出错：{e}[/red]")
        return ""

def parse_device_section(output: str):
    """
    解析 npu-smi 输出中设备状态部分
    设备部分包含两行数据（第一行：NPU、Name、Health、Power、Temp、Hugepages-Usage；
    第二行：Chip、Bus-Id、AICore、Memory-Usage、HBM-Usage），这里主要提取关键信息
    """
    # 利用包含 "Process id" 的行将设备部分和进程部分分开
    parts = output.split("Process id")
    device_section = parts[0]
    lines = device_section.splitlines()
    # 过滤出以 '|' 开头且内容以数字开头的行（设备数据行）
    data_lines = []
    for line in lines:
        if line.startswith("|"):
            content = line.strip().strip("|").strip()
            if content and content[0].isdigit():
                data_lines.append(line)
    devices = []
    # 每个设备数据占2行，成对解析
    for i in range(0, len(data_lines), 2):
        if i + 1 >= len(data_lines):
            break
        line1 = data_lines[i]
        line2 = data_lines[i+1]
        # 解析第一行
        fields1 = [f.strip() for f in line1.split("|") if f.strip()]
        # fields1 示例：["0     910B4", "OK", "88.2        38                0    / 0"]
        tokens0 = fields1[0].split()
        try:
            npu_id = int(tokens0[0])
        except:
            continue
        name = " ".join(tokens0[1:]) if len(tokens0) > 1 else ""
        health = fields1[1]
        tokens_power = fields1[2].split()
        try:
            power = float(tokens_power[0])
        except:
            power = 0.0
        try:
            temp = int(tokens_power[1])
        except:
            temp = 0
        # 解析 Hugepages 使用情况（示例中格式为 "0 / 0"）
        hugepages_used = 0
        hugepages_total = 0
        if len(tokens_power) >= 5:
            try:
                hugepages_used = int(tokens_power[2])
                hugepages_total = int(tokens_power[4])
            except:
                pass

        # 解析第二行
        fields2 = [f.strip() for f in line2.split("|") if f.strip()]
        # fields2 示例：["0", "0000:C1:00.0", "0           0    / 0          2828 / 32768"]
        chip = fields2[0]
        bus_id = fields2[1]
        tokens_line2 = fields2[2].split()
        ai_core = 0
        mem_used = 0
        mem_total = 0
        hbm_used = 0
        hbm_total = 0
        if len(tokens_line2) >= 7:
            try:
                ai_core = int(tokens_line2[0])
                mem_used = int(tokens_line2[1])
                # tokens_line2[2] 应为 "/"，tokens_line2[3] 为 Memory-Usage 总量
                mem_total = int(tokens_line2[3])
                hbm_used = int(tokens_line2[4])
                # tokens_line2[5] 为 "/"，tokens_line2[6] 为 HBM-Usage 总量
                hbm_total = int(tokens_line2[6])
            except:
                pass

        device = {
            "id": npu_id,
            "name": name,
            "health": health,
            "power": power,
            "temp": temp,
            "hugepages_used": hugepages_used,
            "hugepages_total": hugepages_total,
            "chip": chip,
            "bus_id": bus_id,
            "ai_core": ai_core,
            "mem_used": mem_used,
            "mem_total": mem_total,
            "hbm_used": hbm_used,
            "hbm_total": hbm_total,
        }
        devices.append(device)
    return devices

def parse_process_section(output: str):
    """
    解析 npu-smi 输出中的进程信息部分
    进程部分包含两种行：一类为 “No running processes found in NPU X”，
    另一类为具体进程数据行，格式类似：
      | 0       0                 | 2488494       | python3.9                | 99                      |
    """
    lines = output.splitlines()
    process_lines = []
    in_process_section = False
    for line in lines:
        # 找到进程表头后开始处理
        if "Process id" in line and "Process memory(MB)" in line:
            in_process_section = True
            continue
        if in_process_section:
            # 仅处理以 '|' 开头的行，忽略分割线
            if not line.startswith("|"):
                continue
            if set(line.strip()) <= set("+-="):
                continue
            process_lines.append(line)
    processes_by_npu = {}
    for line in process_lines:
        content = line.strip().strip("|").strip()
        # 处理 “No running processes found in NPU X”
        if content.startswith("No running processes found in NPU"):
            match = re.search(r'No running processes found in NPU\s+(\d+)', content)
            if match:
                npu_id = int(match.group(1))
                processes_by_npu[npu_id] = []
            continue
        # 正常的进程数据行
        fields = [f.strip() for f in line.split("|") if f.strip()]
        if len(fields) < 4:
            continue
        # 第一列包含 NPU 和 Chip，这里取第一个数字作为 NPU 编号
        tokens = fields[0].split()
        try:
            npu_id = int(tokens[0])
        except:
            continue
        pid = fields[1]
        proc_name = fields[2]
        try:
            mem = int(fields[3])
        except:
            mem = 0
        proc_info = {
            "pid": pid,
            "name": proc_name,
            "mem": mem
        }
        processes_by_npu.setdefault(npu_id, []).append(proc_info)
    return processes_by_npu

def generate_tables(devices, processes_by_npu):
    """
    利用 Rich 构造两个表格：
      1. 设备摘要表：显示 NPU、名称、健康、功率、温度、进程数；
      2. 进程详情表：显示每个 NPU 上的进程 ID、进程名称、内存占用
    """
    # 设备摘要表
    device_table = Table(title="NPU监控")
    device_table.add_column("NPU", style="bold")
    device_table.add_column("Name")
    device_table.add_column("Health")
    device_table.add_column("Power(W)")
    device_table.add_column("Temp(°C)")
    device_table.add_column("进程数")
    for device in devices:
        npu_id = device["id"]
        proc_count = len(processes_by_npu.get(npu_id, []))
        device_table.add_row(
            str(device["id"]),
            device["name"],
            device["health"],
            f"{device['power']}",
            f"{device['temp']}",
            str(proc_count)
        )
    # 进程详情表
    proc_table = Table(title="进程详情")
    proc_table.add_column("NPU", style="bold")
    proc_table.add_column("PID")
    proc_table.add_column("Process Name")
    proc_table.add_column("Memory(MB)")
    for npu_id, proc_list in processes_by_npu.items():
        for proc in proc_list:
            proc_table.add_row(
                str(npu_id),
                proc["pid"],
                proc["name"],
                str(proc["mem"])
            )
    return device_table, proc_table

def main():
    """
    主循环：每隔 2 秒调用一次 npu-smi，解析输出后更新 TUI 显示
    """
    with Live(refresh_per_second=2, screen=True) as live:
        while True:
            output = get_npu_smi_output()
            if not output:
                time.sleep(2)
                continue
            devices = parse_device_section(output)
            processes_by_npu = parse_process_section(output)
            device_table, proc_table = generate_tables(devices, processes_by_npu)
            # 利用 Group 将两个表格组合显示
            combined = Group(device_table, proc_table)
            live.update(combined)
            time.sleep(2)

if __name__ == "__main__":
    main()

