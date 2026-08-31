"""B 站上游请求治理：统一并发、接口间隔、账号级冷却和可观测状态。

所有显式 requests 调用都应通过 ``governor.call``。治理器不持有 HTTP 客户端，
调用方仍把 ``requests.get``（或测试桩）传入，因此网络层可替换、可测试。

这不是对 B 站公开限额的猜测：参数取项目既有保守值。服务端明确返回 412/429
时，立即打开账号级熔断；冷却期内请求快速失败，不让 Flask 线程睡几分钟。
"""
from __future__ import annotations

import email.utils
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

log = logging.getLogger("bd.governor")


@dataclass(frozen=True)
class GovernorSnapshot:
    """供状态接口/日志使用的只读快照。"""

    cooldown_until: int
    cooldown_seconds: int
    in_flight: int
    total_requests: int
    rate_limit_events: int
    last_status: int


class UpstreamRateLimitError(RuntimeError):
    """B 站明确限流，或账号仍处于冷却期。"""

    def __init__(self, message: str, *, status_code: int = 429,
                 retry_after: float = 0.0, endpoint: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 429)
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.endpoint = endpoint


class RequestGovernor:
    """单进程、单账号的请求闸门。

    ``kind`` 分类：
    - sensitive：arc/search 等账号级敏感接口，至少间隔 1.5 秒；自动追更外层仍保留
      5 秒/UP，所以正常路径不会重复等待，分页突发则会被这里摊开。
    - bulk：弹幕分片，限制短时间突发。
    - auth/default：只限制全局在途数并接入统一冷却。
    """

    _INTERVALS = {
        "default": 0.0,
        "auth": 0.0,
        "sensitive": 1.5,
        "bulk": 0.25,
        "media": 0.5,
    }
    _JITTER = {
        "default": 0.0,
        "auth": 0.0,
        "sensitive": 0.35,
        "bulk": 0.08,
        "media": 0.15,
    }
    _DEFAULT_COOLDOWN = {412: 10 * 60, 429: 2 * 60}

    def __init__(self, max_in_flight: int = 2) -> None:
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, int(max_in_flight)))
        self._next_allowed: dict[str, float] = {}
        self._cooldown_until_mono = 0.0
        self._cooldown_until_wall = 0.0
        self._in_flight = 0
        self._total_requests = 0
        self._rate_limit_events = 0
        self._last_status = 0

    @staticmethod
    def _retry_after_seconds(headers: Optional[Mapping[str, Any]]) -> Optional[float]:
        value = (headers or {}).get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError):
            pass
        try:
            dt = email.utils.parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _cooldown_error_locked(self, endpoint: str = "") -> Optional[UpstreamRateLimitError]:
        remaining = self._cooldown_until_mono - time.monotonic()
        if remaining <= 0:
            return None
        return UpstreamRateLimitError(
            f"B 站请求处于风控冷却期，请约 {max(1, int(remaining))} 秒后重试",
            status_code=self._last_status or 429,
            retry_after=remaining,
            endpoint=endpoint,
        )

    def _reserve(self, kind: str, endpoint: str) -> None:
        kind = kind if kind in self._INTERVALS else "default"
        while True:
            with self._lock:
                err = self._cooldown_error_locked(endpoint)
                if err:
                    raise err
                now = time.monotonic()
                ready = self._next_allowed.get(kind, 0.0)
                wait = ready - now
                if wait <= 0:
                    interval = self._INTERVALS[kind]
                    jitter = random.uniform(0.0, self._JITTER[kind])
                    self._next_allowed[kind] = now + interval + jitter
                    return
            time.sleep(min(wait, 1.0))

    def trip(self, status_code: int, *, headers: Optional[Mapping[str, Any]] = None,
             endpoint: str = "", message: str = "") -> UpstreamRateLimitError:
        """打开账号级冷却并返回应抛出的异常。重复事件只延长、不缩短冷却。"""
        status_code = int(status_code or 429)
        retry_after = self._retry_after_seconds(headers)
        if retry_after is None:
            retry_after = float(self._DEFAULT_COOLDOWN.get(status_code, 2 * 60))
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            self._cooldown_until_mono = max(self._cooldown_until_mono,
                                            now_mono + retry_after)
            self._cooldown_until_wall = max(self._cooldown_until_wall,
                                            now_wall + retry_after)
            self._rate_limit_events += 1
            self._last_status = status_code
        log.warning("B 站限流 status=%s endpoint=%s cooldown=%.0fs",
                    status_code, endpoint or "unknown", retry_after)
        return UpstreamRateLimitError(
            message or f"B 站返回 HTTP {status_code}，已暂停上游请求",
            status_code=status_code,
            retry_after=retry_after,
            endpoint=endpoint,
        )

    def note_api_code(self, code: Any, *, endpoint: str = "") -> None:
        """识别 JSON body 中的限流码；非限流业务错误由调用方处理。"""
        try:
            value = int(code)
        except (TypeError, ValueError):
            return
        if value in (-412, 412):
            raise self.trip(412, endpoint=endpoint, message="B 站接口触发 412 风控")
        if value in (-429, 429):
            raise self.trip(429, endpoint=endpoint, message="B 站接口请求过于频繁")

    def wait(self, kind: str = "default", *, endpoint: str = "") -> None:
        """给 yt-dlp 等无法注入 requests 函数的链路设置启动闸门。"""
        self._reserve(kind, endpoint)

    def call(self, kind: str, request_fn: Callable[..., Any], url: str,
             **kwargs: Any) -> Any:
        """执行一次受治理请求；HTTP 412/429 自动转成冷却异常。"""
        self._reserve(kind, url)
        self._slots.acquire()
        counted = False
        try:
            with self._lock:
                err = self._cooldown_error_locked(url)
                if err:
                    raise err
                self._in_flight += 1
                self._total_requests += 1
                counted = True
            response = request_fn(url, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            if status in (412, 429):
                # 在释放并发槽前打开冷却，避免排队线程抢到槽后继续打上游。
                raise self.trip(status, headers=getattr(response, "headers", None),
                                endpoint=url)
            return response
        finally:
            if counted:
                with self._lock:
                    self._in_flight -= 1
            self._slots.release()

    def snapshot(self) -> GovernorSnapshot:
        with self._lock:
            remaining = max(0.0, self._cooldown_until_mono - time.monotonic())
            return GovernorSnapshot(
                cooldown_until=int(self._cooldown_until_wall) if remaining else 0,
                cooldown_seconds=int(remaining),
                in_flight=self._in_flight,
                total_requests=self._total_requests,
                rate_limit_events=self._rate_limit_events,
                last_status=self._last_status,
            )

    def reset_spacing(self) -> None:
        """测试/登录世代切换时可清接口间隔；刻意不清账号风控冷却。"""
        with self._lock:
            self._next_allowed.clear()


governor = RequestGovernor(max_in_flight=2)


def governed_get(request_fn: Callable[..., Any], url: str, *,
                  kind: str = "default", **kwargs: Any) -> Any:
    return governor.call(kind, request_fn, url, **kwargs)
