"""Fan-control capability reporting and explicitly isolated DEMO simulation.

No subprocess, IPMI, sysfs, BMC or CONFIG_DB writes belong in this module.
The audited Questone2 API exposes readings but its set_speed returns False.
Generic Celestica FCS comments do not establish a usable Questone2 controller.
"""

from copy import deepcopy
import threading


_PLATFORM = "x86_64-cel_questone_2-r0"
_HWSKUS = {"Questone_2", "Questone_2A"}
_LIVE_REASON = (
    "当前 Questone2 平台 API 未实现调速；尚未核实此 BMC 固件的模式读取、"
    "PWM 写入和恢复自动控制接口，也未验证硬件超时保护及安全最低占空比。"
    "风扇状态可读，手动调速暂不可用。"
)


def control_metadata(config, demo=False):
    """Return fresh, JSON-serializable metadata without reading the host.

    Live mode remains unknown: RPM, a target PWM and set_speed=False cannot
    prove the BMC is currently in automatic mode. No temperature guard is
    claimed to be running. A target of 35% does not establish a safe minimum.
    """
    if not isinstance(demo, bool):
        raise TypeError("demo must be a boolean")
    table = config.get("DEVICE_METADATA", {}) if isinstance(config, dict) else {}
    meta = table.get("localhost", {}) if isinstance(table, dict) else {}
    known = isinstance(meta, dict) and meta.get("platform") == _PLATFORM and meta.get("hwsku") in _HWSKUS
    result = {
        "scope": "chassis" if known or demo else "unknown",
        "scope_label": "机箱风扇（BMC 托管；电源风扇单独监测）" if known else "风扇控制范围尚未验证",
        "mode": "unknown", "duty_percent": None,
        "min_percent": None, "max_percent": None, "step_percent": None,
        "write_supported": False, "manual_available": False,
        "available_modes": [], "simulated": False,
        "reason": _LIVE_REASON if known else "此平台尚未验证风扇控制与自动恢复接口；仅显示可用的监测数据。",
        "protection": {
            "active": False, "reason": "control_unverified",
            "description": _LIVE_REASON if known else "此平台尚未验证风扇控制与自动恢复接口；仅显示可用的监测数据。",
        },
        "temperatures": [],
    }
    if demo:
        result.update({
            "scope": "chassis", "scope_label": "DEMO：模拟机箱风扇，不控制任何硬件",
            "mode": "auto", "write_supported": True, "manual_available": True,
            "available_modes": ["auto", "fixed"],
            "reason": "DEMO 仅在内存模拟；不是实机调速或安全范围验证。",
            "min_percent": 20, "max_percent": 100, "step_percent": 1, "simulated": True,
            "protection": {
                "active": False, "reason": "simulation_only",
                "description": "DEMO 仅在内存模拟；20–100% 是演示输入范围，不是实机安全范围。没有运行硬件温控保护。",
            },
        })
    return result


class DemoFanController:
    """In-memory simulator; requires an explicit boolean DEMO opt-in.

    An adapter must still require admin, CSRF, plan/apply and stale-plan checks.
    This simulator deliberately provides no implementation of live writes.
    """

    def __init__(self, *, demo=False):
        if demo is not True:
            raise ValueError("DEMO fan simulation requires explicit demo=True")
        self._lock = threading.Lock()
        self._mode = "auto"
        self._duty = None
        self._closed = False

    @staticmethod
    def validate(values):
        if not isinstance(values, dict) or set(values) - {"mode", "duty_percent"}:
            raise ValueError("风扇设置只能包含 mode 和 duty_percent。")
        mode = values.get("mode")
        if mode not in ("auto", "fixed"):
            raise ValueError("mode 必须为 auto 或 fixed。")
        duty = values.get("duty_percent")
        if mode == "auto":
            if duty is not None:
                raise ValueError("自动模式不能同时指定固定占空比。")
            return {"mode": "auto", "duty_percent": None}
        if isinstance(duty, bool) or not isinstance(duty, int) or not 20 <= duty <= 100:
            raise ValueError("DEMO 固定占空比必须为 20–100 之间的整数。此范围不适用于实机。")
        return {"mode": "fixed", "duty_percent": duty}

    def metadata(self, config=None):
        result = control_metadata(config or {}, demo=True)
        with self._lock:
            result.update(mode=self._mode, duty_percent=self._duty)
            if self._closed:
                result.update(write_supported=False, manual_available=False, available_modes=[])
                result["protection"]["description"] += " 模拟器已关闭。"
        return deepcopy(result)

    def apply(self, values):
        validated = self.validate(values)
        with self._lock:
            if self._closed:
                raise RuntimeError("DEMO fan controller is closed")
            self._mode, self._duty = validated["mode"], validated["duty_percent"]
        return self.metadata()

    def close(self):
        # Simulation cleanup only: never advertised as a hardware fail-safe.
        with self._lock:
            self._mode, self._duty, self._closed = "auto", None, True
