"""
Discord チャンネルにログを送信するカスタム logging.Handler。
バッファリングにより API レート制限を回避。
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord

JST = timezone(timedelta(hours=9))

# ログレベル → 絵文字マッピング
LEVEL_EMOJI = {
    logging.DEBUG:    "🔍",
    logging.INFO:     "ℹ️",
    logging.WARNING:  "⚠️",
    logging.ERROR:    "❌",
    logging.CRITICAL: "🔥",
}

class DiscordLogHandler(logging.Handler):
    def __init__(self, bot, channel_id: int, flush_interval: float = 5.0):
        super().__init__(level=logging.INFO)
        self.bot = bot
        self.channel_id = channel_id
        self._buffer: list[str] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._flush_interval = flush_interval

    def emit(self, record: logging.LogRecord):
        # discord.py 内部ログは除外（無限ループ防止）
        if record.name.startswith("discord"):
            return
        emoji = LEVEL_EMOJI.get(record.levelno, "📋")
        ts = datetime.now(JST).strftime("%H:%M:%S")
        msg = f"`{ts}` {emoji} **[{record.levelname}]** {record.getMessage()}"
        
        # バッファに追加（スレッドセーフ）
        loop = self.bot.loop if hasattr(self.bot, 'loop') and self.bot.loop.is_running() else None
        if loop:
            loop.call_soon_threadsafe(self._add_to_buffer, msg)

    def _add_to_buffer(self, msg: str):
        self._buffer.append(msg)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self._flush_interval)
        await self.flush_buffer()

    async def flush_buffer(self):
        if not self._buffer:
            return
        async with self._lock:
            messages = self._buffer.copy()
            self._buffer.clear()

        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            return

        # 2000文字制限を考慮して分割送信
        chunk = ""
        for msg in messages:
            if len(chunk) + len(msg) + 1 > 1900:
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
                chunk = msg
            else:
                chunk = f"{chunk}\n{msg}" if chunk else msg
        if chunk:
            try:
                await channel.send(chunk)
            except Exception:
                pass
