"""天气助手技能 —— 用免费开放接口 wttr.in 查询实时天气。

数据源：https://wttr.in/<城市>?format=j1
无需 API key；网络不可用时返回友好的降级提示，不影响其他能力。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _fetch_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dabai-harness/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_weather(args: dict) -> str:
    city = str(args.get("city") or "").strip()
    if not city:
        return "请提供要查询的城市名（city 参数），如 '北京'。"
    url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=j1"
    try:
        data = _fetch_json(url)
    except Exception as e:
        return (
            f"天气查询暂时不可用（{e.__class__.__name__}）。"
            "请如实告诉用户网络可能不太稳定，稍后再试。"
        )
    try:
        cur = (data.get("current_condition") or [{}])[0]
        area = (data.get("nearest_area") or [{}])[0]
        name = ((area.get("areaName") or [{}])[0]).get("value", city)
        country = ((area.get("country") or [{}])[0]).get("value", "")
        desc = ((cur.get("weatherDesc") or [{}])[0]).get("value", "未知")
        temp = cur.get("temp_C", "?")
        feels = cur.get("FeelsLikeC", cur.get("feelsLikeC", "?"))
        humidity = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "0")
        obs_time = cur.get("observation_time", "")
        return (
            f"{name}（{country}）当前天气：{desc}，气温 {temp}°C（体感 {feels}°C），"
            f"湿度 {humidity}%，风速 {wind} km/h。观测时间 {obs_time}（UTC）。"
        )
    except Exception as e:
        return f"天气数据解析失败：{e}"


HANDLERS = {
    "weather_check": check_weather,
}
