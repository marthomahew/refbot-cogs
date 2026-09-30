from .pickem import Pickem

__red_end_user_data_statement__ = (
    "This cog stores each member's weekly pick'em picks and tiebreaker guesses, "
    "and who won each week. It can all be deleted on request."
)


async def setup(bot):
    await bot.add_cog(Pickem(bot))
