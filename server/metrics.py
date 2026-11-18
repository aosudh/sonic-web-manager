"""Lightweight shared host telemetry and PMON temperature normalization.

Only /proc files are read. No CLI, Redis, IPMI, configuration or device writes.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import threading
import time


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def cpu_counters(text):
    row = next((line.split() for line in text.splitlines() if line.startswith("cpu ")), [])
    if len(row) < 5:
        raise ValueError("missing aggregate cpu counters")
    values = [int(value) for value in row[1:9]]
    if any(value < 0 for value in values):
        raise ValueError("negative cpu counter")
    return tuple(values + [0] * (8-len(values)))


def cpu_percent(previous, current):
    # guest/guest_nice (fields 9/10) are already part of user/nice. I/O wait
    # is counted as idle here, matching the usual aggregate host usage metric.
    deltas = [new-old for old,new in zip(previous,current)]
    if len(deltas) != 8 or any(value < 0 for value in deltas):
        return None
    total, idle = sum(deltas), deltas[3]+deltas[4]
    return round(100*(total-idle)/total,1) if total > 0 else None


def read_proc(root=Path('/proc')):
    result = {'cpu':None,'memory_percent':None,'uptime':None,'errors':[]}
    try:
        result['cpu'] = cpu_counters((root/'stat').read_text())
    except (OSError, ValueError):
        result['errors'].append('cpu')
    try:
        fields = {line.split(':',1)[0]:int(line.split()[1]) for line in (root/'meminfo').read_text().splitlines()}
        total, available = fields['MemTotal'], fields['MemAvailable']
        if not 0 <= available <= total or total <= 0:
            raise ValueError('invalid memory counters')
        result['memory_percent'] = round(100*(total-available)/total,1)
    except (OSError, ValueError, KeyError, IndexError):
        result['errors'].append('memory')
    try:
        uptime = float((root/'uptime').read_text().split()[0])
        if not math.isfinite(uptime) or uptime < 0:
            raise ValueError('invalid uptime')
        result['uptime'] = int(uptime)
    except (OSError, ValueError, IndexError):
        result['errors'].append('uptime')
    return result


class MetricsSampler:
    """One daemon per adapter; samples independently from browser requests.

    start/close are idempotent. No adapter/Redis/cache lock is held while reading
    /proc or waiting. The first two samples are 200ms apart, then every 2s.
    """
    def __init__(self, interval=2.0, initial_interval=0.2, reader=read_proc,
                 monotonic=time.monotonic):
        self.interval, self.initial_interval = interval, initial_interval
        self._reader, self._clock = reader, monotonic
        self._lock, self._sample_lock = threading.Lock(), threading.Lock()
        self._stop, self._ready = threading.Event(), threading.Event()
        self._thread = None
        self._previous = None
        self._sample_time = None
        self._value = {'cpu_percent':None,'memory_percent':None,'uptime':None,
                       'cpu_state':'warming_up','cpu_warning':'正在建立 CPU 采样窗口。',
                       'cpu_sample_seconds':None,'metrics_sampled_at':None,'memory_warning':None}

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name='sonic-host-metrics', daemon=True)
                self._thread.start()

    def _run(self):
        self.sample_once()
        delay = self.initial_interval
        while not self._stop.wait(delay):
            self.sample_once()
            delay = self.interval

    def sample_once(self):
        with self._sample_lock:
            now = self._clock()
            try:
                sample = self._reader()
            except Exception:
                sample = {'cpu':None,'memory_percent':None,'uptime':None,'errors':['cpu','memory','uptime']}
            cpu = sample.get('cpu')
            percent, seconds = None, None
            state, warning = 'warming_up', '正在建立 CPU 采样窗口。'
            if cpu is None:
                state, warning = 'unknown', '无法读取 /proc/stat；没有使用模拟值或旧值代替。'
            elif self._previous is not None:
                percent = cpu_percent(self._previous[0],cpu)
                seconds = round(now-self._previous[1],3)
                state = 'ok' if percent is not None and seconds > 0 else 'unknown'
                if state != 'ok':
                    percent = None
                warning = None if state=='ok' else 'CPU 计数器未增加或已回退，正在重新采样。'
            self._previous = (cpu,now) if cpu is not None else None
            value = {'cpu_percent':percent,'memory_percent':sample.get('memory_percent'),'uptime':sample.get('uptime'),
                     'cpu_state':state,'cpu_warning':warning,'cpu_sample_seconds':seconds,
                     'metrics_sampled_at':datetime.now(timezone.utc).isoformat(),
                     'memory_warning':'无法读取有效的 MemTotal/MemAvailable。' if 'memory' in sample.get('errors',[]) else None}
            with self._lock:
                self._value, self._sample_time = value, now
            if state != 'warming_up':
                self._ready.set()

    def snapshot(self, wait=0):
        if wait:
            self._ready.wait(min(wait,0.3))
        with self._lock:
            value = copy.deepcopy(self._value)
            age = None if self._sample_time is None else max(0,self._clock()-self._sample_time)
        value['metrics_age_seconds'] = round(age,3) if age is not None else None
        value['metrics_source'] = '/proc/stat, /proc/meminfo, /proc/uptime'
        value['cpu_definition'] = '主机全部 CPU 平均忙碌百分比（0–100%）；idle 与 iowait 不计入忙碌，guest 不重复计数。'
        if self._stop.is_set():
            value.update(cpu_percent=None,cpu_state='stopped',cpu_warning='CPU 采样器已停止。')
        elif age is not None and age > max(10,self.interval*3):
            value.update(cpu_percent=None,memory_percent=None,cpu_state='stale',cpu_warning='CPU 采样已过期，状态未知。')
        return value

    def close(self):
        self._stop.set()
        self._ready.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)


def sample_timestamp(raw, now=None):
    """Normalize PMON calendar text, timezone-aware ISO, or Unix seconds.

    Numeric seconds must be finite and in 2000–2099; reject milliseconds,
    uninitialized epochs and implausible dates rather than guessing a unit.
    """
    now = now or datetime.now(timezone.utc)
    try:
        text = str(raw)
        if isinstance(raw, bool):
            return None, None, False
        if isinstance(raw, (int, float)) or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', text):
            seconds = float(raw)
            if not math.isfinite(seconds) or not 946684800 <= seconds < 4102444800:
                return None, None, False
            stamp = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            stamp = datetime.strptime(text,'%Y%m%d %H:%M:%S').replace(tzinfo=timezone.utc) if len(text)==17 else datetime.fromisoformat(text.replace('Z','+00:00'))
        if stamp.tzinfo is None:
            return None, None, False
        age = (now-stamp).total_seconds()
        return stamp.astimezone(timezone.utc).isoformat(), round(age,1), -10 <= age <= 180
    except (ValueError, TypeError, OverflowError, OSError):
        return None, None, False


def temperature_row(name, item, *, now=None, simulated=False):
    timestamp, age, fresh = sample_timestamp(item.get('timestamp'),now)
    value = number(item.get('temperature'))
    thresholds = {key:number(item.get(key)) for key in ('low_threshold','high_threshold','critical_low_threshold','critical_high_threshold')}
    flag = str(item.get('warning_status','')).lower()
    warning = True if flag=='true' else False if flag=='false' else None
    status = '未知'
    if value is None:
        status = '无有效读数'
    elif not fresh:
        status = '数据过期或时间未知'
    else:
        low, high = thresholds['critical_low_threshold'],thresholds['critical_high_threshold']
        critical = (low is not None and value<=low) or (high is not None and value>=high)
        low, high = thresholds['low_threshold'],thresholds['high_threshold']
        exceeded = (low is not None and value<=low) or (high is not None and value>=high)
        status = '严重告警' if critical else '告警' if warning or exceeded else '正常' if warning is False else '未知'
    return {'name':name,'temperature':value,'unit':'°C',**thresholds,'warning_status':warning,'status':status,
            'timestamp':timestamp,'raw_timestamp':item.get('timestamp'),'age_seconds':age,'fresh':fresh,
            'source':'DEMO 模拟样本' if simulated else 'PMON / STATE_DB',
            'sensor_note':'ASIC 外部传感器，非芯片内部结温。' if 'ASIC External' in name else None}
