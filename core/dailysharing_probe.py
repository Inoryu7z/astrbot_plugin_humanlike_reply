"""探测 Dailysharing 插件的下次主动分享时间。

Dailysharing 使用 apscheduler 的 AsyncIOScheduler 管理定时任务。
每个 job 有 `id` 和 `next_run_time` 属性。
本模块通过查找 dailysharing 插件实例，访问其 scheduler.get_jobs()，
返回最近的几个未来任务时间，供判断LLM参考以避免回复被抢先。
"""

import datetime
from typing import Optional

from astrbot.api import logger


def find_plugin_instance(context, keyword: str):
    """查找插件实例（参考 dailysharing 的 _find_plugin 实现）。

    Args:
        context: AstrBot Context
        keyword: 插件名关键字，如 "daily_sharing" 或 "dayflow"

    Returns:
        插件实例对象，或 None
    """
    try:
        stars = context.get_all_stars()
    except Exception as e:
        logger.debug(f"[HumanlikeReply] get_all_stars 失败: {e}")
        return None

    for meta in stars or []:
        p_id = str(getattr(meta, "id", "") or "")
        p_name = str(getattr(meta, "name", "") or "")
        root_dir_name = str(getattr(meta, "root_dir_name", "") or "")
        if keyword not in p_id and keyword not in p_name and keyword not in root_dir_name:
            continue
        for attr in ("star_instance", "instance", "star_cls"):
            candidate = getattr(meta, attr, None)
            if candidate is not None:
                return candidate
    return None


class DailySharingProbe:
    """探测 Dailysharing 插件的下次任务时间。"""

    def __init__(self, context, plugin_name: str):
        self.context = context
        self.plugin_name = plugin_name
        self._cached_plugin = None
        self._not_found_reported = False

    def _get_plugin(self):
        if self._cached_plugin is not None:
            return self._cached_plugin
        if not self.plugin_name:
            return None
        plugin = find_plugin_instance(self.context, self.plugin_name)
        if plugin is None:
            if not self._not_found_reported:
                logger.debug(
                    f"[HumanlikeReply] 未找到 Dailysharing 插件 '{self.plugin_name}'，跳过任务时间探测"
                )
                self._not_found_reported = True
            return None
        self._cached_plugin = plugin
        return plugin

    def probe_next_tasks(self, count: int = 3) -> list:
        """探测最近的几个未来任务。

        Args:
            count: 返回的任务数量上限

        Returns:
            list[dict]，每项含:
              - job_id: 任务ID
              - next_run_time: datetime
              - seconds_until: 距今秒数
            按时间升序排列。失败返回空列表。
        """
        plugin = self._get_plugin()
        if plugin is None:
            return []

        scheduler = getattr(plugin, "scheduler", None)
        if scheduler is None:
            logger.debug("[HumanlikeReply] Dailysharing 插件无 scheduler 属性")
            return []

        try:
            jobs = scheduler.get_jobs()
        except Exception as e:
            logger.warning(f"[HumanlikeReply] 获取 Dailysharing jobs 失败: {e}")
            return []

        now = datetime.datetime.now()
        future_jobs = []
        for job in jobs:
            try:
                nrt = getattr(job, "next_run_time", None)
                if nrt is None:
                    continue
                # apscheduler 的 next_run_time 可能是 naive datetime（本地时间）
                if nrt.tzinfo is not None:
                    nrt_naive = nrt.replace(tzinfo=None)
                else:
                    nrt_naive = nrt
                if nrt_naive <= now:
                    continue
                seconds = (nrt_naive - now).total_seconds()
                future_jobs.append({
                    "job_id": str(getattr(job, "id", "unknown")),
                    "next_run_time": nrt_naive,
                    "seconds_until": seconds,
                })
            except Exception:
                continue

        future_jobs.sort(key=lambda x: x["seconds_until"])
        return future_jobs[:count]

    def format_next_tasks_text(self, count: int = 3) -> str:
        """格式化探测结果为可读文本，供判断LLM使用。

        Returns:
            格式化字符串，如：
            "1. 约10分钟后(14:30) [persona_xxx_random_0]
             2. 约25分钟后(14:45) [briefing_xxx]"
            无任务时返回空字符串。
        """
        tasks = self.probe_next_tasks(count)
        if not tasks:
            return ""

        lines = []
        for i, task in enumerate(tasks, 1):
            minutes = int(task["seconds_until"] // 60)
            seconds = int(task["seconds_until"] % 60)
            time_str = task["next_run_time"].strftime("%H:%M")
            if minutes >= 1:
                eta = f"约{minutes}分{seconds}秒后"
            else:
                eta = f"约{seconds}秒后"
            job_id = task["job_id"]
            # 简化 job_id 显示，去掉冗长前缀
            short_id = job_id.replace("persona_", "p_").replace("random_", "r_")
            lines.append(f"{i}. {eta}({time_str}) [{short_id}]")
        return "\n".join(lines)

    def nearest_seconds_until(self) -> Optional[float]:
        """返回最近任务的剩余秒数，无任务返回 None。"""
        tasks = self.probe_next_tasks(1)
        if not tasks:
            return None
        return tasks[0]["seconds_until"]
