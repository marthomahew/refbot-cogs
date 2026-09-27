from .modslash import ModSlash

__red_end_user_data_statement__ = (
    "This cog stores the IDs of members blocked from using the report reaction. "
    "Red's data deletion removes them."
)


async def setup(bot):
    await bot.add_cog(ModSlash(bot))
