from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from korvo_server.config import SERVER_ROOT

templates = Jinja2Templates(directory=str(SERVER_ROOT / "korvo_server" / "templates"))
router = APIRouter(tags=["pages"])


@router.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="pages/dashboard.html",
        context={"request": request},
    )
