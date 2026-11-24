from typing import Any, Dict


async def merge_system_messages(request_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理请求体中的 system 消息：
    - 顶层 system 参数（Anthropic 格式）与开头的连续 system 消息合并为 systemInstruction
    - 中间的 system 消息转换为 user 消息（保持顺序）

    Args:
        request_body: OpenAI 或 Claude 格式的请求体，包含 messages 字段

    Returns:
        处理后的请求体
    """
    # 处理 Anthropic 格式的顶层 system 参数
    # Anthropic API 规范: system 是顶层参数，不在 messages 中
    system_content = request_body.get("system")
    if system_content:
        system_parts = []

        if isinstance(system_content, str):
            if system_content.strip():
                system_parts.append({"text": system_content})
        elif isinstance(system_content, list):
            # system 可以是包含多个块的列表
            for item in system_content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text", "").strip():
                        system_parts.append({"text": item["text"]})
                elif isinstance(item, str) and item.strip():
                    system_parts.append({"text": item})

        if system_parts:
            request_body = request_body.copy()
            request_body.pop("system", None)
            request_body["systemInstruction"] = {"parts": system_parts}

    messages = request_body.get("messages", [])
    if not messages:
        return request_body

    # 提取开头的连续 system 消息合并为 systemInstruction
    system_parts = []

    # 如果已经从顶层 system 参数创建了 systemInstruction，获取现有的 parts
    if "systemInstruction" in request_body:
        existing_instruction = request_body.get("systemInstruction", {})
        if isinstance(existing_instruction, dict):
            system_parts = existing_instruction.get("parts", []).copy()

    remaining_messages = []
    collecting_system = True

    # developer 角色与 system 同语义处理（OpenAI 新版指令角色），上游仅支持 user/model
    instruction_roles = ("system", "developer")

    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")

        if role in instruction_roles and collecting_system:
            # 提取 system 消息的文本内容
            if isinstance(content, str):
                if content.strip():
                    system_parts.append({"text": content})
            elif isinstance(content, list):
                # 处理列表格式的 content
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text" and item.get("text", "").strip():
                            system_parts.append({"text": item["text"]})
                    elif isinstance(item, str) and item.strip():
                        system_parts.append({"text": item})
        else:
            # 遇到非 system 消息，停止收集
            collecting_system = False
            if role in instruction_roles:
                # 将后续的 system 消息转换为 user 消息
                converted_message = message.copy()
                converted_message["role"] = "user"
                remaining_messages.append(converted_message)
            else:
                remaining_messages.append(message)

    # 如果没有收集到任何 systemInstruction 内容：
    # 仅当 messages 未发生变化时才返回原始请求体；
    # 存在被转换为 user 的中间 system 消息时，返回更新后的 messages
    if not system_parts:
        if remaining_messages == messages:
            return request_body
        result = request_body.copy()
        result["messages"] = remaining_messages
        return result

    # 构建新的请求体
    result = request_body.copy()

    # 添加或更新 systemInstruction
    result["systemInstruction"] = {"parts": system_parts}

    # 更新 messages 列表（移除已处理的 system 消息）
    result["messages"] = remaining_messages

    return result
