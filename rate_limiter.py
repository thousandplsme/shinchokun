"""
rate_limiter.py — 汎用Rate Limiter & リトライユーティリティ

Discord API や Google Sheets API の呼び出し頻度を制御し、
レート制限エラーが発生した場合に自動で再試行する仕組みを提供する。

【このファイルで学べること】
- asyncio.Semaphore による非同期並行制御
- デコレータパターン（関数を関数で包む設計）
- 指数バックオフ（Exponential Backoff）アルゴリズム
- functools.wraps の役割
"""

import asyncio
import logging
import functools
import random
from typing import TypeVar, Callable, Any, Coroutine

logger = logging.getLogger(__name__)

# ============================================
# 型変数の定義
# ============================================
# TypeVar は「任意の型」を表すための仕組み。
# ジェネリック関数（どんな型でも受け取れる関数）を作る際に使用する。
T = TypeVar("T")


# ============================================
# 指数バックオフ付きリトライデコレータ
# ============================================

def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]]:
    """
    指数バックオフ付きリトライデコレータ。
    
    API呼び出しが失敗した場合、一定時間待ってから再試行する。
    待ち時間は試行ごとに倍増する（指数バックオフ）。
    
    【デコレータとは？】
    関数の前に @with_retry(...) と書くことで、
    その関数の「前後に追加の処理」を挟み込む仕組み。
    
    【指数バックオフとは？】
    リトライの待ち時間を 1秒 → 2秒 → 4秒 のように
    指数的に増加させるアルゴリズム。
    サーバーへの負荷を避けるための一般的なパターン。
    
    【ジッター（Jitter）とは？】
    待ち時間に少しランダム性を加えること。
    複数のクライアントが「同時にリトライ → また同時に失敗」の
    悪循環に陥るのを防ぐ。

    【実務的な設計意図（防衛的プログラミング）】
    外部API（DiscordやGoogle Sheets）は、「ネットワークの瞬断」や
    「API側の制限（429 Too Many Requests）」で一時的に失敗することがあります。
    このデコレータを使うことで、各機能側で都度try-exceptを書く必要がなくなり、
    システム全体がネットワークエラーに対して堅牢（フォールトトレラント）になります。

    Args:
        max_retries: 最大リトライ回数（デフォルト3回）
        base_delay: 基本待ち時間（秒）
        max_delay: 最大待ち時間（秒）。これ以上は増加しない。
        exceptions: リトライ対象の例外クラスのタプル
    
    使用例:
        @with_retry(max_retries=3, base_delay=1.0)
        async def call_api() -> dict:
            ...
    """

    def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
        # functools.wraps は、デコレートされた関数の名前や
        # ドキュメント文字列を保持するためのヘルパー。
        # これがないと、デバッグ時に関数名が 'wrapper' になってしまう。
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            f"❌ {func.__name__} が {max_retries}回リトライ後も失敗: {e}"
                        )
                        raise

                    # 指数バックオフ + ジッター
                    # 2^attempt で指数的に増加、random で±25%のジッターを加える
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = delay * 0.25 * (random.random() * 2 - 1)
                    actual_delay = delay + jitter

                    logger.warning(
                        f"⚠️ {func.__name__} 失敗 (試行 {attempt + 1}/{max_retries + 1}): {e}"
                        f" → {actual_delay:.1f}秒後にリトライ"
                    )

                    # asyncio.sleep で非同期的に待機
                    # （この間、他のタスクが実行できる）
                    await asyncio.sleep(actual_delay)

            raise last_exception

        return wrapper
    return decorator


# ============================================
# DM一斉送信用のRate Limiter
# ============================================

class DmRateLimiter:
    """
    DM一斉送信時の送信間隔を制御するクラス。
    
    Discord APIには「1秒間に送信できるDM数」に制限がある。
    このクラスは、DM送信の間に適切な待ち時間を挟むことで、
    レート制限を回避する。
    
    【asyncio.Semaphore とは？】
    同時に実行できるタスク数を制限する仕組み。
    セマフォの値が 1 なら、同時に1つのタスクしか実行できない。
    
    使用例:
        limiter = DmRateLimiter(interval=1.0)
        for user in users:
            async with limiter:
                await user.send(message)
    """

    def __init__(self, interval: float = 1.0):
        """
        Args:
            interval: DM送信間の最小間隔（秒）
        """
        self._semaphore = asyncio.Semaphore(1)
        self._interval = interval

    async def __aenter__(self):
        """
        async with 文の開始時に呼ばれる。
        セマフォを取得し、送信権を確保する。
        """
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        async with 文の終了時に呼ばれる。
        次の送信まで interval 秒待ってからセマフォを解放する。
        """
        await asyncio.sleep(self._interval)
        self._semaphore.release()


# ============================================
# グローバルインスタンス
# ============================================
# Bot全体で共有するDM送信用Rate Limiter
dm_limiter = DmRateLimiter(interval=1.0)
