#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import re
import time
import math
import psutil
import sys
import select
from datetime import timedelta

from rich import box
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.bar import Bar
from rich.progress import Progress, TaskProgressColumn, RenderableColumn, TextColumn
from rich.console import Console
from rich.layout import Layout

console = Console()

exception_triggered = False
print_exception = console.print_exception
def print_exception_wrapper(*args, **kwargs):
    global exception_triggered
    exception_triggered = True
    print_exception(*args, **kwargs)
console.print_exception = print_exception_wrapper

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
        cmd = ["bash", "info.sh"]
        output = subprocess.check_output(cmd, text=True)
        return output
    except Exception as e:
        console.print_exception(show_locals=True)
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
            except Exception as e:
                console.print_exception(show_locals=True)

        # 拆分 line2
        fields2 = [f.strip() for f in line2.split("|") if f.strip()]
        # fields2[0] => "0", fields2[1] => "0000:C1:00.0", fields2[2] => "0           0    / 0          3043 / 32768"
        # OR: fields2[2] => "0           0    / 0          30431/ 32768"
        chip = fields2[0]
        bus_id = fields2[1]
        tokens_line2_tmp = fields2[2].split()
        tokens_line2 = []
        for token in tokens_line2_tmp:
            if token.endswith("/") and len(token) > 1:
                tokens_line2.append(token[:-1])
                tokens_line2.append("/")
            else:
                tokens_line2.append(token)
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
            except Exception as e:
                console.print_exception(show_locals=True)

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
    uptime_days = uptime_seconds // (24 * 3600)

    return {
        "cpu_percent": cpu_percent,
        "mem_percent": mem_info.percent,
        "mem_used": mem_info.used / (1024**3),
        "mem_total": mem_info.total / (1024**3),
        "swap_percent": swap_info.percent,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "uptime": uptime_days,
    }

def color_for_usage(usage: float) -> str:
    """
    根据使用率返回一个颜色名称，简单分级：
    <40% -> green, <70% -> yellow, >=70% -> red
    """
    if usage < 0.4:
        return "green"
    elif usage < 0.7:
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
    table = Table(box=box.DOUBLE, show_header=False, expand=True)
    table.add_column()
    table.add_row(version_str)
    return table

def make_bar(ratio: float, desc: str = "", color: str = None):
    color = color_for_usage(ratio) if color is None else color
    bar_end = ratio * 100
    cols = [
        TextColumn(desc, style=f"bold {color}") if desc else None,
        RenderableColumn(Bar(size=100, begin=0, end=bar_end, color=color)),
        TaskProgressColumn(style=f"bold {color}", justify="center", text_format="{task.percentage:4.1f}%"),
    ]
    cols = [item for item in cols if item is not None]
    bar = Progress(*cols)
    bar.add_task("", total=100, completed=bar_end, color=color)
    return bar

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
    table.add_column("Bus-Id", ratio=30)
    table.add_column("Health", ratio=15)
    table.add_column("Power(W)", ratio=15)
    table.add_column("Temp(°C)", ratio=15)
    table.add_column("HBM Usage(MB)", ratio=35)
    table.add_column("AICore(%)", ratio=30)

    for dev in devices:
        hbm_ratio = 0
        if dev["hbm_total"] > 0:
            hbm_ratio = dev["hbm_used"] / dev["hbm_total"]
        mem_bar = make_bar(hbm_ratio)
        ai_percentage = dev["ai_core"] / 100.
        ai_bar = make_bar(ai_percentage)
        row_color = color_for_usage(max(hbm_ratio, ai_percentage))

        table.add_row(
            str(dev["id"]),
            dev["name"],
            dev["bus_id"],
            dev["health"],
            f"{dev['power']:.1f}",
            str(dev["temp"]),
            mem_bar,
            ai_bar,
            style=row_color,
        )
    return table

