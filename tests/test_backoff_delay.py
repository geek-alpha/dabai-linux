"""backoff_delay 纯函数契约 + 三处调用方行为一致性。

背景：大白原本有 4 份各自为政的退避实现（harness/core.py retry_async、
agent.py 重连循环、harness/tasks.py 任务重试、sub_agents.py），
只有 retry_async 有抖动/封顶/Retry-After。已全部收敛到 backoff_delay 这一个纯函数，
这里钉住它的行为契约，防止将来某处又偷偷写回裸指数。
"""
import math

import pytest

from harness.core import backoff_delay


def test_attempt1_is_base():
    """第 1 次重试 = base，不加指数。"""
    assert backoff_delay(1, base=1.0, jitter=False) == 1.0
    assert backoff_delay(1, base=2.0, jitter=False) == 2.0


def test_exponential_growth():
    """指数退避：attempt=n → base * 2^(n-1)。"""
    assert backoff_delay(2, base=1.0, jitter=False) == 2.0
    assert backoff_delay(3, base=1.0, jitter=False) == 4.0
    assert backoff_delay(4, base=0.5, jitter=False) == 4.0


def test_cap_never_exceeded():
    """attempt 足够大时 delay 封在 cap，不炸到小时级。"""
    assert backoff_delay(13, base=1.0, jitter=False) == 30.0
    assert backoff_delay(100, base=1.0, jitter=False) == 30.0
    # 大 base 也不能顶穿
    assert backoff_delay(1, base=1000.0, jitter=False) == 30.0


def test_max_exp_stops_growth():
    """指数到 max_exp 封顶：attempt=6, max_exp=4 → base*16 不再涨。"""
    assert backoff_delay(6, base=1.0, max_exp=4, jitter=False) == 16.0
    assert backoff_delay(100, base=1.0, max_exp=4, jitter=False) == 16.0


def test_jitter_within_bounds_and_cap():
    """抖动乘在封顶之前：结果 ∈ [0.8*raw, 1.2*raw] 且永不超 cap。"""
    for attempt in range(1, 15):
        raw = backoff_delay(attempt, base=1.0, jitter=False)
        for _ in range(20):
            d = backoff_delay(attempt, base=1.0, jitter=True)
            assert d <= 30.0, (attempt, d)
            assert 0.8 * raw - 1e-9 <= d <= 1.2 * raw + 1e-9, (attempt, raw, d)


def test_retry_after_takes_priority():
    """服务端给了 Retry-After 就精确听它的，不叠抖动、不按指数。"""
    assert backoff_delay(1, retry_after=7.0) == 7.0
    assert backoff_delay(5, base=1.0, retry_after=3.0) == 3.0
    # 超大 Retry-After 也封顶
    assert backoff_delay(1, retry_after=99999.0) == 30.0
    # 负值/零 不报错
    assert backoff_delay(1, retry_after=0.0) == 0.0


def test_agent_reconnect_equivalent_no_jitter():
    """agent.py 重连循环旧式 min(2.0*2^min(attempt-1,4), 20.0) 的等价复现。"""
    for attempt in range(1, 10):
        legacy = min(2.0 * (2 ** min(attempt - 1, 4)), 20.0)
        assert backoff_delay(attempt, base=2.0, cap=20.0, max_exp=4, jitter=False) == legacy


def test_tasks_scheduler_capped():
    """tasks.py 任务重试旧式 base*2^(attempt-1) 无封顶；收敛后必须封顶。"""
    # 旧行为：backoff=1.0, attempt=13 → 4096s（1 小时+），这是 bug
    legacy_uncapped = 1.0 * (2 ** 12)
    assert legacy_uncapped == 4096.0
    # 新行为：封在 cap
    assert backoff_delay(13, base=1.0, jitter=False) == 30.0


def test_deterministic_without_jitter():
    """jitter=False 时同一输入恒等输出（可测试、可预测）。"""
    assert backoff_delay(4, base=2.0, max_exp=3, jitter=False) == \
        backoff_delay(4, base=2.0, max_exp=3, jitter=False) == 16.0


# ---------- Retry-After 的两种合法形式（RFC 7231）----------
# 只认秒数时，带 HTTP-date 的响应会静默退回 ~1s 指数退避：
# 服务端要求等 30s、我们 1s 后重撞，等于把限流又加重一次。

def _exc_with_retry_after(value):
    class _Resp:
        headers = {"retry-after": value}

    class _Err(Exception):
        response = _Resp()

    return _Err()


def test_retry_after_http_date_也被认():
    """IMF-fixdate：最标准的 HTTP-date 写法。"""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from harness.core import _retry_after_of

    hdr = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=25), usegmt=True)
    ra = _retry_after_of(_exc_with_retry_after(hdr))
    assert ra is not None, f"HTTP-date 没被解析：{hdr}"
    assert 22.0 <= ra <= 25.0, (hdr, ra)


@pytest.mark.parametrize("fmt", [
    "%A, %d-%b-%y %H:%M:%S GMT",   # RFC 850：老网关会发
    "%a %b %d %H:%M:%S %Y",        # asctime：不带时区，必须按 UTC 补
])
def test_retry_after_老格式也能认(fmt):
    """asctime 解析出来是 naive datetime——不补 UTC 会在减法处 TypeError 崩掉。"""
    from datetime import datetime, timedelta, timezone

    from harness.core import _retry_after_of

    hdr = (datetime.now(timezone.utc) + timedelta(seconds=25)).strftime(fmt)
    ra = _retry_after_of(_exc_with_retry_after(hdr))
    assert ra is not None, f"没被解析：{hdr}"
    assert 22.0 <= ra <= 25.0, (hdr, ra)


def test_retry_after_http_date_过去时间归零():
    """服务端给的时间已经过去 → 立即重试，不返回负数。"""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from harness.core import _retry_after_of

    hdr = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
    assert _retry_after_of(_exc_with_retry_after(hdr)) == 0.0


def test_retry_after_http_date_超大也封顶():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from harness.core import _retry_after_of

    hdr = format_datetime(datetime.now(timezone.utc) + timedelta(days=1), usegmt=True)
    assert _retry_after_of(_exc_with_retry_after(hdr)) == 30.0


@pytest.mark.parametrize("bad", ["", "abc", "nan", "12.5.3", "None"])
def test_retry_after_非法值退回指数退避(bad):
    """坏值必须退回指数退避，不能当成 0 秒打热循环。"""
    from harness.core import _retry_after_of

    assert _retry_after_of(_exc_with_retry_after(bad)) is None


def test_retry_after_无穷大封顶而非归零():
    """1e999 解析成 inf：封顶到 30s（与 99999 同待遇），不是 None 也不是 0。"""
    from harness.core import _retry_after_of

    assert _retry_after_of(_exc_with_retry_after("1e999")) == 30.0
