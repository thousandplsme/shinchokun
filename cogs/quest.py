"""
cogs/quest.py — クエスト自動配信＆完了報告システム

毎週月曜 09:00 にGoogle Sheetsからクエストを読み込み、
各メンバーにDMで配信する。完了ボタン押下でEXPを獲得。

【このファイルで学べること】
- discord.ui.View / discord.ui.Button の永続化パターン
- custom_id を使った動的なボタン識別
- DM送信時のRate Limit対策
- Google Sheets → Discord の連携フロー
- ephemeral メッセージ（本人だけに見えるメッセージ）
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands

import config
from rate_limiter import dm_limiter
from gamification import ExpManager

logger = logging.getLogger(__name__)


# ============================================
# クエスト完了ボタンView（永続）
# ============================================

class QuestView(discord.ui.View):
    """
    クエストDMに付くボタンUI。
    
    【永続View（timeout=None）の仕組み】
    通常のViewはBot再起動時に無効になるが、
    timeout=None + custom_id を設定すると、
    Bot再起動後もボタンが機能する（永続View）。
    
    ただし、Bot起動時に bot.add_view(QuestView(bot)) を
    呼ぶ必要がある（bot.py の setup_hook で実行）。
    
    【custom_id にデータを埋め込むパターン】
    custom_id="quest_complete:week_start:quest_title:exp_reward" のように
    区切り文字で情報を埋め込むと、ボタンクリック時にデータを復元できる。
    しかし custom_id は 100文字制限があるため、長いタイトルは切り詰める。
    
    ここでは、動的な custom_id は使わず、
    「最新週のクエストをDBから検索する」方式を採用する。
    """

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="✅ クエスト完了！",
        style=discord.ButtonStyle.success,
        custom_id="quest_complete_btn",
    )
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        完了ボタンが押された時の処理。
        
        【処理フロー】
        1. Google Sheetsから今週のクエストを取得
        2. このユーザーのクエストを特定
        3. 未完了のクエストがあれば完了処理
        4. EXP付与 → 演出メッセージ
        5. 全体チャンネル共有の提案
        """
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        sheets = self.bot.sheets
        week_start = sheets.get_current_week_start()

        # ユーザーの今週のクエストを取得
        quests = await sheets.get_weekly_quests(week_start)
        user_quests = [q for q in quests if q["discord_id"] == user_id]

        if not user_quests:
            await interaction.followup.send(
                "⚠️ 今週のクエストが見つかりません。", ephemeral=True
            )
            return

        # 完了済みクエストをDBから取得
        completions = await self.bot.db.get_quest_completions(user_id, week_start)
        completed_titles = {c["quest_title"] for c in completions}

        # 未完了のクエストを1つ完了させる
        pending_quests = [q for q in user_quests if q["quest_title"] not in completed_titles]

        if not pending_quests:
            await interaction.followup.send(
                "🎉 今週のクエストは**すべて完了**しています！ お疲れさまでした！ 🏆",
                ephemeral=True,
            )
            return

        # 未完了が複数ある場合はセレクトメニューを表示
        if len(pending_quests) > 1:
            view = QuestSelectView(self.bot, pending_quests, week_start)
            await interaction.followup.send(
                "どのクエストを完了しますか？ 🗡️",
                view=view,
                ephemeral=True,
            )
        else:
            # 1つだけなら直接完了処理
            quest = pending_quests[0]
            await self._complete_quest(interaction, quest, week_start)

    async def _complete_quest(self, interaction: discord.Interaction, quest: dict, week_start: str):
        """クエスト完了の共通処理"""
        user_id = interaction.user.id
        quest_title = quest["quest_title"]
        exp_reward = quest["exp_reward"]

        # 1. DB に完了記録（二重完了防止）
        is_new = await self.bot.db.record_quest_completion(
            user_id, week_start, quest_title, exp_reward
        )
        if not is_new:
            await interaction.followup.send(
                f"⚠️ 「{quest_title}」は既に完了済みです。", ephemeral=True
            )
            return

        # 2. Google Sheets のステータス更新
        try:
            await self.bot.sheets.update_quest_status(
                week_start, user_id, quest_title, "完了"
            )
        except Exception as e:
            logger.warning(f"⚠️ Sheetsステータス更新失敗（続行）: {e}")

        # 3. EXP付与
        guild = None
        if config.GUILD_ID:
            guild = self.bot.get_guild(config.GUILD_ID)

        exp_manager = ExpManager(self.bot.db, self.bot.sheets)
        result = await exp_manager.grant_exp(
            user_id, exp_reward,
            f"クエスト完了: {quest_title}",
            guild=guild,
        )

        # 4. 達成メッセージを構築
        msg = f"⚔️ **クエスト達成！**  「{quest_title}」\n\n"
        msg += ExpManager.build_exp_message(exp_reward, result["total_exp"], result["new_level"])

        levelup_msg = ExpManager.build_levelup_message(result)
        if levelup_msg:
            msg += f"\n\n{levelup_msg}"

        # 5. 全体共有ボタン付きで送信
        share_view = QuestShareView(self.bot, quest_title, exp_reward)
        await interaction.followup.send(msg, view=share_view, ephemeral=True)


