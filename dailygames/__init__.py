from .dailygames import DailyGames

__red_end_user_data_statement__ = (
    "This cog stores the daily game scores members post (who, which game, the score) "
    "for about a month, to show yesterday's results. It can be deleted on request."
)


async def setup(bot):
    await bot.add_cog(DailyGames(bot))
