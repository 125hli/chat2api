import asyncio
import time
import types
import warnings

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse

from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens, verify_token, get_req_token
import chatgpt.globals as globals
from chatgpt.reverseProxy import chatgpt_reverse_proxy
from utils.Logger import logger
from utils.config import api_prefix, scheduled_refresh, authorization_list, enable_gateway
from utils.retry import async_retry

warnings.filterwarnings("ignore")

app = FastAPI()
scheduler = AsyncIOScheduler()
templates = Jinja2Templates(directory="templates")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/4', kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token):
    chat_service = ChatService(req_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


async def process(request_data, req_token):
    chat_service = await to_send_conversation(request_data, req_token)
    await chat_service.prepare_send_conversation()
    res = await chat_service.send_conversation()
    return chat_service, res


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, req_token: str = Depends(oauth2_scheme)):
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    chat_service, res = await async_retry(process, request_data, req_token)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client)
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open("data/token.txt", "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def upload_post():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open("data/token.txt", "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get("/", response_class=HTMLResponse)
async def chatgpt(request: Request):
    if not enable_gateway:
        raise HTTPException(status_code=404, detail="Gateway is disabled")

    seed_token = request.query_params.get("seed", None)
    if not seed_token:
        seed_token = str(int(time.time()))

    response = templates.TemplateResponse("chatgpt.html", {"request": request, "seed_token": seed_token})
    # response.set_cookie("req_token", value=req_token)
    # response.set_cookie("access_token", value=access_token)
    response.set_cookie("seed_token", value=seed_token)
    return response


# @app.get("/backend-api/conversations")
# async def get_conversations():
#     return {"items": [], "total": 0, "limit": 28, "offset": 0, "has_missing_conversations": False}


@app.get("/backend-api/gizmos/bootstrap")
async def get_gizmos_bootstrap():
    return {"gizmos": []}


@app.get("/backend-api/me")
async def get_me():
    created = int(time.time())
    return {
        "object": "user",
        "id": "org-chatgpt",
        "email": "chatgpt@openai.com",
        "name": "ChatGPT",
        "picture": "https://cdn.auth0.com/avatars/ai.png",
        "created": created,
        "phone_number": None,
        "mfa_flag_enabled": False,
        "amr": [],
        "groups": [],
        "orgs": {
            "object": "list",
            "data": [
                {
                    "object": "organization",
                    "id": "org-chatgpt",
                    "created": 1715641300,
                    "title": "Personal",
                    "name": "user-chatgpt",
                    "description": "Personal org for chatgpt@openai.com",
                    "personal": True,
                    "settings": {},
                    "parent_org_id": None,
                    "is_default": False,
                    "role": "owner",
                    "is_scale_tier_authorized_purchaser": None,
                    "is_scim_managed": False,
                    "projects": {
                        "object": "list",
                        "data": []
                    },
                    "groups": [],
                    "geography": None
                }
            ]
        },
        "has_payg_project_spend_limit": None
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def reverse_proxy(request: Request, path: str):
    if path.startswith("c/"):
        seed_token = request.cookies.get("seed_token")
        if not seed_token:
            seed_token = str(int(time.time()))
        redirect_url = str(request.base_url) + "?seed=" + seed_token
        return RedirectResponse(url=redirect_url, status_code=302)
    return await chatgpt_reverse_proxy(request, path)
