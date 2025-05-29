import json
import logging
from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from utils.mcp_client import McpClients


class McpTool(Tool):

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        servers_config_json = tool_parameters.get("servers_config") or self.runtime.credentials.get("servers_config")
        if not servers_config_json:
            raise ValueError("Please fill in the servers_config")
        try:
            servers_config = json.loads(servers_config_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"servers_config must be a valid JSON string: {e}")

        tool_name = tool_parameters.get("tool_name", "")
        if not tool_name:
            raise ValueError("Please fill in the tool_name")
        arguments_json = tool_parameters.get("arguments", "")
        if not arguments_json:
            raise ValueError("Please fill in the arguments")
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Arguments must be a valid JSON string: {e}")

        mcp_clients = None
        try:
            mcp_clients = McpClients(servers_config)
            result = mcp_clients.execute_tool(tool_name, arguments)
            yield self.create_text_message(result)
        except Exception as e:
            error_msg = f"Error calling MCP Server tool: {str(e)}"
            logging.error(error_msg)
            yield self.create_text_message(error_msg)
        finally:
            if mcp_clients:
                mcp_clients.close()
