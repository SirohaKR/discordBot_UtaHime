# -*- coding: utf-8 -*-
"""cogs/music.py의 cog_before_invoke에서 정상적인 안내(전용 채널 미지정, 음성 채널 미접속 등)로
명령 실행을 중단시킬 때 쓰는 예외.

이미 사용자에게 안내 메시지(필요하면 에러 알림/로그까지)를 보낸 뒤 raise하므로,
main.py의 on_command_error에서는 추가 알림 없이 조용히 무시한다.
"""

from discord.ext import commands


class HandledCommandError(commands.CommandError):
    pass
