"""The tool catalog shared by the Bridge HTTP API and its MCP adapter."""

import json
import logging
from inspect import signature
from typing import Callable, get_origin, get_type_hints

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from .wechat_tools import NATIVE_NAME, call_native_tool, list_native_tools

logger = logging.getLogger(__name__)


class ToolContent(BaseModel):
    content: list[dict]


def tool_error(detail: str) -> dict:
    return {"content": [{"type": "text", "text": detail}], "isError": True}


class ToolRegistry:
    def __init__(self, native_client: Callable[[], httpx.AsyncClient | None], native_token: str):
        self.native_client = native_client
        self.native_token = native_token
        self.local: dict[str, tuple[Callable, type[BaseModel], dict]] = {}
        self.conversation: set[str] = set()

    def tool(self, *, conversation: bool = False):
        def register(function):
            name = function.__name__
            if name.startswith("wechat_") or name in self.local:
                raise ValueError(f"Invalid or duplicate Bridge tool name: {name}")
            hints = get_type_hints(function, include_extras=True)
            fields = {
                parameter: (hints[parameter], ... if entry.default is entry.empty else entry.default)
                for parameter, entry in signature(function).parameters.items()
            }
            arguments = create_model(f"{name}Arguments", __config__=ConfigDict(extra="forbid"), **fields)
            parameters = arguments.model_json_schema()
            parameters.pop("title", None)
            description = {"name": name, "description": function.__doc__, "inputSchema": parameters}
            returned = hints["return"]
            if returned is not ToolContent:
                schema = TypeAdapter(returned).json_schema()
                if get_origin(returned) is list:
                    schema = {"type": "object", "properties": {"result": schema}, "required": ["result"]}
                description["outputSchema"] = schema
            self.local[name] = (function, arguments, description)
            if conversation:
                self.conversation.add(name)
            return function

        return register

    async def list_tools(self, *, conversation: bool = False) -> list[dict]:
        tools = [description for name, (_, _, description) in self.local.items()
                 if not conversation or name in self.conversation]
        if not conversation and (client := self.native_client()) is not None:
            try:
                tools.extend(await list_native_tools(client, self.native_token))
            except ValueError:
                logger.warning("Native WeChat tool discovery is unavailable")
        return tools

    async def call_tool(self, name: str, arguments: dict, *, conversation: bool = False) -> dict:
        if conversation and name not in self.conversation:
            return tool_error("This tool is unavailable to the conversation agent")
        if name in self.local:
            function, model, _ = self.local[name]
            try:
                result = await function(**dict(model.model_validate(arguments)))
            except ValidationError as error:
                return tool_error(str(error.errors(include_input=False, include_url=False)))
            except HTTPException as error:
                return tool_error(str(error.detail))
            except ValueError as error:
                return tool_error(str(error))
            if isinstance(result, ToolContent):
                return result.model_dump()
            items = result if isinstance(result, list) else [result]
            return {
                "content": [{"type": "text", "text": json.dumps(item, ensure_ascii=False, indent=2)}
                            for item in items],
                "structuredContent": {"result": result} if isinstance(result, list) else result,
            }
        if NATIVE_NAME.fullmatch(name) and (client := self.native_client()) is not None:
            try:
                return await call_native_tool(client, self.native_token, name, arguments)
            except ValueError as error:
                return tool_error(str(error))
        return tool_error("Unknown tool")
