"""
Gemini Router - Handles native Gemini format API requests (Antigravity backend)
处理原生Gemini格式请求的路由模块（Antigravity后端）
"""

# 标准库
import json
import sys
from pathlib import Path as _PathlibPath

# 添加项目根目录到Python路径
project_root = _PathlibPath(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 第三方库
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# 本地模块 - 配置和日志
from config import get_anti_truncation_max_attempts
from log import log

# 本地模块 - 工具和认证
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_gemini_flexible,
)

# 本地模块 - 基础路由工具
from src.router.hi_check import is_health_check_request, create_health_check_response
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
)

# 本地模块 - 数据模型
from src.models import GeminiRequest, model_to_dict

# 本地模块 - API 层
from src.api.antigravity import non_stream_request, stream_request

# 本地模块 - 转换器
from src.converter.antigravity_fix import normalize_antigravity_request
from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

@router.post("/v1beta/models/{model:path}:generateContent")
@router.post("/v1/models/{model:path}:generateContent")
async def generate_content(
    gemini_request: "GeminiRequest",
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    处理Gemini格式的内容生成请求（非流式）

    Args:
        gemini_request: Gemini格式的请求体
        model: 模型名称
        api_key: API 密钥
    """
    log.debug(f"[ANTIGRAVITY] Non-streaming request for model: {model}")

    # 转换为字典
    normalized_dict = model_to_dict(gemini_request)

    # 健康检查
    if is_health_check_request(normalized_dict, format="gemini"):
        response = create_health_check_response(format="gemini")
        return JSONResponse(content=response)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # 对于抗截断模型的非流式请求，给出警告
    if use_anti_truncation:
        log.warning("抗截断功能仅在流式传输时有效，非流式请求将忽略此设置")

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 规范化 Gemini 请求 (使用 antigravity 模式)
    normalized_dict = await normalize_antigravity_request(normalized_dict)

    # 准备API请求格式 - 提取model并将其他字段放入request中
    api_request = {
        "model": normalized_dict.pop("model"),
        "request": normalized_dict
    }

    # 调用 API 层的非流式请求
    response = await non_stream_request(
        body=api_request,
        log_ctx={
            "api_format": "gemini",
            "client_ip": request.client.host if request.client else "",
            "client_model": model,
        },
    )

    # 解包装响应：Antigravity API 可能返回的格式有额外的 response 包装层
    # 需要提取并返回标准 Gemini 格式
    # 保持 Gemini 原生的 inlineData 格式,不进行 Markdown 转换
    try:
        if response.status_code == 200:
            response_data = json.loads(response.body if hasattr(response, 'body') else response.content)
            # 如果有 response 包装，解包装它
            if "response" in response_data:
                unwrapped_data = response_data["response"]
                return JSONResponse(content=unwrapped_data)
        # 错误响应或没有 response 字段，直接返回
        return response
    except Exception as e:
        log.warning(f"Failed to unwrap response: {e}, returning original response")
        return response

@router.post("/v1beta/models/{model:path}:streamGenerateContent")
@router.post("/v1/models/{model:path}:streamGenerateContent")
async def stream_generate_content(
    gemini_request: GeminiRequest,
    request: Request,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    处理Gemini格式的流式内容生成请求

    Args:
        gemini_request: Gemini格式的请求体
        model: 模型名称
        api_key: API 密钥
    """
    log.debug(f"[ANTIGRAVITY] Streaming request for model: {model}")

    # 转换为字典
    normalized_dict = model_to_dict(gemini_request)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 请求日志上下文（api_format + 客户端 IP + 客户端原始模型名）
    log_ctx = {
        "api_format": "gemini",
        "client_ip": request.client.host if request.client else "",
        "client_model": model,
    }

    # ========== 流式抗截断生成器 ==========
    async def anti_truncation_generator():
        # 先进行基础标准化
        normalized_req = await normalize_antigravity_request(normalized_dict.copy())

        # 准备API请求格式 - 提取model并将其他字段放入request中
        api_request = {
            "model": normalized_req.pop("model") if "model" in normalized_req else real_model,
            "request": normalized_req
        }

        max_attempts = await get_anti_truncation_max_attempts()

        # 首先对payload应用反截断指令
        anti_truncation_payload = apply_anti_truncation(api_request)

        first_attempt_stream = stream_request(body=anti_truncation_payload, native=False, log_ctx=log_ctx)
        try:
            first_chunk = await read_first_async_item(first_attempt_stream)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        first_attempt_pending = True

        async def stream_request_wrapper(payload):
            nonlocal first_attempt_pending

            if first_attempt_pending:
                first_attempt_pending = False
                stream_gen = prepend_async_item(first_chunk, first_attempt_stream)
            else:
                stream_gen = stream_request(body=payload, native=False, log_ctx=log_ctx)
            return StreamingResponse(stream_gen, media_type="text/event-stream")

        # 创建反截断处理器
        processor = AntiTruncationStreamProcessor(
            stream_request_wrapper,
            anti_truncation_payload,
            max_attempts,
        )

        # 迭代 process_stream() 生成器，并展开 response 包装
        async for chunk in processor.process_stream():
            if isinstance(chunk, (str, bytes)):
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # 解析并展开 response 包装
                if chunk_str.startswith("data: "):
                    json_str = chunk_str[6:].strip()

                    # 跳过 [DONE] 标记
                    if json_str == "[DONE]":
                        yield chunk
                        continue

                    try:
                        # 解析JSON
                        data = json.loads(json_str)

                        # 展开 response 包装
                        if "response" in data and "candidates" not in data:
                            log.debug(f"[ANTIGRAVITY-ANTI-TRUNCATION] 展开response包装")
                            unwrapped_data = data["response"]
                            # 重新构建SSE格式
                            yield f"data: {json.dumps(unwrapped_data, ensure_ascii=False)}\n\n".encode('utf-8')
                        else:
                            # 已经是展开的格式，直接返回
                            yield chunk
                    except json.JSONDecodeError:
                        # JSON解析失败，直接返回原始chunk
                        yield chunk
                else:
                    # 不是SSE格式，直接返回
                    yield chunk
            else:
                # 其他类型，直接返回
                yield chunk

    # ========== 普通流式生成器 ==========
    async def normal_stream_generator():
        normalized_req = await normalize_antigravity_request(normalized_dict.copy())

        # 准备API请求格式 - 提取model并将其他字段放入request中
        api_request = {
            "model": normalized_req.pop("model"),
            "request": normalized_req
        }

        # 所有流式请求都使用非 native 模式（SSE格式）并展开 response 包装
        log.debug(f"[ANTIGRAVITY] 使用非native模式，将展开response包装")
        stream_gen = stream_request(body=api_request, native=False, log_ctx=log_ctx)
        try:
            first_chunk = await read_first_async_item(stream_gen)
        except StopAsyncIteration:
            return

        if isinstance(first_chunk, Response):
            yield first_chunk
            return

        # 展开 response 包装
        async for chunk in prepend_async_item(first_chunk, stream_gen):
            # 检查是否是Response对象（错误情况）
            if isinstance(chunk, Response):
                # 将Response转换为SSE格式的错误消息
                try:
                    error_content = chunk.body if isinstance(chunk.body, bytes) else (chunk.body or b'').encode('utf-8')
                    error_json = json.loads(error_content.decode('utf-8'))
                except Exception:
                    error_json = {"error": {"code": chunk.status_code, "message": "upstream error", "status": "ERROR"}}
                log.error(f"[ANTIGRAVITY STREAM] 返回错误给客户端: status={chunk.status_code}, error={str(error_json)[:200]}")
                yield f"data: {json.dumps(error_json)}\n\n".encode('utf-8')
                yield b"data: [DONE]\n\n"
                return

            # 处理SSE格式的chunk
            if isinstance(chunk, (str, bytes)):
                chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk

                # 解析并展开 response 包装
                if chunk_str.startswith("data: "):
                    json_str = chunk_str[6:].strip()

                    # 跳过 [DONE] 标记
                    if json_str == "[DONE]":
                        yield chunk
                        continue

                    try:
                        # 解析JSON
                        data = json.loads(json_str)

                        # 展开 response 包装
                        if "response" in data and "candidates" not in data:
                            log.debug(f"[ANTIGRAVITY] 展开response包装")
                            unwrapped_data = data["response"]
                            # 重新构建SSE格式
                            yield f"data: {json.dumps(unwrapped_data, ensure_ascii=False)}\n\n".encode('utf-8')
                        else:
                            # 已经是展开的格式，直接返回
                            yield chunk
                    except json.JSONDecodeError:
                        # JSON解析失败，直接返回原始chunk
                        yield chunk
                else:
                    # 不是SSE格式，直接返回
                    yield chunk

    # ========== 根据模式选择生成器 ==========
    if use_anti_truncation:
        log.info("启用流式抗截断功能")
        return await build_streaming_response_or_error(anti_truncation_generator())
    else:
        return await build_streaming_response_or_error(normal_stream_generator())

@router.post("/v1beta/models/{model:path}:countTokens")
@router.post("/v1/models/{model:path}:countTokens")
async def count_tokens(
    request: Request = None,
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    模拟Gemini格式的token计数

    使用简单的启发式方法：大约4字符=1token
    """

    try:
        request_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # 简单的token计数模拟 - 基于文本长度估算
    total_tokens = 0

    # 如果有contents字段
    if "contents" in request_data:
        for content in request_data["contents"]:
            if "parts" in content:
                for part in content["parts"]:
                    if "text" in part:
                        # 简单估算：大约4字符=1token
                        text_length = len(part["text"])
                        total_tokens += max(1, text_length // 4)

    # 如果有generateContentRequest字段
    elif "generateContentRequest" in request_data:
        gen_request = request_data["generateContentRequest"]
        if "contents" in gen_request:
            for content in gen_request["contents"]:
                if "parts" in content:
                    for part in content["parts"]:
                        if "text" in part:
                            text_length = len(part["text"])
                            total_tokens += max(1, text_length // 4)

    # 返回Gemini格式的响应
    return JSONResponse(content={"totalTokens": total_tokens})