# ============================================
# クエスト選択メニュー（複数クエスト未完了時）
# ============================================

class QuestSelectView(discord.ui.View):
    """
    未完了クエストが複数ある場合に表示する選択メニュー。
    
    【discord.ui.Select とは？】
    ドロップダウンメニューで1つの選択肢を選べるUI部品。
    options に SelectOption のリストを渡す。
    """

    def __init__(self, bot: commands.Bot, quests: list[dict], week_start: str):
        super().__init__(timeout=120)  # 2分でタイムアウト
        self.bot = bot
        self.quests = {q["quest_title"]: q for q in quests}
        self.week_start = week_start

        # セレクトメニューを動的に作成
        options = [
            discord.SelectOption(
                label=q["quest_title"][:100],  # ラベルは最大100文字
                description=f"+{q['exp_reward']} EXP",
                value=q["quest_title"],
            )
            for q in quests
        ]

        select = discord.ui.Select(
            placeholder="完了したクエストを選択...",
            options=options,
            custom_id="quest_select_menu",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        """セレクトメニューで選択された時の処理"""
        selected_title = interaction.data["values"][0]
        quest = self.quests.get(selected_title)

        if quest is None:
            await interaction.response.send_message("⚠️ クエストが見つかりません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # QuestView の完了処理を再利用
        quest_view = QuestView(self.bot)
        await quest_view._complete_quest(interaction, quest, self.week_start)


# ============================================
# 全体チャンネル共有ボタン
# ============================================

class QuestShareView(discord.ui.View):
    """
    クエスト完了後に「全体チャンネルに共有する？」を表示するView。
    
    共有は任意。メンバーの自主性を尊重する。
    """

    def __init__(self, bot: commands.Bot, quest_title: str, exp_earned: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.quest_title = quest_title
        self.exp_earned = exp_earned

    @discord.ui.button(
        label="📢 チームに報告する",
        style=discord.ButtonStyle.primary,
    )
    async def share_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """全体チャンネルにクエスト完了を報告"""
        channel = self.bot.get_channel(config.PROGRESS_CHANNEL_ID)
        if channel is None:
            await interaction.response.send_message(
                "⚠️ 報告チャンネルが見つかりません。", ephemeral=True
            )
            return

        # 報告Embedを作成
        embed = discord.Embed(
            title="⚔️ クエスト達成報告！",
            description=(
                f"**{interaction.user.display_name}** が "
                f"クエスト「{self.quest_title}」を達成しました！ 🎉"
            ),
            color=config.COLOR_SUCCESS,
        )
        embed.add_field(name="⭐ 獲得EXP", value=f"+{self.exp_earned} EXP", inline=True)

        # プロフィール情報を追加
        profile = await self.bot.db.get_user_profile(interaction.user.id)
        if profile:
            embed.add_field(name="🏅 称号", value=profile["current_title"], inline=True)
            embed.add_field(name="📊 レベル", value=f"Lv.{profile['level']}", inline=True)

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="しんちょくん 🤖 | クエスト達成")
        embed.timestamp = discord.utils.utcnow()

        await channel.send(embed=embed)

        # ボタンを無効化
        button.disabled = True
        button.label = "✅ 報告済み"
        await interaction.response.edit_message(view=self)
        logger.info(f"📢 全体共有: {interaction.user} → {self.quest_title}")

    @discord.ui.button(
        label="スキップ",
        style=discord.ButtonStyle.secondary,
    )
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """共有をスキップ"""
        # Viewごと削除
        await interaction.response.edit_message(view=None)


# ============================================
# クエスト配信Cog
# ============================================

class QuestCog(commands.Cog):
    """
    クエスト自動配信＆完了報告を管理するCog。
    
    【Cog（コグ）とは？】
    discord.py の機能グループ化の仕組み。
    関連するコマンドやイベントハンドラを1つのクラスにまとめられる。
    Botの setup_hook で load_extension("cogs.quest") と呼ぶことで読み込まれる。
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def deliver_weekly_quests(self):
        """
        週次クエストを配信する（スケジューラから毎週月曜 09:00 に呼ばれる）。
        
        【処理フロー】
        1. Google Sheets から今週のタスクとメンバー情報を取得
        2. メンバーごとにクエストをフィルタリング
        3. autonomy_level に応じてDMの内容を変える:
           - high: 大目標のみ
           - low: 大目標 + サブタスク（ヒント）付き
        4. 完了ボタン付きDMを送信
        """
        logger.info("📜 週次クエスト配信を開始...")

        sheets = self.bot.sheets
        if sheets is None:
            logger.error("❌ Google Sheets 未接続のためクエスト配信をスキップします。")
            return

        try:
            week_start = sheets.get_current_week_start()
        except Exception as e:
            logger.error(f"❌ 週開始日の取得に失敗: {e}")
            return

        # Google Sheets からデータ取得
        try:
            quests = await sheets.get_weekly_quests(week_start)
        except Exception as e:
            logger.error(f"❌ クエストデータの取得に失敗: {e}")
            return

        if not quests:
            logger.info(f"⚠️ {week_start} のクエストが見つかりません。配信をスキップ。")
            return

        members = sheets.get_members()

        # メンバーごとにクエストをグルーピング
        # defaultdict 的な処理: member_id → [quest1, quest2, ...]
        member_quests: dict[int, list[dict]] = {}
        for q in quests:
            uid = q["discord_id"]
            if uid not in member_quests:
                member_quests[uid] = []
            member_quests[uid].append(q)

        # 各メンバーにDMを送信
        sent_count = 0
        for member_info in members:
            uid = member_info["discord_id"]
            if uid not in member_quests:
                continue

            user_quests = member_quests[uid]
            autonomy = member_info.get("autonomy_level", "low")

            try:
                user = await self.bot.fetch_user(uid)
                if user is None:
                    logger.warning(f"⚠️ ユーザーが見つかりません: {uid}")
                    continue

                # DM Embed を構築（メンバー別メッセージ対応）
                embed = self._build_quest_embed(user_quests, autonomy, week_start, uid)

                # 完了ボタン付きで送信（Rate Limiter で間隔制御）
                view = QuestView(self.bot)
                async with dm_limiter:
                    await user.send(embed=embed, view=view)

                sent_count += 1
                logger.info(f"📩 クエスト配信: {user.name} ({len(user_quests)}件)")

            except discord.Forbidden:
                logger.warning(f"⚠️ DM送信不可: {uid}")
            except Exception as e:
                logger.error(f"❌ クエスト配信エラー: {uid} - {e}")

        logger.info(f"✅ クエスト配信完了: {sent_count}人に送信")

    def _build_quest_embed(
        self, quests: list[dict], autonomy: str, week_start: str,
        discord_id: int = 0,
    ) -> discord.Embed:
        """
        クエスト配信用のEmbedを構築する。
        
        v2.1: メンバー別メッセージ対応。
        discord_id に応じたトーンで挨拶文を変更。
        """
        # メンバー別挨拶文
        greeting = "今週のクエストです 🗡️"
        if discord_id and self.bot.sheets:
            custom = self.bot.sheets.get_message(discord_id, "quest_greeting")
            if custom:
                greeting = custom

        embed = discord.Embed(
            title=f"🗡️ {greeting}",
            description=(
                f"**{week_start}** 〜 の週次クエストです。\n"
                f"完了したら **「✅ クエスト完了！」** ボタンを押してください！"
            ),
            color=config.COLOR_QUEST,
        )

        for i, quest in enumerate(quests, 1):
            title = quest["quest_title"]
            detail = quest.get("quest_detail", "")
            exp = quest["exp_reward"]

            if autonomy == "high":
                value = f"⭐ 報酬: **+{exp} EXP**"
            else:
                value = f"📝 ヒント: {detail}\n⭐ 報酬: **+{exp} EXP**" if detail else f"⭐ 報酬: **+{exp} EXP**"

            embed.add_field(name=f"**Quest {i}**: {title}", value=value, inline=False)

        embed.set_footer(text="しんちょくん 🤖 | クエスト配信")
        embed.timestamp = discord.utils.utcnow()
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
