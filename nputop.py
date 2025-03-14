#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import re
import time
import math
import psutil
from datetime import timedelta

from rich import box
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.console import Console, Group
from rich.layout import Layout

console = Console()

################################################################################
# 1. 调用 npu-smi 并解析输出
################################################################################

def get_npu_smi_output():
    """
    调用 npu-smi 命令并返回其输出文本
    """
    try:
        cmd = ["npu-smi", "info"]
        cmd = ["ssh", "ict_raw", "npu-smi", "info"]
        cmd = ["bash", "example.sh"]
        output = subprocess.check_output(cmd, text=True)
        return output
    except Exception as e:
        console.print(f"[red]执行 npu-smi 出错：{e}[/red]")
        return ""

def parse_device_section(output: str):
    """
    解析 npu-smi 输出中设备状态部分
    返回形如:
    [
      {
        "id": 0,
        "name": "910B4",
        "health": "OK",
        "power": 88.2,
        "temp": 39,
        "hugepages_used": 0,
        "hugepages_total": 0,
        "chip": "0",
        "bus_id": "0000:C1:00.0",
        "ai_core": 0,
        "mem_used": 0,
        "mem_total": 0,
        "hbm_used": 3043,
        "hbm_total": 32768
      },
      ...
    ]
    """
    parts = output.split("Process id")  # 将设备信息与进程信息分段
    device_section = parts[0]
    lines = device_section.splitlines()

    # 找出数据行（示例里每个NPU对应2行）
    data_lines = []
    for line in lines:
        if line.startswith("|"):
            content = line.strip().strip("|").strip()
            # 判断是否以数字开头来识别设备数据行
            if content and content[0].isdigit():
                data_lines.append(line)

    devices = []
    for i in range(0, len(data_lines), 2):
        if i + 1 >= len(data_lines):
            break
        line1 = data_lines[i]
        line2 = data_lines[i+1]

        # line1 示例：
        # "| 0     910B4               | OK            | 88.2        39                0    / 0             |"
        # line2 示例：
        # "| 0                         | 0000:C1:00.0  | 0           0    / 0          3043 / 32768         |"

        # 拆分 line1
        fields1 = [f.strip() for f in line1.split("|") if f.strip()]
        # fields1[0] => "0     910B4", fields1[1] => "OK", fields1[2] => "88.2        39                0    / 0"
        tokens0 = fields1[0].split()
        npu_id = int(tokens0[0])
        name = " ".join(tokens0[1:]) if len(tokens0) > 1 else ""
        health = fields1[1]
        # 解析功率、温度、Hugepages
        tokens_power = fields1[2].split()
        # 例: ["88.2", "39", "0", "/", "0"]
        power = float(tokens_power[0]) if tokens_power else 0.0
        temp = int(tokens_power[1]) if len(tokens_power) > 1 else 0
        huge_used = 0
        huge_total = 0
        if len(tokens_power) >= 5:
            try:
                huge_used = int(tokens_power[2])
                huge_total = int(tokens_power[4])
            except:
                pass

        # 拆分 line2
        fields2 = [f.strip() for f in line2.split("|") if f.strip()]
        # fields2[0] => "0", fields2[1] => "0000:C1:00.0", fields2[2] => "0           0    / 0          3043 / 32768"
        chip = fields2[0]
        bus_id = fields2[1]
        tokens_line2 = fields2[2].split()
        # 例: ["0", "0", "/", "0", "3043", "/", "32768"]
        ai_core = 0
        mem_used = 0
        mem_total = 0
        hbm_used = 0
        hbm_total = 0
        if len(tokens_line2) >= 7:
            try:
                ai_core = int(tokens_line2[0])
                mem_used = int(tokens_line2[1])
                mem_total = int(tokens_line2[3])
                hbm_used = int(tokens_line2[4])
                hbm_total = int(tokens_line2[6])
            except:
                pass

        device = {
            "id": npu_id,
            "name": name,
            "health": health,
            "power": power,
            "temp": temp,
            "hugepages_used": huge_used,
            "hugepages_total": huge_total,
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
    返回形如:
    {
      0: [
        {"pid": "2488494", "name": "python3.9", "mem": 99},
        ...
      ],
      1: [...],
      ...
    }
    """
    lines = output.splitlines()
    process_lines = []
    in_process_section = False

    for line in lines:
        # 当出现 "Process id" 和 "Process memory(MB)" 时，说明进程表格开始
        if "Process id" in line and "Process memory(MB)" in line:
            in_process_section = True
            continue
        if in_process_section:
            # 仅处理以 '|' 开头且不是分割线的行
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
        # 正常进程行: "| 0       0                 | 2488494       | python3.9                | 99                      |"
        fields = [f.strip() for f in line.split("|") if f.strip()]
        if len(fields) < 4:
            continue
        # fields[0] => "0       0", fields[1] => "2488494", fields[2] => "python3.9", fields[3] => "99"
        # 取第一个数字作为 NPU ID
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

        processes_by_npu.setdefault(npu_id, []).append({
            "pid": pid,
            "name": proc_name,
            "mem": mem
        })

    return processes_by_npu

################################################################################
# 2. 获取系统信息（CPU、内存、Swap、负载、Uptime 等），并做可视化
################################################################################

def get_system_info():
    """
    利用 psutil 获取 CPU、内存、Swap、负载、系统运行时长等信息
    返回一个字典
    """
    cpu_percent = psutil.cpu_percent(interval=None)
    mem_info = psutil.virtual_memory()
    swap_info = psutil.swap_memory()
    load1, load5, load15 = (0.0, 0.0, 0.0)
    if hasattr(psutil, "getloadavg"):
        load1, load5, load15 = psutil.getloadavg()

    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    uptime_str = str(timedelta(seconds=int(uptime_seconds)))  # 形如 "12 days, 1:23:45"

    return {
        "cpu_percent": cpu_percent,
        "mem_percent": mem_info.percent,
        "mem_used": mem_info.used / (1024**3),
        "mem_total": mem_info.total / (1024**3),
        "swap_percent": swap_info.percent,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "uptime": uptime_str,
    }

def make_bar(usage: float, length: int = 10) -> str:
    """
    生成类似 nvitop 的使用率条 (partial blocks)。usage 为 0~100之间的数值
    length 表示进度条的宽度（以“整块”计）
    """
    # 先转换为 [0,1]
    frac = min(max(usage / 100.0, 0), 1)
    blocks = "▏▎▍▌▋▊▉█"
    total = int(length * 8 * frac)  # 一格=8分
    full = total // 8
    rem = total % 8
    bar = "█" * full
    if rem > 0:
        bar += blocks[rem - 1]
    # 不足的用空格补齐
    bar = bar.ljust(length, " ")
    return bar

def color_for_usage(usage: float) -> str:
    """
    根据使用率返回一个颜色名称，简单分级：
    <40% -> green, <70% -> yellow, >=70% -> red
    """
    if usage < 40:
        return "green"
    elif usage < 70:
        return "yellow"
    else:
        return "red"

################################################################################
# 3. 生成类似 nvitop 的表格和信息面板
################################################################################

def make_top_header(version_str: str = "NPU-SMI TUI 1.0.0"):
    """
    仿照 nvitop 的顶栏（仅示意），可放置 Driver Version / npu-smi 版本等信息
    """
    # 为了模仿 nvitop 的 ASCII 边框，这里用 Table + box.SQUARE_DOUBLE_HEAD
    table = Table(box=box.SQUARE_DOUBLE_HEAD, show_header=False, expand=True)
    table.add_column()
    table.add_row(version_str)
    return table

def make_device_table(devices):
    """
    生成显示 NPU 概要信息的表格
    """
    table = Table(
        # box=box.MINIMAL_DOUBLE_HEAD,
        show_header=True,
        # title="NPU Overview",
        expand=True,
    )
    table.add_column("NPU", ratio=10)
    table.add_column("Name", ratio=10)
    table.add_column("Bus-Id", ratio=20)
    table.add_column("Health", ratio=15)
    table.add_column("Power(W)", ratio=15)
    table.add_column("Temp(°C)", ratio=15)
    table.add_column("HBM Usage(MB)", ratio=40)
    table.add_column("AICore(%)", ratio=35)

    for dev in devices:
        hbm_ratio = 0
        if dev["hbm_total"] > 0:
            hbm_ratio = dev["hbm_used"] / dev["hbm_total"] * 100
        color = color_for_usage(hbm_ratio)
        bar = make_bar(hbm_ratio, length=6)
        usage_str = f"[{color}]{bar} {hbm_ratio:.1f}%[/{color}]"

        table.add_row(
            str(dev["id"]),
            dev["name"],
            dev["bus_id"],
            dev["health"],
            f"{dev['power']:.1f}",
            str(dev["temp"]),
            usage_str,
            str(dev["ai_core"]),
        )
    return table

def make_process_table(processes_by_npu):
    """
    生成显示进程信息的表格
    """
    table = Table(
        # box=box.MINIMAL_DOUBLE_HEAD,
        show_header=True,
        # title="Processes",
        expand=True,
    )
    table.add_column("NPU", ratio=10)
    table.add_column("PID", ratio=10)
    table.add_column("Memory(MB)", ratio=20)
    table.add_column("Process Name", ratio=120)

    for npu_id, proc_list in processes_by_npu.items():
        if not proc_list:
            # 没有进程则跳过或显示空
            continue
        for proc in proc_list:
            table.add_row(
                str(npu_id),
                proc["pid"],
                str(proc["mem"]),
                proc["name"],
            )
    return table

def make_system_usage_panel(sysinfo):
    """
    构造类似 nvitop 底部的 CPU / MEM / SWP / UPTIME / LOAD AVG 显示
    采用纯文本行 + 进度条的方式
    """
    # CPU
    cpu_usage = sysinfo["cpu_percent"]
    cpu_bar = make_bar(cpu_usage, length=5)
    cpu_color = color_for_usage(cpu_usage)
    cpu_str = f"[bold white]CPU:[/bold white] [{cpu_color}]{cpu_bar} {cpu_usage:.1f}%[/{cpu_color}]"

    # MEM
    mem_usage = sysinfo["mem_percent"]
    mem_bar = make_bar(mem_usage, length=25)
    mem_color = color_for_usage(mem_usage)
    mem_str = f"[bold white]MEM:[/bold white] [{mem_color}]{mem_bar} {mem_usage:.1f}%[/{mem_color}]  USED: {sysinfo['mem_used']:.2f}GiB"

    # SWP
    swap_usage = sysinfo["swap_percent"]
    swap_bar = make_bar(swap_usage, length=10)
    swap_color = color_for_usage(swap_usage)
    swap_str = f"[bold white]SWP:[/bold white] [{swap_color}]{swap_bar} {swap_usage:.1f}%[/{swap_color}]"

    # UPTIME
    uptime_str = f"[bold white]UPTIME:[/bold white] {sysinfo['uptime']}"
    # LOAD AVG
    load_str = f"( Load Average:  {sysinfo['load1']:.2f}  {sysinfo['load5']:.2f}  {sysinfo['load15']:.2f} )"

    line1 = f"{cpu_str}   {uptime_str}   {load_str}"
    line2 = f"{mem_str}   {swap_str}"

    return f"{line1}\n{line2}"

################################################################################
# 4. 主循环，结合 Live 动态刷新
################################################################################

def main():
    with Live(refresh_per_second=2, screen=False) as live:
        while True:
            # 解析 npu-smi
            output = get_npu_smi_output()
            if not output:
                time.sleep(2)
                continue
            devices = parse_device_section(output)
            processes_by_npu = parse_process_section(output)

            # 系统信息
            sysinfo = get_system_info()

            # 顶部标题（示例：npu-smi 版本信息，可自行修改）
            header_table = make_top_header("npu-smi 23.0.6    (Mock TUI)")

            # 设备信息表
            device_table = make_device_table(devices)

            # 进程表
            process_table = make_process_table(processes_by_npu)

            # 底部系统使用率
            sys_usage_text = make_system_usage_panel(sysinfo)

            # 修改布局：将各 section 紧挨排列，剩余空间放在最下面
            layout = Layout(name="root")
            layout.split_column(
                Layout(header_table, name="top", size=3),
                Layout(device_table, name="devices"),
                Layout(process_table, name="processes"),
                Layout(Text(sys_usage_text, justify="left"), name="bottom", size=3),
                Layout(name=""),
            )

            live.update(layout)
            time.sleep(2)

if __name__ == "__main__":
    main()
