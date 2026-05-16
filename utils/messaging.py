"""
utils/messaging.py — DM送信・メッセージ関連ユーティリティ

DiscordのDM送信処理を共通化し、権限不足やAPIエラーを安全にハンドリングするためのモジュールです。

【設計意図（実務的なWhy）】
- エラーハンドリングの集約: 各機能（SOS、ミーティングなど）でバラバラに行われていたDM送信処理を一つにまとめることで、後から「ログ出力を増やしたい」「送信失敗時の挙動を変えたい」といった変更が1箇所で済みます。
- API制限・権限不足の考慮: `discord.Forbidden`（ユーザーがDMを拒否している）や `discord.HTTPException`（Discordのサーバーエラー）などを明示的にキャッチすることで、「なぜ送信できなかったか」の原因を特定しやすくなり、またBot全体がクラッシュするのを防ぎます（フォールトトレラント設計）。
"""

import logging
from typing import Optional

import discord
from discord.ext import commands

from rate_limiter import dm_limiter

logger = logging.getLogger(__name__)

async def send_dm_safely(
    bot: commands.Bot,
    user_id: int,
    embed: discord.Embed,
    view: Optional[discord.ui.View] = None,
    content: Optional[str] = None
) -> bool:
    """
    指定したユーザーにDMを安全に送信します。
    ユーザーがDMを許可していない場合や、API制限エラーが発生した場合も適切に処理します。

    Args:
        bot: Discord Botインスタンス
        user_id: 送信先ユーザーのDiscord ID
        embed: 送信するEmbedオブジェクト
        view: 送信するUI View（ボタンなど）。省略可能。
        content: 送信するテキストメッセージ。省略可能。

    Returns:
        bool: 送信に成功した場合は True、失敗した場合は False
    """
    try:
        user = await bot.fetch_user(user_id)
        if not user:
            logger.warning(f"⚠️ ユーザー情報の取得に失敗しました (ID: {user_id})")
            return False

        async with dm_limiter:
            await user.send(content=content, embed=embed, view=view)
        
        return True

    except discord.Forbidden:
        # ユーザーがサーバーメンバーからのDMを許可していないか、Botをブロックしている場合
        logger.error(f"❌ DM送信失敗: {user_id} はDMを受け付けない設定になっています (discord.Forbidden)")
        return False
    except discord.HTTPException as e:
        # Discord APIの何らかのエラー（500 Internal Server Error など）
        logger.error(f"❌ DM送信APIエラー (ID: {user_id}): {e} (discord.HTTPException)")
        return False
    except Exception as e:
        # 予期しないその他の例外
        logger.error(f"❌ DM送信予期せぬエラー (ID: {user_id}): {e}")
        return False
