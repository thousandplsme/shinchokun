"""
cogs/commands.py — 管理者用スラッシュコマンド（v2.2）

全コマンドを管理者権限化。ユーザー向け機能はハブチャンネルに移行済み。
v2.2: /sync_exp, /sync_roles を追加。
"""

import logging
import discord
from discord.ext import commands
from discord import app_commands

import config
from gamification import ExpManager, TitleSystem

logger = logging.getLogger(__name__)


class CommandsCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答を確認します")
    @app_commands.default_permissions(administrator=True)
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! レイテンシ: **{latency}ms**", ephemeral=True)

    @app_commands.command(name="test_quest", description="【テスト用】クエスト配信を手動で実行します")
    @app_commands.default_permissions(administrator=True)
    async def test_quest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        quest_cog = self.bot.get_cog("QuestCog")
        if quest_cog:
            try:
                await quest_cog.deliver_weekly_quests()
                await interaction.followup.send("✅ クエスト配信を実行しました！", ephemeral=True)
            except Exception as e:
                logger.error(f"❌ test_quest エラー: {e}")
                await interaction.followup.send(f"❌ クエスト配信中にエラーが発生しました:\n```{e}```", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ QuestCogが読み込まれていません。", ephemeral=True)

    @app_commands.command(name="test_summary", description="【テスト用】週次サマリーを手動で投稿します")
    @app_commands.default_permissions(administrator=True)
    async def test_summary(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        summary_cog = self.bot.get_cog("SummaryCog")
        if summary_cog:
            await summary_cog.post_weekly_summary()
            await interaction.followup.send("✅ 週次サマリーを投稿しました！", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ SummaryCogが読み込まれていません。", ephemeral=True)

    # === v2.2 新規コマンド ===

    @app_commands.command(name="sync_exp", description="SheetsのEXPログからDBのEXPを再計算・同期します")
    @app_commands.default_permissions(administrator=True)
    async def sync_exp(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self.bot.sheets:
            await interaction.followup.send("⚠️ Google Sheets が未接続です。", ephemeral=True)
            return

        # 1. EXPログ全件取得
        logs = await self.bot.sheets.get_all_exp_logs()
        if not logs:
            # EXPログが空 → 全メンバーのEXPをリセット
            member_ids = config.get_active_member_ids()
            reset_count = 0
            for uid in member_ids:
                profile = await self.bot.db.get_user_profile(uid)
                if profile and profile["total_exp"] > 0:
                    job_type = profile.get("job_type", "")
                    if not job_type and self.bot.sheets:
                        member = self.bot.sheets.get_member(uid)
                        if member:
                            job_type = member.get("job_type", "")
                    job_name = self.bot.sheets.get_job_name(job_type) if self.bot.sheets else job_type
                    new_title = TitleSystem.get_title(1, job_name)
                    await self.bot.db.upsert_user_profile(
                        discord_id=uid, total_exp=0, level=1,
                        job_type=job_type, current_title=new_title,
                        consecutive_weeks=0,
                    )
                    reset_count += 1
            embed = discord.Embed(
                title="🔄 EXPリセット完了",
                description=f"EXPログが空のため、**{reset_count}人** のEXPを0にリセットしました。",
                color=config.COLOR_WARNING,
            )
            embed.set_footer(text="しんちょくん 🤖 | EXP同期")
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"🔄 EXPログ空 → {reset_count}人リセット")
            return

        # 2. ユーザーごとに合算
        totals: dict[int, int] = {}
        for log in logs:
            uid = log["discord_id"]
            totals[uid] = totals.get(uid, 0) + log["amount"]

        # 3. DB更新
        results = []
        for uid, new_exp in totals.items():
            old_profile = await self.bot.db.get_user_profile(uid)
            old_exp = old_profile["total_exp"] if old_profile else 0

            new_level = TitleSystem.calculate_level(new_exp)

            # 職種名を取得
            job_type = old_profile["job_type"] if old_profile else ""
            if not job_type and self.bot.sheets:
                member = self.bot.sheets.get_member(uid)
                if member:
                    job_type = member.get("job_type", "")
            job_name = self.bot.sheets.get_job_name(job_type) if self.bot.sheets else job_type
            new_title = TitleSystem.get_title(new_level, job_name)

            await self.bot.db.upsert_user_profile(
                discord_id=uid, total_exp=new_exp, level=new_level,
                job_type=job_type, current_title=new_title,
                consecutive_weeks=old_profile["consecutive_weeks"] if old_profile else 0,
            )

            diff = new_exp - old_exp
            diff_str = f"+{diff}" if diff >= 0 else str(diff)
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"ID:{uid}"
            results.append(f"**{name}**: {old_exp} → {new_exp} ({diff_str})")

        # 4. 結果表示
        embed = discord.Embed(
            title="🔄 EXP同期完了",
            description=f"EXPログ {len(logs)} 件から {len(totals)} 人分を同期しました。",
            color=config.COLOR_SUCCESS,
        )
        embed.add_field(name="📊 同期結果", value="\n".join(results) or "なし", inline=False)
        embed.set_footer(text="しんちょくん 🤖 | EXP同期")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"🔄 EXP同期完了: {len(totals)}人")

    @app_commands.command(name="sync_roles", description="全メンバーの称号ロールを一括付与します")
    @app_commands.default_permissions(administrator=True)
    async def sync_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("⚠️ サーバー内で実行してください。", ephemeral=True)
            return

        exp_manager = ExpManager(self.bot.db, self.bot.sheets)
        member_ids = config.get_active_member_ids()
        success = 0
        errors = []

        for uid in member_ids:
            try:
                # ensure_profile で初回ならDB作成
                profile = await exp_manager.ensure_profile(uid, guild=guild)
                level = profile["level"]

                # 称号を常に再計算（DB上の壊れたタイトルを修復）
                job_type = profile.get("job_type", "")
                if not job_type and self.bot.sheets:
                    member_info = self.bot.sheets.get_member(uid)
                    if member_info:
                        job_type = member_info.get("job_type", "")
                job_name = self.bot.sheets.get_job_name(job_type) if self.bot.sheets else job_type
                title = TitleSystem.get_title(level, job_name)

                # DB上のタイトルが壊れていたら修復
                if title != profile["current_title"]:
                    logger.info(f"🔧 称号修復: ID={uid}, '{profile['current_title']}' → '{title}'")
                    await self.bot.db.upsert_user_profile(
                        discord_id=uid, total_exp=profile["total_exp"], level=level,
                        job_type=job_type, current_title=title,
                        consecutive_weeks=profile.get("consecutive_weeks", 0),
                    )

                if not title or not title.strip():
                    continue

                # ロールの存在確認＆付与
                role = discord.utils.get(guild.roles, name=title)
                if role is None:
                    rank = TitleSystem.get_title_rank(level)
                    color = discord.Color(config.ROLE_COLORS.get(rank, 0x95A5A6))
                    role = await guild.create_role(
                        name=title, color=color,
                        reason="しんちょくん: 称号ロール一括作成",
                    )
                    logger.info(f"✨ ロール作成: {title}")

                member = guild.get_member(uid)
                if member is None:
                    member = await guild.fetch_member(uid)
                if role not in member.roles:
                    await member.add_roles(role, reason="称号ロール一括同期")

                success += 1
                user = self.bot.get_user(uid)
                logger.info(f"🏅 ロール同期: {user.display_name if user else uid} → {title}")

            except discord.NotFound:
                errors.append(f"ID:{uid} — メンバーが見つかりません")
            except discord.Forbidden:
                errors.append(f"ID:{uid} — 権限不足")
            except Exception as e:
                errors.append(f"ID:{uid} — {e}")

        embed = discord.Embed(
            title="🏅 ロール同期完了",
            description=f"**{success}/{len(member_ids)}** 人にロールを付与しました。",
            color=config.COLOR_SUCCESS if not errors else config.COLOR_WARNING,
        )
        if errors:
            embed.add_field(name="⚠️ エラー", value="\n".join(errors[:5]), inline=False)
        embed.set_footer(text="しんちょくん 🤖 | ロール同期")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"🏅 ロール同期完了: {success}/{len(member_ids)}人")

    @app_commands.command(name="send_quest", description="特定メンバーに未送信のクエストを追加配信します")
    @app_commands.default_permissions(administrator=True)
    async def send_quest(self, interaction: discord.Interaction, target: discord.Member):
        await interaction.response.defer(ephemeral=True)
        quest_cog = self.bot.get_cog("QuestCog")
        if not quest_cog:
            await interaction.followup.send("⚠️ QuestCogが読み込まれていません。", ephemeral=True)
            return
        
        sheets = self.bot.sheets
        if not sheets:
            await interaction.followup.send("⚠️ Google Sheets が未接続です。", ephemeral=True)
            return

        try:
            week_start = sheets.get_current_week_start()
            quests = await sheets.get_weekly_quests(week_start)
        except Exception as e:
            logger.error(f"❌ send_quest: データ取得エラー: {e}")
            await interaction.followup.send(f"❌ データ取得に失敗しました:\n```{e}```", ephemeral=True)
            return

        # 対象ユーザーの未送信クエストを抽出
        user_quests = [q for q in quests if q["discord_id"] == target.id and not q.get("sent", False)]
        
        if not user_quests:
            await interaction.followup.send(f"✅ {target.display_name} さんの未送信クエストはありません。", ephemeral=True)
            return

        # 最大5件に制限
        user_quests = user_quests[:5]
        
        # autonomy_level の取得
        member_info = sheets.get_member(target.id)
        autonomy = member_info.get("autonomy_level", "low") if member_info else "low"

        # QuestCog のメソッドを使って送信
        try:
            embed = quest_cog._build_quest_embed(user_quests, autonomy, week_start, target.id)
            # from cogs.quest import QuestView (QuestCog に依存するため直接インスタンス化できない場合は注意)
            # ここではQuestCogに依存するUI要素を取得するのが難しいため、
            # 簡単な方法として QuestCog 側に send_quest_to_user メソッドを設ける方がクリーンですが、
            # UIクラスを直接インポートして使用します。
            from cogs.quest import QuestView
            view = QuestView(self.bot)
            
            await target.send(embed=embed, view=view)
            
            # sent フラグの更新
            for q in user_quests:
                await sheets.update_quest_sent_status(week_start, target.id, q["quest_title"], True)
                
            await interaction.followup.send(f"✅ {target.display_name} さんに {len(user_quests)}件 のクエストを追加配信しました！", ephemeral=True)
            logger.info(f"📩 追加クエスト配信: {target.name} ({len(user_quests)}件)")
            
        except discord.Forbidden:
            await interaction.followup.send(f"⚠️ {target.display_name} さんにDMを送信できませんでした。", ephemeral=True)
        except Exception as e:
            logger.error(f"❌ send_quest 送信エラー: {e}")
            await interaction.followup.send(f"❌ 送信中にエラーが発生しました:\n```{e}```", ephemeral=True)

    @app_commands.command(name="help_bot", description="管理者用コマンド一覧を表示します")
    @app_commands.default_permissions(administrator=True)
    async def help_bot(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🤖 しんちょくん — 管理者コマンド",
            description="メンバー向け機能は **コマンドチャンネル** のボタンから利用できます。",
            color=config.COLOR_PRIMARY,
        )
        cmds = [
            ("`/ping`", "Botの応答確認"),
            ("`/setup_hub`", "コマンドチャンネルに常設フォームを設置"),
            ("`/test_quest`", "クエスト配信を手動実行"),
            ("`/send_quest`", "特定メンバーに未送信クエストを追加配信"),
            ("`/test_summary`", "週次サマリーを手動投稿"),
            ("`/sync_exp`", "SheetsのEXPログからDB再計算"),
            ("`/sync_roles`", "全メンバーの称号ロールを一括付与"),
            ("`/help_bot`", "このヘルプを表示"),
        ]
        for cmd, desc in cmds:
            embed.add_field(name=cmd, value=desc, inline=False)
        embed.set_footer(text="しんちょくん 🤖 | 管理者ヘルプ")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandsCog(bot))
