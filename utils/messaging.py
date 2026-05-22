"""
utils/messaging.py — DM送信・メッセージ関連ユーティリティ

DiscordのDM送信処理を共通化し、権限不足やAPIエラーを安全にハンドリングするためのモジュールです。

【設計意図（実務的なWhy）】
- エラーハンドリングの集約: 各機能でバラバラに行われていたDM送信処理を一つにまとめることで、
  後から「ログ出力を増やしたい」「送信失敗時の挙動を変えたい」といった変更が1箇所で済みます。
- API制限・権限不足の考慮: discord.Forbidden や discord.HTTPException を明示的にキャッチし、
  Bot全体がクラッシュするのを防ぎます（フォールトトレラント設計）。
"""

import logging
from typing import Optional, Union

import discord
from discord.ext import commands

from rate_limiter import dm_limiter

logger = logging.getLogger(__name__)


async def send_dm_safely(
    target: Union[discord.User, discord.Member, commands.Bot],
    user_id_or_embed: Union[int, discord.Embed, None] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
    content: Optional[str] = None
) -> bool:
    """
    指定したユーザーにDMを安全に送信します。

    【2つの呼び出し方に対応】
    1. send_dm_safely(user, embed=embed, view=view)  ← User/Member を直接渡す
    2. send_dm_safely(bot, user_id, embed)            ← Bot + user_id で指定

    Returns:
        bool: 送信に成功した場合は True、失敗した場合は False
    """
    user = None
    try:
        # 呼び出しパターンの判定
        if isinstance(target, (discord.User, discord.Member)):
            # パターン1: User/Member を直接渡す
            user = target
            if isinstance(user_id_or_embed, discord.Embed) and embed is None:
                embed = user_id_or_embed
        elif isinstance(target, commands.Bot):
            # パターン2: Bot + user_id
            if not isinstance(user_id_or_embed, int):
                logger.error("❌ send_dm_safely: Bot を渡す場合は第2引数に user_id (int) が必要です")
                return False
            user = await target.fetch_user(user_id_or_embed)
            if not user:
                logger.warning(f"⚠️ ユーザー情報の取得に失敗しました (ID: {user_id_or_embed})")
                return False
        else:
            logger.error(f"❌ send_dm_safely: 不明な target 型: {type(target)}")
            return False

        async with dm_limiter:
            await user.send(content=content, embed=embed, view=view)

        return True

    except discord.Forbidden:
        uid = getattr(user, 'id', 'unknown')
        logger.error(f"❌ DM送信失敗: {uid} はDMを受け付けない設定です (discord.Forbidden)")
        return False
    except discord.HTTPException as e:
        uid = getattr(user, 'id', 'unknown')
        logger.error(f"❌ DM送信APIエラー (ID: {uid}): {e}")
        return False
    except Exception as e:
        logger.error(f"❌ DM送信予期せぬエラー: {e}")
        return False