def make_process_table(processes_by_npu):
    """
    生成显示进程信息的表格，新增 CPU 使用率统计，并将 Process Name 改为 Command。
    """
    table = Table(
        show_header=True,
        expand=True,
    )
    table.add_column("NPU", ratio=10)
    table.add_column("PID", ratio=15)
    table.add_column("CPU(%)", ratio=15)          # 新增 CPU 使用率列
    table.add_column("Memory", ratio=15)
    table.add_column("Command", ratio=120)        # 修改列标题

    for npu_id, proc_list in processes_by_npu.items():
        if not proc_list:
            continue
        for proc in proc_list:
            try:
                pid_int = int(proc["pid"])
                # 获取进程 CPU 使用率，interval=0.1 快速采样
                cpu_usage = psutil.Process(pid_int).cpu_percent(interval=0.1)
            except Exception as e:
                # console.print_exception(show_locals=True)
                cpu_usage = 0.0
            try:
                pid_int = int(proc["pid"])
                executable, *args = psutil.Process(pid_int).cmdline()
                cmd_str = f"{executable}\n    {' '.join(args)}"
            except Exception as e:
                # console.print_exception(show_locals=True)
                cmd_str = proc["name"]
            cmd = Text(cmd_str, no_wrap=True)
            table.add_row(
                str(npu_id),
                proc["pid"],
                f"{cpu_usage:.1f}",
                f"{proc['mem']}MB",
                cmd
            )
    return table

def make_system_usage_panel(sysinfo):
    """
    重构后的系统信息面板，使用无边框的 2 行 3 列表格实现：
    第一行：CPU 使用率 bar | UPTIME | LOAD AVG（load1/load5/load15）
    第二行：MEM 使用率 bar | MEM USED | SWP bar
    """
    table = Table(show_header=False, box=None, expand=True)
    table.add_column(justify="center", ratio=7)
    table.add_column(justify="right", ratio=3)
    table.add_column(justify="left", ratio=5)

    # 第一行数据
    cpu_color = "bright_cyan"
    cpu_usage = sysinfo["cpu_percent"] / 100.
    cpu_bar = make_bar(cpu_usage, desc="[ CPU: ", color=cpu_color)
    uptime = f"UPTIME: {sysinfo['uptime']:.1f} days ]"
    uptime = Text(uptime, style=f"bold {cpu_color}")
    load1, load5, load15 = sysinfo["load1"], sysinfo["load5"], sysinfo["load15"]
    load_avg = f"Load Average: {load1:.2f} {load5:.2f} {load15:.2f}"
    load_avg = Text(load_avg, style=f"bold white")

    # 第二行数据
    mem_color = "orchid1"
    mem_usage = sysinfo["mem_percent"] / 100.
    mem_bar = make_bar(mem_usage, desc="[ MEM: ", color=mem_color)
    mem_used = f"USED: {sysinfo['mem_used']:.2f}GiB ]"
    mem_used = Text(mem_used, style=f"bold {mem_color}")
    swap_usage = sysinfo["swap_percent"] / 100.
    swap_bar = make_bar(swap_usage, desc="SWP: ", color="blue")

    table.add_row(cpu_bar, uptime, load_avg)
    table.add_row(mem_bar, mem_used, swap_bar)

    return table

################################################################################
# 4. 主循环，结合 Live 动态刷新
################################################################################

def main():
    global exception_triggered

    with Live(refresh_per_second=1, screen=False, auto_refresh=False) as live:
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
            height = device_table.row_count + 4

            # 进程表（新增 CPU 列和 Command 列）
            process_table = make_process_table(processes_by_npu)

            # 底部系统使用率
            sys_usage_text = make_system_usage_panel(sysinfo)

            empty_panel = Panel(Text(""), box=box.SIMPLE)

            # 修改布局：将各 section 紧挨排列，剩余空间放在最下面
            layout = Layout(name="root")
            layout.split_column(
                Layout(header_table, name="top", size=3),
                Layout(device_table, name="devices", size=height),
                Layout(sys_usage_text, name="bottom", size=3),
                Layout(process_table, name="processes"),
                # Layout(empty_panel),
            )
            # live.stop()
            # breakpoint()

            if not exception_triggered:
                live.update(layout)
                live.refresh()
            else:
                time.sleep(10)
                exception_triggered = False
            time.sleep(2)

if __name__ == "__main__":
    main()
