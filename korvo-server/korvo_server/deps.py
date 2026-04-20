from typing import Annotated

from fastapi import Depends, Request

from korvo_server.db import KorvoDB


def get_kdb(request: Request) -> KorvoDB:
    return request.app.state.kdb


KorvoDep = Annotated[KorvoDB, Depends(get_kdb)]
